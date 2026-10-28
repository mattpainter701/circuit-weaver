"""YAML project specification parser.

Parses a YAML project file into a list of ComponentDefs ready for the
schematic engine. Subcircuit templates are resolved from the registry,
standalone components from the component registry or KiCad library.

Example project.yaml:
    project: MyBoard
    company: Acme Corp
    power:
      - type: buck
        ic: AP62300
        ref: U1
        vin: 5
        vout: 3.3
        iout: 2
        vin_net: VBUS_5V
        en_net: VBUS_5V
      - type: ldo
        ic: TLV75518
        ref: U2
        vin: 3.3
        vout: 1.8
    digital:
      - ic: ESP32-WROOM-32E
        ref: U3
    connectors:
      - ic: USB-C-PWR
        ref: J1

Usage:
    from circuit_weaver.project_spec import load_project
    components = load_project("project.yaml")
    generate_from_components(components, output_dir="output/", project_name="MyBoard")
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .component_db import (
    BUILTIN_REGISTRY,
    ComponentDef,
    ComponentRegistry,
    PinDef,
    PowerPin,
    PowerReq,
)
from .kicad_lib import KiCadLibrary
from .subcircuits.base import BoundaryPort, SubcircuitRegistry, get_default_registry
from .supply_ratings import parse_supply_ratings


def _make_stub_component(name: str, category: str, ref: str = "", reason: str = "") -> ComponentDef:
    """Create a minimal stub ComponentDef for an unresolved component.

    The stub renders as a visible placeholder in the schematic with a
    warning annotation so the issue is never silently hidden.
    """
    annotation = f"UNRESOLVED: {reason}" if reason else f"UNRESOLVED: '{name}' not found"
    return ComponentDef(
        mpn=name or "UNKNOWN",
        ref_prefix=ref[:1].upper() if ref else "U",
        value=name or "UNKNOWN",
        footprint="",
        description="Unresolved component — verify manually",
        category=category,
        source_ref=ref,
        annotations=[annotation],
        pins=[
            PinDef("1", "~", "passive", "L"),
            PinDef("2", "~", "passive", "R"),
        ],
    )


def _parse_yaml(path: str | Path) -> dict:
    """Load a YAML mapping with the required full YAML parser."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - guarded by package metadata
        raise RuntimeError("PyYAML is required to parse circuit-weaver design specifications") from exc

    parsed = yaml.safe_load(text)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"Design specification must contain a top-level mapping: {path}")
    return parsed


def _simple_yaml_parse(text: str) -> dict:
    """Parse a minimal YAML subset for legacy discovery-only callers.

    Handles: top-level keys, string/number values, lists of dicts (one indent level).
    Does NOT handle: nested dicts, multi-line strings, anchors, tags.

    Design loading must use :func:`_parse_yaml`; this helper is not safe for
    generation or validation.
    """
    result: dict[str, Any] = {}
    current_key = None
    current_list: list[dict] | None = None
    current_item: dict[str, Any] | None = None

    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip())

        # Top-level key: "project: MyBoard"
        if indent == 0 and ":" in stripped:
            # Flush current list
            if current_key and current_list is not None:
                if current_item:
                    current_list.append(current_item)
                result[current_key] = current_list

            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                result[key] = _coerce_value(val)
                current_key = None
                current_list = None
                current_item = None
            else:
                current_key = key
                current_list = []
                current_item = None
            continue

        # List item start: "  - type: buck"
        if stripped.startswith("- ") and current_list is not None:
            if current_item:
                current_list.append(current_item)
            current_item = {}
            item_content = stripped[2:].strip()
            if ":" in item_content:
                k, _, v = item_content.partition(":")
                current_item[k.strip()] = _coerce_value(v.strip())
            continue

        # List item continuation: "    vin: 5"
        if indent >= 4 and current_item is not None and ":" in stripped:
            k, _, v = stripped.partition(":")
            current_item[k.strip()] = _coerce_value(v.strip())
            continue

    # Flush final list
    if current_key and current_list is not None:
        if current_item:
            current_list.append(current_item)
        result[current_key] = current_list

    return result


def _coerce_value(val: str) -> Any:
    """Coerce a YAML value string to int, float, bool, or str."""
    if not val:
        return ""
    low = val.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low == "null" or low == "~":
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    # Strip quotes
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    return val


# ================================================================
# Section categories -> sheet categories
# ================================================================

_SECTION_CATEGORY_MAP = {
    "power": "power",
    "power_distribution": "power",
    "digital": "mcu",
    "mcu": "mcu",
    "fpga": "fpga",
    "rf": "rf",
    "rf_frontend": "rf",
    "transceiver": "transceiver",
    "clock": "clock",
    "usb": "usb",
    "ethernet": "ethernet",
    "connectors": "connector",
    "sensors": "sensor",
    "storage": "storage",
    "debug": "debug",
    "communication": "communication",
    "analog": "analog",
    "discrete": "misc",
    "motor": "power",
    "motor_control": "power",
    "protection": "misc",
    "audio": "analog",
    "display": "mcu",
    "misc": "misc",
}


