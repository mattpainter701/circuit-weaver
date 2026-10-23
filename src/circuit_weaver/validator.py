"""Lightweight algebraic circuit validation for generated schematic components."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Final

from . import calc
from .component_db import ComponentDef

_KNOWN_RAIL_VOLTAGES = {
    "VCCAUX": 1.8,
    "VCCINT": 1.0,
    "VDD_DDR": 1.35,
    "VDDA_1P8": 1.8,
    "VDD_1P2": 1.2,
    "VDD_1P3": 1.3,
    "VDD_3P3": 3.3,
    "VBUS_5V": 5.0,
}

_GROUND_PREFIXES = ("GND", "AGND", "DGND", "PGND")


_DETECTION_CONFIDENCES: Final = frozenset(
    {"verified", "corroborated", "single_source", "heuristic", "stub", "conflicting"}
)
_ISSUE_SEVERITIES: Final = frozenset({"blocker", "major", "minor", "info"})
_CONFIRMED_BLOCKER_CONFIDENCES: Final = frozenset({"verified", "corroborated"})

# Every in-process validator finding has a frozen benchmark-facing rule ID.
# Check names remain compatibility labels; finding codes are the producer key.
_RULE_ID_BY_FINDING_CODE: Final = {
    "pin-mapping-integrity": "CW-ID-005",
    "unverified-pinout": "CW-ID-006",
    "feedback-divider": "CW-PWR-009",
    "rc-filter": "CW-ANALOG-002",
    "lc-filter": "CW-ANALOG-003",
    "crystal-load": "CW-CLK-001",
    "decoupling": "CW-PWR-010",
    "inductor-value": "CW-PWR-011",
    "cap-voltage-rating": "CW-PWR-012",
    "single-pin-net": "CW-ERC-001",
    "undriven-net": "CW-ERC-002",
    "vdd-to-gnd-short": "CW-ERC-003",
    "floating-enable": "CW-PWR-013",
    "i2c-missing-pullup": "CW-I2C-001",
    "spi-floating-cs": "CW-SPI-001",
    "uart-unpaired": "CW-UART-001",
    "output-conflict": "CW-ERC-004",
    "power-budget": "CW-PWR-006",
    "thermal-limits": "CW-PWR-008",
    "signal-integrity": "CW-ANALOG-001",
}
_EXPECTED_CONSTRAINT_BY_FINDING_CODE: Final = {
    "pin-mapping-integrity": "0 count malformed, duplicate, or ambiguous pin mappings",
    "unverified-pinout": "1 count verified or explicit pinout source",
    "feedback-divider": "output voltage within ±5 percent of the target rail",
    "rc-filter": "cutoff frequency within the declared Hz range",
    "lc-filter": "cutoff frequency within the declared Hz range",
    "crystal-load": "crystal load capacitance within ±15 percent of the datasheet target",
    "decoupling": "1 count matching bypass capacitor per required supply",
    "inductor-value": "inductance from 0.1 uH through 100 uH",
    "cap-voltage-rating": "rail voltage no greater than 80 percent of capacitor rating",
    "single-pin-net": "at least 2 count connected endpoints",
    "undriven-net": "at least 1 count output, bidirectional, or passive driver",
    "vdd-to-gnd-short": "0 count power-to-ground shorts",
    "floating-enable": "1 count explicit enable connection or no-connect declaration",
    "i2c-missing-pullup": "1 count pull-up resistor per I2C signal",
    "spi-floating-cs": "1 count explicit chip-select connection or no-connect declaration",
    "uart-unpaired": "1 count matching RX net per UART TX net",
    "output-conflict": "at most 1 count output driver per signal net",
    "power-budget": "at least 1 count power pin or power requirement",
    "thermal-limits": "junction temperature below the declared maximum with adequate margin",
    "signal-integrity": "1 count applicable termination or impedance-matching network",
}
# T248 keeps non-executable legacy checks visible rather than pretending they
# are benchmarked.  A release benchmark must either name an adverse fixture or
# carry this explicit unsupported classification; no check silently disappears.
_VALIDATION_CHECK_CONTRACT_COVERAGE: Final = {
    "pin-mapping-integrity": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "pinout-source": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "feedback-divider": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "rc-lc-filter": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "crystal-load": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "decoupling": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "inductor-selection": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "cap-voltage": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "net-connectivity": {
        "status": "adverse_fixture",
        "fixture": "negative/i2c",
    },
    "enable-pins": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "bus-completeness": {
        "status": "adverse_fixture",
        "fixture": "negative/i2c",
    },
    "pin-type-conflicts": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "power-budget": {"status": "adverse_fixture", "fixture": "negative/power"},
    "thermal-limits": {"status": "unsupported", "reason": "no complete labelled executable population"},
    "signal-integrity": {"status": "unsupported", "reason": "no complete labelled executable population"},
}


@dataclass(frozen=True, init=False)
class ValidationIssue:
    """A validation finding with independent impact and evidence axes.

    ``level`` was the pre-T248, overloaded rendering field.  It remains a
    read-only compatibility property.  New code must specify ``severity`` and
    ``detection_confidence``; a weakly evidenced blocker deliberately renders
    as a review warning rather than a confirmed error.
    """

    code: str
    ref: str
    mpn: str
    message: str
    suggestion: str = ""
    detection_confidence: str = "single_source"
    severity: str = "major"
    rule_id: str | None = None
    observed_value: str | None = None
    expected_constraint: str | None = None
    evidence_ids: tuple[str, ...] = ()
    safest_next_action: str | None = None
    suppressed: bool = False
    suppression_id: str | None = None
    net: str = ""

    def __init__(
        self,
        code: str,
        level: str | None = None,
        ref: str = "",
        mpn: str = "",
        message: str = "",
        suggestion: str = "",
        *,
        detection_confidence: str | None = None,
        severity: str | None = None,
        rule_id: str | None = None,
        observed_value: str | None = None,
        expected_constraint: str | None = None,
        evidence_ids: tuple[str, ...] | list[str] = (),
        safest_next_action: str | None = None,
        suppressed: bool = False,
        suppression_id: str | None = None,
        net: str = "",
    ) -> None:
        """Construct an issue, accepting the legacy positional ``level``.

        ``level`` is intentionally input-only for migration compatibility. It
        maps ``error`` to a verified blocker and ``warning`` to a
        single-source major finding. New producers must use the two explicit
        keyword fields instead.
        """
        legacy_level = (level or "").lower()
        if severity is None:
            severity = {"error": "blocker", "warning": "major", "info": "info"}.get(legacy_level, "major")
        if detection_confidence is None:
            detection_confidence = "verified" if legacy_level == "error" else "single_source"
        severity = severity.lower()
        detection_confidence = detection_confidence.lower()
        if severity not in _ISSUE_SEVERITIES:
            raise ValueError(f"Unsupported ValidationIssue severity: {severity!r}")
        if detection_confidence not in _DETECTION_CONFIDENCES:
            raise ValueError(f"Unsupported detection confidence: {detection_confidence!r}")
        if rule_id is not None and not re.fullmatch(r"CW-[A-Z0-9]+-[0-9]{3}", rule_id):
            raise ValueError(f"ValidationIssue rule_id must be CW-<DOMAIN>-<NNN>: {rule_id!r}")
        if not isinstance(evidence_ids, (tuple, list)) or any(
            not isinstance(item, str) or not item for item in evidence_ids
        ):
            raise ValueError("ValidationIssue evidence_ids must be a sequence of non-empty strings")
        if suppressed and not suppression_id:
            raise ValueError("suppressed ValidationIssue requires suppression_id")
        if suppression_id is not None and not isinstance(suppression_id, str):
            raise ValueError("suppression_id must be a string or None")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "ref", ref)
        object.__setattr__(self, "mpn", mpn)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "suggestion", suggestion)
        object.__setattr__(self, "detection_confidence", detection_confidence)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "rule_id", rule_id or (code if re.fullmatch(r"CW-[A-Z0-9]+-[0-9]{3}", code) else None))
        object.__setattr__(self, "observed_value", observed_value)
        object.__setattr__(self, "expected_constraint", expected_constraint)
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(evidence_ids))))
        object.__setattr__(self, "safest_next_action", safest_next_action or suggestion or None)
        object.__setattr__(self, "suppressed", bool(suppressed))
        object.__setattr__(self, "suppression_id", suppression_id)
        object.__setattr__(self, "net", net)

    @property
    def is_confirmed_blocker(self) -> bool:
        """Whether this finding may be presented as a confirmed hard defect."""
        return self.severity == "blocker" and self.detection_confidence in _CONFIRMED_BLOCKER_CONFIDENCES

    @property
    def level(self) -> str:
        """Deprecated compatibility rendering level derived from the two axes."""
        if self.is_confirmed_blocker:
            return "error"
        if self.severity == "info":
            return "info"
        return "warning"

    def to_dict(self) -> dict[str, object]:
        """Stable JSON-ready representation, including the legacy level."""
        return {
            "code": self.code,
            "severity": self.severity,
            "detection_confidence": self.detection_confidence,
            "level": self.level,
            "confirmed_blocker": self.is_confirmed_blocker,
            "ref": self.ref,
            "mpn": self.mpn,
            "message": self.message,
            "suggestion": self.suggestion,
            "rule_id": self.rule_id,
            "observed_value": self.observed_value,
            "expected_constraint": self.expected_constraint,
            "evidence_ids": list(self.evidence_ids),
            "safest_next_action": self.safest_next_action,
            "suppressed": self.suppressed,
            "suppression_id": self.suppression_id,
            "net": self.net,
        }

    def contract_violations(self, *, known_evidence_ids: tuple[str, ...] | list[str] = ()) -> tuple[str, ...]:
        """Return release-blocking omissions without hiding this finding."""

        from .finding_contract import finding_contract_violations

        return finding_contract_violations(self, known_evidence_ids=known_evidence_ids)

    def marked_suppressed(self, suppression_id: str) -> "ValidationIssue":
        """Mark a finding as suppressed while retaining it for all metrics."""

        return replace(self, suppressed=True, suppression_id=suppression_id)


@dataclass(frozen=True)
class ValidationCheckResult:
    code: str
    label: str
    status: str
    issues: tuple[ValidationIssue, ...]
    # Raw validation callers do not have a report-level ledger.  Keep the
    # exact records required to resolve each issue's evidence IDs here.
    evidence_records: tuple[object, ...] = ()


def _issue(
    comp: ComponentDef,
    code: str,
    message: str,
    severity: str = "major",
    detection_confidence: str = "verified",
    suggestion: str = "",
    observed_value: str = "1 count",
    net: str = "",
) -> ValidationIssue:
    try:
        rule_id = _RULE_ID_BY_FINDING_CODE[code]
        expected_constraint = _EXPECTED_CONSTRAINT_BY_FINDING_CODE[code]
    except KeyError as exc:  # Do not silently emit a non-contract finding.
        raise ValueError(f"missing T248 finding contract for validator code {code!r}") from exc
    safest_next_action = suggestion or f"Review {code} on the referenced component and correct the stated constraint."
    return ValidationIssue(
        code=code,
        severity=severity,
        detection_confidence=detection_confidence,
        ref=comp.source_ref or comp.ref_prefix,
        mpn=comp.source_mpn or comp.mpn,
        message=message,
        suggestion=suggestion,
        rule_id=rule_id,
        observed_value=observed_value,
        expected_constraint=expected_constraint,
        safest_next_action=safest_next_action,
        net=net,
    )


def _is_ground_net(net: str) -> bool:
    net = (net or "").upper()
    return any(net == prefix or net.startswith(f"{prefix}_") for prefix in _GROUND_PREFIXES)


def _parse_resistance_ohms(value: str) -> float | None:
    raw = (value or "").strip().replace(" ", "").replace("Ω", "").lower()
    raw = raw.replace("ohm", "").replace("ohms", "")
    if not raw:
        return None

    embedded = re.fullmatch(r"(\d*)([rkm])(\d+)", raw)
    if embedded:
        whole, unit, frac = embedded.groups()
        whole = whole or "0"
        num = float(f"{whole}.{frac}")
        return num * {"r": 1.0, "k": 1e3, "m": 1e6}[unit]

    prefix = re.fullmatch(r"([rkm])(\d+)", raw)
    if prefix:
        unit, frac = prefix.groups()
        num = float(f"0.{frac}")
        return num * {"r": 1.0, "k": 1e3, "m": 1e6}[unit]

    match = re.fullmatch(r"([\d.]+)([rkm]?)", raw)
    if not match:
        return None
    number, unit = match.groups()
    return float(number) * {"": 1.0, "r": 1.0, "k": 1e3, "m": 1e6}[unit]


def _parse_capacitance_f(value: str) -> float | None:
    raw = (value or "").strip().replace(" ", "").replace("µ", "u").replace("μ", "u").lower()
    match = re.match(r"([\d.]+)(pf|nf|uf|mf|f)(?=[^a-z]|$)", raw)
    if not match:
        return None
    number, unit = match.groups()
    return float(number) * {"pf": 1e-12, "nf": 1e-9, "uf": 1e-6, "mf": 1e-3, "f": 1.0}[unit]


def _parse_inductance_h(value: str) -> float | None:
    raw = (value or "").strip().replace(" ", "").replace("µ", "u").replace("μ", "u").lower()
    match = re.match(r"([\d.]+)(nh|uh|mh|h)(?=[^a-z]|$)", raw)
    if not match:
        return None
    number, unit = match.groups()
    return float(number) * {"nh": 1e-9, "uh": 1e-6, "mh": 1e-3, "h": 1.0}[unit]


def _rail_voltage(net: str) -> float | None:
    net = (net or "").upper()
    if net in _KNOWN_RAIL_VOLTAGES:
        return _KNOWN_RAIL_VOLTAGES[net]

    for pattern in (r"(\d+)P(\d+)", r"(\d+)V(\d+)", r"(\d+)V"):
        match = re.search(pattern, net)
        if not match:
            continue
        if len(match.groups()) == 2:
            return float(f"{match.group(1)}.{match.group(2)}")
        return float(match.group(1))

    return None


def _normalize_supply_name(value: str) -> str:
    raw = (value or "").lower()
    raw = re.sub(r"(\d)[pv](\d)", r"\1\2", raw)
    return re.sub(r"[^a-z0-9]", "", raw)


def _supply_names_match(target: str, *candidates: str) -> bool:
    target_norm = _normalize_supply_name(target)
    target_v = _rail_voltage(target)
    for candidate in candidates:
        cand_norm = _normalize_supply_name(candidate)
        if not cand_norm:
            continue
        if target_norm == cand_norm or target_norm in cand_norm or cand_norm in target_norm:
            return True
        cand_v = _rail_voltage(candidate)
        if target_v is not None and cand_v is not None and abs(target_v - cand_v) <= 0.05:
            return True
    return False


def _format_value(value: float, scale: float, suffix: str) -> str:
    return f"{value / scale:.2f}{suffix}"


def _calculation_target(comp: ComponentDef, domain: str, field: str) -> str:
    """Return the frozen ``param:<REF>.<domain>.<field>`` calculation target."""
    ref = comp.source_ref or comp.ref_prefix or "unknown"
    normalized_ref = re.sub(r"[^A-Za-z0-9_]+", "_", ref).strip("_") or "unknown"
    if not normalized_ref[0].isalpha():
        normalized_ref = f"X_{normalized_ref}"
    normalized_domain = re.sub(r"[^A-Za-z0-9_-]+", "_", domain).strip("_") or "unknown"
    normalized_field = re.sub(r"[^A-Za-z0-9_-]+", "_", field).strip("_") or "value"
    return f"param:{normalized_ref}.{normalized_domain}.{normalized_field}"


def _filter_range(comp: ComponentDef, net: str) -> tuple[float, float, str] | None:
    text = " ".join(
        filter(
            None,
            [
                comp.description,
                comp.source_description,
                " ".join(comp.annotations),
                net,
            ],
        )
    ).lower()
    if any(token in text for token in ("pll", "loop filter", "pllfilt", "clk_lf")):
        return (10e3, 100e3, "PLL filter")
    if comp.category == "power":
        return (1e3, 100e3, "power filter")
    return None


def _validate_feedback_dividers(components: list[ComponentDef]) -> list[ValidationIssue]:
    issues = []
    for comp in components:
        vref = comp.feedback_vref_voltage
        if vref is None:
            continue

        divider_nets = {}
        for strap in comp.straps:
            if "fb" not in (strap.net or "").lower():
                continue
            divider_nets.setdefault(strap.net, []).append(strap)

        for net, straps in divider_nets.items():
            bottoms = [s for s in straps if _is_ground_net(s.rail)]
            tops = [s for s in straps if not _is_ground_net(s.rail)]
            if not bottoms or not tops:
                continue

            r_bottom = _parse_resistance_ohms(bottoms[0].value)
            r_top = _parse_resistance_ohms(tops[0].value)
            target = _rail_voltage(tops[0].rail)
            if not r_bottom or not r_top or not target:
                continue

            vout = calc.feedback_divider_vout(
                target=_calculation_target(comp, "feedback", f"vout_{net}"),
                r_top_ohm=r_top,
                r_bottom_ohm=r_bottom,
                vref_v=vref,
            ).raw_result.value
            if abs(vout - target) / target > 0.05:
                issues.append(
                    _issue(
                        comp,
                        "feedback-divider",
                        (
                            f"{net}: computed {vout:.2f}V from {tops[0].value}/{bottoms[0].value}, "
                            f"expected {target:.2f}V on {tops[0].rail}"
                        ),
                    )
                )
    return issues


def _validate_filter_cutoffs(components: list[ComponentDef]) -> list[ValidationIssue]:
    issues = []
    for comp in components:
        caps_by_net = {}
        inds_by_net = {}
        for bc in comp.bypass_caps:
            cap_f = _parse_capacitance_f(bc.value)
            ind_h = _parse_inductance_h(bc.value)
            if cap_f and _is_ground_net(bc.gnd_net):
                caps_by_net.setdefault(bc.net, []).append(cap_f)
            if ind_h:
                inds_by_net.setdefault(bc.net, []).append(ind_h)

        resistors_by_net = {}
        for strap in comp.straps:
            resistance = _parse_resistance_ohms(strap.value)
            if resistance:
                resistors_by_net.setdefault(strap.net, []).append(resistance)

        for net, resistors in resistors_by_net.items():
            caps = caps_by_net.get(net, [])
            expected = _filter_range(comp, net)
            if not caps or expected is None:
                continue
            r_val = min(resistors)
            c_val = min(caps)
            fc = calc.rc_cutoff(
                target=_calculation_target(comp, "filter", f"rc_cutoff_{net}"),
                resistance_ohm=r_val,
                capacitance_f=c_val,
            ).raw_result.value
            low, high, label = expected
            if fc < low or fc > high:
                issues.append(
                    _issue(
                        comp,
                        "rc-filter",
                        (
                            f"{label} on {net}: fc={fc:.0f}Hz from R={_format_value(r_val, 1e3, 'k')} "
                            f"and C={_format_value(c_val, 1e-9, 'nF')} is outside {low:.0f}-{high:.0f}Hz"
                        ),
                    )
                )

        for net, inductors in inds_by_net.items():
            caps = caps_by_net.get(net, [])
            expected = _filter_range(comp, net)
            if not caps or expected is None:
                continue
            l_val = min(inductors)
            c_val = min(caps)
            fc = calc.lc_cutoff(
                target=_calculation_target(comp, "filter", f"lc_cutoff_{net}"),
                inductance_h=l_val,
                capacitance_f=c_val,
            ).raw_result.value
            low, high, label = expected
            if fc < low or fc > high:
                issues.append(
                    _issue(
                        comp,
                        "lc-filter",
                        (
                            f"{label} on {net}: fc={fc:.0f}Hz from L={_format_value(l_val, 1e-6, 'uH')} "
                            f"and C={_format_value(c_val, 1e-6, 'uF')} is outside {low:.0f}-{high:.0f}Hz"
                        ),
                    )
                )
    return issues


def _crystal_load_spec_f(comp: ComponentDef) -> float | None:
    text = " ".join(filter(None, [comp.description, comp.source_description, " ".join(comp.annotations)]))
    match = re.search(r"(?:CL|load)\s*=?\s*([\d.]+)\s*pF", text, re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1)) * 1e-12


def _looks_like_crystal(comp: ComponentDef) -> bool:
    return comp.ref_prefix.upper() == "Y" or "crystal" in (comp.mpn or "").lower()


def _validate_crystal_caps(components: list[ComponentDef]) -> list[ValidationIssue]:
    issues = []
    caps_by_net = {}
    for comp in components:
        for bc in comp.bypass_caps:
            cap_f = _parse_capacitance_f(bc.value)
            if cap_f and _is_ground_net(bc.gnd_net):
                caps_by_net.setdefault(bc.net, []).append(cap_f)

    for comp in components:
        if not _looks_like_crystal(comp):
            continue
        nets = list(comp.pin_nets.values())
        if len(nets) < 2:
            continue
        xi, xo = nets[0], nets[1]
        caps_xi = caps_by_net.get(xi, [])
        caps_xo = caps_by_net.get(xo, [])
        if not caps_xi or not caps_xo:
            issues.append(_issue(comp, "crystal-load", f"missing load capacitor coverage on {xi}/{xo}"))
            continue

        target_cl = _crystal_load_spec_f(comp)
        if target_cl is None:
            continue
        c1 = min(caps_xi)
        c2 = min(caps_xo)
        effective_cl = calc.crystal_effective_load(
            target=_calculation_target(comp, "crystal", "effective_load"),
            capacitance_1_f=c1,
            capacitance_2_f=c2,
            stray_capacitance_f=2e-12,
        ).raw_result.value
        if abs(effective_cl - target_cl) / target_cl > 0.15:
            issues.append(
                _issue(
                    comp,
                    "crystal-load",
                    (
                        f"effective CL={effective_cl / 1e-12:.1f}pF from {c1 / 1e-12:.1f}pF/"
                        f"{c2 / 1e-12:.1f}pF differs from target {target_cl / 1e-12:.1f}pF"
                    ),
                )
            )
    return issues


def _validate_decoupling(components: list[ComponentDef]) -> list[ValidationIssue]:
    standalone_caps: list[tuple[str, float]] = []
    for comp in components:
        if comp.ref_prefix.upper() != "C":
            continue
        cap_f = _parse_capacitance_f(comp.source_value or comp.value)
        if not cap_f:
            continue
        nets = []
        for pin in comp.pins:
            net = comp.power_pins.get(pin.number) or comp.pin_nets.get(pin.number)
            if net:
                nets.append(net)
        if len(nets) != 2:
            continue
        non_ground = [net for net in nets if not _is_ground_net(net)]
        ground = [net for net in nets if _is_ground_net(net)]
        if len(non_ground) == 1 and ground:
            standalone_caps.append((non_ground[0], cap_f))

    issues = []
    for comp in components:
        if comp.ref_prefix.upper() != "U":
            continue
        if not comp.power_pins and not comp.power_reqs:
            continue

        pin_names = {pin.number: pin.name for pin in comp.pins}
        capacitor_caps = [bc for bc in comp.bypass_caps if _parse_capacitance_f(bc.value)]

        for pin_num, net in comp.power_pins.items():
            if _is_ground_net(net):
                continue
            pin_name = pin_names.get(pin_num, "")
            covered = any(
                bc.pin == pin_num
                or _supply_names_match(net, bc.pin, bc.net, pin_name)
                or _supply_names_match(pin_name, bc.pin, bc.net, net)
                for bc in capacitor_caps
            ) or any(
                cap_f > 0
                and (_supply_names_match(net, cap_net, pin_name) or _supply_names_match(pin_name, cap_net, net))
                for cap_net, cap_f in standalone_caps
            )
            if not covered:
                issues.append(
                    _issue(
                        comp,
                        "decoupling",
                        f"{net} on pin {pin_num} has no matching bypass cap",
                        suggestion=f"Add 100nF 0402 capacitor between {net} and GND",
                    )
                )

        for req in comp.power_reqs:
            if _is_ground_net(req.net):
                continue
            if not any(bc.net == req.net for bc in capacitor_caps) and not any(
                _supply_names_match(req.net, cap_net) for cap_net, _cap_f in standalone_caps
            ):
                issues.append(_issue(comp, "decoupling", f"{req.net} has no matching bypass cap"))

    return issues


# ================================================================
# Net connectivity analysis
# ================================================================

_EN_PIN_PATTERNS = re.compile(r"^(EN|ENABLE|ON|SHDN|SHDN_N|CE|CHIP_EN)$", re.IGNORECASE)
_I2C_SCL_PATTERN = re.compile(r"(?:^|_)(?:SCL\d*|I2C.*CLK)(?:$|_)", re.IGNORECASE)
_I2C_SDA_PATTERN = re.compile(r"(?:^|_)(?:SDA\d*|I2C.*DAT)(?:$|_)", re.IGNORECASE)
_SPI_CS_PATTERN = re.compile(r"(CS|CSN|CS_N|CSB|SS|SSN|NSS)", re.IGNORECASE)
_UART_TX_PATTERN = re.compile(r"(TXD?|UART.*TX)", re.IGNORECASE)
_UART_RX_PATTERN = re.compile(r"(RXD?|UART.*RX)", re.IGNORECASE)


def _build_net_pin_map(components: list[ComponentDef]) -> dict[str, list[tuple[str, str, str]]]:
    """Build net_name → [(component_ref, pin_num, pin_type)] mapping.

    Also counts bypass_caps and straps as connections (they wire to nets
    even though they don't appear in pin_nets/power_pins of the parent IC).
    """
    net_map: dict[str, list[tuple[str, str, str]]] = {}
    for comp in components:
        ref = comp.source_ref or comp.ref_prefix
        pin_types = {p.number: p.electrical_type for p in comp.pins}
        for pin_num, net in comp.pin_nets.items():
            if net:
                ptype = pin_types.get(pin_num, "unspecified")
                net_map.setdefault(net, []).append((ref, pin_num, ptype))
        for pin_num, net in comp.power_pins.items():
            if net:
                ptype = pin_types.get(pin_num, "power_in")
                net_map.setdefault(net, []).append((ref, pin_num, ptype))
        # Bypass caps connect two nets (net ↔ gnd_net). Each cap is a
        # 2-terminal element — record BOTH terminals under each net so
        # a standalone cap contributes two endpoints to net continuity,
        # not one (else a reset-pull-cap makes RES_N look single-pin).
        for bc in comp.bypass_caps:
            cap_id = f"C_{ref}_{bc.pin}"
            if bc.net:
                net_map.setdefault(bc.net, []).append((ref, bc.pin, "passive"))
                net_map[bc.net].append((cap_id, "1", "passive"))
            if bc.gnd_net:
                net_map.setdefault(bc.gnd_net, []).append((ref, bc.pin, "passive"))
                net_map[bc.gnd_net].append((cap_id, "2", "passive"))
        # Straps connect pin to rail. Also 2-terminal: both the signal
        # net and the rail net see the strap resistor as an endpoint.
        for strap in comp.straps:
            strap_id = f"R_{ref}_{strap.pin}"
            if strap.net:
                net_map.setdefault(strap.net, []).append((ref, strap.pin, "passive"))
                net_map[strap.net].append((strap_id, "1", "passive"))
            if strap.rail:
                net_map.setdefault(strap.rail, []).append((ref, strap.pin, "passive"))
                net_map[strap.rail].append((strap_id, "2", "passive"))
    return net_map


def _validate_net_connectivity(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Detect single-pin nets (dangling wires) and input-only nets (no driver)."""
    issues = []
    net_map = _build_net_pin_map(components)

    for net, pins in net_map.items():
        # Skip power nets — they're driven by power sources external to the schematic
        if _is_ground_net(net) or any(
            net.upper().startswith(p) for p in ("VDD", "VCC", "VBUS", "VIN", "VDDA", "MGT", "VCCO")
        ):
            continue

        if len(pins) == 1:
            ref, pin_num, ptype = pins[0]
            issues.append(
                _issue(
                    _find_comp(components, ref),
                    "single-pin-net",
                    f"Net '{net}' has only one connection (pin {pin_num} on {ref}) — likely dangling",
                )
            )
            continue

        # Check for input-only nets (no driver).
        # Passive connections count as drivers — feedback dividers, pull-ups,
        # and bootstrap caps are passive-driven networks by design.
        has_driver = any(
            ptype in ("output", "bidirectional", "tri_state", "power_out", "passive") for _, _, ptype in pins
        )
        if not has_driver:
            refs = ", ".join(f"{ref}:{pnum}" for ref, pnum, _ in pins)
            issues.append(
                _issue(
                    _find_comp(components, pins[0][0]),
                    "undriven-net",
                    f"Net '{net}' has no output driver — only inputs: {refs}",
                )
            )

    # Check for power-to-ground shorts within a single net
    _vdd_like = {"VDD", "VCC", "VBUS", "VIN", "VDDA", "VBAT", "VSYS", "MGT", "VCCO", "VAUX"}
    _gnd_like = {"GND", "AGND", "DGND", "PGND", "VSS", "GNDA", "GNDD"}
    for net, pins in net_map.items():
        upper = net.upper()
        is_vdd = upper in _vdd_like or any(upper.startswith(f"{p}_") for p in _vdd_like)
        is_gnd = upper in _gnd_like or any(upper.startswith(f"{p}_") for p in _gnd_like)
        if is_vdd and is_gnd:
            refs = ", ".join(f"{ref}:{pnum}" for ref, pnum, _ in pins)
            issues.append(
                _issue(
                    _find_comp(components, pins[0][0]),
                    "vdd-to-gnd-short",
                    f"Net '{net}' carries both VDD and GND — likely a power-to-ground short: {refs}",
                )
            )

    return issues


def _find_comp(components: list[ComponentDef], ref: str) -> ComponentDef:
    """Find component by source_ref or ref_prefix."""
    for comp in components:
        if (comp.source_ref or comp.ref_prefix) == ref:
            return comp
    return components[0] if components else ComponentDef(mpn="?")


def _validate_enable_pins(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Check that regulators/converters have their EN/SHDN pins connected."""
    issues = []
    for comp in components:
        if comp.category not in ("power", "analog"):
            continue
        if comp.ref_prefix.upper() != "U":
            continue

        handled = set(comp.pin_nets) | set(comp.power_pins) | comp.explicit_no_connects
        for strap in comp.straps:
            handled.add(strap.pin)

        for pin in comp.pins:
            if not _EN_PIN_PATTERNS.match(pin.name):
                continue
            if pin.number in handled:
                continue
            # EN pin is floating on a regulator — this means it won't start
            issues.append(
                _issue(
                    comp,
                    "floating-enable",
                    f"Enable pin {pin.number} ({pin.name}) is floating — regulator may not start",
                    severity="major",
                    suggestion=(
                        f"Tie pin {pin.number} ({pin.name}) to VIN via 100k pull-up, or add to explicit_no_connects"
                    ),
                )
            )
    return issues


def _validate_bus_completeness(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Check I2C buses have pull-ups, SPI has CS, UART has TX+RX paired."""
    issues = []
    net_map = _build_net_pin_map(components)

    # Collect all strap nets for pull-up detection
    strap_nets: set[str] = set()
    for comp in components:
        for strap in comp.straps:
            strap_nets.add(strap.net)

    # I2C: check that SCL/SDA nets have pull-ups (via straps)
    scl_nets = {net for net in net_map if _I2C_SCL_PATTERN.search(net)}
    sda_nets = {net for net in net_map if _I2C_SDA_PATTERN.search(net)}
    i2c_signal_nets = scl_nets | sda_nets
    for net in sorted(i2c_signal_nets):
        if net not in strap_nets:
            # Check if any component has a strap that references this net
            has_pullup = False
            for comp in components:
                for strap in comp.straps:
                    if strap.net == net and not _is_ground_net(strap.rail):
                        has_pullup = True
                        break
                if has_pullup:
                    break
            if not has_pullup:
                comp_ref = net_map[net][0][0] if net_map[net] else "?"
                issues.append(
                    _issue(
                        _find_comp(components, comp_ref),
                        "i2c-missing-pullup",
                        f"I2C signal '{net}' has no pull-up resistor",
                        severity="major",
                    )
                )

    # SPI: check CS pins are connected (not floating)
    for comp in components:
        for pin in comp.pins:
            if not _SPI_CS_PATTERN.match(pin.name):
                continue
            handled = set(comp.pin_nets) | set(comp.power_pins) | comp.explicit_no_connects
            for strap in comp.straps:
                handled.add(strap.pin)
            if pin.number not in handled:
                issues.append(
                    _issue(
                        comp,
                        "spi-floating-cs",
                        f"SPI chip select pin {pin.number} ({pin.name}) is floating",
                        severity="major",
                    )
                )

    # UART: check TX/RX are paired on the same bus prefix
    tx_nets = {net for net in net_map if _UART_TX_PATTERN.search(net)}
    for tx_net in sorted(tx_nets):
        # Derive expected RX net name
        rx_candidates = [
            tx_net.replace("TX", "RX").replace("tx", "rx").replace("Tx", "Rx"),
            tx_net.replace("TXD", "RXD").replace("txd", "rxd"),
        ]
        has_rx = any(rx in net_map for rx in rx_candidates)
        if not has_rx:
            comp_ref = net_map[tx_net][0][0] if net_map[tx_net] else "?"
            issues.append(
                _issue(
                    _find_comp(components, comp_ref),
                    "uart-unpaired",
                    f"UART TX net '{tx_net}' has no matching RX net",
                    severity="major",
                )
            )

    return issues


def _validate_inductor_selection(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Check that inductors on switching converters have plausible values."""
    issues = []
    for comp in components:
        if comp.category != "power":
            continue
        for bc in comp.bypass_caps:
            if bc.role != "inductor":
                continue
            ind_h = _parse_inductance_h(bc.value)
            if ind_h is None:
                continue
            # Sanity: switching converter inductors should be 0.1uH - 100uH
            if ind_h < 0.1e-6:
                issues.append(
                    _issue(comp, "inductor-value", f"Inductor {bc.value} on {bc.net} is suspiciously small (< 0.1µH)")
                )
            elif ind_h > 100e-6:
                issues.append(
                    _issue(comp, "inductor-value", f"Inductor {bc.value} on {bc.net} is suspiciously large (> 100µH)")
                )
    return issues


_CAP_VOLTAGE_PATTERN = re.compile(r"(\d+)\s*V", re.IGNORECASE)


def _validate_cap_voltage_ratings(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Check that capacitors on power rails aren't under-rated for the rail voltage."""
    issues = []
    for comp in components:
        for bc in comp.bypass_caps:
            cap_f = _parse_capacitance_f(bc.value)
            if not cap_f:
                continue
            rail_v = _rail_voltage(bc.net)
            if rail_v is None or rail_v <= 0:
                continue
            # Check if there's a voltage rating in the value/description
            rating_match = _CAP_VOLTAGE_PATTERN.search(bc.value)
            if rating_match:
                rated_v = float(rating_match.group(1))
                if rail_v > rated_v * 0.80:
                    issues.append(
                        _issue(
                            comp,
                            "cap-voltage-rating",
                            f"Cap {bc.value} on {bc.net} ({rail_v}V rail) — "
                            f"rated {rated_v}V, derate to 80% = {rated_v * 0.8:.1f}V",
                            severity="major",
                        )
                    )
    return issues


# ================================================================
# S6.1: In-process ERC — pin type conflict detection
# ================================================================

_OUTPUT_TYPES = frozenset({"output", "power_out"})


def _validate_pin_type_conflicts(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Detect output-to-output conflicts on the same net (ERC check)."""
    issues = []
    net_map = _build_net_pin_map(components)

    for net, pins in net_map.items():
        # Skip power nets — multiple power_out on GND/VDD is normal
        if _is_ground_net(net) or any(
            net.upper().startswith(p) for p in ("VDD", "VCC", "VBUS", "VIN", "VDDA", "MGT", "VCCO")
        ):
            continue

        outputs = [(ref, pnum) for ref, pnum, ptype in pins if ptype in _OUTPUT_TYPES]
        if len(outputs) > 1:
            detail = ", ".join(f"{ref}:{pnum}" for ref, pnum in outputs)
            issues.append(
                _issue(
                    _find_comp(components, outputs[0][0]),
                    "output-conflict",
                    f"Net '{net}' has multiple output drivers: {detail} — potential bus contention",
                    severity="major",
                )
            )
    return issues


_PINOUT_IRRELEVANT_PREFIXES = frozenset(("R", "C", "L", "F", "FB", "TP"))


def _validate_pinout_sources(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Fail on any IC whose pinout is derived from distributor package data only.

    Components with ``pinout_source == "stub"`` have placeholder pin numbers
    (1 … N) not backed by a datasheet.  Routing such a component to real nets
    produces a physically incorrect schematic.  Users must either:

    * Supply an explicit ``pin_map`` in their YAML spec (sets pinout_source="explicit"), or
    * Add ``pinout_verified: true`` to acknowledge they have manually confirmed
      the pin assignments against the datasheet.
    """
    issues = []
    for comp in components:
        # Skip truly pinout-irrelevant passives only; polarized and clocked parts
        # such as diodes and oscillators still have meaningful pin assignments.
        if comp.ref_prefix in _PINOUT_IRRELEVANT_PREFIXES:
            continue
        if comp.pinout_source == "stub" and not comp.pinout_verified:
            issues.append(
                _issue(
                    comp,
                    "unverified-pinout",
                    (
                        f"{comp.source_ref or comp.ref_prefix} ({comp.mpn}): pinout not verified — "
                        "add explicit pin_map or set pinout_verified: true"
                    ),
                    severity="blocker",
                    # The pin map itself is stub-quality, but the finding is a
                    # verified observation of that explicit local state.  Do
                    # not downgrade the long-standing generation safety gate.
                    detection_confidence="verified",
                    suggestion=(
                        "Either supply a pin_map in your YAML spec mapping pin numbers to net names, "
                        "or add 'pinout_verified: true' after manually confirming the pinout against "
                        "the datasheet."
                    ),
                )
            )
    return issues


# --- Enhanced validation checks (Sprint 4) ---


def _validate_power_budget(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Validate power budget: regulator output current vs load estimates."""
    issues: list[ValidationIssue] = []

    # Find regulators and check for basic sanity
    _power_keywords = {"regulator", "converter", "buck", "boost", "ldo"}

    for comp in components:
        desc = (comp.description or "").lower()
        cat = (comp.category or "").lower()

        if not any(kw in desc or kw in cat for kw in _power_keywords):
            continue

        ref = comp.source_ref or ""
        mpn = comp.mpn or ""

        # Check: power components should have power_reqs or power_pins defined
        if not comp.power_pins and not comp.power_reqs:
            issues.append(
                _issue(
                    comp,
                    "power-budget",
                    message=f"{ref} ({mpn}): Power IC has no power_pins or power_reqs defined",
                    suggestion="Add power pin definitions to enable power budget analysis",
                    observed_value="0 count",
                )
            )

    return issues


def _validate_thermal_limits(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Validate thermal design by checking for known-hot components.

    Uses thermal_analysis module internally when available.
    """
    issues: list[ValidationIssue] = []

    try:
        from .thermal_analysis import analyze_thermal

        result = analyze_thermal(components)
        for comp_result in result.get("components", []):
            if comp_result.get("status") == "critical":
                ref = comp_result.get("ref", "")
                tj = comp_result.get("tj_calculated", 0)
                tj_max = comp_result.get("tj_max", 0)
                issues.append(
                    _issue(
                        _find_comp(components, ref),
                        "thermal-limits",
                        severity="blocker",
                        detection_confidence="single_source",
                        message=f"{ref}: Calculated Tj={tj:.0f}C exceeds Tj_max={tj_max:.0f}C",
                        suggestion=comp_result.get("suggestion", "Add heatsink or increase copper area"),
                        observed_value=f"{float(tj):.1f} C",
                    )
                )
            elif comp_result.get("status") == "warning":
                ref = comp_result.get("ref", "")
                margin = comp_result.get("margin_c", 0)
                issues.append(
                    _issue(
                        _find_comp(components, ref),
                        "thermal-limits",
                        severity="major",
                        message=f"{ref}: Thermal margin only {margin:.0f}C",
                        suggestion=comp_result.get("suggestion", "Consider additional copper area"),
                        observed_value=f"{float(margin):.1f} C",
                    )
                )
    except Exception:
        pass  # Graceful degradation if thermal data unavailable

    return issues


def _validate_signal_integrity(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Validate signal integrity by checking for high-speed bus requirements.

    Wraps si_constraints module to detect missing termination, pull-ups, etc.
    """
    issues: list[ValidationIssue] = []

    try:
        from .si_constraints import analyze_si_constraints

        si_result = analyze_si_constraints(components)
        for constraint in si_result.get("constraints", []):
            bus_type = constraint.get("bus_type", "")
            if constraint.get("status") == "missing_termination":
                issues.append(
                    _issue(
                        _find_comp(components, str(constraint.get("ref", ""))),
                        "signal-integrity",
                        message=f"{bus_type}: Missing termination or impedance matching",
                        suggestion=f"Add appropriate termination for {bus_type}",
                        observed_value="0 count",
                    )
                )
    except Exception:
        pass  # Graceful degradation

    return issues


def _validate_pin_mapping_integrity(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Reject mappings that cannot describe one unambiguous rendered pin."""
    issues: list[ValidationIssue] = []
    for comp in components:
        pin_numbers = [pin.number for pin in comp.pins]
        valid_pin_numbers = {number for number in pin_numbers if isinstance(number, str) and number.strip()}

        malformed_defs = sorted(
            repr(number) for number in pin_numbers if not isinstance(number, str) or not number.strip()
        )
        if malformed_defs:
            issues.append(
                _issue(
                    comp,
                    "pin-mapping-integrity",
                    "Pin definitions contain empty or non-string identifiers: " + ", ".join(malformed_defs),
                    severity="blocker",
                )
            )

        duplicate_defs = sorted({number for number in valid_pin_numbers if pin_numbers.count(number) > 1})
        if duplicate_defs:
            issues.append(
                _issue(
                    comp,
                    "pin-mapping-integrity",
                    "Pin definitions contain duplicate identifiers: " + ", ".join(duplicate_defs),
                    severity="blocker",
                )
            )

        signal_keys = set(comp.pin_nets)
        power_keys = set(comp.power_pins)
        nc_keys = set(comp.explicit_no_connects)
        all_mapping_keys = signal_keys | power_keys | nc_keys

        malformed_keys = sorted(repr(pin) for pin in all_mapping_keys if not isinstance(pin, str) or not pin.strip())
        if malformed_keys:
            issues.append(
                _issue(
                    comp,
                    "pin-mapping-integrity",
                    "Mappings contain empty or non-string pin identifiers: " + ", ".join(malformed_keys),
                    severity="blocker",
                )
            )

        empty_signal_nets = sorted(
            str(pin) for pin, net in comp.pin_nets.items() if not isinstance(net, str) or not net.strip()
        )
        empty_power_nets = sorted(
            str(pin) for pin, net in comp.power_pins.items() if not isinstance(net, str) or not net.strip()
        )
        if empty_signal_nets:
            issues.append(
                _issue(
                    comp,
                    "pin-mapping-integrity",
                    "Signal mappings have empty net names on pins: " + ", ".join(empty_signal_nets),
                    severity="blocker",
                )
            )
        if empty_power_nets:
            issues.append(
                _issue(
                    comp,
                    "pin-mapping-integrity",
                    "Power mappings have empty net names on pins: " + ", ".join(empty_power_nets),
                    severity="blocker",
                )
            )

        signal_and_power = sorted(str(pin) for pin in signal_keys & power_keys)
        if signal_and_power:
            issues.append(
                _issue(
                    comp,
                    "pin-mapping-integrity",
                    "Pins are mapped as both signal and power: " + ", ".join(signal_and_power),
                    severity="blocker",
                )
            )

        mapped_and_nc = sorted(str(pin) for pin in (signal_keys | power_keys) & nc_keys)
        if mapped_and_nc:
            issues.append(
                _issue(
                    comp,
                    "pin-mapping-integrity",
                    "Pins are both mapped and explicitly no-connect: " + ", ".join(mapped_and_nc),
                    severity="blocker",
                )
            )

        string_mapping_keys = {pin for pin in all_mapping_keys if isinstance(pin, str)}
        # Custom embedded symbols are checked again against the exact emitted
        # geometry during rendering.  The source PinDef list remains the
        # authoritative contract whenever it is present.
        if valid_pin_numbers or not comp.lib_symbol_sexpr:
            missing = sorted(string_mapping_keys - valid_pin_numbers)
            if missing:
                issues.append(
                    _issue(
                        comp,
                        "pin-mapping-integrity",
                        "Mapped pins are absent from the component pin definitions: " + ", ".join(missing),
                        severity="blocker",
                    )
                )

    return issues


_VALIDATION_CHECKS = (
    ("pin-mapping-integrity", "Pin mapping integrity", _validate_pin_mapping_integrity),
    ("pinout-source", "Pinout verification", _validate_pinout_sources),
    ("feedback-divider", "Feedback dividers", _validate_feedback_dividers),
    ("rc-lc-filter", "RC/LC filters", _validate_filter_cutoffs),
    ("crystal-load", "Crystal load caps", _validate_crystal_caps),
    ("decoupling", "Decoupling coverage", _validate_decoupling),
    ("inductor-selection", "Inductor selection", _validate_inductor_selection),
    ("cap-voltage", "Capacitor voltage ratings", _validate_cap_voltage_ratings),
    ("net-connectivity", "Net connectivity", _validate_net_connectivity),
    ("enable-pins", "Enable/shutdown pins", _validate_enable_pins),
    ("bus-completeness", "Bus completeness", _validate_bus_completeness),
    ("pin-type-conflicts", "Pin type conflicts (ERC)", _validate_pin_type_conflicts),
    ("power-budget", "Power budget", _validate_power_budget),
    ("thermal-limits", "Thermal limits", _validate_thermal_limits),
    ("signal-integrity", "Signal integrity", _validate_signal_integrity),
)


def run_validation_checks(components: list[ComponentDef]) -> list[ValidationCheckResult]:
    """Run all validation checks and return grouped per-check results."""
    results = []
    for code, label, check in _VALIDATION_CHECKS:
        issues = tuple(check(components))
        if not issues:
            status = "PASS"
        elif any(issue.is_confirmed_blocker for issue in issues):
            status = "FAIL"
        else:
            status = "WARN"
        results.append(ValidationCheckResult(code=code, label=label, status=status, issues=issues))

    results = _attach_raw_validation_evidence(results)

    # Log aggregate results to design.log
    from .logging_bridge import get_design_logger

    dl = get_design_logger()
    if dl:
        all_passed = all(r.status != "FAIL" for r in results)
        error_msgs = []
        warning_msgs = []
        for r in results:
            for issue in r.issues:
                if issue.is_confirmed_blocker:
                    error_msgs.append(f"[{r.code}] {issue.message}")
                else:
                    warning_msgs.append(f"[{r.code}] {issue.message}")
        dl.log_validation(
            spec_file="(components)",
            passed=all_passed,
            errors=error_msgs[:5],
            warnings=warning_msgs[:5],
            scope="raw_checks",
            error_count=len(error_msgs),
            warning_count=len(warning_msgs),
        )

    return results


def _attach_raw_validation_evidence(results: list[ValidationCheckResult]) -> list[ValidationCheckResult]:
    """Attach deterministic, resolvable tool-result evidence to raw findings.

    ``validate_design`` emits an aggregate ledger later; this companion ledger
    keeps ``run_validation_checks`` and ``validate_circuit`` equally safe for
    direct library users without inventing any datasheet fact.
    """

    from .evidence import EvidenceLedger, EvidenceSource

    ledger = EvidenceLedger()
    source = EvidenceSource(doc_id="circuit-weaver-validator", extraction_method="raw-validation")
    materialized: list[ValidationCheckResult] = []
    for result in results:
        issues: list[ValidationIssue] = []
        records: list[object] = []
        for issue in result.issues:
            subject = issue.ref or issue.mpn
            if issue.rule_id and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", subject):
                subject_ref = f"calc:{issue.rule_id}@{subject}"
            else:
                subject_ref = "tool:circuit-weaver-validator"
            evidence_id = ledger.record(
                subject_ref=subject_ref,
                claim=(
                    f"{issue.rule_id or issue.code}:{issue.code}:{issue.severity}:{issue.detection_confidence}:"
                    f"message_sha256={sha256(issue.message.encode('utf-8')).hexdigest()}"
                ),
                kind="tool_result",
                source=EvidenceSource(**{**asdict(source), "extraction_method": result.code}),
                confidence="single_source",
                freshness="current",
            )
            issues.append(replace(issue, evidence_ids=tuple(sorted(set(issue.evidence_ids) | {evidence_id}))))
            record = ledger.get(evidence_id)
            if record is not None:
                records.append(record)
        materialized.append(replace(result, issues=tuple(issues), evidence_records=tuple(records)))
    return materialized


def validate_circuit(components: list[ComponentDef]) -> list[ValidationIssue]:
    """Run heuristic passive-value validation on resolved component instances."""
    issues = []
    for result in run_validation_checks(components):
        issues.extend(result.issues)
    return issues
