"""Design IR compilation — spec normalization, component resolution, validation.

Extracted from ``dispatcher.py`` (Sprint 44 T187 refactor). Houses the
design-loading pipeline: normalizes a design spec into a canonical IR,
resolves components through the template registry, runs Sprint 41 generational
repair, and hydrates block attributes / part bindings / shared-net interfaces.

Usage:
    from .design_loader import compile_design_ir
    result = compile_design_ir(spec)
    # result is a CompiledDesign with ir, components, metadata, ...
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from .component_db import ComponentDef, PresentationWiringPolicy, auto_generate_bypass_caps
from .design_ir import (
    DesignBlock,
    DesignInterface,
    DesignIR,
    design_ir_to_engine_spec,
    normalize_design_spec,
)
from .project_spec import resolve_project_spec
from .subcircuits.base import BoundaryPort

_logger = logging.getLogger(__name__)

_STANDARD_PROFILE = "standard"
_POWER_NET_PREFIXES = (
    "GND",
    "AGND",
    "DGND",
    "PGND",
    "VDD",
    "VCC",
    "VBAT",
    "VBUS",
    "VIN",
    "VDDA",
    "VSS",
    "MGT",
    "VCCO",
)


@dataclass
class CompiledDesign:
    """A fully resolved, validated design ready for generation.

    Attributes:
        ir: Normalized design intermediate representation.
        components: List of resolved component definitions.
        metadata: Design metadata (project, company, version, description).
        engine_spec: Engine-ready spec dictionary.
        repair_actions: List of auto-repair actions taken, if any.
    """

    ir: DesignIR
    components: list[ComponentDef]
    metadata: dict[str, Any]
    engine_spec: dict[str, Any]
    repair_actions: list[dict[str, Any]] = field(default_factory=list)


def _ensure_profile(profile: str) -> str:
    normalized = (profile or _STANDARD_PROFILE).strip().lower()
    if normalized != _STANDARD_PROFILE:
        raise ValueError(f"Unsupported MVP validation profile '{profile}'")
    return normalized


def _is_power_net(name: str) -> bool:
    net = str(name or "").upper()
    return any(net == prefix or net.startswith(f"{prefix}_") for prefix in _POWER_NET_PREFIXES)


def _block_primary_key(block: DesignBlock) -> str:
    return block.ref or block.id


def _group_components_by_block_key(components: list[ComponentDef]) -> dict[str, list[ComponentDef]]:
    groups: dict[str, list[ComponentDef]] = {}
    for comp in components:
        key = comp.source_ref or ""
        if not key:
            continue
        groups.setdefault(key, []).append(comp)
    return groups


def _derive_required_support(group: list[ComponentDef]) -> dict[str, Any]:
    return {
        "bypass_caps": sum(len(c.bypass_caps) for c in group),
        "straps": sum(len(c.straps) for c in group),
        "power_reqs": sum(len(c.power_reqs) for c in group),
        "boundary_ports": sum(len(c.template_boundary_ports) for c in group),
        "local_wires": sum(len(c.template_local_wires) for c in group),
    }


def _derive_part_bindings(primary: ComponentDef) -> dict[str, Any]:
    return {
        "mpn": primary.source_mpn or primary.mpn,
        "value": primary.source_value or primary.value,
        "footprint": primary.footprint,
        "manufacturer": primary.source_manufacturer,
    }


def _apply_block_attributes(ir: DesignIR, components: list[ComponentDef]) -> None:
    groups = _group_components_by_block_key(components)
    for block in ir.blocks:
        group = groups.get(_block_primary_key(block))
        if not group:
            continue
        # Preserve canonical ownership on every resolved component produced by
        # a block.  The sheet allocator, assembly manifest, and placement
        # review pipeline all consume these fields; keeping them only in the
        # DesignIR caused small designs to collapse back into one generic
        # sheet and detached generated support parts from their owner section.
        for comp in group:
            comp.functional_section = block.section
            comp.block_id = block.id
        primary = group[0]
        if block.interfaces:
            primary.template_boundary_ports = [BoundaryPort(iface.name, iface.direction) for iface in block.interfaces]
        if block.presentation_group:
            for comp in group:
                comp.presentation_group = block.presentation_group
        if block.part_bindings:
            footprint = str(block.part_bindings.get("footprint", "")).strip()
            mpn = str(block.part_bindings.get("mpn", "")).strip()
            value = str(block.part_bindings.get("value", "")).strip()
            if footprint:
                primary.footprint = footprint
            if mpn:
                primary.source_mpn = mpn
                primary.mpn = mpn
            if value:
                primary.value = value
                primary.source_value = value


def _apply_approved_overrides(ir: DesignIR, components: list[ComponentDef]) -> None:
    groups = _group_components_by_block_key(components)
    for override in ir.approved_overrides:
        target = str(override.get("target", "")).strip()
        kind = str(override.get("kind", "")).strip().lower()
        value = override.get("value")
        group = groups.get(target)
        if not group:
            continue
        primary = group[0]
        if kind == "annotation_append" and value:
            primary.annotations.append(str(value))
        elif kind == "footprint_override" and value:
            primary.footprint = str(value)
        elif kind == "presentation_group" and value:
            for comp in group:
                comp.presentation_group = str(value)
        elif kind == "part_binding" and isinstance(value, dict):
            for attr, key in [
                ("footprint", "footprint"),
                ("source_mpn", "mpn"),
                ("mpn", "mpn"),
                ("value", "value"),
                ("source_value", "value"),
            ]:
                v = str(value.get(key, "")).strip()
                if v:
                    setattr(primary, attr, v)
        elif kind == "support_passives" and value:
            primary.presentation_wiring_policy = PresentationWiringPolicy(support_passives=str(value))


def _synthesize_shared_net_interfaces(ir: DesignIR, components: list[ComponentDef]) -> None:
    """Auto-declare interfaces on non-power signal nets shared across blocks.

    The MVP validator requires every block that touches a non-power
    signal net shared across blocks to declare an interface for that
    net. This pass walks every non-power signal net that appears on
    two or more blocks and appends a ``DesignInterface`` to each
    participating block's ``interfaces`` list.
    """
    groups = _group_components_by_block_key(components)

    net_contributors: dict[str, dict[str, str]] = {}
    for block in ir.blocks:
        block_key = _block_primary_key(block)
        for comp in groups.get(block_key, []):
            pin_type_by_num = {pin.number: pin.electrical_type for pin in comp.pins}
            for pin_num, net in comp.pin_nets.items():
                if not net or _is_power_net(net):
                    continue
                etype = pin_type_by_num.get(pin_num, "bidirectional")
                direction = (
                    "output"
                    if etype == "output"
                    else "input"
                    if etype == "input"
                    else "passive"
                    if etype in ("power_in", "power_out")
                    else "bidirectional"
                )
                existing = net_contributors.setdefault(net, {}).get(block_key)
                if existing is None:
                    net_contributors[net][block_key] = direction
                elif existing != direction:
                    net_contributors[net][block_key] = "bidirectional"

    block_by_key = {_block_primary_key(block): block for block in ir.blocks}
    for net, contributors in net_contributors.items():
        if len(contributors) < 2:
            continue
        for block_key, direction in contributors.items():
            block = block_by_key.get(block_key)
            if block is None:
                continue
            if any(iface.name == net for iface in block.interfaces):
                continue
            block.interfaces.append(DesignInterface(block_id=block.id, name=net, direction=direction).normalized())


def _hydrate_ir_from_components(ir: DesignIR, components: list[ComponentDef]) -> DesignIR:
    groups = _group_components_by_block_key(components)
    hydrated_blocks = []
    for block in ir.blocks:
        group = groups.get(_block_primary_key(block))
        interfaces = list(block.interfaces)
        required_support = copy.deepcopy(block.required_support or {})
        part_bindings = copy.deepcopy(block.part_bindings or {})
        if group:
            primary = group[0]
            if not interfaces and primary.template_boundary_ports:
                interfaces = [
                    DesignInterface(
                        block_id=block.id,
                        name=port.name,
                        direction=port.direction,
                    ).normalized()
                    for port in primary.template_boundary_ports
                ]
            if not required_support:
                required_support = _derive_required_support(group)
            if not part_bindings:
                part_bindings = _derive_part_bindings(primary)
        hydrated_blocks.append(
            DesignBlock(
                id=block.id,
                section=block.section,
                kind=block.kind,
                ref=block.ref,
                template_type=block.template_type,
                ic=block.ic,
                params=copy.deepcopy(block.params),
                value=block.value,
                description=block.description,
                mpn=block.mpn,
                required_support=required_support,
                part_bindings=part_bindings,
                presentation_group=block.presentation_group,
                interfaces=interfaces,
            ).normalized()
        )

    all_interfaces = []
    for block in hydrated_blocks:
        all_interfaces.extend(block.interfaces)

    return DesignIR(
        metadata=copy.deepcopy(ir.metadata),
        blocks=hydrated_blocks,
        interfaces=all_interfaces,
        power_domains=copy.deepcopy(ir.power_domains),
        approved_overrides=copy.deepcopy(ir.approved_overrides),
        pcb_constraints=copy.deepcopy(ir.pcb_constraints),
        operating_cases=copy.deepcopy(ir.operating_cases),
    )


def compile_design_ir(
    spec: dict[str, Any],
    *,
    enrich_parts: bool = False,
    use_kicad: bool = True,
) -> CompiledDesign:
    """Compile a design spec into normalized IR + resolved engine components.

    Applies Sprint 41 generational repairs in the middle of the pipeline:
    resolve components once so bus participants are visible, call
    ``generational_repair.auto_repair_design`` to synthesize missing
    conditioning blocks (e.g. I2C pull-ups), then re-resolve if any
    synthetic blocks were added. Users opt out via ``auto_repair: false``
    at the top of the spec.
    """
    from .generational_repair import apply_component_repairs, auto_repair_design

    ir = normalize_design_spec(spec)
    original_block_count = len(ir.blocks)
    engine_spec = design_ir_to_engine_spec(ir)
    components, metadata = resolve_project_spec(engine_spec, enrich_parts=enrich_parts, use_kicad=use_kicad)
    components = copy.deepcopy(components)

    repair_enabled = bool(spec.get("auto_repair", True))
    repaired_ir, component_repairs, repair_actions = auto_repair_design(ir, components, enabled=repair_enabled)
    repair_records = [action.to_dict() for action in repair_actions]
    if repair_actions:
        ir = repaired_ir
        engine_spec = design_ir_to_engine_spec(ir)
        if len(repaired_ir.blocks) != original_block_count:
            components, metadata = resolve_project_spec(engine_spec, enrich_parts=enrich_parts, use_kicad=use_kicad)
            components = copy.deepcopy(components)
        apply_component_repairs(components, component_repairs)

    _apply_block_attributes(ir, components)
    _apply_approved_overrides(ir, components)
    _synthesize_shared_net_interfaces(ir, components)
    auto_generate_bypass_caps(components)
    hydrated_ir = _hydrate_ir_from_components(ir, components)
    metadata.update(
        {
            "project": hydrated_ir.metadata.get("project", metadata.get("project", "project")),
            "company": hydrated_ir.metadata.get("company", metadata.get("company", "")),
            "version": hydrated_ir.metadata.get("version", metadata.get("version", "")),
            "description": hydrated_ir.metadata.get("description", metadata.get("description", "")),
        }
    )
    return CompiledDesign(
        ir=hydrated_ir,
        components=components,
        metadata=metadata,
        engine_spec=engine_spec,
        repair_actions=repair_records,
    )
