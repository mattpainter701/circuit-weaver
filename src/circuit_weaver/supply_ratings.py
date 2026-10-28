"""Supply-pin voltage ratings and the rail-versus-rating check.

Part data states each supply pin's datasheet limits once, in the normalized
``supply_ratings`` field. The validator works out each power-input pin's rail
voltage from the design: explicit ``power_domains`` first, then regulator or
source block parameters (``vin``/``vin_net``, ``vout``/``rail_name``, ...),
and finally the rail's name (``VIN_12V``). It then compares the two.

The check is part-neutral. It uses only pin electrical types, the netlist,
declared design intent and the normalized rating data, never a part number.
It also fails closed: a supply pin on a known rail with no rating, or a rated
pin on a rail with no known voltage, is reported instead of passing silently.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

SUPPLY_RATING_KINDS = frozenset({"supply", "negative_supply", "floating", "reference_input"})
RATING_CONFIDENCES = frozenset({"verified", "corroborated", "single_source"})
"""``verified``: read from the manufacturer datasheet (or asserted by the
design owner in the spec). ``corroborated``: the datasheet's table values
confirmed through more than one independent channel without the PDF itself
being read. ``single_source``: one secondary source. Only verified or
corroborated ratings can turn a declared over/under-voltage into a confirmed
error; a single-source rating produces a review warning."""
_CONFIRMING_CONFIDENCES = frozenset({"verified", "corroborated"})
"""``supply`` is a ground-referenced supply input and the only kind checked
against rail voltage. ``negative_supply`` (op-amp V-), ``floating``
(bootstrap or offset supplies referenced to another pin) and
``reference_input`` (ADC/DAC VREF) are recorded so they count as rated, but
their limits are relative to other pins and need a dedicated check."""

# Block parameters that pair a net with the voltage the design declares on it.
# Each entry is (net parameter, nominal parameter, min parameter, max parameter).
_RAIL_PARAMETER_PAIRS: tuple[tuple[str, str, str | None, str | None], ...] = (
    ("vin_net", "vin", "vin_min", "vin_max"),
    ("rail_name", "vout", None, None),
    ("vout_net", "vout", None, None),
    ("out_net", "vout", None, None),
    ("output_net", "vout", None, None),
    ("vm_net", "vm", None, None),
    ("vdd_net", "vdd", None, None),
    ("vbus_net", "vbus", None, None),
    ("vcoil_net", "vcoil", None, None),
    ("vbat_net", "vbat", None, None),
    ("bat_net", "vbat", None, None),
)

_GROUND_PREFIXES = ("GND", "AGND", "DGND", "PGND", "SGND", "VSS")
_RATING_OPTIONAL_REF_PREFIXES = frozenset({"J", "P", "CN", "TP", "R", "C", "L", "FB", "F", "D", "LED", "Y", "X"})
_RATING_OPTIONAL_CATEGORIES = frozenset({"connector", "passive", "debug"})

# Basis of a rail voltage, in decreasing authority.
BASIS_POWER_DOMAIN = "power_domain"
BASIS_BLOCK_PARAMETER = "block_parameter"
BASIS_NET_NAME = "net_name"
_DECLARED_BASES = frozenset({BASIS_POWER_DOMAIN, BASIS_BLOCK_PARAMETER})


@dataclass(frozen=True)
class SupplyRating:
    """Datasheet voltage limits for one or more pins of a part.

    ``v_min``/``v_max`` are the recommended operating range and ``v_abs_max``
    is the absolute maximum rating, all ground-referenced. ``None`` means the
    source did not state the value; it is never a guess.
    """

    pins: tuple[str, ...]
    kind: str = "supply"
    v_min: float | None = None
    v_max: float | None = None
    v_abs_max: float | None = None
    source: str = ""
    note: str = ""
    confidence: str = "verified"

    @property
    def confirming(self) -> bool:
        return self.confidence in _CONFIRMING_CONFIDENCES

    @property
    def upper_limit(self) -> float | None:
        """The limit an over-voltage is judged against."""
        return self.v_max if self.v_max is not None else self.v_abs_max

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"pins": list(self.pins), "kind": self.kind}
        for name in ("v_min", "v_max", "v_abs_max"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        data["confidence"] = self.confidence
        if self.source:
            data["source"] = self.source
        if self.note:
            data["note"] = self.note
        return data


@dataclass(frozen=True)
class RailVoltage:
    """What the design says about one rail's voltage."""

    net: str
    v_min: float
    v_max: float
    basis: str
    sources: tuple[str, ...]

    @property
    def declared(self) -> bool:
        return self.basis in _DECLARED_BASES


