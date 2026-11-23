"""Compile electrical intent into evidence-linked, enforceable PCB rules."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .component_db import ComponentDef
from .design_ir import DesignIR, PowerDomain
from .pcb_contracts import PcbConstraint
from .si_constraints import analyze_si_constraints


class PcbConstraintConflictError(ValueError):
    """Constraint compilation found incompatible intent before board mutation."""


@dataclass(frozen=True)
class ConstraintCompilation:
    constraints: tuple[PcbConstraint, ...]
    conflict_ids: tuple[str, ...] = ()
    unverified_requirements: tuple[dict[str, Any], ...] = ()

    @property
    def ready(self) -> bool:
        return not self.conflict_ids

    def require_ready(self) -> tuple[PcbConstraint, ...]:
        if self.conflict_ids:
            rendered = ", ".join(self.conflict_ids)
            raise PcbConstraintConflictError(f"PCB constraint conflicts must be resolved before mutation: {rendered}")
        return self.constraints

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "conflict_ids": list(self.conflict_ids),
            "constraints": [item.to_dict() for item in self.constraints],
            "unverified_requirements": [
                {**dict(item), "verification_status": "not_verified"}
                for item in self.unverified_requirements
            ],
        }


_FAB_PROFILES: dict[str, dict[str, float]] = {
    "jlcpcb": {
        "trace_width_min": 0.127,
        "trace_spacing_min": 0.127,
        "via_diameter_min": 0.45,
        "via_drill_min": 0.2,
    },
    "jlcpcb_4layer": {
        "trace_width_min": 0.09,
        "trace_spacing_min": 0.09,
        "via_diameter_min": 0.3,
        "via_drill_min": 0.15,
    },
    "pcbway": {
        "trace_width_min": 0.1,
        "trace_spacing_min": 0.1,
        "via_diameter_min": 0.3,
        "via_drill_min": 0.15,
    },
}

_NET_CLASS_BY_PROTOCOL = {
    "usb2": "USB2",
    "usb3": "USB3",
    "can": "CAN",
    "rs485": "RS485",
    "ethernet_100": "ETHERNET",
    "ethernet_1g": "ETHERNET",
    "lvds": "LVDS",
    "pcie": "PCIE",
    "mipi_dsi": "MIPI",
    "mipi_csi": "MIPI",
    "ddr3": "DDR",
    "ddr4": "DDR",
}


def _evidence_for(evidence_by_subject: Mapping[str, Sequence[str]], *subjects: str) -> tuple[str, ...]:
    return tuple(sorted({item for subject in subjects for item in evidence_by_subject.get(subject, ())}))


def _require_calculation_evidence(evidence_ids: Sequence[str], label: str) -> tuple[str, ...]:
    normalized = tuple(sorted(set(evidence_ids)))
    if not normalized:
        raise ValueError(f"calculated PCB constraint {label} requires evidence")
    return normalized


def _create(
    *,
    klass: str,
    target: str,
    params: Mapping[str, Any],
    origin: str,
    evidence_ids: Sequence[str] = (),
) -> PcbConstraint:
    if origin == "calculated":
        evidence_ids = _require_calculation_evidence(evidence_ids, f"{klass}:{target}")
    return PcbConstraint.create(
        klass=klass,
        target=target,
        params=params,
        origin=origin,
        evidence_ids=evidence_ids,
    )


def _fab_constraints(profile: str, evidence_id: str) -> list[PcbConstraint]:
    try:
        values = _FAB_PROFILES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown fabrication profile: {profile!r}") from exc
    if not evidence_id:
        raise ValueError("fabrication profile constraints require evidence")
    evidence = (evidence_id,)
    return [
        _create(
            klass="width",
            target="net_class:Default",
            params={"minimum": {"value": values["trace_width_min"], "unit": "mm"}},
            origin="fab_profile",
            evidence_ids=evidence,
        ),
        _create(
            klass="clearance",
            target="net_class:Default",
            params={"minimum": {"value": values["trace_spacing_min"], "unit": "mm"}},
            origin="fab_profile",
            evidence_ids=evidence,
        ),
        _create(
            klass="via",
            target="net_class:Default",
            params={
                "diameter_min": {"value": values["via_diameter_min"], "unit": "mm"},
                "drill_min": {"value": values["via_drill_min"], "unit": "mm"},
            },
            origin="fab_profile",
            evidence_ids=evidence,
        ),
    ]


def _power_constraints(domain: PowerDomain) -> list[PcbConstraint]:
    normalized = domain.normalized()
    if normalized.i_peak_ma is None and normalized.i_steady_ma is None:
        return []
    current_ma = max(value for value in (normalized.i_peak_ma, normalized.i_steady_ma) if value is not None)
    if current_ma >= 2000:
        width_mm = 1.0
    elif current_ma >= 1000:
        width_mm = 0.5
    else:
        width_mm = 0.25
    evidence = _require_calculation_evidence(
        (normalized.evidence_id,) if normalized.evidence_id else (),
        f"power-domain:{normalized.net}",
    )
    return [
        _create(
            klass="net_class",
            target=f"net:{normalized.net}",
            params={"name": "POWER"},
            origin="calculated",
            evidence_ids=evidence,
        ),
        _create(
            klass="width",
            target=f"net:{normalized.net}",
            params={
                "minimum": {"value": width_mm, "unit": "mm"},
                "design_current": {"value": current_ma, "unit": "mA"},
                "rule": "cw-power-width-v1",
            },
            origin="calculated",
            evidence_ids=evidence,
        ),
    ]


def _si_constraints(
    components: Sequence[ComponentDef],
    evidence_by_subject: Mapping[str, Sequence[str]],
) -> tuple[list[PcbConstraint], tuple[dict[str, Any], ...]]:
    analysis = analyze_si_constraints(list(components))
    compiled: list[PcbConstraint] = []
    for pair in analysis["diff_pairs"]:
        positive, negative = pair["net_p"], pair["net_n"]
        evidence = _evidence_for(evidence_by_subject, f"net:{positive}", f"net:{negative}")
        compiled.append(
            _create(
                klass="diff_pair",
                target=f"net:{positive}",
                params={"negative_net": negative, "protocol": pair["bus_type"]},
                origin="calculated",
                evidence_ids=evidence,
            )
        )
    for impedance in analysis["impedance_constraints"]:
        for net in sorted(set(impedance["nets"])):
            evidence = _evidence_for(evidence_by_subject, f"net:{net}")
            compiled.extend(
                (
                    _create(
                        klass="net_class",
                        target=f"net:{net}",
                        params={"name": _NET_CLASS_BY_PROTOCOL.get(impedance["bus_type"], "HIGH_SPEED")},
                        origin="calculated",
                        evidence_ids=evidence,
                    ),
                    _create(
                        klass="impedance",
                        target=f"net:{net}",
                        params={
                            "target": {"value": impedance["target_ohms"], "unit": "ohm"},
                            "tolerance": {"value": impedance["tolerance_pct"], "unit": "%"},
                            "mode": impedance["type"],
                        },
                        origin="calculated",
                        evidence_ids=evidence,
                    ),
                )
            )
    for group in analysis["length_groups"]:
        nets = sorted(set(group["nets"]))
        if not nets:
            continue
        evidence = _evidence_for(evidence_by_subject, *(f"net:{net}" for net in nets))
        compiled.append(
            _create(
                klass="length",
                target=f"net_class:{_NET_CLASS_BY_PROTOCOL.get(group['bus_type'], 'HIGH_SPEED')}",
                params={
                    "nets": nets,
                    "match_tolerance": {"value": group["tolerance_mm"], "unit": "mm"},
                },
                origin="calculated",
                evidence_ids=evidence,
            )
        )
    return compiled, tuple(analysis["unverified_constraints"])


def _named_net_constraints(
    components: Sequence[ComponentDef],
    evidence_by_subject: Mapping[str, Sequence[str]],
) -> list[PcbConstraint]:
    nets = sorted({net for component in components for net in component.pin_nets.values() if net})
    compiled: list[PcbConstraint] = []
    for net in nets:
        upper = net.upper()
        if re.search(r"(?:^|_)S(?:DA|CL)(?:_|$)", upper):
            name = "I2C"
        elif any(token in upper for token in ("XTAL", "OSC_IN", "OSC_OUT")):
            name = "CRYSTAL"
        elif any(token in upper for token in ("ANALOG", "SENSE", "VSNS", "ADC_IN")):
            name = "ANALOG_SENSE"
        elif upper in {"SW", "PH", "LX"} or upper.endswith(("_SW", "_PH", "_LX")):
            name = "SWITCH_NODE"
        else:
            continue
        evidence = _evidence_for(evidence_by_subject, f"net:{net}")
        compiled.append(
            _create(
                klass="net_class",
                target=f"net:{net}",
                params={"name": name},
                origin="calculated",
                evidence_ids=evidence,
            )
        )
        if name in {"CRYSTAL", "SWITCH_NODE"}:
            compiled.append(
                _create(
                    klass="length" if name == "CRYSTAL" else "keepout",
                    target=f"net:{net}",
                    params=(
                        {"maximum": {"value": 15.0, "unit": "mm"}}
                        if name == "CRYSTAL"
                        else {"copper_exclusion": {"value": 1.0, "unit": "mm"}}
                    ),
                    origin="calculated",
                    evidence_ids=evidence,
                )
            )
    return compiled


def _explicit_constraint(raw: Mapping[str, Any], default_origin: str) -> PcbConstraint:
    klass = str(raw.get("klass") or raw.get("kind") or "").strip().lower()
    if klass == "length_match":
        klass = "length"
    origin = str(raw.get("origin") or default_origin).strip().lower()
    params = raw.get("params")
    if not isinstance(params, Mapping):
        reserved = {"id", "klass", "kind", "target", "origin", "evidence_ids", "conflicts"}
        params = {key: value for key, value in raw.items() if key not in reserved}
    return _create(
        klass=klass,
        target=str(raw.get("target") or "").strip(),
        params=params,
        origin=origin,
        evidence_ids=tuple(raw.get("evidence_ids") or ()),
    )


def _mark_conflicts(
    constraints: Iterable[PcbConstraint],
    unverified_requirements: tuple[dict[str, Any], ...] = (),
) -> ConstraintCompilation:
    by_id: dict[str, PcbConstraint] = {}
    for constraint in constraints:
        current = by_id.get(constraint.id)
        if current is None:
            by_id[constraint.id] = constraint
        else:
            by_id[constraint.id] = replace(
                current,
                evidence_ids=tuple(sorted(set(current.evidence_ids) | set(constraint.evidence_ids))),
            )
    rows = list(by_id.values())
    conflicts: dict[str, set[str]] = {item.id: set() for item in rows}
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if _constraints_conflict(left, right):
                conflicts[left.id].add(right.id)
                conflicts[right.id].add(left.id)
    marked = tuple(
        replace(item, conflicts=tuple(sorted(conflicts[item.id])))
        for item in sorted(rows, key=lambda row: row.id)
    )
    conflict_ids = tuple(sorted(item.id for item in marked if item.conflicts))
    return ConstraintCompilation(marked, conflict_ids, unverified_requirements)


def _constraints_conflict(left: PcbConstraint, right: PcbConstraint) -> bool:
    if left.klass != right.klass or left.target != right.target or left.params == right.params:
        return False
    if left.klass in {"width", "clearance", "via"}:
        fab = left if left.origin == "fab_profile" else right if right.origin == "fab_profile" else None
        proposed = right if fab is left else left if fab is right else None
        if fab is None or proposed is None:
            return False
        fields = (
            ("minimum",)
            if left.klass in {"width", "clearance"}
            else ("diameter_min", "drill_min")
        )
        return any(
            (candidate := _param_number(proposed, field)) is not None
            and (floor := _param_number(fab, field)) is not None
            and candidate < floor
            for field in fields
        )
    if left.klass in {"length", "keepout"}:
        return False
    return True


def compile_pcb_constraints(
    design_ir: DesignIR,
    *,
    components: Sequence[ComponentDef] = (),
    fab_profile: str,
    fab_profile_evidence_id: str,
    evidence_by_subject: Mapping[str, Sequence[str]] | None = None,
    manufacturer_constraints: Iterable[Mapping[str, Any]] = (),
    user_constraints: Iterable[Mapping[str, Any]] = (),
) -> ConstraintCompilation:
    """Compile every origin and flag incompatible records before board mutation."""

    evidence = evidence_by_subject or {}
    constraints: list[PcbConstraint] = _fab_constraints(fab_profile, fab_profile_evidence_id)
    constraints.extend(item for domain in design_ir.power_domains for item in _power_constraints(domain))
    si_constraints, unverified_requirements = _si_constraints(components, evidence)
    constraints.extend(si_constraints)
    constraints.extend(_named_net_constraints(components, evidence))
    constraints.extend(_explicit_constraint(item, "user") for item in design_ir.pcb_constraints)
    constraints.extend(_explicit_constraint(item, "manufacturer") for item in manufacturer_constraints)
    constraints.extend(_explicit_constraint(item, "user") for item in user_constraints)
    return _mark_conflicts(constraints, unverified_requirements)


def _param_number(constraint: PcbConstraint, name: str) -> float | None:
    value = constraint.params.get(name)
    if isinstance(value, Mapping) and isinstance(value.get("value"), (int, float)):
        return float(value["value"])
    return None


def render_kicad_dru_report(compilation: ConstraintCompilation) -> dict[str, object]:
    """Render DRU text and account for every requested constraint ID."""

    constraints = compilation.require_ready()
    lines = ["(version 1)"]
    emitted_ids: list[str] = []
    unemitted: list[dict[str, str]] = []
    for item in constraints:
        if ":" not in item.target:
            unemitted.append({"id": item.id, "klass": item.klass, "reason": "unsupported_target_format"})
            continue
        condition_target = item.target.split(":", 1)[1]
        if item.target.startswith("net:"):
            condition = f"A.NetName == '{condition_target}'"
        elif item.target.startswith("net_class:"):
            condition = f"A.NetClass == '{condition_target}'"
        else:
            condition = f"A.Reference == '{condition_target}'"
        rendered: list[str] = []
        if item.klass == "width" and (value := _param_number(item, "minimum")) is not None:
            rendered.append(f"  (constraint track_width (min {value:g}mm))")
        elif item.klass == "clearance" and (value := _param_number(item, "minimum")) is not None:
            rendered.append(f"  (constraint clearance (min {value:g}mm))")
        elif item.klass == "via":
            diameter = _param_number(item, "diameter_min")
            drill = _param_number(item, "drill_min")
            if diameter is not None:
                rendered.append(f"  (constraint via_diameter (min {diameter:g}mm))")
            if drill is not None:
                rendered.append(f"  (constraint hole_size (min {drill:g}mm))")
        elif (
            item.klass == "length"
            and "match_tolerance" not in item.params
            and (value := _param_number(item, "maximum")) is not None
        ):
            rendered.append(f"  (constraint length (max {value:g}mm))")
        elif item.klass == "keepout":
            rendered.append("  (constraint disallow track via zone)")
        if not rendered:
            if item.klass == "length" and "match_tolerance" in item.params:
                reason = "length_match_tolerance_not_supported_by_kicad_dru_renderer"
            elif item.klass == "length":
                reason = "length_rule_requires_renderable_maximum"
            else:
                reason = f"constraint_class_{item.klass}_not_supported_by_kicad_dru_renderer"
            unemitted.append({"id": item.id, "klass": item.klass, "reason": reason})
            continue
        lines.extend((f'(rule "{item.id}"', f'  (condition "{condition}")', *rendered, ")"))
        emitted_ids.append(item.id)
    requested_ids = [item.id for item in constraints]
    unemitted_ids = [item["id"] for item in unemitted]
    lines.append(f"# circuit-weaver-constraints {json.dumps(requested_ids)}")
    lines.append("# circuit-weaver-constraints-note requested intent only; not every item is enforced")
    lines.append(f"# circuit-weaver-rendered-constraints {json.dumps(emitted_ids)}")
    lines.append(f"# circuit-weaver-unrendered-constraints {json.dumps(unemitted_ids)}")
    return {
        "status": "complete" if not unemitted else "partial",
        "requested_ids": [item.id for item in constraints],
        "emitted_ids": emitted_ids,
        "unemitted_ids": [item["id"] for item in unemitted],
        "unemitted": unemitted,
        "rule_count": len(emitted_ids),
        "text": "\n".join(lines) + "\n",
    }


def render_kicad_dru(compilation: ConstraintCompilation) -> str:
    """Render the KiCad rules file; use render_kicad_dru_report for accounting."""

    return str(render_kicad_dru_report(compilation)["text"])