# ================================================================
# Net name handling for KiCad-imported components
# ================================================================

# Default power pin name → project rail name mapping
_DEFAULT_POWER_MAP = {
    "VCC": "VDD_3P3",
    "VDD": "VDD_3P3",
    "VIN": "VIN",
    "VBUS": "VBUS_5V",
    "V+": "VDD_3P3",
    "V-": "GND",
    "GND": "GND",
    "VSS": "GND",
    "AGND": "GND",
    "DGND": "GND",
    "GNDA": "GND",
    "GNDD": "GND",
    "VSSA": "GND",
}

_POWER_NET_PREFIXES = ("GND", "AGND", "DGND", "PGND", "VDD", "VCC", "VBUS", "VIN", "VDDA", "MGT", "VCCO")

# Signal pin names that should remain global (shared across instances)
_GLOBAL_SIGNAL_NAMES = frozenset(
    {
        "SDA",
        "SCL",
        "MOSI",
        "MISO",
        "SCLK",
        "SCK",
        "COPI",
        "CIPO",
        "TX",
        "RX",
        "TXD",
        "RXD",
        "CTS",
        "RTS",
        "D+",
        "D-",
        "USB_DP",
        "USB_DM",
        "CAN_H",
        "CAN_L",
        "CANH",
        "CANL",
        "SWDIO",
        "SWCLK",
        "SWO",
        "NRST",
        "RESET",
        "RESET_N",
    }
)

# Short/generic pin names that must be prefixed with ref to avoid collisions
_GENERIC_PIN_NAMES = frozenset(
    {
        "A",
        "B",
        "C",
        "D",
        "E",
        "G",
        "S",
        "IN",
        "OUT",
        "IN+",
        "IN-",
        "OUT+",
        "OUT-",
        "FB",
        "EN",
        "SW",
        "BST",
        "PG",
        "SS",
        "COMP",
        "RT",
        "SYNC",
        "NC",
        "~",
    }
)


def _apply_power_map(item: dict, comp: ComponentDef) -> None:
    """Remap KiCad-imported power pin net names to project rail names.

    Uses an explicit ``power_map`` from the YAML item first, then falls
    back to ``_DEFAULT_POWER_MAP`` for common power pin name conventions.
    """
    if not comp.power_pins:
        return
    explicit_map = item.get("power_map") or {}
    if not isinstance(explicit_map, dict):
        explicit_map = {}
    updated = {}
    for pin_num, net in comp.power_pins.items():
        if net in explicit_map:
            updated[pin_num] = explicit_map[net]
        elif net.upper() in _DEFAULT_POWER_MAP:
            updated[pin_num] = _DEFAULT_POWER_MAP[net.upper()]
        else:
            updated[pin_num] = net
    comp.power_pins = updated
    for power_pin in comp.power_pin_defs:
        if power_pin.net in explicit_map:
            power_pin.net = str(explicit_map[power_pin.net])
        elif power_pin.net.upper() in _DEFAULT_POWER_MAP:
            power_pin.net = _DEFAULT_POWER_MAP[power_pin.net.upper()]
    for req in comp.power_reqs:
        if req.net in explicit_map:
            req.net = str(explicit_map[req.net])
        elif req.net.upper() in _DEFAULT_POWER_MAP:
            req.net = _DEFAULT_POWER_MAP[req.net.upper()]


def _apply_supply_ratings(item: dict, comp: ComponentDef) -> None:
    """Apply instance-level datasheet supply ratings for the block's own part.

    Ratings name pin numbers of one part, so they only ever apply to the
    block's primary component, never to its generated support passives.
    Malformed ratings raise here rather than quietly disabling the check.
    """
    if "supply_ratings" not in item:
        return
    comp.supply_ratings = [
        rating.to_dict()
        for rating in parse_supply_ratings(
            item.get("supply_ratings"), where=f"{comp.source_ref or comp.mpn}.supply_ratings"
        )
    ]