@dataclass(frozen=True)
class SupplyFinding:
    """A rail-versus-rating finding before it is rendered by a caller."""

    code: str
    rule_id: str | None
    level: str
    subject: str
    message: str
    suggestion: str
    calculation: dict[str, Any]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_supply_ratings(
    raw: Any, *, where: str = "supply_ratings", default_confidence: str = "verified"
) -> tuple[SupplyRating, ...]:
    """Parse and validate a ``supply_ratings`` list.

    Malformed data raises ``ValueError``. A bad rating must not be read as
    "no rating", because that would silently disable the check.
    """

    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{where} must be a list of rating objects")
    ratings: list[SupplyRating] = []
    for index, item in enumerate(raw):
        label = f"{where}[{index}]"
        if isinstance(item, SupplyRating):
            ratings.append(item)
            continue
        if not isinstance(item, Mapping):
            raise ValueError(f"{label} must be an object")
        pins_raw = item.get("pins")
        if isinstance(pins_raw, (str, int)):
            pins_raw = [pins_raw]
        if not isinstance(pins_raw, (list, tuple)) or not pins_raw:
            raise ValueError(f"{label}.pins must be a non-empty list of pin numbers")
        pins = tuple(str(pin).strip() for pin in pins_raw if str(pin).strip())
        if not pins:
            raise ValueError(f"{label}.pins must be a non-empty list of pin numbers")
        kind = str(item.get("kind") or "supply").strip().lower()
        if kind not in SUPPLY_RATING_KINDS:
            valid = ", ".join(sorted(SUPPLY_RATING_KINDS))
            raise ValueError(f"{label}.kind {kind!r} is not one of: {valid}")
        values: dict[str, float | None] = {}
        for name in ("v_min", "v_max", "v_abs_max"):
            value = item.get(name)
            parsed = _number(value)
            if value is not None and parsed is None:
                raise ValueError(f"{label}.{name} must be a number in volts")
            values[name] = parsed
        v_min, v_max, v_abs_max = values["v_min"], values["v_max"], values["v_abs_max"]
        if v_min is not None and v_max is not None and v_min > v_max:
            raise ValueError(f"{label}: v_min {v_min} V is above v_max {v_max} V")
        if v_max is not None and v_abs_max is not None and v_max > v_abs_max:
            raise ValueError(f"{label}: v_max {v_max} V is above v_abs_max {v_abs_max} V")
        if kind == "supply" and v_max is None and v_abs_max is None:
            raise ValueError(f"{label}: a supply rating needs v_max or v_abs_max")
        confidence = str(item.get("confidence") or default_confidence).strip().lower()
        if confidence not in RATING_CONFIDENCES:
            valid = ", ".join(sorted(RATING_CONFIDENCES))
            raise ValueError(f"{label}.confidence {confidence!r} is not one of: {valid}")
        ratings.append(
            SupplyRating(
                pins=pins,
                kind=kind,
                v_min=v_min,
                v_max=v_max,
                v_abs_max=v_abs_max,
                source=str(item.get("source") or "").strip(),
                note=str(item.get("note") or "").strip(),
                confidence=confidence,
            )
        )
    seen: dict[str, int] = {}
    for index, rating in enumerate(ratings):
        for pin in rating.pins:
            if pin in seen:
                raise ValueError(f"{where}: pin {pin} is rated twice (entries {seen[pin]} and {index})")
            seen[pin] = index
    return tuple(ratings)


def _is_ground_name(name: str) -> bool:
    """Pin names such as GND, GND2, PGND, AGND_1 or VSS denote ground."""
    upper = (name or "").upper()
    return any(upper.startswith(prefix) for prefix in _GROUND_PREFIXES)


