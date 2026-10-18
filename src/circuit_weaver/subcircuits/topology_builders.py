"""Data-driven topology builders.

Each builder takes IC data (from JSON) and design parameters, then produces
a SubcircuitResult — the same output as legacy template classes, but without
any hardcoded IC knowledge. New ICs are added by writing JSON, not Python.

Builder functions are keyed by topology name in TOPOLOGY_BUILDERS dict.
"""

from __future__ import annotations

import re
from typing import Any

from .. import calc
from ..component_db import (
    BypassCap,
    ComponentDef,
    PinDef,
    StrapConfig,
    emit_and_retain_passive_synthesis,
    infer_pin_roles_from_pins,
    normalize_pin_role_name,
    normalize_pin_roles,
)
from ..evidence import EvidenceLedger, EvidenceSource
from .base import (
    FP_0402C,
    FP_0402R,
    GROUND_NET_PREFIXES,
    POWER_NET_PREFIXES,
    BoundaryPort,
    SubcircuitResult,
    cap_footprint,
    crystal_load_caps,
    format_capacitance,
    format_inductance,
    format_resistance,
    ind_footprint,
    res_footprint,
    snap_cap,
    snap_to_e24,
    snap_to_e96,
)

# T228 — pin names that should never be routed to a synthesized signal net.
# Mirrors generator._NC_PIN_NAME_PATTERNS so unmapped pins like "~", "NC",
# "DNC", "RESERVED" become explicit_no_connects instead of phantom
# per-instance nets like "~_U1" / "RESERVED_U4".
_NC_PIN_NAME_RE = re.compile(
    r"^(~|NC|DNC|N\.?C\.?|NO.?CONNECT|RESERVED)$",
    re.IGNORECASE,
)

# A power pin name is a documented identity, not a substring search.  The
# allowlist covers package/vendor spelling variants that are electrically the
# same rail.  In particular, signal labels such as MAINSVIN_SENSE must never
# acquire a synthesized VIN connection merely because they contain "VIN".
_POWER_PIN_EQUIVALENTS = frozenset(
    {
        "VINA",
        "VINB",
        "VIN1",
        "VIN2",
        "PVIN",
        "AVIN",
        "DVIN",
        "INPUT",
        "IN",
        "IN1",
        "IN2",
        "VI",
        "VBAT",
        "VPLUS",
        "VPOS",
        "V+",
        "AVDDH",
        "AVDDL",
        "DVDDH",
        "DVDDL",
        "DVDDIO",
        "VDDL",
        "VDDA",
    }
)


def _is_power_input_pin_name(name: str) -> bool:
    """Return whether *name* exactly identifies a positive power input pin."""

    normalized = str(name or "").strip().upper()
    if normalized in _POWER_PIN_EQUIVALENTS:
        return True
    return any(normalized == prefix or normalized.startswith(f"{prefix}_") for prefix in POWER_NET_PREFIXES)


_SAFE_UNMAPPED_PIN_TYPES = frozenset(
    {
        "output",
        "power_out",
        "open_collector",
        "open_emitter",
        "free",
        "no_connect",
    }
)


def _pins_from_data(ic_data: dict) -> list[PinDef]:
    """Convert JSON pin list to PinDef objects."""
    return [PinDef(p["number"], p["name"], p["type"], p["side"]) for p in ic_data.get("pins", [])]


def _pin_role(ic_data: dict, role: str) -> str | None:
    """Look up a pin number by role name from pin_roles or pin_<role> keys."""
    roles = ic_data.get("pin_roles", {})
    if role in roles:
        return str(roles[role])
    key = f"pin_{role}"
    val = ic_data.get(key)
    return str(val) if val is not None else None


def _collect_pin_roles(ic_data: dict) -> dict[str, str]:
    """Collect canonical role -> pin mappings from ic_data legacy and new fields."""
    roles = normalize_pin_roles(ic_data.get("pin_roles", {}))
    for key, value in ic_data.items():
        role = normalize_pin_role_name(str(key))
        pin = str(value or "").strip()
        if role and pin and role not in roles:
            roles[role] = pin
    return roles


_I2C_TRISE_MAX = {
    100_000: 1000e-9,
    400_000: 300e-9,
    1_000_000: 120e-9,
}


def _calc_i2c_pullup_resistance(speed_hz: float, c_bus_f: float) -> float:
    """Calculate I2C pull-up resistance from bus speed and capacitance."""
    trise = _I2C_TRISE_MAX.get(speed_hz)
    if trise is None:
        for spd in sorted(_I2C_TRISE_MAX):
            if spd >= speed_hz:
                trise = _I2C_TRISE_MAX[spd]
                break
        if trise is None:
            trise = _I2C_TRISE_MAX[1_000_000]
    if c_bus_f <= 0:
        c_bus_f = 100e-12
    return trise / (0.8473 * c_bus_f)


# ================================================================
# Switching regulator builder (buck, boost, buck_boost)
# ================================================================