def _apply_power_envelopes(item: dict, comp: ComponentDef) -> None:
    """Apply explicit instance-level typed power metadata.

    This intentionally only changes fields supplied in the spec.  In
    particular, unspecified current and voltage limits remain ``None``.
    """
    raw_reqs = item.get("power_reqs")
    if "dropout_voltage" in item:
        comp.dropout_voltage = item.get("dropout_voltage")
    if "feedback_vref_voltage" in item or "feedback_vref" in item:
        comp.feedback_vref_voltage = item.get("feedback_vref_voltage", item.get("feedback_vref"))
        comp.feedback_vref_provenance = item.get("feedback_vref_provenance") or item.get("vref_provenance")
        comp.feedback_vref_evidence_id = item.get("feedback_vref_evidence_id")
        if comp.feedback_vref_voltage is not None and comp.feedback_vref_voltage <= 0:
            raise ValueError("feedback Vref must be positive")
        if comp.feedback_vref_voltage is not None and not (
            comp.feedback_vref_provenance or comp.feedback_vref_evidence_id
        ):
            raise ValueError("feedback Vref requires feedback_vref_provenance or feedback_vref_evidence_id")
    if isinstance(raw_reqs, list):
        parsed: list[PowerReq] = []
        for raw in raw_reqs:
            if not isinstance(raw, dict):
                continue
            net = str(raw.get("net", "")).strip()
            if not net:
                continue
            sequencing = raw.get("sequencing") if isinstance(raw.get("sequencing"), dict) else {}
            provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
            parsed.append(
                PowerReq(
                    net=net,
                    voltage=raw.get("voltage"),
                    max_current_ma=raw.get("max_current_ma"),
                    v_min=raw.get("v_min"),
                    v_nominal=raw.get("v_nominal"),
                    v_max=raw.get("v_max"),
                    direction=raw.get("direction"),
                    i_peak_ma=raw.get("i_peak_ma"),
                    i_steady_ma=raw.get("i_steady_ma"),
                    sequence_order=raw.get("sequence_order", sequencing.get("order")),
                    sequence_dependency=raw.get("sequence_dependency", sequencing.get("dependency")),
                    tolerance=raw.get("tolerance"),
                    evidence_id=raw.get("evidence_id", provenance.get("evidence_id")),
                )
            )
        comp.power_reqs = parsed

    raw_pins = item.get("power_pin_defs")
    if isinstance(raw_pins, list):
        parsed_pins: list[PowerPin] = []
        for raw in raw_pins:
            if not isinstance(raw, dict):
                continue
            pin, net = str(raw.get("pin", "")).strip(), str(raw.get("net", "")).strip()
            if not pin or not net:
                continue
            sequencing = raw.get("sequencing") if isinstance(raw.get("sequencing"), dict) else {}
            provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
            parsed_pins.append(
                PowerPin(
                    pin=pin,
                    net=net,
                    v_min=raw.get("v_min"),
                    v_nominal=raw.get("v_nominal"),
                    v_max=raw.get("v_max"),
                    direction=raw.get("direction"),
                    i_peak_ma=raw.get("i_peak_ma"),
                    i_steady_ma=raw.get("i_steady_ma"),
                    sequence_order=raw.get("sequence_order", sequencing.get("order")),
                    sequence_dependency=raw.get("sequence_dependency", sequencing.get("dependency")),
                    tolerance=raw.get("tolerance"),
                    evidence_id=raw.get("evidence_id", provenance.get("evidence_id")),
                )
            )
        comp.power_pin_defs = parsed_pins


def _apply_net_prefix(item: dict, comp: ComponentDef) -> None:
    """Prefix generic signal pin names with the instance ref to avoid collisions.

    KiCad symbols use short names like ``G``, ``S``, ``D`` that would
    create global nets shared between all instances of the same symbol.
    This prefixes those names with the component's ref designator while
    leaving actual bus signals (SDA, SCL, TX, RX, ...) as global nets.
    """
    ref = comp.source_ref or ""
    if not ref or not comp.pin_nets:
        return
    updated = {}
    for pin_num, net in comp.pin_nets.items():
        if net in _GLOBAL_SIGNAL_NAMES or _is_power_net_name(net):
            updated[pin_num] = net
        elif net.upper() in _GENERIC_PIN_NAMES or len(net) <= 3:
            updated[pin_num] = f"{ref}_{net}"
        else:
            updated[pin_num] = net
    comp.pin_nets = updated


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Return a boolean from YAML-ish input while tolerating strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1", "on"):
            return True
        if lowered in ("false", "no", "0", "off"):
            return False
    if value is None:
        return default
    return bool(value)


def _is_power_net_name(net_name: str) -> bool:
    """Return True when *net_name* should be treated as a power rail."""
    normalized = str(net_name or "").strip().upper()
    return any(normalized == prefix or normalized.startswith(f"{prefix}_") for prefix in _POWER_NET_PREFIXES)


def _pin_sort_key(pin_number: str) -> tuple[int, int, str]:
    """Sort pin identifiers numerically when possible, then lexically."""
    raw = str(pin_number or "").strip()
    match = re.fullmatch(r"(\d+)", raw)
    if match:
        return (0, int(match.group(1)), "")
    alpha_num = re.fullmatch(r"([A-Za-z]+)(\d+)", raw)
    if alpha_num:
        prefix, suffix = alpha_num.groups()
        return (1, int(suffix), prefix.upper())
    return (2, 0, raw.upper())