def _supply_pin_numbers(pins: Iterable[Any]) -> list[str]:
    numbers = []
    for pin in pins or ():
        if isinstance(pin, Mapping):
            number = str(pin.get("number", ""))
            name = str(pin.get("name", ""))
            etype = str(pin.get("type", pin.get("electrical_type", "")))
        else:
            number = str(getattr(pin, "number", ""))
            name = str(getattr(pin, "name", ""))
            etype = str(getattr(pin, "electrical_type", ""))
        if etype == "power_in" and number and not _is_ground_name(name):
            numbers.append(number)
    return numbers


def normalize_part_supply_ratings(entry: Mapping[str, Any], *, where: str = "part") -> tuple[SupplyRating, ...]:
    """Return a part entry's ratings in the normalized shape.

    ``supply_ratings`` is authoritative. Older entries that only carry
    ``vin_min``/``vin_max``/``vin_abs_max`` (applied to the ``pin_vin`` role),
    ``vdd_min``/``vdd_max`` or ``supply_range`` (applied to every non-ground
    supply pin) are read the same way, so custom part files keep working.
    """

    if "supply_ratings" in entry:
        return parse_supply_ratings(entry.get("supply_ratings"), where=f"{where}.supply_ratings")

    supply_pins = _supply_pin_numbers(entry.get("pins", ()))
    legacy: list[dict[str, Any]] = []
    if any(key in entry for key in ("vin_min", "vin_max", "vin_abs_max")):
        pin_vin = entry.get("pin_vin")
        pins = [str(pin_vin)] if pin_vin else supply_pins
        if pins and (entry.get("vin_max") is not None or entry.get("vin_abs_max") is not None):
            legacy.append(
                {
                    "pins": pins,
                    "v_min": entry.get("vin_min"),
                    "v_max": entry.get("vin_max"),
                    "v_abs_max": entry.get("vin_abs_max"),
                    "source": "legacy vin_min/vin_max fields",
                }
            )
    elif any(key in entry for key in ("vdd_min", "vdd_max")):
        if supply_pins and entry.get("vdd_max") is not None:
            legacy.append(
                {
                    "pins": supply_pins,
                    "v_min": entry.get("vdd_min"),
                    "v_max": entry.get("vdd_max"),
                    "source": "legacy vdd_min/vdd_max fields",
                }
            )
    elif isinstance(entry.get("supply_range"), (list, tuple)) and len(entry["supply_range"]) == 2:
        low, high = entry["supply_range"]
        if supply_pins and high is not None:
            legacy.append({"pins": supply_pins, "v_min": low, "v_max": high, "source": "legacy supply_range field"})
    # Older fields carry no provenance, so they can only ever warn.
    return parse_supply_ratings(legacy, where=f"{where} (legacy rating fields)", default_confidence="single_source")


def _instance_power_pin_ratings(component: Any) -> tuple[SupplyRating, ...]:
    """Per-pin envelopes a spec declared with ``power_pin_defs``."""
    ratings = []
    for power_pin in getattr(component, "power_pin_defs", ()) or ():
        v_max = _number(getattr(power_pin, "v_max", None))
        v_min = _number(getattr(power_pin, "v_min", None))
        if v_max is None and v_min is None:
            continue
        ratings.append(
            SupplyRating(
                pins=(str(power_pin.pin),),
                v_min=v_min,
                v_max=v_max,
                source="design spec power_pin_defs",
            )
        )
    return tuple(ratings)


def _part_entry_ratings(component: Any) -> tuple[SupplyRating, ...]:
    from .ic_data import get_ic_data

    for mpn in (getattr(component, "mpn", ""), getattr(component, "source_mpn", "")):
        if not mpn:
            continue
        entry = get_ic_data(mpn)
        if entry:
            return normalize_part_supply_ratings(entry, where=f"part {mpn}")
    return ()


def component_supply_ratings(component: Any) -> tuple[SupplyRating, ...]:
    """Resolve a component instance's ratings.

    Precedence: instance ``power_pin_defs`` envelopes, then the instance's own
    ``supply_ratings`` (from the spec or the built-in registry), then the
    curated part entry with the same part number.
    """

    by_pin: dict[str, SupplyRating] = {}
    layers = (
        _part_entry_ratings(component)
        if not getattr(component, "supply_ratings", None)
        else parse_supply_ratings(
            component.supply_ratings, where=f"{getattr(component, 'mpn', 'component')}.supply_ratings"
        ),
        _instance_power_pin_ratings(component),
    )
    for layer in layers:
        for rating in layer:
            for pin in rating.pins:
                by_pin[pin] = rating
    # Re-group so a rating that covered several pins stays one entry.
    ordered: dict[int, SupplyRating] = {}
    for rating in by_pin.values():
        ordered.setdefault(id(rating), rating)
    return tuple(ordered.values())