def build_switching_regulator(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build a switching regulator subcircuit, dispatching by topology."""
    topology = ic_data.get("topology", "buck")
    if topology == "boost":
        return _build_boost(ic_data, params)
    elif topology == "buck_boost":
        return _build_buck_boost(ic_data, params)
    return _build_buck(ic_data, params)


def _build_buck(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build a buck (step-down) regulator."""
    return _build_switching_core(ic_data, params, "buck")


def _build_boost(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build a boost (step-up) regulator."""
    return _build_switching_core(ic_data, params, "boost")


def _build_buck_boost(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build a buck-boost (inverting/step-up-down) regulator."""
    return _build_switching_core(ic_data, params, "buck_boost")


def _build_switching_core(
    ic_data: dict,
    params: dict[str, Any],
    topology: str,
) -> SubcircuitResult:
    """Shared core: feedback, caps, pins, and component assembly for switching regulators."""
    vin = params["vin"]
    vout = params["vout"]
    iout = params["iout"]
    ic_name = params.get("ic", ic_data.get("_mpn", "UNKNOWN"))
    ref = params.get("ref", "U")
    rail_name = params.get("rail_name") or f"VDD_{vout:.1f}V".replace(".", "P")
    vin_net = params.get("vin_net", "VIN")
    if "en_net" in params:
        en_net = params["en_net"]
    else:
        en_net = None if ic_data.get("enable_default") == "floating" else vin_net
    vout_ripple = params.get("vout_ripple", 0.020)
    fsw = params.get("fsw", ic_data.get("fsw", 600e3))

    vref = float(ic_data.get("vref", 0.8))
    r_fbb = float(params.get("r_fbb", ic_data.get("r_fbb_default", 100e3)))

    # Each actual design input gets one local source record before it is fed to
    # a calculation.  In particular, a data-driven IC value is configuration
    # evidence, not an invented datasheet citation.
    ledger = EvidenceLedger()
    calc_records: list[calc.CalculationRecord] = []

    def _input_evidence(name: str, value: float, unit: str) -> str:
        return ledger.record(
            subject_ref=f"param:{ref}.switching.{name}",
            claim=f"user-declared switching parameter {name}={value:g} {unit}",
            kind="user",
            source=EvidenceSource(extraction_method="switching-topology-parameter"),
            confidence="single_source",
            freshness="unknown",
        )

    evidence = {
        "vin_v": _input_evidence("vin", vin, "V"),
        "vout_v": _input_evidence("vout", vout, "V"),
        "output_current_a": _input_evidence("iout", iout, "A"),
        "switching_frequency_hz": _input_evidence("fsw", fsw, "Hz"),
        "ripple_ratio": _input_evidence("ripple_ratio", float(params.get("ripple_ratio", 0.3)), "ratio"),
        "output_ripple_v": _input_evidence("vout_ripple", vout_ripple, "V"),
        "r_bottom_ohm": _input_evidence("r_fbb", r_fbb, "ohm"),
    }
    vref_provenance = ic_data.get("vref_provenance")
    if vref_provenance:
        vref_evidence = ledger.record(
            subject_ref=f"param:{ref}.feedback.vref",
            claim=f"declared feedback reference voltage={vref:g} V",
            kind="datasheet",
            source=EvidenceSource(doc_id=str(vref_provenance), extraction_method="feedback-vref"),
            confidence="single_source",
            freshness="unknown",
        )
    else:
        vref_evidence = _input_evidence("vref", vref, "V")
    evidence["vref_v"] = vref_evidence

    def _emit(record: calc.CalculationRecord) -> calc.CalculationRecord:
        emitted = calc.emit_calculation_evidence(record, ledger)
        calc_records.append(emitted)
        return emitted

    try:
        # Divider legs are selected jointly.  The <=2x per-leg scale window is
        # part of the record, so a future selector cannot independently round
        # its ratio away.
        feedback_top = calc.feedback_divider_top(
            target=f"param:{ref}.feedback.rtop",
            vout_v=vout,
            vref_v=vref,
            r_bottom_ohm=r_fbb,
            evidence_ids={
                "vout_v": evidence["vout_v"],
                "vref_v": vref_evidence,
                "r_bottom_ohm": evidence["r_bottom_ohm"],
            },
        )
        feedback_bottom_decision = calc.bounded_fallback_scalar(
            target=f"param:{ref}.feedback.rbottom",
            value=r_fbb,
            minimum=1e3,
            maximum=1e6,
            unit="ohm",
            series=None,
        )
        if feedback_bottom_decision.finding is not None:
            raise ValueError(f"{feedback_bottom_decision.finding.rule_id}: feedback-bottom selection withheld")
        divider = calc.apply_ratio_preserving_divider_selection(
            feedback_top,
            feedback_bottom_decision.calculation,
            target_vout_v=vout,
            vref_v=vref,
            series="E96",
            max_scale_factor=2.0,
        )
        feedback_top = _emit(divider.top)
        feedback_bottom = _emit(divider.bottom)
        r_fbt = calc.require_selection(feedback_top).value
        r_fbb_snapped = calc.require_selection(feedback_bottom).value
        feedback_vout = _emit(
            calc.feedback_divider_vout(
                target=f"param:{ref}.feedback.vout",
                r_top_ohm=r_fbt,
                r_bottom_ohm=r_fbb_snapped,
                vref_v=vref,
                evidence_ids={
                    "r_top_ohm": feedback_top.emits_evidence,
                    "r_bottom_ohm": feedback_bottom.emits_evidence,
                    "vref_v": vref_evidence,
                },
            )
        )
        actual_vout = feedback_vout.raw_result.value

        if topology == "buck":
            inductor_raw = calc.buck_inductor(
                target=f"param:{ref}.switching.inductor",
                vin_v=vin,
                vout_v=vout,
                switching_frequency_hz=fsw,
                output_current_a=iout,
                ripple_ratio=float(params.get("ripple_ratio", 0.3)),
                evidence_ids=evidence,
            )
        elif topology == "boost":
            inductor_raw = calc.boost_inductor(
                target=f"param:{ref}.switching.inductor",
                vin_v=vin,
                vout_v=vout,
                switching_frequency_hz=fsw,
                output_current_a=iout,
                ripple_ratio=float(params.get("ripple_ratio", 0.3)),
                evidence_ids=evidence,
            )
        else:
            vin_min = float(params.get("vin_min", vin * 0.8))
            vin_min_evidence = _input_evidence("vin_min", vin_min, "V")
            inductor_raw = calc.buck_boost_inductor(
                target=f"param:{ref}.switching.inductor",
                vin_min_v=vin_min,
                vout_v=vout,
                switching_frequency_hz=fsw,
                output_current_a=iout,
                ripple_ratio=float(params.get("ripple_ratio", 0.3)),
                evidence_ids={**evidence, "vin_min_v": vin_min_evidence},
            )
        inductor = _emit(calc.apply_e_series_selection(inductor_raw, series="E24", direction="nearest"))
        l_val = calc.require_selection(inductor).value
        if topology == "buck":
            duty_cycle = vout / vin
            delta_il = (vin - vout) * duty_cycle / (fsw * l_val)
        else:
            sizing_vin = vin if topology == "boost" else vin_min
            duty_cycle = 1.0 - sizing_vin / vout
            delta_il = sizing_vin * duty_cycle / (fsw * l_val)

        output_cap_raw = calc.buck_output_cap(
            target=f"param:{ref}.switching.cout",
            ripple_current_a=abs(delta_il),
            switching_frequency_hz=fsw,
            output_ripple_v=vout_ripple,
            evidence_ids={
                "switching_frequency_hz": evidence["switching_frequency_hz"],
                "output_ripple_v": evidence["output_ripple_v"],
                "ripple_current_a": _input_evidence("delta_il", abs(delta_il), "A"),
            },
        )
        output_cap = _emit(calc.apply_capacitor_selection(output_cap_raw, series="E24"))
        cout_val = calc.require_selection(output_cap).value

        def _fallback_cap(name: str, value: float, minimum: float, maximum: float) -> calc.CalculationRecord:
            decision = calc.bounded_fallback_scalar(
                target=f"param:{ref}.switching.{name}",
                value=value,
                minimum=minimum,
                maximum=maximum,
                unit="F",
                series="E24",
                direction="up",
            )
            if decision.finding is not None:
                raise ValueError(f"{decision.finding.rule_id}: {name} selection withheld")
            return _emit(decision.calculation)

        cin = _fallback_cap("cin", 22e-6 if iout > 2.0 else 10e-6, 1e-6, 47e-6)
        bootstrap = _fallback_cap("cbst", 100e-9, 10e-9, 1e-6)
        cin_val = calc.require_selection(cin).value
        cbst_val = calc.require_selection(bootstrap).value
        if topology == "buck_boost":
            cin_bulk = _fallback_cap("cin_bulk", 10e-6, 1e-6, 47e-6)
            cin_hf = _fallback_cap("cin_hf", 100e-9, 10e-9, 1e-6)
            cout_bulk = _fallback_cap("cout_bulk", 22e-6, 1e-6, 47e-6)
            cout_hf = _fallback_cap("cout_hf", 100e-9, 10e-9, 1e-6)
            cvaux = _fallback_cap("cvaux", 10e-6, 1e-6, 47e-6)
            cin_bulk_val = calc.require_selection(cin_bulk).value
            cin_hf_val = calc.require_selection(cin_hf).value
            cout_bulk_val = calc.require_selection(cout_bulk).value
            cout_hf_val = calc.require_selection(cout_hf).value
            c_vaux_val = calc.require_selection(cvaux).value
    except ValueError as exc:
        message = str(exc)
        if "CW-PSV-" not in message:
            message = f"CW-PSV-001: switching passive synthesis withheld: {message}"
        raise ValueError(message) from exc

    def _trace(record: calc.CalculationRecord) -> dict[str, object]:
        """Return the mandatory passive trace fields from an emitted record."""
        calc.require_selection(record)
        if record.emits_evidence is None:
            raise ValueError("CW-PSV-001: selected switching passive has no calculation evidence")
        return {
            "selection_policy": record.policy,
            "confidence": record.confidence,
            "calculation_id": record.id,
            "evidence_ids": (record.emits_evidence,),
        }

    # Net names
    sw_net = f"SW_{ref}"
    bst_net = f"BST_{ref}"
    fb_net = f"FB_{ref}"
    l1_net = f"L1_{ref}"
    l2_net = f"L2_{ref}"
    pg_net = f"PG_{ref}"
    vaux_net = f"VAUX_{ref}"

    # Power pins
    pin_vin = _pin_role(ic_data, "vin")
    pin_gnd = _pin_role(ic_data, "gnd")
    pin_sw = _pin_role(ic_data, "sw")
    pin_fb = _pin_role(ic_data, "fb")
    pin_en = _pin_role(ic_data, "en")
    pin_bst = _pin_role(ic_data, "bst")
    pin_l1 = _pin_role(ic_data, "l1")
    pin_l2 = _pin_role(ic_data, "l2")
    pin_pg = _pin_role(ic_data, "pg")
    pin_vaux = _pin_role(ic_data, "vaux")
    pin_ps_sync = _pin_role(ic_data, "ps_sync")

    power_pins: dict[str, str] = {}
    if pin_vin:
        power_pins[pin_vin] = vin_net
    if pin_gnd:
        power_pins[pin_gnd] = "GND"
    for extra in ic_data.get("pin_gnd_extra", []):
        power_pins[str(extra)] = "GND"

    # Auto-detect additional power pins from pin list (VIN2, VOUT2, EPAD, etc.)
    pins = _pins_from_data(ic_data)
    for pin in pins:
        if pin.number in power_pins:
            continue
        name_upper = pin.name.upper()
        if pin.electrical_type == "power_in":
            if name_upper in ("GND", "VSS", "VEE", "PGND", "SGND", "EPAD"):
                power_pins[pin.number] = "GND"
            elif name_upper in ("VIN", "VINA", "VINB", "VBAT"):
                power_pins[pin.number] = vin_net
            elif name_upper in ("VOUT", "VOUTA", "VOUTB"):
                power_pins[pin.number] = rail_name
        elif pin.electrical_type == "power_out":
            if name_upper in ("VOUT", "VOUTA", "VOUTB", "VAUX"):
                power_pins[pin.number] = rail_name

    pin_nets: dict[str, str] = {}
    if topology == "buck_boost":
        if pin_l1:
            pin_nets[pin_l1] = l1_net
        if pin_l2:
            pin_nets[pin_l2] = l2_net
        if pin_pg:
            pin_nets[pin_pg] = pg_net
        if pin_vaux:
            pin_nets[pin_vaux] = vaux_net
        if pin_ps_sync:
            pin_nets[pin_ps_sync] = "GND"
    else:
        if pin_sw:
            pin_nets[pin_sw] = sw_net
    if pin_fb:
        pin_nets[pin_fb] = fb_net
    if pin_en and en_net is not None:
        pin_nets[pin_en] = en_net
    if pin_bst and topology != "buck_boost":
        pin_nets[pin_bst] = bst_net

    # Mark any unused input/bidirectional pins as explicit no-connect
    explicit_no_connects: set[str] = set()
    for pin in pins:
        if pin.number in power_pins or pin.number in pin_nets:
            continue
        if pin.electrical_type in ("input", "bidirectional"):
            explicit_no_connects.add(pin.number)

    bypass_caps: list[BypassCap] = []
    if topology == "buck_boost":
        bypass_caps = [
            BypassCap(
                "CIN_BULK",
                vin_net,
                "GND",
                format_capacitance(cin_bulk_val),
                cap_footprint(cin_bulk_val),
                role="input_cap",
                presentation="topology_local",
                **_trace(cin_bulk),
            ),
            BypassCap(
                "CIN_HF",
                vin_net,
                "GND",
                format_capacitance(cin_hf_val),
                cap_footprint(cin_hf_val),
                role="input_cap",
                presentation="topology_local",
                **_trace(cin_hf),
            ),
            BypassCap(
                "COUT_BULK",
                rail_name,
                "GND",
                format_capacitance(cout_bulk_val),
                cap_footprint(cout_bulk_val),
                role="output_cap",
                presentation="topology_local",
                **_trace(cout_bulk),
            ),
            BypassCap(
                "COUT_HF",
                rail_name,
                "GND",
                format_capacitance(cout_hf_val),
                cap_footprint(cout_hf_val),
                role="output_cap",
                presentation="topology_local",
                **_trace(cout_hf),
            ),
            BypassCap(
                "L",
                l1_net,
                l2_net,
                format_inductance(l_val),
                ind_footprint(l_val, iout),
                role="inductor",
                presentation="topology_local",
                **_trace(inductor),
            ),
        ]
        if pin_vaux and ic_data.get("has_vaux"):
            bypass_caps.append(
                BypassCap(
                    "CVAUX",
                    vaux_net,
                    "GND",
                    format_capacitance(c_vaux_val),
                    cap_footprint(c_vaux_val),
                    role="decoupling",
                    presentation="topology_local",
                    **_trace(cvaux),
                ),
            )
    else:
        bypass_caps = [
            BypassCap(
                "CIN",
                vin_net,
                "GND",
                format_capacitance(cin_val),
                cap_footprint(cin_val),
                role="input_cap",
                presentation="topology_local",
                **_trace(cin),
            ),
            BypassCap(
                "COUT",
                rail_name,
                "GND",
                format_capacitance(cout_val),
                cap_footprint(cout_val),
                role="output_cap",
                presentation="topology_local",
                **_trace(output_cap),
            ),
            BypassCap(
                "L",
                vin_net if topology == "boost" else sw_net,
                sw_net if topology == "boost" else rail_name,
                format_inductance(l_val),
                ind_footprint(l_val, iout),
                role="inductor",
                presentation="topology_local",
                **_trace(inductor),
            ),
        ]
        if pin_bst:
            bypass_caps.append(
                BypassCap(
                    "CBST",
                    bst_net,
                    sw_net,
                    format_capacitance(cbst_val),
                    FP_0402C,
                    role="bootstrap_cap",
                    presentation="topology_local",
                    **_trace(bootstrap),
                ),
            )

    straps = [
        StrapConfig(
            "FBT",
            fb_net,
            rail_name,
            format_resistance(r_fbt),
            FP_0402R,
            role="feedback_top",
            presentation="topology_local",
            **_trace(feedback_top),
        ),
        StrapConfig(
            "FBB",
            fb_net,
            "GND",
            format_resistance(r_fbb_snapped),
            FP_0402R,
            role="feedback_bottom",
            presentation="topology_local",
            **_trace(feedback_bottom),
        ),
    ]

    annotations = [
        f"{rail_name}: {vout}V from {vin_net} at {iout}A",
        f"Vout = {vref}V * (1 + {format_resistance(r_fbt)}/{format_resistance(r_fbb_snapped)}) = {actual_vout:.3f}V",
    ]
    if topology == "buck_boost":
        annotations += [
            f"L={format_inductance(l_val)} (sized for Vin_min={params.get('vin_min', vin * 0.8)}V)",
            f"Cin={format_capacitance(cin_bulk_val)}+{format_capacitance(cin_hf_val)}, "
            f"Cout={format_capacitance(cout_bulk_val)}+{format_capacitance(cout_hf_val)}",
            f"fsw={fsw / 1e6:.1f}MHz",
        ]
    else:
        annotations += [
            f"L={format_inductance(l_val)}, Cin={format_capacitance(cin_val)}, Cout={format_capacitance(cout_val)}",
            f"fsw={fsw / 1e3:.0f}kHz, ripple={delta_il:.2f}A ({abs(delta_il / iout * 100):.0f}%)",
        ]

    ic_comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="U",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="power",
        pins=_pins_from_data(ic_data),
        power_pins=power_pins,
        pin_nets=pin_nets,
        bypass_caps=bypass_caps,
        straps=straps,
        annotations=annotations,
        explicit_no_connects=explicit_no_connects,
        feedback_vref_voltage=vref,
        feedback_vref_provenance=ic_data.get("vref_provenance") or "switching-topology-parameter",
        feedback_vref_evidence_id=vref_evidence,
    )
    ic_comp.source_ref = ref

    # Retain every exact producer record through the shared ComponentDef
    # boundary.  This deliberately reuses the same deterministic evidence IDs
    # emitted into the local construction ledger rather than reconstructing
    # provenance later during validation.
    for record in calc_records:
        input_evidence = tuple(
            item
            for item in (ledger.get(item.evidence_id) for item in record.inputs if item.evidence_id)
            if item is not None
        )
        emit_and_retain_passive_synthesis(ic_comp, record, input_evidence=input_evidence)

    ports = [
        BoundaryPort(vin_net, "input"),
        BoundaryPort(rail_name, "output"),
        BoundaryPort("GND", "passive"),
    ]
    if pin_en and en_net is not None:
        ports.append(BoundaryPort(en_net, "input"))
    if topology == "buck_boost" and pin_pg:
        ports.append(BoundaryPort(pg_net, "output"))

    topo_label = {"buck": "Buck", "boost": "Boost", "buck_boost": "Buck-Boost"}[topology]
    return SubcircuitResult(
        components=[ic_comp],
        boundary_ports=ports,
        annotations=[f"{topo_label} {ic_name}: {vin_net} ({vin}V) -> {rail_name} ({actual_vout:.2f}V) at {iout}A"],
        primary_category="power",
    )


# ================================================================
# Linear regulator builder (ldo)
# ================================================================


def build_linear_regulator(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build an LDO regulator subcircuit."""
    ic_name = params.get("ic", ic_data.get("_mpn", "UNKNOWN"))
    vin = params["vin"]
    vout = params.get("vout", ic_data.get("vout_fixed", 3.3))
    iout = params.get("iout", ic_data.get("iout_max", 0.5))
    rail_name = params.get("rail_name") or f"VDD_{vout:.1f}V".replace(".", "P")
    vin_net = params.get("vin_net", "VIN")
    en_net = params.get("en_net", vin_net)

    vdropout = ic_data.get("vdropout", 0.2)
    warnings = []
    if vin - vout < vdropout:
        warnings.append(f"WARNING: Vin-Vout={vin - vout:.2f}V < Vdropout={vdropout:.3f}V")
    pdiss = (vin - vout) * iout
    if pdiss > 0.5:
        warnings.append(f"WARNING: Pdiss={pdiss:.2f}W > 500mW — needs heatsink")

    ref = params.get("ref", "U")
    ledger = EvidenceLedger()
    records: list[calc.CalculationRecord] = []

    def _ldo_cap(role: str, value: float) -> calc.CalculationRecord:
        target = f"param:{ref}.ldo.{role}"
        datasheet_url = str(ic_data.get("datasheet_url") or "").strip()
        if datasheet_url:
            source_id = ledger.record(
                subject_ref=target,
                claim=f"datasheet recommends {value:g} F for {role}",
                kind="datasheet",
                source=EvidenceSource(uri=datasheet_url, extraction_method="ldo-cap-recommendation"),
                confidence="single_source",
                freshness="unknown",
            )
            decision = calc.ldo_minimum_capacitor(
                target=target,
                minimum_capacitance_f=value,
                evidence_id=source_id,
            )
            if decision.finding is not None:
                raise ValueError(f"{decision.finding.rule_id}: LDO {role} selection withheld")
            selected = decision.calculation
        else:
            decision = calc.bounded_fallback_scalar(
                target=target,
                value=value,
                minimum=100e-9,
                maximum=100e-6,
                unit="F",
                series="E24",
                direction="up",
            )
            if decision.finding is not None:
                raise ValueError(f"{decision.finding.rule_id}: LDO {role} selection withheld")
            selected = decision.calculation
        emitted = calc.emit_calculation_evidence(selected, ledger)
        records.append(emitted)
        return emitted

    try:
        cin = _ldo_cap("cin", float(ic_data.get("cin", 1e-6)))
        cout = _ldo_cap("cout", float(ic_data.get("cout", 1e-6)))
        cin_val = calc.require_selection(cin).value
        cout_val = calc.require_selection(cout).value
    except ValueError as exc:
        message = str(exc)
        if "CW-PSV-" not in message:
            message = f"CW-PSV-001: LDO passive synthesis withheld: {message}"
        raise ValueError(message) from exc

    def _trace(record: calc.CalculationRecord) -> dict[str, object]:
        if record.emits_evidence is None:
            raise ValueError("CW-PSV-001: LDO passive has no calculation evidence")
        return {
            "selection_policy": record.policy,
            "confidence": record.confidence,
            "calculation_id": record.id,
            "evidence_ids": (record.emits_evidence,),
        }

    pin_vin = _pin_role(ic_data, "vin")
    pin_gnd = _pin_role(ic_data, "gnd")
    pin_out = _pin_role(ic_data, "out")
    pin_en = _pin_role(ic_data, "en")

    power_pins: dict[str, str] = {}
    if pin_vin:
        power_pins[pin_vin] = vin_net
    if pin_gnd:
        power_pins[pin_gnd] = "GND"
    if pin_out:
        power_pins[pin_out] = rail_name

    pin_nets: dict[str, str] = {}
    if pin_en:
        pin_nets[pin_en] = en_net

    bypass_caps = [
        BypassCap(
            "CIN",
            vin_net,
            "GND",
            format_capacitance(cin_val),
            cap_footprint(cin_val),
            role="decoupling",
            presentation="topology_local",
            **_trace(cin),
        ),
        BypassCap(
            "COUT",
            rail_name,
            "GND",
            format_capacitance(cout_val),
            cap_footprint(cout_val),
            role="decoupling",
            presentation="topology_local",
            **_trace(cout),
        ),
    ]

    iq_ua = ic_data.get("iq_ua", 0)
    annotations = [
        f"{rail_name}: {vout}V from {vin_net} at {iout}A ({ic_name})",
        f"Dropout: {vdropout:.3f}V, Pdiss: {pdiss:.2f}W, Iq: {iq_ua}uA",
    ] + warnings

    ic_comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="U",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="power",
        pins=_pins_from_data(ic_data),
        power_pins=power_pins,
        pin_nets=pin_nets,
        bypass_caps=bypass_caps,
        annotations=annotations,
    )
    ic_comp.source_ref = ref
    for record in records:
        input_evidence = tuple(
            item
            for item in (
                ledger.get(input_value.evidence_id) for input_value in record.inputs if input_value.evidence_id
            )
            if item is not None
        )
        emit_and_retain_passive_synthesis(ic_comp, record, input_evidence=input_evidence)

    ports = [
        BoundaryPort(vin_net, "input"),
        BoundaryPort(rail_name, "output"),
        BoundaryPort("GND", "passive"),
    ]
    if pin_en:
        ports.append(BoundaryPort(en_net, "input"))

    return SubcircuitResult(
        components=[ic_comp],
        boundary_ports=ports,
        annotations=[f"LDO {ic_name}: {vin_net} ({vin}V) -> {rail_name} ({vout}V) at {iout}A"] + warnings,
        primary_category="power",
    )


# ================================================================
# Thin verdict-A builders
# ================================================================


def build_can_transceiver(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build a CAN transceiver with legacy-compatible CAN net options."""
    ic_name = params.get("ic", ic_data.get("_mpn", "SN65HVD230"))
    ref = params.get("ref", "U")
    vdd_net = params.get("vdd_net", "VDD_3P3")
    txd_net = params.get("txd_net", f"CAN_TXD_{ref}")
    rxd_net = params.get("rxd_net", f"CAN_RXD_{ref}")
    bus_prefix = params.get("bus_net_prefix", "CAN")
    termination = params.get("termination", False)
    slope_control = params.get("slope_control", False)

    pins = _pins_from_data(ic_data)
    canh_net = f"{bus_prefix}_H_{ref}"
    canl_net = f"{bus_prefix}_L_{ref}"
    vref_net = f"CAN_VREF_{ref}"

    pin_vcc = _pin_role(ic_data, "vcc")
    pin_gnd = _pin_role(ic_data, "gnd")
    pin_txd = _pin_role(ic_data, "txd")
    pin_rxd = _pin_role(ic_data, "rxd")
    pin_canh = _pin_role(ic_data, "canh")
    pin_canl = _pin_role(ic_data, "canl")
    pin_vref = _pin_role(ic_data, "vref")
    pin_rs = _pin_role(ic_data, "rs")

    power_pins = {pin_vcc: vdd_net, pin_gnd: "GND"}
    pin_nets = {
        pin_txd: txd_net,
        pin_rxd: rxd_net,
        pin_canh: canh_net,
        pin_canl: canl_net,
        pin_vref: vref_net,
    }

    if slope_control:
        rs_net = f"CAN_RS_{ref}"
        pin_nets[pin_rs] = rs_net
    elif ic_data.get("rs_highspeed_to_gnd", False):
        pin_nets[pin_rs] = "GND"

    ledger = EvidenceLedger()
    records: list[calc.CalculationRecord] = []

    def _fallback(
        name: str,
        value: float,
        minimum: float,
        maximum: float,
        unit: str,
        *,
        series: str,
        direction: str = "nearest",
    ) -> calc.CalculationRecord:
        decision = calc.bounded_fallback_scalar(
            target=f"param:{ref}.can.{name}",
            value=value,
            minimum=minimum,
            maximum=maximum,
            unit=unit,
            series=series,
            direction=direction,
        )
        if decision.finding is not None:
            raise ValueError(f"{decision.finding.rule_id}: CAN {name} selection withheld")
        emitted = calc.emit_calculation_evidence(decision.calculation, ledger)
        records.append(emitted)
        return emitted

    try:
        cvcc = _fallback("cvcc", 100e-9, 10e-9, 1e-6, "F", series="E24", direction="up")
        cvref = _fallback("cvref", 100e-9, 10e-9, 1e-6, "F", series="E24", direction="up")
        if slope_control:
            rs = _fallback("slope_control", 10e3, 1e3, 100e3, "ohm", series="E24")
        if termination:
            rt1 = _fallback("termination_top", 60.0, 54.0, 66.0, "ohm", series="E96")
            rt2 = _fallback("termination_bottom", 60.0, 54.0, 66.0, "ohm", series="E96")
            ct = _fallback("termination_cap", 4.7e-9, 1e-9, 10e-9, "F", series="E24", direction="up")
    except ValueError as exc:
        message = str(exc)
        if "CW-PSV-" not in message:
            message = f"CW-PSV-001: CAN passive synthesis withheld: {message}"
        raise ValueError(message) from exc

    def _trace(record: calc.CalculationRecord) -> dict[str, object]:
        selected = calc.require_selection(record)
        if record.emits_evidence is None:
            raise ValueError("CW-PSV-001: CAN passive has no calculation evidence")
        return {
            "value": format_capacitance(selected.value) if selected.unit == "F" else format_resistance(selected.value),
            "selection_policy": record.policy,
            "confidence": record.confidence,
            "calculation_id": record.id,
            "evidence_ids": (record.emits_evidence,),
        }

    bypass_caps = [
        BypassCap(
            "C_VCC",
            vdd_net,
            "GND",
            footprint=FP_0402C,
            role="decoupling",
            presentation="topology_local",
            **_trace(cvcc),
        ),
        BypassCap(
            "C_VREF",
            vref_net,
            "GND",
            footprint=FP_0402C,
            role="decoupling",
            presentation="topology_local",
            **_trace(cvref),
        ),
    ]
    straps = []
    if slope_control:
        straps.append(
            StrapConfig(
                "RS",
                rs_net,
                "GND",
                footprint=FP_0402R,
                role="slope_control",
                presentation="topology_local",
                **_trace(rs),
            )
        )
    if termination:
        term_mid_net = f"CAN_TERM_MID_{ref}"
        straps.extend(
            [
                StrapConfig(
                    "RT1",
                    canh_net,
                    term_mid_net,
                    footprint=FP_0402R,
                    role="termination",
                    presentation="topology_local",
                    **_trace(rt1),
                ),
                StrapConfig(
                    "RT2",
                    term_mid_net,
                    canl_net,
                    footprint=FP_0402R,
                    role="termination",
                    presentation="topology_local",
                    **_trace(rt2),
                ),
            ]
        )
        bypass_caps.append(
            BypassCap(
                "CT",
                term_mid_net,
                "GND",
                footprint=cap_footprint(4.7e-9),
                role="termination",
                presentation="topology_local",
                **_trace(ct),
            )
        )

    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="U",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="digital",
        pins=pins,
        power_pins={k: v for k, v in power_pins.items() if k},
        pin_nets={k: v for k, v in pin_nets.items() if k},
        bypass_caps=bypass_caps,
        straps=straps,
        annotations=[
            f"CAN transceiver {ic_name}: {ic_data.get('vdd')}V, {ic_data.get('speed_mbps')}Mbps",
            f"Termination: {'split 2x60R + 4.7nF' if termination else 'none'}",
        ],
    )
    comp.source_ref = ref
    for record in records:
        emit_and_retain_passive_synthesis(comp, record)

    return SubcircuitResult(
        components=[comp],
        boundary_ports=[
            BoundaryPort(vdd_net, "input"),
            BoundaryPort("GND", "passive"),
            BoundaryPort(txd_net, "input"),
            BoundaryPort(rxd_net, "output"),
            BoundaryPort(canh_net, "bidirectional"),
            BoundaryPort(canl_net, "bidirectional"),
        ],
        annotations=[
            f"CAN transceiver {ic_name}: {vdd_net} ({ic_data.get('vdd')}V), "
            f"{'terminated' if termination else 'unterminated'}"
        ],
        primary_category="digital",
    )


def build_eeprom(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build I2C EEPROM or SPI flash with legacy-compatible strapping."""
    if ic_data.get("interface") == "spi":
        return _build_spi_eeprom(ic_data, params)
    return _build_i2c_eeprom(ic_data, params)


def _build_i2c_eeprom(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    ic_name = params.get("ic", ic_data.get("_mpn", "24LC256"))
    ref = params.get("ref", "U")
    vdd_net = params.get("vdd_net", "VDD_3P3")
    sda_net = params.get("sda_net", "I2C_SDA")
    scl_net = params.get("scl_net", "I2C_SCL")
    addr_offset = params.get("i2c_addr_offset", 0)
    write_protect = params.get("write_protect", False)

    power_pins = {
        _pin_role(ic_data, "vcc"): vdd_net,
        _pin_role(ic_data, "gnd"): "GND",
    }
    pin_nets = {
        _pin_role(ic_data, "sda"): sda_net,
        _pin_role(ic_data, "scl"): scl_net,
    }
    for i, pin_num in enumerate(ic_data.get("pin_addr", [])):
        power_pins[str(pin_num)] = vdd_net if addr_offset & (1 << i) else "GND"
    pin_wp = _pin_role(ic_data, "wp")
    if pin_wp:
        power_pins[pin_wp] = vdd_net if write_protect else "GND"

    actual_addr = ic_data.get("i2c_base_addr", 0x50) + addr_offset
    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="U",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="digital",
        pins=_pins_from_data(ic_data),
        power_pins={k: v for k, v in power_pins.items() if k},
        pin_nets={k: v for k, v in pin_nets.items() if k},
        bypass_caps=[
            BypassCap("C_VDD", vdd_net, "GND", "100nF", FP_0402C, role="decoupling", presentation="topology_local")
        ],
        annotations=[
            f"EEPROM {ic_name}: {ic_data.get('capacity_kbit')}Kbit I2C",
            f"Address: 0x{actual_addr:02X} (offset={addr_offset})",
        ],
    )
    comp.source_ref = ref
    return SubcircuitResult(
        components=[comp],
        boundary_ports=[
            BoundaryPort(vdd_net, "input"),
            BoundaryPort("GND", "passive"),
            BoundaryPort(sda_net, "bidirectional"),
            BoundaryPort(scl_net, "input"),
        ],
        annotations=[f"EEPROM {ic_name}: {ic_data.get('capacity_kbit')}Kbit, I2C 0x{actual_addr:02X}"],
        primary_category="digital",
    )


def _build_spi_eeprom(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    ic_name = params.get("ic", ic_data.get("_mpn", "AT25SF128A"))
    ref = params.get("ref", "U")
    vdd_net = params.get("vdd_net", "VDD_3P3")
    cs_net = params.get("cs_net", f"FLASH_CS_{ref}")
    mosi_net = params.get("mosi_net", "SPI_MOSI")
    miso_net = params.get("miso_net", "SPI_MISO")
    sclk_net = params.get("sclk_net", "SPI_SCLK")
    write_protect = params.get("write_protect", False)

    power_pins = {
        _pin_role(ic_data, "vcc"): vdd_net,
        _pin_role(ic_data, "gnd"): "GND",
        _pin_role(ic_data, "wp"): "GND" if write_protect else vdd_net,
        _pin_role(ic_data, "hold"): vdd_net,
    }
    pin_nets = {
        _pin_role(ic_data, "cs"): cs_net,
        _pin_role(ic_data, "si"): mosi_net,
        _pin_role(ic_data, "so"): miso_net,
        _pin_role(ic_data, "sck"): sclk_net,
    }
    cap_mbit = ic_data.get("capacity_kbit", 0) // 1024
    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="U",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="digital",
        pins=_pins_from_data(ic_data),
        power_pins={k: v for k, v in power_pins.items() if k},
        pin_nets={k: v for k, v in pin_nets.items() if k},
        bypass_caps=[
            BypassCap("C_VDD", vdd_net, "GND", "100nF", FP_0402C, role="decoupling", presentation="topology_local")
        ],
        annotations=[f"Flash {ic_name}: {cap_mbit}Mbit SPI NOR"],
    )
    comp.source_ref = ref
    return SubcircuitResult(
        components=[comp],
        boundary_ports=[
            BoundaryPort(vdd_net, "input"),
            BoundaryPort("GND", "passive"),
            BoundaryPort(cs_net, "input"),
            BoundaryPort(mosi_net, "input"),
            BoundaryPort(miso_net, "output"),
            BoundaryPort(sclk_net, "input"),
        ],
        annotations=[f"Flash {ic_name}: {cap_mbit}Mbit SPI NOR, CS={cs_net}"],
        primary_category="digital",
    )


def build_protection(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build a passive TVS/ESD protection device without IC decoupling."""
    ic_name = params.get("ic", ic_data.get("_mpn", "SMBJ5.0A"))
    ref = params.get("ref", "D")
    protect_net = params.get("protect_net", "PROTECTED_NET")
    gnd_net = params.get("gnd_net", "GND")
    direction = "bidirectional" if ic_data.get("bidirectional") else "unidirectional"
    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="D",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="protection",
        pins=_pins_from_data(ic_data),
        pin_nets={"1": protect_net, "2": gnd_net},
        annotations=[
            f"Protection: {ic_name} ({direction}) on {protect_net}",
            f"Vrwm={ic_data.get('vrwm')}V, Vbr={ic_data.get('vbr_min')}V, Vc={ic_data.get('vc_max')}V",
        ],
    )
    comp.source_ref = ref
    return SubcircuitResult(
        components=[comp],
        boundary_ports=[BoundaryPort(protect_net, "bidirectional"), BoundaryPort(gnd_net, "passive")],
        annotations=[f"TVS {ic_name} on {protect_net}"],
        primary_category="protection",
    )


def build_i2c_bus(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build I2C pull-ups or level-shifter with shared bus net names."""
    if ic_data.get("has_level_shift"):
        return _build_i2c_level_shifter(ic_data, params)
    return _build_i2c_pullups(ic_data, params)


def _build_i2c_pullups(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    ic_name = params.get("ic", ic_data.get("_mpn", "PULLUPS_ONLY"))
    ref = params.get("ref", "RP")
    vdd = params.get("vdd", 3.3)
    speed = params.get("speed", 400_000)
    c_bus = params.get("c_bus", 100e-12)
    vdd_net = params.get("vdd_net", "VDD_3P3")
    gnd_net = params.get("gnd_net", "GND")
    sda_net = params.get("sda_net", "I2C_SDA")
    scl_net = params.get("scl_net", "I2C_SCL")
    r_pullup_raw = _calc_i2c_pullup_resistance(speed, c_bus)
    r_pullup = snap_to_e24(r_pullup_raw)
    speed_str = f"{speed / 1e6:g}MHz" if speed >= 1_000_000 else f"{speed / 1e3:g}kHz"

    annotations = [
        f"I2C pull-ups: {format_resistance(r_pullup)} to {vdd_net} ({vdd}V)",
        f"Speed: {speed_str}, C_bus={c_bus * 1e12:g}pF",
        f"R = t_rise / (0.8473 * C_bus) = {r_pullup_raw:.0f} -> {format_resistance(r_pullup)}",
    ]
    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix=ic_data.get("ref_prefix", "RP"),
        value="I2C_PULLUPS",
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="communication",
        pins=_pins_from_data(ic_data),
        power_pins={str(ic_data.get("pin_vdd")): vdd_net, str(ic_data.get("pin_gnd")): gnd_net},
        pin_nets={str(ic_data.get("pin_sda")): sda_net, str(ic_data.get("pin_scl")): scl_net},
        straps=[
            StrapConfig(
                "R_SDA",
                sda_net,
                vdd_net,
                format_resistance(r_pullup),
                FP_0402R,
                role="i2c_pullup",
                presentation="topology_local",
            ),
            StrapConfig(
                "R_SCL",
                scl_net,
                vdd_net,
                format_resistance(r_pullup),
                FP_0402R,
                role="i2c_pullup",
                presentation="topology_local",
            ),
        ],
        annotations=annotations,
    )
    comp.source_ref = ref
    return SubcircuitResult(
        components=[comp],
        boundary_ports=[
            BoundaryPort(vdd_net, "input"),
            BoundaryPort(gnd_net, "passive"),
            BoundaryPort(sda_net, "bidirectional"),
            BoundaryPort(scl_net, "bidirectional"),
        ],
        annotations=[
            f"I2C pull-ups: {format_resistance(r_pullup)} to {vdd_net}, {speed_str}, C_bus={c_bus * 1e12:g}pF"
        ],
        primary_category="communication",
    )


def _build_i2c_level_shifter(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    ic_name = params.get("ic", ic_data.get("_mpn", "PCA9306"))
    ref = params.get("ref", "U")
    gnd_net = params.get("gnd_net", "GND")
    vdd_low_net = params.get("vdd_low_net") or params.get("vdd_net", "VDD_3P3")
    vdd_high_net = params.get("vdd_high_net", "VDD_5V")
    sda_low_net = params.get("sda_low_net") or params.get("sda_net", "I2C_SDA")
    scl_low_net = params.get("scl_low_net") or params.get("scl_net", "I2C_SCL")
    sda_high_net = params.get("sda_high_net", f"SDA_HV_{ref}")
    scl_high_net = params.get("scl_high_net", f"SCL_HV_{ref}")
    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix=ic_data.get("ref_prefix", "U"),
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="communication",
        pins=_pins_from_data(ic_data),
        power_pins={str(ic_data.get("pin_vref1")): vdd_low_net, str(ic_data.get("pin_vref2")): vdd_high_net},
        pin_nets={
            str(ic_data.get("pin_sda1")): sda_low_net,
            str(ic_data.get("pin_scl1")): scl_low_net,
            str(ic_data.get("pin_sda2")): sda_high_net,
            str(ic_data.get("pin_scl2")): scl_high_net,
        },
        bypass_caps=[
            BypassCap(
                "C_VREF1", vdd_low_net, gnd_net, "100nF", FP_0402C, role="decoupling", presentation="topology_local"
            ),
            BypassCap(
                "C_VREF2", vdd_high_net, gnd_net, "100nF", FP_0402C, role="decoupling", presentation="topology_local"
            ),
        ],
        annotations=[
            f"I2C level shifter: {vdd_low_net} <-> {vdd_high_net}",
            f"Low side: {sda_low_net}/{scl_low_net}, High side: {sda_high_net}/{scl_high_net}",
            "PCA9306: internal pull-ups, no external pull-ups needed",
        ],
    )
    comp.source_ref = ref
    return SubcircuitResult(
        components=[comp],
        boundary_ports=[
            BoundaryPort(vdd_low_net, "input"),
            BoundaryPort(vdd_high_net, "input"),
            BoundaryPort(gnd_net, "passive"),
            BoundaryPort(sda_low_net, "bidirectional"),
            BoundaryPort(scl_low_net, "bidirectional"),
            BoundaryPort(sda_high_net, "bidirectional"),
            BoundaryPort(scl_high_net, "bidirectional"),
        ],
        annotations=[f"I2C level shifter {ic_name}: {vdd_low_net} <-> {vdd_high_net}"],
        primary_category="communication",
    )


def build_battery_charger(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build Li-ion charger support passives with legacy-compatible nets."""
    ic_name = params.get("ic", ic_data.get("_mpn", "MCP73831T-2ACI/OT"))
    ref = params.get("ref", "U")
    ichg = params.get("ichg", min(float(ic_data.get("ichg_max", 0.5)), 0.5))
    vcell = params.get("vcell", 4.2)
    vin_net = params.get("vin_net", "VUSB")
    bat_net = params.get("bat_net", "VBAT")
    gnd_net = params.get("gnd_net", "GND")
    stat_net = params.get("stat_net", f"STAT_{ref}")
    prog_net = f"PROG_{ref}"
    k_prog = ic_data.get("k_prog", 1000)
    rprog = snap_to_e96(k_prog / ichg)
    actual_ichg = k_prog / rprog
    cin_val = 4.7e-6
    cbat_val = 4.7e-6

    power_pins = {
        str(ic_data.get("pin_vdd")): vin_net,
        str(ic_data.get("pin_gnd")): gnd_net,
        str(ic_data.get("pin_bat")): bat_net,
    }
    pin_nets = {
        str(ic_data.get("pin_prog")): prog_net,
        str(ic_data.get("pin_stat")): stat_net,
    }
    if ic_data.get("pin_ce"):
        pin_nets[str(ic_data["pin_ce"])] = vin_net

    straps = [
        StrapConfig(
            "RPROG",
            prog_net,
            gnd_net,
            format_resistance(rprog),
            FP_0402R,
            role="current_program",
            presentation="topology_local",
        )
    ]
    if ic_data.get("has_temp_pin") and ic_data.get("pin_temp"):
        temp_net = f"TEMP_{ref}"
        pin_nets[str(ic_data["pin_temp"])] = temp_net
        straps.append(
            StrapConfig(
                "RTEMP",
                temp_net,
                gnd_net,
                format_resistance(snap_to_e96(10e3)),
                FP_0402R,
                role="temp_disable",
                presentation="topology_local",
            )
        )

    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="U",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="power",
        pins=_pins_from_data(ic_data),
        power_pins={k: v for k, v in power_pins.items() if k and k != "None"},
        pin_nets={k: v for k, v in pin_nets.items() if k and k != "None"},
        bypass_caps=[
            BypassCap(
                "CIN",
                vin_net,
                gnd_net,
                format_capacitance(cin_val),
                cap_footprint(cin_val),
                role="input_cap",
                presentation="topology_local",
            ),
            BypassCap(
                "CBAT",
                bat_net,
                gnd_net,
                format_capacitance(cbat_val),
                cap_footprint(cbat_val),
                role="output_cap",
                presentation="topology_local",
            ),
        ],
        straps=straps,
        annotations=[
            f"Charge {bat_net}: {ichg}A into {vcell}V cell from {vin_net}",
            f"Rprog = {k_prog} / {ichg}A = {format_resistance(rprog)} (actual {actual_ichg:.3f}A)",
        ],
    )
    comp.source_ref = ref
    return SubcircuitResult(
        components=[comp],
        boundary_ports=[
            BoundaryPort(vin_net, "input"),
            BoundaryPort(bat_net, "output"),
            BoundaryPort(gnd_net, "passive"),
            BoundaryPort(stat_net, "output"),
        ],
        annotations=[f"Charger {ic_name}: {vin_net} -> {bat_net} ({vcell}V) at {ichg}A"],
        primary_category="power",
    )


def build_battery_monitor(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build battery fuel gauge support passives with legacy-compatible nets."""
    ic_name = params.get("ic", ic_data.get("_mpn", "MAX17048G+T"))
    ref = params.get("ref", "U")
    bat_net = params.get("bat_net", "VBAT")
    gnd_net = params.get("gnd_net", "GND")
    cell_capacity_mah = params.get("cell_capacity_mah", 2000)
    i2c_bus = params.get("i2c_bus", "I2C")
    sda_net = params.get("sda_net", f"{i2c_bus}_SDA")
    scl_net = params.get("scl_net", f"{i2c_bus}_SCL")

    power_pins = {
        str(ic_data.get("pin_vdd")): bat_net,
        str(ic_data.get("pin_gnd")): gnd_net,
    }
    for gnd_pin in ic_data.get("extra_gnd_pins", []):
        power_pins[str(gnd_pin)] = gnd_net
    pin_nets = {
        str(ic_data.get("pin_sda")): sda_net,
        str(ic_data.get("pin_scl")): scl_net,
    }
    bypass_caps = [
        BypassCap(
            "CVDD",
            bat_net,
            gnd_net,
            format_capacitance(snap_cap(1e-6)),
            cap_footprint(snap_cap(1e-6)),
            role="decoupling",
            presentation="topology_local",
        )
    ]
    straps: list[StrapConfig] = []
    ports = [
        BoundaryPort(bat_net, "input"),
        BoundaryPort(gnd_net, "passive"),
        BoundaryPort(sda_net, "bidirectional"),
        BoundaryPort(scl_net, "input"),
    ]
    annotations = [
        f"Fuel gauge {ic_name}: {ic_data.get('method')}",
        f"Cell capacity: {cell_capacity_mah}mAh, I2C addr: {ic_data.get('i2c_addr')}",
    ]

    if not ic_data.get("has_rsense"):
        power_pins[str(ic_data.get("pin_ctg"))] = gnd_net
        cell_net = f"CELL_{ref}"
        qstrt_net = f"QSTRT_{ref}"
        rcell_val = snap_to_e96(100.0)
        ccell_val = snap_cap(1e-6)
        pin_nets[str(ic_data.get("pin_cell"))] = cell_net
        pin_nets[str(ic_data.get("pin_qstrt"))] = qstrt_net
        straps.extend(
            [
                StrapConfig(
                    "RCELL",
                    bat_net,
                    cell_net,
                    format_resistance(rcell_val),
                    FP_0402R,
                    role="cell_filter_series",
                    presentation="topology_local",
                ),
                StrapConfig(
                    "RQSTRT",
                    qstrt_net,
                    gnd_net,
                    format_resistance(snap_to_e96(10e3)),
                    FP_0402R,
                    role="qstrt_pulldown",
                    presentation="topology_local",
                ),
            ]
        )
        bypass_caps.append(
            BypassCap(
                "CCELL",
                cell_net,
                gnd_net,
                format_capacitance(ccell_val),
                cap_footprint(ccell_val),
                role="cell_filter_cap",
                presentation="topology_local",
            )
        )
        annotations.extend(
            [
                f"CELL filter: {format_resistance(rcell_val)} + {format_capacitance(ccell_val)}",
                "QSTRT pulled low (quick start disabled)",
            ]
        )
    else:
        rsense_val = params.get("rsense", ic_data.get("rsense_default", 0.010))
        rsense_snapped = snap_to_e96(rsense_val)
        srp_net = f"SRP_{ref}"
        srn_net = f"SRN_{ref}"
        pin_nets[str(ic_data.get("pin_srp"))] = srp_net
        pin_nets[str(ic_data.get("pin_srn"))] = srn_net
        power_pins[str(ic_data.get("pin_bat"))] = bat_net
        if ic_data.get("pin_bin"):
            pin_nets[str(ic_data["pin_bin"])] = bat_net
        straps.append(
            StrapConfig(
                "RSENSE",
                srp_net,
                srn_net,
                format_resistance(rsense_snapped),
                res_footprint(rsense_snapped, 0.25),
                role="current_sense",
                presentation="topology_local",
            )
        )
        if ic_data.get("pin_gpout"):
            gpout_net = f"GPOUT_{ref}"
            pin_nets[str(ic_data["pin_gpout"])] = gpout_net
            ports.append(BoundaryPort(gpout_net, "output"))
        annotations.append(f"Rsense: {format_resistance(rsense_snapped)}")

    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="U",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="power",
        pins=_pins_from_data(ic_data),
        power_pins={k: v for k, v in power_pins.items() if k and k != "None"},
        pin_nets={k: v for k, v in pin_nets.items() if k and k != "None"},
        bypass_caps=bypass_caps,
        straps=straps,
        annotations=annotations,
    )
    comp.source_ref = ref
    return SubcircuitResult(
        components=[comp],
        boundary_ports=ports,
        annotations=[
            f"Battery monitor {ic_name}: {ic_data.get('method')}, "
            f"{cell_capacity_mah}mAh cell, I2C addr {ic_data.get('i2c_addr')}"
        ],
        primary_category="power",
    )


def build_display_driver(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build display-controller passives and bus nets with legacy-compatible wiring."""
    ic_name = params.get("ic", ic_data.get("_mpn", "SSD1306"))
    ref = params.get("ref", "U")
    interface = params.get("interface", "i2c").lower()
    vdd_net = params.get("vdd_net", "VDD_3P3")
    gnd_net = params.get("gnd_net", "GND")
    resolution = params.get("resolution", ic_data.get("resolution", "128x64"))
    display_type = ic_data.get("display_type", "display")
    res_n_net = f"RES_N_{ref}"
    dc_net = f"DC_{ref}"
    cs_n_net = f"CS_N_{ref}"
    iref_net = f"IREF_{ref}"

    power_pins = {str(ic_data.get("pin_vdd")): vdd_net, str(ic_data.get("pin_gnd")): gnd_net}
    if ic_data.get("pin_vcc"):
        power_pins[str(ic_data["pin_vcc"])] = vdd_net

    pin_nets = {str(ic_data.get("pin_res_n")): res_n_net}
    if interface == "i2c":
        pin_nets[str(ic_data.get("pin_sda"))] = params.get("sda_net", "I2C_SDA")
        pin_nets[str(ic_data.get("pin_scl"))] = params.get("scl_net", "I2C_SCL")
        if ic_data.get("pin_dc"):
            pin_nets[str(ic_data["pin_dc"])] = gnd_net
        if ic_data.get("pin_cs_n"):
            pin_nets[str(ic_data["pin_cs_n"])] = gnd_net
        interface_note = "Interface: I2C (DC=GND -> addr 0x3C)"
    else:
        pin_nets[str(ic_data.get("pin_sda"))] = params.get("mosi_net", "SPI_MOSI")
        pin_nets[str(ic_data.get("pin_scl"))] = params.get("sclk_net", "SPI_SCLK")
        if ic_data.get("pin_dc"):
            pin_nets[str(ic_data["pin_dc"])] = dc_net
        if ic_data.get("pin_cs_n"):
            pin_nets[str(ic_data["pin_cs_n"])] = params.get("cs_net", cs_n_net)
        interface_note = "Interface: SPI (4-wire)"

    ledger = EvidenceLedger()
    records: list[calc.CalculationRecord] = []

    def _reset_fallback(
        name: str, value: float, minimum: float, maximum: float, unit: str, series: str, direction: str
    ) -> calc.CalculationRecord:
        decision = calc.bounded_fallback_scalar(
            target=f"param:{ref}.display.{name}",
            value=value,
            minimum=minimum,
            maximum=maximum,
            unit=unit,
            series=series,
            direction=direction,
        )
        if decision.finding is not None:
            raise ValueError(f"{decision.finding.rule_id}: display {name} selection withheld")
        emitted = calc.emit_calculation_evidence(decision.calculation, ledger)
        records.append(emitted)
        return emitted

    reset_resistor = _reset_fallback("reset_pullup", 10e3, 1e3, 100e3, "ohm", "E96", "nearest")
    reset_cap = _reset_fallback("reset_delay_cap", 100e-9, 10e-9, 1e-6, "F", "E24", "up")
    decoupling_cap = _reset_fallback("decoupling", 100e-9, 10e-9, 1e-6, "F", "E24", "up")
    bulk_cap = _reset_fallback("bulk_cap", 10e-6, 1e-6, 47e-6, "F", "E24", "up")
    rres_val = calc.require_selection(reset_resistor).value
    cres_val = calc.require_selection(reset_cap).value
    cdec_val = calc.require_selection(decoupling_cap).value
    cbulk_val = calc.require_selection(bulk_cap).value

    def _reset_trace(record: calc.CalculationRecord) -> dict[str, object]:
        if record.emits_evidence is None:
            raise ValueError("CW-PSV-001: display reset passive has no calculation evidence")
        return {
            "selection_policy": record.policy,
            "confidence": record.confidence,
            "calculation_id": record.id,
            "evidence_ids": (record.emits_evidence,),
        }

    bypass_caps = [
        BypassCap(
            "CVDD",
            vdd_net,
            gnd_net,
            format_capacitance(cdec_val),
            cap_footprint(cdec_val),
            role="decoupling",
            presentation="topology_local",
            **_reset_trace(decoupling_cap),
        ),
        BypassCap(
            "CVDD_BULK",
            vdd_net,
            gnd_net,
            format_capacitance(cbulk_val),
            cap_footprint(cbulk_val),
            role="bulk_cap",
            presentation="topology_local",
            **_reset_trace(bulk_cap),
        ),
        BypassCap(
            "CRES",
            res_n_net,
            gnd_net,
            format_capacitance(cres_val),
            cap_footprint(cres_val),
            role="reset_delay",
            presentation="topology_local",
            **_reset_trace(reset_cap),
        ),
    ]
    straps = [
        StrapConfig(
            "RRES",
            vdd_net,
            res_n_net,
            format_resistance(rres_val),
            FP_0402R,
            role="reset_pullup",
            presentation="topology_local",
            **_reset_trace(reset_resistor),
        )
    ]
    annotations = [
        f"Display {ic_name}: {resolution} {display_type.upper()}, {interface.upper()}",
        f"Reset RC delay: {format_resistance(rres_val)} * {format_capacitance(cres_val)}",
        interface_note,
    ]

    if ic_data.get("has_charge_pump"):
        riref = _reset_fallback(
            "iref_set",
            float(ic_data.get("riref_default", 910e3)),
            100e3,
            10e6,
            "ohm",
            "E96",
            "nearest",
        )
        cpump = _reset_fallback("charge_pump_cap", 2.2e-6, 100e-9, 10e-6, "F", "E24", "up")
        riref_val = calc.require_selection(riref).value
        pin_nets[str(ic_data.get("pin_iref"))] = iref_net
        straps.append(
            StrapConfig(
                "RIREF",
                iref_net,
                gnd_net,
                format_resistance(riref_val),
                FP_0402R,
                role="iref_set",
                presentation="topology_local",
                **_reset_trace(riref),
            )
        )
        cpump_val = calc.require_selection(cpump).value
        if ic_data.get("pin_c1p"):
            c1p_net = f"C1P_{ref}"
            pin_nets[str(ic_data["pin_c1p"])] = c1p_net
            bypass_caps.append(
                BypassCap(
                    "C1P",
                    c1p_net,
                    gnd_net,
                    format_capacitance(cpump_val),
                    cap_footprint(cpump_val),
                    role="charge_pump_cap",
                    presentation="topology_local",
                    **_reset_trace(cpump),
                )
            )
        if ic_data.get("pin_c2p"):
            c2p_net = f"C2P_{ref}"
            pin_nets[str(ic_data["pin_c2p"])] = c2p_net
            bypass_caps.append(
                BypassCap(
                    "C2P",
                    c2p_net,
                    gnd_net,
                    format_capacitance(cpump_val),
                    cap_footprint(cpump_val),
                    role="charge_pump_cap",
                    presentation="topology_local",
                    **_reset_trace(cpump),
                )
            )
        if ic_data.get("pin_vcomh"):
            pin_nets[str(ic_data["pin_vcomh"])] = f"VCOMH_{ref}"
        annotations.extend(
            [f"IREF: {format_resistance(riref_val)}", f"Charge pump caps: 2x {format_capacitance(cpump_val)}"]
        )

    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="U",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="digital",
        pins=_pins_from_data(ic_data),
        power_pins={k: v for k, v in power_pins.items() if k and k != "None"},
        pin_nets={k: v for k, v in pin_nets.items() if k and k != "None"},
        bypass_caps=bypass_caps,
        straps=straps,
        annotations=annotations,
    )
    comp.source_ref = ref
    for record in records:
        emit_and_retain_passive_synthesis(comp, record)
    ports = [BoundaryPort(vdd_net, "input"), BoundaryPort(gnd_net, "passive")]
    for net_name in dict.fromkeys(comp.pin_nets.values()):
        if net_name not in {vdd_net, gnd_net}:
            ports.append(BoundaryPort(net_name, "bidirectional"))
    return SubcircuitResult(
        components=[comp],
        boundary_ports=ports,
        annotations=[f"Display {ic_name}: {resolution} {display_type}, {interface}"],
        primary_category="digital",
    )


def build_passive_diode(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build a passive diode/LED without IC power-pin requirements."""
    ic_name = params.get("ic", ic_data.get("_mpn", "1N4148W"))
    ref = params.get("ref", "D")
    anode_net = params.get("anode_net", params.get("a_net", f"A_{ref}"))
    cathode_net = params.get("cathode_net", params.get("k_net", f"K_{ref}"))
    label = "LED" if ic_data.get("topology") == "led" else "Diode"
    comp = ComponentDef(
        mpn=ic_name,
        ref_prefix="D",
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="passive",
        pins=_pins_from_data(ic_data),
        pin_nets={"A": anode_net, "K": cathode_net},
        annotations=[f"{label} {ic_name}: {anode_net} -> {cathode_net}"],
    )
    comp.source_ref = ref
    return SubcircuitResult(
        components=[comp],
        boundary_ports=[BoundaryPort(anode_net, "passive"), BoundaryPort(cathode_net, "passive")],
        annotations=[f"{label} {ic_name}: {anode_net} -> {cathode_net}"],
        primary_category="passive",
    )


def build_crystal_oscillator(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Build a crystal plus load-cap network from CL/frequency params."""
    freq = params["freq"]
    cl_spec_pf = params["cl_spec"]
    c_stray = params.get("c_stray", 3e-12)
    ic_name = params.get("ic", ic_data.get("_mpn", "UNKNOWN"))
    ref = params.get("ref", "Y")
    xtal_in_net = params.get("xtal_in_net", "XTAL_IN")
    xtal_out_net = params.get("xtal_out_net", "XTAL_OUT")

    cl_val = snap_cap(crystal_load_caps(cl_spec_pf * 1e-12, c_stray))
    r_fb = snap_to_e96(1e6)
    freq_mhz = freq / 1e6

    pin_xtal1 = _pin_role(ic_data, "xtal1") or _pin_role(ic_data, "xtal_in")
    pin_xtal2 = _pin_role(ic_data, "xtal2") or _pin_role(ic_data, "xtal_out")
    if not pin_xtal1 or not pin_xtal2:
        raise ValueError(f"{ic_name} crystal_oscillator entry must declare xtal pin roles")

    power_pins: dict[str, str] = {}
    for gnd_pin in ic_data.get("gnd_pins", []) or []:
        power_pins[str(gnd_pin)] = "GND"

    xtal = ComponentDef(
        mpn=ic_name,
        ref_prefix="Y",
        value=f"{freq_mhz:g}MHz",
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category="clock",
        pins=_pins_from_data(ic_data),
        power_pins=power_pins,
        pin_nets={str(pin_xtal1): xtal_in_net, str(pin_xtal2): xtal_out_net},
        bypass_caps=[
            BypassCap(
                "CL1",
                xtal_in_net,
                "GND",
                format_capacitance(cl_val),
                cap_footprint(cl_val),
                role="load_cap",
                presentation="topology_local",
            ),
            BypassCap(
                "CL2",
                xtal_out_net,
                "GND",
                format_capacitance(cl_val),
                cap_footprint(cl_val),
                role="load_cap",
                presentation="topology_local",
            ),
        ],
        straps=[
            StrapConfig(
                "RFB",
                xtal_in_net,
                xtal_out_net,
                format_resistance(r_fb),
                FP_0402R,
                role="feedback",
                presentation="topology_local",
            ),
        ],
        annotations=[
            f"Crystal {freq_mhz:g}MHz, CL={cl_spec_pf:g}pF",
            f"Load caps: 2x {format_capacitance(cl_val)}",
            f"Feedback R: {format_resistance(r_fb)}",
        ],
    )
    xtal.source_ref = ref

    return SubcircuitResult(
        components=[xtal],
        boundary_ports=[
            BoundaryPort(xtal_in_net, "bidirectional"),
            BoundaryPort(xtal_out_net, "bidirectional"),
            BoundaryPort("GND", "passive"),
        ],
        annotations=[
            f"Crystal oscillator {ic_name}: {freq_mhz:g}MHz, "
            f"CL={cl_spec_pf:g}pF, load caps 2x {format_capacitance(cl_val)}",
        ],
        primary_category="clock",
    )


# ================================================================
# Generic wiring builder (pin map + decoupling)
# ================================================================


def build_generic(ic_data: dict, params: dict[str, Any]) -> SubcircuitResult:
    """Fallback builder for ICs without specialized topology logic.

    Assigns power pins, wires all non-power signal pins to boundary ports,
    adds standard decoupling, and exports boundary ports.
    Works for: protection, connectors, EEPROM, RTC, display, wireless, etc.
    """
    ic_name = params.get("ic", ic_data.get("_mpn", "UNKNOWN"))
    ref = params.get("ref", "U")
    vdd_net = params.get("vdd_net", "VDD_3P3")
    gnd_net = params.get("gnd_net", "GND")

    pins = _pins_from_data(ic_data)

    # Read raw pin_vdd / pin_gnd from ic_data (may be list or scalar)
    raw_vdd = ic_data.get("pin_vdd") or ic_data.get("pin_vcc") or ic_data.get("pin_vin")
    raw_gnd = ic_data.get("pin_gnd")
    raw_vpos = ic_data.get("pin_vpos")
    raw_vneg = ic_data.get("pin_vneg")

    power_pins: dict[str, str] = {}

    # Handle pin_vdd as list or scalar
    if raw_vdd:
        if isinstance(raw_vdd, list):
            for p in raw_vdd:
                power_pins[str(p)] = vdd_net
        else:
            power_pins[str(raw_vdd)] = vdd_net

    # Handle pin_gnd as list or scalar
    if raw_gnd:
        if isinstance(raw_gnd, list):
            for p in raw_gnd:
                power_pins[str(p)] = gnd_net
        else:
            power_pins[str(raw_gnd)] = gnd_net

    if raw_vpos:
        power_pins[str(raw_vpos)] = vdd_net
    if raw_vneg:
        power_pins[str(raw_vneg)] = gnd_net

    # Handle multiple GND pins (pin_gnd_extra as list or scalar)
    for key in ic_data:
        if key.startswith("pin_gnd") and key != "pin_gnd":
            extras = ic_data[key]
            if isinstance(extras, list):
                for p in extras:
                    power_pins[str(p)] = gnd_net
            else:
                power_pins[str(extras)] = gnd_net

    # Auto-detect additional power pins by name from the pin list
    for pin in pins:
        if pin.number in power_pins:
            continue
        name_upper = pin.name.upper()
        if pin.electrical_type == "power_in":
            if any(
                name_upper == p or name_upper.startswith(f"{p}_") or name_upper == p for p in GROUND_NET_PREFIXES
            ) or name_upper in ("VEE", "SGND", "COM", "V-", "EPAD"):
                power_pins[pin.number] = gnd_net
            elif _is_power_input_pin_name(name_upper):
                power_pins[pin.number] = vdd_net

    # T228/T234 — shared interface routing now keys off normalized pin roles
    # rather than a fixed list of `pin_<role>` field names. This lets both
    # curated ic_data entries and imported parts converge on one contract.
    # Pin-name inference fills roles the metadata didn't declare so parts
    # whose pins are named USB_DP / SDA / XTAL1 land on the shared buses
    # instead of leaving those pins unmapped.
    pin_roles = _collect_pin_roles(ic_data)
    for role, pin_num in infer_pin_roles_from_pins(pins).items():
        if role not in pin_roles and pin_num not in power_pins:
            pin_roles.setdefault(role, pin_num)
    shared_role_nets = {
        "sda": params.get("sda_net", "I2C_SDA"),
        "scl": params.get("scl_net", "I2C_SCL"),
        "mosi": params.get("mosi_net", "SPI_MOSI"),
        "miso": params.get("miso_net", "SPI_MISO"),
        "sclk": params.get("sclk_net", params.get("sck_net", "SPI_SCLK")),
        # Chip-select is genuinely per-device, so there is no safe shared
        # default. Map it only when the caller names the net — otherwise the
        # pin stays unmapped where the SPI repair pass (or the fail-closed
        # generation guard) can deal with it honestly.
        "cs": params.get("cs_net", ""),
        # USB high-speed differential pair — shared with the USB connector.
        "dp": params.get("usb_dp_net", "USB_DP"),
        "dm": params.get("usb_dm_net", "USB_DM"),
        "dp1": params.get("usb_dp_net", "USB_DP"),
        "dm1": params.get("usb_dm_net", "USB_DM"),
        "dp2": params.get("usb_dp_net", "USB_DP"),
        "dm2": params.get("usb_dm_net", "USB_DM"),
        # Crystal oscillator nets default to shared names so the MCU and
        # crystal block resolve onto the same interface unless the caller
        # explicitly requests per-instance aliases.
        "xtal_in": params.get("xtal_in_net", "XTAL_IN"),
        "xtal_out": params.get("xtal_out_net", "XTAL_OUT"),
    }

    # Wire all non-power signal pins to explicit shared bus nets where IC
    # metadata provides them, otherwise to per-instance boundary ports.
    pin_nets: dict[str, str] = {}
    for role, net_name in shared_role_nets.items():
        pin_num = pin_roles.get(role)
        if net_name and pin_num and str(pin_num) not in power_pins:
            pin_nets[str(pin_num)] = net_name

    # ic_data may also declare arbitrary pin -> net mappings via signal_nets.
    # Used by catalog entries that need a signal interface beyond the
    # well-known buses above.
    signal_nets_map = ic_data.get("signal_nets") or {}
    if isinstance(signal_nets_map, dict):
        for pin_num, net_name in signal_nets_map.items():
            spn = str(pin_num)
            if spn in power_pins:
                continue
            if not isinstance(net_name, str) or not net_name:
                continue
            # Allow callers to override via params (e.g. params={"net_<pin>": "MY_NET"})
            override = params.get(f"net_{spn}") or params.get(f"net_{net_name.lower()}")
            pin_nets[spn] = override or net_name

    # T228 — collect explicit no-connect pins from ic_data and pin name.
    # Pins whose name marks them NC (e.g. "~", "NC", "DNC", "RESERVED") must
    # never be routed to a synthesized signal net; they belong in
    # explicit_no_connects so the schematic emits an `(no_connect)` marker
    # without a warning.
    explicit_ncs: set[str] = set()
    unmapped_required_pins: dict[str, str] = {}
    declared_ncs = ic_data.get("explicit_no_connects") or []
    if isinstance(declared_ncs, (list, tuple, set)):
        for pin_num in declared_ncs:
            explicit_ncs.add(str(pin_num))

    # T234 — metadata-declared optional pins (debug/test interfaces, strap
    # options) may stay unrouted without failing generation; the emit-time
    # classifier still surfaces them as reviewable warnings.
    optional_pins: set[str] = set()
    for key in ("debug_pins", "optional_pins"):
        declared = ic_data.get(key) or []
        if isinstance(declared, (list, tuple, set)):
            optional_pins.update(str(p) for p in declared)

    power_types = {"power_in", "power_out"}
    topo = str(ic_data.get("topology", "") or "").strip().lower()
    for pin in pins:
        if pin.number in power_pins:
            continue
        if pin.number in pin_nets:
            continue
        if pin.electrical_type in power_types:
            continue
        # T228 — pin names indicating intentional no-connect bypass synthesis
        # entirely. The classifier in generator.py already knows these, but
        # build_generic ran first and gave them synthesized nets like "~_U1"
        # before the classifier could see them.
        if _NC_PIN_NAME_RE.match(pin.name) or pin.name.upper().startswith(("NC", "RESERVED")):
            explicit_ncs.add(pin.number)
            continue
        if pin.number in optional_pins:
            continue
        # T228 — never synthesize per-instance signal nets like `{PIN}_{REF}`.
        # A signal pin either routes through declared metadata (pin roles,
        # signal_nets, params) or it stays unmapped: output-like pins become
        # silent no-connects at emit; everything else is recorded so
        # generation fails closed with the pin name and component reference.
        if pin.electrical_type not in _SAFE_UNMAPPED_PIN_TYPES:
            unmapped_required_pins[pin.number] = pin.name

    # Detect ref_prefix based on topology / component type
    detected_ref_prefix = "U"
    if ic_data.get("connector_type"):
        detected_ref_prefix = "J"
    elif topo == "crystal_oscillator" or "crystal" in ic_data.get("description", "").lower():
        detected_ref_prefix = "Y"
    elif topo == "protection":
        detected_ref_prefix = "D"
    elif topo in ("mosfet_switch",):
        detected_ref_prefix = "Q"
    ref_prefix = ic_data.get("ref_prefix", detected_ref_prefix)
    raw_recommended_bypass = list(ic_data.get("recommended_bypass") or [])
    default_datasheet = str(ic_data.get("datasheet_url") or "").strip()
    normalizable_recommended_bypass = [
        item
        for item in raw_recommended_bypass
        if not isinstance(item, dict)
        or str(item.get("precedence_policy") or item.get("policy") or "datasheet") != "datasheet"
        or bool(item.get("provenance") or item.get("evidence_id") or default_datasheet)
    ]
    ic_comp = ComponentDef(
        mpn=ic_name,
        ref_prefix=ref_prefix,
        value=ic_name,
        footprint=ic_data.get("footprint", ""),
        description=ic_data.get("description", ""),
        category=ic_data.get("category", "digital"),
        pins=pins,
        power_pins=power_pins,
        pin_roles=pin_roles,
        pin_nets=pin_nets,
        bypass_caps=[],
        recommended_bypass=normalizable_recommended_bypass,
        passive_recommendations=list(ic_data.get("passive_recommendations") or []),
        datasheet_url=str(ic_data.get("datasheet_url") or ""),
        reference_layout_url=str(ic_data.get("reference_layout_url") or ""),
        official_references=[
            {str(key): str(value) for key, value in item.items()}
            for item in (ic_data.get("official_references") or [])
            if isinstance(item, dict)
        ],
        explicit_no_connects=explicit_ncs,
        unmapped_required_pins=unmapped_required_pins,
        annotations=[f"{ic_data.get('description', ic_name)}"],
        feedback_vref_voltage=(
            ic_data.get("vref") if (ic_data.get("vref_provenance") or ic_data.get("vref_evidence_id")) else None
        ),
        feedback_vref_provenance=ic_data.get("vref_provenance"),
        feedback_vref_evidence_id=ic_data.get("vref_evidence_id"),
    )
    # Preserve legacy parser output for round-trip compatibility, but do not
    # promote an unprovenanced text hint into the typed recommendation list.
    # Consumers synthesize only from ``passive_recommendations``.
    ic_comp.recommended_bypass = raw_recommended_bypass
    ic_comp.source_ref = ref

    # T228 / F7 — only declare BoundaryPort entries for nets that genuinely
    # cross the IC boundary (shared buses, named external interfaces). Synthesized
    # per-instance nets (e.g. `THRESH_U3`, `XTAL_IN_U1`) are still kept on
    # `ic_comp.pin_nets` so support passives can reference them, but promoting
    # them to hierarchical sheet pins creates phantom interfaces — they have no
    # consumer outside this IC.
    ports = [
        BoundaryPort(vdd_net, "input"),
        BoundaryPort(gnd_net, "passive"),
    ]
    seen_ports: set[str] = {vdd_net, gnd_net}
    ref_suffix = f"_{ref}"
    for net_name in pin_nets.values():
        if not net_name or net_name in seen_ports:
            continue
        # Skip per-instance synthesized nets ('FOO_U1', 'PROG_U2', etc.) — they
        # only connect within this IC's local cluster.
        if net_name.endswith(ref_suffix) or ref_suffix in net_name:
            continue
        ports.append(BoundaryPort(net_name, "bidirectional"))
        seen_ports.add(net_name)

    return SubcircuitResult(
        components=[ic_comp],
        boundary_ports=ports,
        annotations=[f"{ic_name}: {vdd_net}"],
        primary_category=ic_data.get("primary_category", "digital"),
    )


# ================================================================
# Topology builder registry
# ================================================================


TOPOLOGY_BUILDERS: dict[str, Any] = {
    "buck": build_switching_regulator,
    "boost": build_switching_regulator,
    "buck_boost": build_switching_regulator,
    "ldo": build_linear_regulator,
    "can_transceiver": build_can_transceiver,
    "eeprom": build_eeprom,
    "protection": build_protection,
    "i2c_bus": build_i2c_bus,
    "battery_charger": build_battery_charger,
    "battery_monitor": build_battery_monitor,
    "display_driver": build_display_driver,
    "diode": build_passive_diode,
    "led": build_passive_diode,
    "crystal_oscillator": build_crystal_oscillator,
}

# All other topologies fall through to build_generic
_GENERIC_TOPOLOGIES = {
    "connector",
    "usb_c_connector",
    "rtc",
    "wireless_module",
    "mosfet_switch",
    "relay_driver",
    "gate_driver",
    "led_driver",
    "power_mux",
    "ethernet_phy",
    "usb_controller",
    "usb_hub",
    "charge_pump",
    "voltage_reference",
    "motor_driver",
    "rs485_transceiver",
    "level_shifter",
    "spi_bus",
    "adc",
    "dac",
    "current_sense",
    "opamp",
    "sensor_frontend",
    "audio_amplifier",
    "clock_synth",
}

for _topo in _GENERIC_TOPOLOGIES:
    TOPOLOGY_BUILDERS.setdefault(_topo, build_generic)


def get_builder(topology: str):
    """Look up the builder function for a topology."""
    return TOPOLOGY_BUILDERS.get(topology, build_generic)


# ================================================================
# Topology-level template metadata (T-release-prep)
#
# Descriptions and parameter schemas for topologies served by
# DataDrivenTemplate (no registered template class). This is
# topology-family data, not per-part behavior: the schema documents
# what the builder functions above consume, independent of which
# catalog IC is selected.
# ================================================================

_SWITCHER_COMMON_SCHEMA: list[dict[str, Any]] = [
    {"name": "vin", "type": "number", "required": True, "description": "Input voltage in volts"},
    {"name": "vout", "type": "number", "required": True, "description": "Output voltage in volts"},
    {"name": "iout", "type": "number", "required": True, "description": "Maximum output current in amps"},
    {"name": "ic", "type": "string", "description": "Regulator IC MPN from the IC catalog"},
    {"name": "ref", "type": "string", "default": "U", "description": "Reference designator for the IC"},
    {"name": "rail_name", "type": "string", "description": "Output rail net name; defaults to VDD_{vout}"},
    {"name": "vin_net", "type": "string", "default": "VIN", "description": "Input rail net name"},
    {"name": "en_net", "type": "string", "description": "Enable net name; defaults to vin_net"},
    {"name": "fsw", "type": "number", "description": "Switching frequency override in hertz"},
    {
        "name": "r_fbb",
        "type": "number",
        "minimum": 1000.0,
        "description": "Bottom feedback resistor override in ohms (1k to 1M declared selection window)",
    },
    {"name": "ripple_ratio", "type": "number", "default": 0.3, "description": "Target inductor ripple ratio"},
    {"name": "vout_ripple", "type": "number", "default": 0.02, "description": "Target output ripple in volts"},
]

_GENERIC_CATALOG_SCHEMA: list[dict[str, Any]] = [
    {"name": "ic", "type": "string", "description": "IC MPN from the catalog or imported part data"},
    {"name": "ref", "type": "string", "default": "U", "description": "Reference designator for the IC"},
    {"name": "vdd_net", "type": "string", "default": "VDD_3P3", "description": "Supply rail net name"},
    {"name": "gnd_net", "type": "string", "default": "GND", "description": "Ground net name"},
]

_DIODE_SCHEMA: list[dict[str, Any]] = [
    {"name": "ic", "type": "string", "description": "Device MPN from the IC catalog"},
    {"name": "ref", "type": "string", "default": "D", "description": "Reference designator for the device"},
    {"name": "anode_net", "type": "string", "description": "Anode net name; defaults to A_{ref}"},
    {"name": "cathode_net", "type": "string", "description": "Cathode net name; defaults to K_{ref}"},
]

TOPOLOGY_TEMPLATE_INFO: dict[str, dict[str, Any]] = {
    "buck": {
        "description": "Synchronous buck DC-DC converter with feedback divider and calculated LC filter",
        "param_schema": _SWITCHER_COMMON_SCHEMA,
    },
    "boost": {
        "description": "Boost DC-DC step-up converter with feedback divider and calculated inductor",
        "param_schema": _SWITCHER_COMMON_SCHEMA,
    },
    "buck_boost": {
        "description": "Buck-boost DC-DC converter with feedback divider",
        "param_schema": _SWITCHER_COMMON_SCHEMA
        + [
            {
                "name": "vin_min",
                "type": "number",
                "description": "Minimum input voltage for inductor sizing; defaults to 0.8 * vin",
            },
        ],
    },
    "ldo": {
        "description": "LDO linear regulator with input/output decoupling and dropout/dissipation checks",
        "param_schema": [
            {"name": "vin", "type": "number", "required": True, "description": "Input voltage in volts"},
            {
                "name": "vout",
                "type": "number",
                "description": "Output voltage in volts; inferred from fixed-output ICs when omitted",
            },
            {
                "name": "iout",
                "type": "number",
                "description": "Maximum output current in amps; defaults to the IC's rated current",
            },
            {"name": "ic", "type": "string", "description": "LDO IC MPN from the IC catalog"},
            {"name": "ref", "type": "string", "default": "U", "description": "Reference designator for the IC"},
            {"name": "rail_name", "type": "string", "description": "Output rail net name; defaults to VDD_{vout}"},
            {"name": "vin_net", "type": "string", "default": "VIN", "description": "Input rail net name"},
            {"name": "en_net", "type": "string", "description": "Enable net name; defaults to vin_net"},
        ],
    },
    "can_transceiver": {
        "description": "CAN bus transceiver with decoupling and optional split termination",
        "param_schema": [
            {"name": "ic", "type": "string", "description": "CAN transceiver IC MPN from the IC catalog"},
            {"name": "ref", "type": "string", "default": "U", "description": "Reference designator for the IC"},
            {"name": "vdd_net", "type": "string", "default": "VDD_3P3", "description": "Supply rail net name"},
            {"name": "txd_net", "type": "string", "description": "TXD net from MCU; defaults to CAN_TXD_{ref}"},
            {"name": "rxd_net", "type": "string", "description": "RXD net to MCU; defaults to CAN_RXD_{ref}"},
            {
                "name": "bus_net_prefix",
                "type": "string",
                "default": "CAN",
                "description": "Prefix for CANH/CANL bus nets",
            },
            {
                "name": "termination",
                "type": "boolean",
                "default": False,
                "description": "Enable split termination (2x60R + 4.7nF)",
            },
            {
                "name": "slope_control",
                "type": "boolean",
                "default": False,
                "description": "Enable slope control via 10k on RS pin (otherwise RS to GND)",
            },
        ],
    },
    "eeprom": {
        "description": "I2C EEPROM or SPI flash with address configuration and write protect",
        "param_schema": [
            {"name": "ic", "type": "string", "description": "EEPROM/flash IC MPN from the IC catalog"},
            {"name": "ref", "type": "string", "default": "U", "description": "Reference designator for the IC"},
            {"name": "vdd_net", "type": "string", "default": "VDD_3P3", "description": "Supply rail net name"},
            {
                "name": "i2c_addr_offset",
                "type": "integer",
                "default": 0,
                "description": "I2C address offset (0-7) set by A0/A1/A2 pins",
            },
            {"name": "sda_net", "type": "string", "default": "I2C_SDA", "description": "I2C SDA net name"},
            {"name": "scl_net", "type": "string", "default": "I2C_SCL", "description": "I2C SCL net name"},
            {
                "name": "cs_net",
                "type": "string",
                "description": "SPI chip select net name (SPI flash only); defaults to FLASH_CS_{ref}",
            },
            {
                "name": "mosi_net",
                "type": "string",
                "default": "SPI_MOSI",
                "description": "SPI MOSI net name (SPI flash only)",
            },
            {
                "name": "miso_net",
                "type": "string",
                "default": "SPI_MISO",
                "description": "SPI MISO net name (SPI flash only)",
            },
            {
                "name": "sclk_net",
                "type": "string",
                "default": "SPI_SCLK",
                "description": "SPI SCLK net name (SPI flash only)",
            },
            {
                "name": "write_protect",
                "type": "boolean",
                "default": False,
                "description": "Enable hardware write protection (WP tied high)",
            },
        ],
    },
    "protection": {
        "description": "Passive TVS/ESD protection device clamping a target net",
        "param_schema": [
            {
                "name": "protect_net",
                "type": "string",
                "required": True,
                "description": "Net to protect (e.g., VBUS_5V, USB_DP)",
            },
            {"name": "ic", "type": "string", "description": "Protection device MPN from the IC catalog"},
            {"name": "ref", "type": "string", "default": "D", "description": "Reference designator for the device"},
            {"name": "gnd_net", "type": "string", "default": "GND", "description": "Ground reference net name"},
            {
                "name": "protection_type",
                "type": "string",
                "default": "tvs",
                "options": ["tvs", "esd"],
                "description": "Protection type: TVS for power lines, ESD for signal lines",
            },
        ],
    },
    "diode": {
        "description": "Discrete diode (rectifier/Schottky/signal) with anode/cathode net wiring",
        "param_schema": _DIODE_SCHEMA,
    },
    "led": {
        "description": "Discrete LED with anode/cathode net wiring (pair with a series resistor or LED driver)",
        "param_schema": _DIODE_SCHEMA,
    },
    "component": {
        "description": "Catalog-defined IC placed from normalized pin and interface metadata",
        "param_schema": _GENERIC_CATALOG_SCHEMA,
    },
    "logic": {
        "description": "Logic-family IC (gates, buffers, glue logic) built from catalog pin metadata",
        "param_schema": _GENERIC_CATALOG_SCHEMA,
    },
    "sensor": {
        "description": "Sensor IC with decoupling and shared-bus wiring from catalog metadata",
        "param_schema": _GENERIC_CATALOG_SCHEMA,
    },
}