def _apply_partial_pin_overrides(item: dict, comp: ComponentDef) -> None:
    """Sprint 41 — surgical overrides on top of registry defaults.

    Three YAML keys are honored, all optional and all merged onto
    whatever the registry / resolver produced:

    * ``no_connects``: list of pin numbers to mark as intentional
      no-connects. These pins are removed from ``pin_nets`` /
      ``power_pins`` (if present) and added to ``explicit_no_connects``
      so the validator + generator treat them as silent NC.
    * ``pin_nets_extra``: ``{pin_num: net_name}`` mapping merged into
      the component's existing ``pin_nets``. Lets users rewire the MCU
      side of a bus (e.g. ``{"16": "I2C_SDA", "17": "I2C_SCL"}`` on an
      ESP32) without re-declaring every default pin via the
      heavier-handed ``pin_map``.
    * ``power_pins_extra``: ``{pin_num: net_name}`` similarly extends
      existing ``power_pins``.

    These are additive; they do not reset the registry defaults the way
    ``pin_map`` does. Conflicting pin numbers in ``pin_nets_extra`` take
    precedence over registry defaults.
    """
    # Track which nets are removed from the component so any template-
    # declared boundary port for that net can be retired too.
    dropped_nets: set[str] = set()
    added_nets: set[str] = set()

    # Intentional no-connects
    raw_ncs = item.get("no_connects") or []
    if isinstance(raw_ncs, (list, tuple, set)):
        for raw in raw_ncs:
            pin_num = str(raw).strip()
            if not pin_num:
                continue
            old_signal = comp.pin_nets.pop(pin_num, None)
            old_power = comp.power_pins.pop(pin_num, None)
            if old_signal:
                dropped_nets.add(old_signal)
            if old_power:
                dropped_nets.add(old_power)
            comp.explicit_no_connects.add(pin_num)

    # Extra signal pins (merged, not replacing). A pin that was
    # previously assigned to power_pins is cleared first so the two
    # dicts never hold the same pin.
    raw_extra = item.get("pin_nets_extra") or {}
    if isinstance(raw_extra, dict):
        for pin_num, net_name in raw_extra.items():
            p = str(pin_num).strip()
            n = str(net_name).strip()
            if not p or not n:
                continue
            old_signal = comp.pin_nets.get(p)
            old_power = comp.power_pins.pop(p, None)
            if old_signal and old_signal != n:
                dropped_nets.add(old_signal)
            if old_power and old_power != n:
                dropped_nets.add(old_power)
            comp.pin_nets[p] = n
            added_nets.add(n)
            comp.explicit_no_connects.discard(p)

    # Extra power pins (merged, not replacing). Pulls the pin off
    # ``pin_nets`` so a single pin is never double-assigned.
    raw_power_extra = item.get("power_pins_extra") or {}
    original_power_pins = dict(comp.power_pins)
    explicit_power_overrides: dict[str, str] = {}
    if isinstance(raw_power_extra, dict):
        for pin_num, net_name in raw_power_extra.items():
            p = str(pin_num).strip()
            n = str(net_name).strip()
            if not p or not n:
                continue
            old_signal = comp.pin_nets.pop(p, None)
            old_power = comp.power_pins.get(p)
            if old_signal and old_signal != n:
                dropped_nets.add(old_signal)
            if old_power and old_power != n:
                dropped_nets.add(old_power)
            comp.power_pins[p] = n
            explicit_power_overrides[p] = n
            added_nets.add(n)
            comp.explicit_no_connects.discard(p)

    # Pin-addressed bypass recommendations follow their supply pin when a
    # design explicitly remaps that pin (for example Nordic VDDH routed to
    # the regulated 1.8 V rail).  Internal DEC pins remain untouched because
    # they are not keys in this pin override map.
    for cap in comp.bypass_caps:
        replacement = explicit_power_overrides.get(str(cap.pin).strip())
        if replacement:
            cap.net = replacement

    # A component-level rail requirement follows the user's remap only when
    # every pin assigned to that rail is moved together to the same net.
    # Partial or split-rail overrides keep the original requirement intact.
    for requirement in comp.power_reqs:
        supply_pins = {
            pin_num for pin_num, net_name in original_power_pins.items()
            if net_name == requirement.net
        }
        remapped_nets = {
            explicit_power_overrides[pin_num]
            for pin_num in supply_pins
            if pin_num in explicit_power_overrides
        }
        if supply_pins and supply_pins.issubset(explicit_power_overrides) and len(remapped_nets) == 1:
            requirement.net = next(iter(remapped_nets))
            rail_voltage_match = re.search(r"(?:^|_)(\d+)P(\d+)(?:$|_)", requirement.net.upper())
            if rail_voltage_match:
                nominal_voltage = float(f"{rail_voltage_match.group(1)}.{rail_voltage_match.group(2)}")
                requirement.voltage = nominal_voltage
                requirement.v_nominal = nominal_voltage

    # Retire stale template boundary ports so placement_readiness's
    # orphan-interface check doesn't flag a net that the user
    # legitimately rewired. Only drop ports whose net was removed AND
    # not replaced — if the same name was reassigned the port is still
    # valid.
    still_dropped = dropped_nets - added_nets
    if still_dropped and comp.template_boundary_ports:
        comp.template_boundary_ports = [port for port in comp.template_boundary_ports if port.name not in still_dropped]

    # Added nets become declared interfaces so
    # ``_validate_shared_net_interfaces`` doesn't complain about the
    # rewired signals. Direction is a best-guess from the original pin's
    # electrical type, defaulting to bidirectional for unknowns.
    if added_nets:
        existing_port_names = {port.name for port in comp.template_boundary_ports}
        pin_type_by_num = {pin.number: pin.electrical_type for pin in comp.pins}
        for pin_num, net_name in {**comp.pin_nets, **comp.power_pins}.items():
            if net_name not in added_nets or net_name in existing_port_names:
                continue
            etype = pin_type_by_num.get(pin_num, "bidirectional")
            if etype == "output":
                direction = "output"
            elif etype == "input":
                direction = "input"
            elif etype in ("power_in", "power_out"):
                direction = "passive"
            else:
                direction = "bidirectional"
            comp.template_boundary_ports.append(BoundaryPort(net_name, direction))
            existing_port_names.add(net_name)