# ----------------------------------------------------------------------------
# Rail voltages
# ----------------------------------------------------------------------------

_RANGE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*v?\s*(?:-|–|to|\.\.)\s*([0-9]*\.?[0-9]+)\s*v?\s*$", re.IGNORECASE)
_SINGLE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*v?\s*$", re.IGNORECASE)


def parse_voltage(value: Any) -> tuple[float, float] | None:
    """Parse ``12``, ``"12V"`` or ``"4.5-5.5 V"`` into (min, max) volts."""
    number = _number(value)
    if number is not None:
        return (number, number)
    if not isinstance(value, str):
        return None
    match = _RANGE_RE.match(value)
    if match:
        low, high = float(match.group(1)), float(match.group(2))
        return (min(low, high), max(low, high))
    match = _SINGLE_RE.match(value)
    if match:
        number = float(match.group(1))
        return (number, number)
    return None


_NAME_PATTERNS = (
    re.compile(r"(\d+)P(\d+)"),
    re.compile(r"(\d+)V(\d+)"),
    re.compile(r"(\d+(?:\.\d+)?)V(?![A-Z0-9])"),
)


def voltage_from_net_name(net: str) -> float | None:
    """Read a voltage encoded in a rail name (``VDD_3P3``, ``VIN_12V``, ``3V3``)."""
    upper = (net or "").upper()
    for pattern in _NAME_PATTERNS:
        match = pattern.search(upper)
        if not match:
            continue
        if len(match.groups()) == 2:
            return float(f"{match.group(1)}.{match.group(2)}")
        return float(match.group(1))
    return None


def _is_ground_net(net: str) -> bool:
    upper = (net or "").upper()
    return any(upper == prefix or upper.startswith(f"{prefix}_") for prefix in _GROUND_PREFIXES)


def _field(item: Any, *names: str) -> Any:
    for name in names:
        value = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        if value is not None:
            return value
    return None


def declared_rail_voltages(ir: Any) -> tuple[dict[str, RailVoltage], list[tuple[str, list[str]]]]:
    """Collect rail voltages the design states, plus conflicting declarations.

    Returns ``(rails, conflicts)``. When several declarations disagree, the
    rail keeps the widest envelope (worst case for both over- and under-
    voltage) and the disagreement is reported separately.
    """

    observations: dict[str, list[tuple[float, float, str, str]]] = {}

    for domain in getattr(ir, "power_domains", ()) or ():
        net = str(_field(domain, "net", "name") or "").strip()
        if not net:
            continue
        nominal = _number(_field(domain, "v_nominal", "voltage"))
        v_min = _number(_field(domain, "v_min"))
        v_max = _number(_field(domain, "v_max"))
        tolerance = _number(_field(domain, "tolerance"))
        if nominal is not None and tolerance is not None:
            v_min = v_min if v_min is not None else nominal * (1 - tolerance)
            v_max = v_max if v_max is not None else nominal * (1 + tolerance)
        low = v_min if v_min is not None else nominal
        high = v_max if v_max is not None else nominal
        if low is None and high is None:
            continue
        low = low if low is not None else high
        high = high if high is not None else low
        observations.setdefault(net, []).append(
            (float(low), float(high), BASIS_POWER_DOMAIN, f"power_domains entry for {net}")
        )

    for block in getattr(ir, "blocks", ()) or ():
        params = getattr(block, "params", None) or {}
        if not isinstance(params, Mapping):
            continue
        owner = str(getattr(block, "ref", "") or getattr(block, "id", "") or "block")
        kind = str(getattr(block, "template_type", "") or getattr(block, "kind", "") or "")
        for net_key, nominal_key, min_key, max_key in _RAIL_PARAMETER_PAIRS:
            net = str(params.get(net_key) or "").strip()
            if not net or _is_ground_net(net):
                continue
            parsed = parse_voltage(params.get(nominal_key))
            low = _number(params.get(min_key)) if min_key else None
            high = _number(params.get(max_key)) if max_key else None
            if parsed is None and low is None and high is None:
                continue
            if parsed is not None:
                low = parsed[0] if low is None else min(low, parsed[0])
                high = parsed[1] if high is None else max(high, parsed[1])
            low = low if low is not None else high
            high = high if high is not None else low
            label = f"{owner}{f' ({kind})' if kind else ''} {nominal_key}"
            observations.setdefault(net, []).append((float(low), float(high), BASIS_BLOCK_PARAMETER, label))

    rails: dict[str, RailVoltage] = {}
    conflicts: list[tuple[str, list[str]]] = []
    for net, entries in observations.items():
        domain_entries = [entry for entry in entries if entry[2] == BASIS_POWER_DOMAIN]
        used = domain_entries or entries
        low = min(entry[0] for entry in used)
        high = max(entry[1] for entry in used)
        sources = tuple(f"{entry[3]} = {_format_range(entry[0], entry[1])}" for entry in used)
        rails[net] = RailVoltage(
            net=net,
            v_min=low,
            v_max=high,
            basis=used[0][2] if domain_entries else BASIS_BLOCK_PARAMETER,
            sources=sources,
        )
        nominals = sorted({round((entry[0] + entry[1]) / 2, 4) for entry in entries})
        if len(nominals) > 1 and (nominals[-1] - nominals[0]) / max(nominals[-1], 0.1) > 0.10:
            conflicts.append((net, [f"{entry[3]} = {_format_range(entry[0], entry[1])}" for entry in entries]))
    return rails, conflicts