def _apply_pinout_overrides(item: dict, comp: ComponentDef) -> None:
    """Apply YAML-level pinout overrides for a standalone component instance."""
    if "pinout_verified" in item:
        comp.pinout_verified = _coerce_bool(item.get("pinout_verified"), comp.pinout_verified)

    raw_pin_map = item.get("pin_map") or {}
    if not isinstance(raw_pin_map, dict) or not raw_pin_map:
        return

    normalized_pin_map = {
        str(pin_num).strip(): str(net_name).strip()
        for pin_num, net_name in raw_pin_map.items()
        if str(pin_num).strip() and str(net_name).strip()
    }
    if not normalized_pin_map:
        return

    previous_nets = set(comp.pin_nets.values()) | set(comp.power_pins.values())
    ordered_pin_numbers = sorted(normalized_pin_map, key=_pin_sort_key)
    existing_by_number = {str(pin.number).strip(): copy.deepcopy(pin) for pin in comp.pins if str(pin.number).strip()}

    # Distributor stubs only carry two placeholder "~" pins; replace those
    # with a deterministic generic symbol derived from the explicit YAML map.
    placeholder_only = bool(existing_by_number) and all((pin.name or "~") == "~" for pin in existing_by_number.values())
    if placeholder_only or not existing_by_number:
        split_index = max(1, (len(ordered_pin_numbers) + 1) // 2)
        rebuilt_pins: list[PinDef] = []
        for idx, pin_num in enumerate(ordered_pin_numbers):
            existing = existing_by_number.get(pin_num)
            if existing and existing.name != "~":
                pin = existing
            else:
                pin = PinDef(
                    pin_num,
                    f"PIN{pin_num}",
                    "power_in" if _is_power_net_name(normalized_pin_map[pin_num]) else "passive",
                    "L" if idx < split_index else "R",
                )
            rebuilt_pins.append(pin)
        comp.pins = rebuilt_pins
    else:
        missing = [pin_num for pin_num in ordered_pin_numbers if pin_num not in existing_by_number]
        if missing:
            split_index = max(1, (len(ordered_pin_numbers) + 1) // 2)
            for pin_num in missing:
                idx = ordered_pin_numbers.index(pin_num)
                comp.pins.append(
                    PinDef(
                        pin_num,
                        f"PIN{pin_num}",
                        "power_in" if _is_power_net_name(normalized_pin_map[pin_num]) else "passive",
                        "L" if idx < split_index else "R",
                    )
                )

    comp.pinout_source = "explicit"
    comp.pin_nets = {}
    comp.power_pins = {}
    for pin_num in ordered_pin_numbers:
        net_name = normalized_pin_map[pin_num]
        if _is_power_net_name(net_name):
            comp.power_pins[pin_num] = net_name
        else:
            comp.pin_nets[pin_num] = net_name

    new_nets = set(comp.pin_nets.values()) | set(comp.power_pins.values())
    dropped_nets = previous_nets - new_nets
    added_nets = new_nets - previous_nets

    if dropped_nets and comp.template_boundary_ports:
        comp.template_boundary_ports = [port for port in comp.template_boundary_ports if port.name not in dropped_nets]

    if added_nets:
        existing_port_names = {port.name for port in comp.template_boundary_ports}
        pin_type_by_num = {pin.number: pin.electrical_type for pin in comp.pins}
        for pin_num, net_name in {**comp.pin_nets, **comp.power_pins}.items():
            if net_name not in added_nets or net_name in existing_port_names:
                continue
            etype = pin_type_by_num.get(pin_num, "bidirectional")
            if etype == "output":
                direction = "output"
            elif etype == "input":
                direction = "input"
            elif etype in ("power_in", "power_out"):
                direction = "passive"
            else:
                direction = "bidirectional"
            comp.template_boundary_ports.append(BoundaryPort(net_name, direction))
            existing_port_names.add(net_name)


def _try_easyeda_resolve(item: dict, ic_name: str, parts_lookup=None) -> ComponentDef | None:
    """Attempt to resolve a component via EasyEDA/LCSC (4th-tier fallback).

    Tries two paths:
    1. Explicit ``lcsc:`` key in the YAML item → fetch directly by LCSC ID
    2. MPN lookup via parts_lookup → get LCSC code → fetch from EasyEDA
    """
    from .easyeda_api import fetch_easyeda_component
    from .easyeda_parser import easyeda_to_component_def

    # Path 1: Explicit LCSC part number in YAML
    lcsc_id = str(item.get("lcsc", "")).strip()

    # Path 2: Look up MPN → LCSC code via parts_lookup
    if not lcsc_id and parts_lookup:
        lookup_mpn = str(item.get("mpn") or ic_name).strip()
        if lookup_mpn:
            try:
                data = parts_lookup.lookup(lookup_mpn)
                if data:
                    lcsc_id = data.get("lcsc", "")
            except Exception:
                pass

    if not lcsc_id:
        return None

    try:
        ee_data = fetch_easyeda_component(lcsc_id)
    except Exception as exc:
        print(f"  WARNING: EasyEDA fetch failed for {lcsc_id}: {exc}")
        return None

    if not ee_data:
        return None

    comp = easyeda_to_component_def(ee_data)
    if comp:
        comp.lcsc_pn = lcsc_id
        print(f"  -> Resolved '{ic_name}' from EasyEDA ({lcsc_id}): {len(comp.pins)} pins")
    return comp


def _resolve_component(
    item: dict,
    section_category: str,
    subcircuit_reg: SubcircuitRegistry,
    component_reg: ComponentRegistry,
    kicad_lib: KiCadLibrary | None,
    parts_lookup=None,
) -> list[ComponentDef]:
    """Resolve a single project spec item into ComponentDef(s).

    If the item has a 'type' key, it's a subcircuit template.
    Otherwise it's a standalone component resolved from registries.
    """
    template_type = item.get("type")

    ref = item.get("ref", "")

    if template_type == "component":
        # Compatibility: some external design specs use ``type: component``
        # to mean "resolve this as a standalone part via the normal resolver
        # chain", not "instantiate the narrow data-driven component topology".
        template_type = ""

    if template_type:
        template = subcircuit_reg.get(template_type)
        if template is None:
            print(f"  WARNING: Unknown subcircuit type '{template_type}', creating stub")
            return [
                _make_stub_component(
                    template_type,
                    section_category,
                    ref,
                    reason=f"Unknown subcircuit type '{template_type}'",
                )
            ]

        errors = template.validate_params(item)
        if errors:
            for err in errors:
                print(f"  ERROR [{ref or '?'}]: {err}")
            return [
                _make_stub_component(
                    item.get("ic", template_type),
                    section_category,
                    ref,
                    reason=f"Template validation failed: {'; '.join(errors)}",
                )
            ]

        result = template.generate(item)
        if result.components:
            primary = result.components[0]
            primary.template_annotations.extend(result.annotations)
            primary.template_boundary_ports.extend(copy.deepcopy(result.boundary_ports))
            primary.template_local_wires.extend(copy.deepcopy(result.local_wires))
            _apply_pinout_overrides(item, primary)
            # Sprint 41 — apply surgical pin overrides (pin_nets_extra,
            # power_pins_extra, no_connects) to the template's primary
            # component. Template-emitted ICs often have instance-
            # scoped default net names ("USB_DP_U2") that need to be
            # rewired to the project-global bus.
            _apply_partial_pin_overrides(item, primary)
        for result_index, comp in enumerate(result.components):
            # A template block ref identifies its first schematic component.
            # Multi-part topologies must let the assembly manifest allocate
            # distinct refs for the remaining physical components.
            if ref and result_index == 0:
                comp.source_ref = ref
            if result_index == 0:
                _apply_supply_ratings(item, comp)
            comp.category = section_category
            _apply_power_envelopes(item, comp)
            _apply_power_map(item, comp)
            _maybe_enrich_component(item, comp, parts_lookup)
        return result.components

    # Standalone component — resolve from registries
    ic = item.get("ic", "")
    if not ic:
        # Try passive inference: {value: "10k", ref: "R1"}
        value = str(item.get("value", "")).strip()
        if value and ref:
            from .component_db import infer_passive_component

            passive = infer_passive_component(ref, value, str(item.get("footprint", "")))
            if passive:
                passive.source_ref = ref
                passive.category = section_category
                _apply_partial_pin_overrides(item, passive)
                return [passive]
        print(f"  WARNING: Item in '{section_category}' has no 'type' or 'ic', creating stub")
        return [_make_stub_component("", section_category, ref, reason="No 'type' or 'ic' specified")]

    explicit_lcsc = bool(str(item.get("lcsc", "")).strip())
    comp = None
    source = ""

    # An explicit lcsc: key is an intentional override: prefer the EasyEDA
    # symbol even if earlier tiers could resolve the MPN by name.
    if explicit_lcsc:
        comp = _try_easyeda_resolve(item, ic, parts_lookup)
        if comp:
            source = "easyeda"

    # Main resolution chain: registry → ic_data → KiCad lib → cache →
    # EasyEDA → DigiKey → Mouser. Delegated to SymbolResolver so every
    # caller uses the same 7-tier fallback.
    if not comp:
        from .symbol_resolver import SymbolResolver

        resolver = SymbolResolver(
            component_reg=component_reg,
            kicad_lib=kicad_lib,
        )
        comp, source = resolver.resolve(ic, item=item, category=section_category)

    # Last chance: EasyEDA-by-MPN path that consults parts_lookup for the
    # LCSC mapping (SymbolResolver's easyeda tier uses a direct LCSC search
    # but does not see the caller's parts_lookup, so this remains as a
    # complementary path for YAMLs that provide an explicit lcsc elsewhere).
    if not comp and not explicit_lcsc:
        comp = _try_easyeda_resolve(item, ic, parts_lookup)
        if comp:
            source = "easyeda"

    if not comp:
        print(
            f"  WARNING: Unknown component '{ic}', not in registry, ic_data, "
            f"KiCad library, cache, EasyEDA, DigiKey, or Mouser — creating stub"
        )
        return [
            _make_stub_component(
                ic,
                section_category,
                ref,
                reason=f"'{ic}' unresolved through all 7 tiers (registry/ic_data/kicad/cache/easyeda/digikey/mouser)",
            )
        ]

    if source and source not in ("registry", ""):
        print(f"  -> Resolved '{ic}' via {source} ({len(comp.pins)} pins)")

    instance = copy.deepcopy(comp)
    instance.category = section_category
    if ref:
        instance.source_ref = ref
    if item.get("value"):
        instance.value = item["value"]
    if item.get("description"):
        instance.description = item["description"]
    if item.get("mpn"):
        instance.source_mpn = str(item["mpn"])
    _apply_pinout_overrides(item, instance)
    _apply_partial_pin_overrides(item, instance)
    _apply_power_envelopes(item, instance)
    _apply_supply_ratings(item, instance)
    _apply_power_map(item, instance)
    _apply_net_prefix(item, instance)
    _maybe_enrich_component(item, instance, parts_lookup)
    return [instance]


def _maybe_enrich_component(item: dict, comp: ComponentDef, parts_lookup) -> None:
    """Optionally fill distributor metadata on a resolved component instance."""
    if parts_lookup is None:
        return

    lookup_mpn = str(item.get("mpn") or comp.source_mpn or comp.mpn or "").strip()
    if not lookup_mpn:
        return

    if not comp.source_mpn:
        comp.source_mpn = lookup_mpn

    try:
        data = parts_lookup.lookup(lookup_mpn)
    except Exception as exc:
        print(f"  WARNING: Part lookup failed for '{lookup_mpn}': {exc}")
        return

    if not data:
        return

    from .parts_lookup import enrich_component

    enrich_component(comp, data)


def resolve_project_spec(
    spec: dict,
    subcircuit_reg: SubcircuitRegistry | None = None,
    component_reg: ComponentRegistry | None = None,
    kicad_lib: KiCadLibrary | None = None,
    parts_lookup=None,
    enrich_parts: bool = False,
    use_kicad: bool = True,
) -> tuple[list[ComponentDef], dict]:
    """Resolve a parsed project spec dict into component instances + metadata."""
    if subcircuit_reg is None:
        subcircuit_reg = get_default_registry()
    if component_reg is None:
        component_reg = BUILTIN_REGISTRY
    # Load project-local component database (JSON) if specified or auto-detected
    # A design owns its verified parts in ``components.json`` next to the
    # design spec.  The explicit ``components_db`` field remains available
    # for shared registries, but projects should not need a global install or
    # machine-specific KiCad setup to resolve approved components.
    components_db = spec.get("components_db", "")
    source_path = spec.get("_source_path")
    if not components_db and source_path:
        sibling_registry = Path(source_path).with_name("components.json")
        if sibling_registry.is_file():
            components_db = str(sibling_registry)
    if components_db:
        from pathlib import Path as _P

        p = _P(str(components_db))
        if p.is_file():
            n = component_reg.load_json(str(p))
            print(f"  Loaded {n} components from {p}")
        elif p.is_dir():
            n = component_reg.load_json_dir(str(p))
            print(f"  Loaded {n} components from {p}/")
    if kicad_lib is None and use_kicad:
        kicad_lib = KiCadLibrary()
    if enrich_parts and parts_lookup is None:
        from .parts_lookup import PartsLookup

        parts_lookup = PartsLookup()

    project_name = spec.get("project", "project")
    company = spec.get("company", "")

    metadata = {
        "project": project_name,
        "company": company,
        "version": spec.get("version", ""),
        "description": spec.get("description", ""),
    }

    components: list[ComponentDef] = []

    for section_key, items in spec.items():
        if section_key in (
            "project",
            "company",
            "version",
            "description",
            "spec_version",
            "presentation_profile",
            "components_db",
            "power_domains",
        ):
            continue

        if not isinstance(items, list):
            continue

        category = _SECTION_CATEGORY_MAP.get(section_key, section_key)
        for item in items:
            if not isinstance(item, dict):
                continue
            resolved = _resolve_component(
                item,
                category,
                subcircuit_reg,
                component_reg,
                kicad_lib,
                parts_lookup=parts_lookup,
            )
            components.extend(resolved)

    return components, metadata


def load_project(
    yaml_path: str | Path,
    subcircuit_reg: SubcircuitRegistry | None = None,
    component_reg: ComponentRegistry | None = None,
    kicad_lib: KiCadLibrary | None = None,
    parts_lookup=None,
    enrich_parts: bool = False,
) -> tuple[list[ComponentDef], dict]:
    """Load a YAML project spec and resolve all components.

    Returns (components, metadata) where metadata has project name, company, etc.
    """
    spec = _parse_yaml(yaml_path)
    spec["_source_path"] = str(Path(yaml_path).resolve())
    components, metadata = resolve_project_spec(
        spec,
        subcircuit_reg=subcircuit_reg,
        component_reg=component_reg,
        kicad_lib=kicad_lib,
        parts_lookup=parts_lookup,
        enrich_parts=enrich_parts,
    )
    metadata["spec_path"] = str(yaml_path)

    print(f"Project '{metadata['project']}': {len(components)} component(s) from {yaml_path}")
    return components, metadata


def update_spec_with_sourced_data(spec_path: Path | str, sourced_data: dict[str, Any]) -> None:
    """Update a YAML spec with auto-discovered MPN/LCSC data.

    Fills in blank MPN and LCSC fields for components based on sourced_data dict.
    Only updates blank fields — does not overwrite existing values.

    Args:
        spec_path: Path to the YAML spec file.
        sourced_data: Dict mapping MPN → {mpn, lcsc_pn, digikey_pn, ...} from auto-source.
    """
    spec_path = Path(spec_path)
    spec = _parse_yaml(spec_path)

    # Track what we updated
    updated_count = 0

    # Iterate through all categories and components
    for category in spec:
        if not isinstance(spec[category], list):
            continue
        for comp in spec[category]:
            if not isinstance(comp, dict):
                continue

            # Get the component's MPN (from ic, mpn, or value field)
            comp_mpn = comp.get("ic") or comp.get("mpn") or comp.get("value", "")
            if not comp_mpn:
                continue

            # Look up sourced data by MPN
            if comp_mpn not in sourced_data:
                continue

            sourced = sourced_data[comp_mpn]

            # Fill in blank LCSC field
            if not comp.get("lcsc") and sourced.get("lcsc_pn"):
                comp["lcsc"] = sourced["lcsc_pn"]
                updated_count += 1

            # Fill in blank MPN field (if source field missing)
            if not comp.get("mpn") and sourced.get("mpn"):
                comp["mpn"] = sourced["mpn"]

            # Fill in blank DigiKey field if available
            if not comp.get("digikey") and sourced.get("digikey_pn"):
                comp["digikey"] = sourced["digikey_pn"]

    # Write updated spec back to file
    import sys

    try:
        import yaml

        yaml_text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=False)
    except ImportError:
        # Fallback: JSON dump if YAML unavailable
        import json

        yaml_text = json.dumps(spec, indent=2) + "\n"

    spec_path.write_text(yaml_text, encoding="utf-8", newline="")
    print(f"Updated {updated_count} component(s) in {spec_path}", file=sys.stderr)