def _format_volts(value: float) -> str:
    return f"{value:g} V"


def _format_range(low: float, high: float) -> str:
    return _format_volts(low) if abs(high - low) < 1e-9 else f"{low:g}–{high:g} V"


# ----------------------------------------------------------------------------
# The check
# ----------------------------------------------------------------------------


def _component_subject(component: Any) -> str:
    return str(getattr(component, "source_ref", "") or getattr(component, "mpn", "") or "component")


def _rating_optional(component: Any) -> bool:
    """Parts whose supply pins are conduits rather than loads (connectors,
    passives, test points) are checked when rated, but a missing rating on
    them is not reported."""
    prefix = str(getattr(component, "ref_prefix", "") or "").upper()
    category = str(getattr(component, "category", "") or "").lower()
    return prefix in _RATING_OPTIONAL_REF_PREFIXES or category in _RATING_OPTIONAL_CATEGORIES


def _pin_net(component: Any, pin_number: str) -> str:
    power_pins = getattr(component, "power_pins", {}) or {}
    pin_nets = getattr(component, "pin_nets", {}) or {}
    return str(power_pins.get(pin_number) or pin_nets.get(pin_number) or "").strip()


def _covered_by_typed_envelope(component: Any, net: str, declared_domains: set[str]) -> bool:
    """The power-envelope check already compares a declared domain against a
    component power requirement that states its own limit; don't double-report."""
    if net not in declared_domains:
        return False
    for req in getattr(component, "power_reqs", ()) or ():
        if getattr(req, "net", None) == net and (
            _number(getattr(req, "v_max", None)) is not None or _number(getattr(req, "v_min", None)) is not None
        ):
            return True
    return False


def check_supply_ratings(ir: Any, components: Iterable[Any]) -> list[SupplyFinding]:
    """Compare every rated supply pin's rail voltage with its rating."""

    declared, conflicts = declared_rail_voltages(ir)
    declared_domains = {
        str(_field(domain, "net", "name") or "").strip() for domain in getattr(ir, "power_domains", ()) or ()
    }
    findings: list[SupplyFinding] = []

    for net, sources in conflicts:
        findings.append(
            SupplyFinding(
                code="power-domain-voltage-conflict",
                rule_id=None,
                level="error",
                subject=net,
                message=f"Rail '{net}' is declared at different voltages: {'; '.join(sources)}",
                suggestion=(
                    "Make every block that names this rail agree on its voltage, or split it into separate rails."
                ),
                calculation={"rail": net, "declarations": sources},
            )
        )

    for component in components:
        if getattr(component, "is_virtual_container", False):
            continue
        subject = _component_subject(component)
        mpn = str(getattr(component, "mpn", "") or "")
        try:
            ratings = component_supply_ratings(component)
        except ValueError as exc:
            findings.append(
                SupplyFinding(
                    code="supply-rating-invalid",
                    rule_id=None,
                    level="error",
                    subject=subject,
                    message=f"{subject} ({mpn}): supply rating data is malformed: {exc}",
                    suggestion="Correct the part's supply_ratings entry against its datasheet.",
                    calculation={"component": subject, "mpn": mpn},
                )
            )
            continue
        rating_by_pin = {pin: rating for rating in ratings for pin in rating.pins}

        unrated: dict[str, list[str]] = {}
        unknown_rail: dict[str, list[str]] = {}
        grouped: dict[tuple[str, int], list[str]] = {}
        for pin in getattr(component, "pins", ()) or ():
            if getattr(pin, "electrical_type", "") != "power_in":
                continue
            number = str(pin.number)
            net = _pin_net(component, number)
            if not net or _is_ground_net(net) or _is_ground_name(str(pin.name)):
                continue
            rating = rating_by_pin.get(number)
            if rating is None:
                if not _rating_optional(component):
                    unrated.setdefault(net, []).append(f"{number} ({pin.name})")
                continue
            if rating.kind != "supply":
                continue
            if _covered_by_typed_envelope(component, net, declared_domains):
                continue
            grouped.setdefault((net, id(rating)), []).append(f"{number} ({pin.name})")

        for net, pin_labels in sorted(unrated.items()):
            rail = declared.get(net)
            known = rail is not None or voltage_from_net_name(net) is not None
            rail_text = f" at {_format_range(rail.v_min, rail.v_max)}" if rail else ""
            findings.append(
                SupplyFinding(
                    code="supply-rating-missing",
                    rule_id="CW-PWR-014",
                    level="warning",
                    subject=subject,
                    message=(
                        f"{subject} ({mpn}): supply pin{'s' if len(pin_labels) > 1 else ''} "
                        f"{', '.join(pin_labels)} on '{net}'{rail_text} "
                        f"{'have' if len(pin_labels) > 1 else 'has'} no datasheet voltage rating, "
                        "so over- and under-voltage cannot be checked"
                    ),
                    suggestion=(
                        "Add supply_ratings (recommended v_min/v_max and absolute v_abs_max per supply pin) "
                        "to the part entry from its datasheet."
                    ),
                    calculation={
                        "component": subject,
                        "mpn": mpn,
                        "rail": net,
                        "pins": pin_labels,
                        "rail_voltage_known": known,
                    },
                )
            )

        by_id = {id(rating): rating for rating in ratings}
        for (net, rating_id), pin_labels in sorted(grouped.items()):
            rating = by_id[rating_id]
            rail = declared.get(net)
            if rail is None:
                implied = voltage_from_net_name(net)
                if implied is None:
                    unknown_rail.setdefault(net, []).extend(pin_labels)
                    continue
                rail = RailVoltage(
                    net=net,
                    v_min=implied,
                    v_max=implied,
                    basis=BASIS_NET_NAME,
                    sources=(f"rail name '{net}' implies {_format_volts(implied)}",),
                )
            findings.extend(_compare(component, subject, mpn, rating, rail, pin_labels))

        for net, pin_labels in sorted(unknown_rail.items()):
            findings.append(
                SupplyFinding(
                    code="supply-voltage-unknown",
                    rule_id="CW-PWR-015",
                    level="warning",
                    subject=subject,
                    message=(
                        f"{subject} ({mpn}): supply pin{'s' if len(pin_labels) > 1 else ''} {', '.join(pin_labels)} "
                        f"on '{net}' cannot be checked against the part's rating because the design never states "
                        f"the voltage of '{net}'"
                    ),
                    suggestion=(
                        f"Declare '{net}' in power_domains (v_nominal or v_min/v_max), or name the regulator block "
                        "that produces it (rail_name + vout)."
                    ),
                    calculation={"component": subject, "mpn": mpn, "rail": net, "pins": pin_labels},
                )
            )

    return findings


def _compare(
    component: Any,
    subject: str,
    mpn: str,
    rating: SupplyRating,
    rail: RailVoltage,
    pin_labels: list[str],
) -> list[SupplyFinding]:
    findings: list[SupplyFinding] = []
    declared = rail.declared
    level = "error" if declared and rating.confirming else "warning"
    # A voltage read from the rail's name is a strong hint, not a statement of
    # intent, and a single-source rating is not yet trustworthy, so either one
    # makes the finding a review warning rather than a confirmed error.
    hints = []
    if not declared:
        hints.append("this voltage is only implied by the rail's name; declare it to confirm")
    if not rating.confirming:
        hints.append(f"the rating is {rating.confidence.replace('_', '-')}; confirm it against the datasheet")
    hint = f" ({'; '.join(hints)})" if hints else ""
    pins_text = f"pin{'s' if len(pin_labels) > 1 else ''} {', '.join(pin_labels)}"
    basis_text = "; ".join(rail.sources)
    rating_text = rating.source or "part supply_ratings"
    common_inputs = {
        "component": subject,
        "mpn": mpn,
        "pins": pin_labels,
        "rail": rail.net,
        "rail_v_min": {"value": rail.v_min, "unit": "V"},
        "rail_v_max": {"value": rail.v_max, "unit": "V"},
        "rail_basis": rail.basis,
        "rail_sources": list(rail.sources),
        "rating": rating.to_dict(),
    }

    upper = rating.upper_limit
    if upper is not None and rail.v_max > upper + 1e-9:
        over_abs = rating.v_abs_max is not None and rail.v_max > rating.v_abs_max + 1e-9
        if over_abs:
            consequence = (
                f"above its absolute maximum rating of {_format_volts(rating.v_abs_max)}; this can destroy the part"
            )
        elif rating.v_max is not None:
            abs_text = f" (absolute maximum {_format_volts(rating.v_abs_max)})" if rating.v_abs_max is not None else ""
            consequence = f"above its recommended maximum of {_format_volts(rating.v_max)}{abs_text}"
        else:
            consequence = f"above its absolute maximum rating of {_format_volts(upper)}"
        findings.append(
            SupplyFinding(
                code="power-over-voltage",
                rule_id="CW-PWR-001",
                level=level,
                subject=subject,
                message=(
                    f"{subject} ({mpn}) {pins_text} on '{rail.net}' sees {_format_volts(rail.v_max)}, "
                    f"{consequence}{hint}. Rail voltage from: {basis_text}. Rating from: {rating_text}"
                ),
                suggestion=(
                    f"Power {subject} from a rail of at most {_format_volts(upper)}, add a regulator in front of it, "
                    f"or choose a part rated for {_format_volts(rail.v_max)}."
                ),
                calculation={
                    "rule_id": "CW-PWR-001",
                    "equation": "rail_v_max <= rating_v_max",
                    "inputs": common_inputs,
                    "observed": {"value": rail.v_max, "unit": "V"},
                    "expected": {"maximum": {"value": upper, "unit": "V"}},
                    "margin": {"value": round(upper - rail.v_max, 6), "unit": "V"},
                },
            )
        )
    if rating.v_min is not None and rail.v_min < rating.v_min - 1e-9:
        findings.append(
            SupplyFinding(
                code="power-under-voltage",
                rule_id="CW-PWR-002",
                level=level,
                subject=subject,
                message=(
                    f"{subject} ({mpn}) {pins_text} on '{rail.net}' sees {_format_volts(rail.v_min)}, "
                    f"below its recommended minimum of {_format_volts(rating.v_min)}{hint}. "
                    f"Rail voltage from: {basis_text}. Rating from: {rating_text}"
                ),
                suggestion=(
                    f"Power {subject} from a rail of at least {_format_volts(rating.v_min)}, or choose a part that "
                    f"operates at {_format_volts(rail.v_min)}."
                ),
                calculation={
                    "rule_id": "CW-PWR-002",
                    "equation": "rail_v_min >= rating_v_min",
                    "inputs": common_inputs,
                    "observed": {"value": rail.v_min, "unit": "V"},
                    "expected": {"minimum": {"value": rating.v_min, "unit": "V"}},
                    "margin": {"value": round(rail.v_min - rating.v_min, 6), "unit": "V"},
                },
            )
        )
    return findings
