"""Canonical design IR and normalization helpers for the schematic MVP flow.

The MVP keeps YAML/JSON design specs as the authoring format, but all mutation,
validation, and artifact generation operate on a normalized intermediate
representation. KiCad output remains derived from this IR.
"""

from __future__ import annotations

import copy
import math
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any

_METADATA_KEYS = (
    "project",
    "company",
    "version",
    "description",
    "spec_version",
)
_CANONICAL_KEYS = {
    "blocks",
    "interfaces",
    "power_domains",
    "approved_overrides",
    "pcb_constraints",
}
_BLOCK_KINDS = {"template", "component"}
_INTERFACE_DIRECTIONS = {"input", "output", "bidirectional", "passive"}
_POWER_DIRECTIONS = {"source", "load", "bidirectional"}
_OVERRIDE_KINDS = {
    "annotation_append",
    "footprint_override",
    "part_binding",
    "presentation_group",
    "support_passives",
}
_PCB_CONSTRAINT_KINDS = {
    "approved_substitution",
    "diff_pair",
    "keepout",
    "length_match",
    "net_class",
    "placement",
    "route_channel",
}
_BLOCK_RESERVED_KEYS = {
    "id",
    "section",
    "kind",
    "type",
    "template_type",
    "ic",
    "ref",
    "params",
    "value",
    "description",
    "mpn",
    "required_support",
    "part_bindings",
    "presentation_group",
    "interfaces",
}


def _stable_token(*parts: Any) -> str:
    text = "|".join(str(part or "") for part in parts)
    return f"{zlib.crc32(text.encode('utf-8')) & 0xFFFFFFFF:08x}"


_PRESENTATION_PROFILES = {"default", "review"}


def _normalize_metadata(spec: dict[str, Any]) -> dict[str, Any]:
    profile = str(spec.get("presentation_profile", "default")).strip().lower()
    if profile not in _PRESENTATION_PROFILES:
        valid = ", ".join(sorted(_PRESENTATION_PROFILES))
        raise ValueError(f"Unknown presentation_profile '{profile}'. Expected one of: {valid}")
    result = {
        "project": str(spec.get("project", "project")),
        "company": str(spec.get("company", "")),
        "version": str(spec.get("version", "")),
        "description": str(spec.get("description", "")),
        "spec_version": str(spec.get("spec_version", "mvp_ir_v1")),
    }
    if profile != "default":
        result["presentation_profile"] = profile
    return result


@dataclass
class DesignInterface:
    block_id: str
    name: str
    direction: str = "bidirectional"
    description: str = ""

    def normalized(self) -> "DesignInterface":
        direction = (self.direction or "bidirectional").strip().lower()
        if direction not in _INTERFACE_DIRECTIONS:
            valid = ", ".join(sorted(_INTERFACE_DIRECTIONS))
            raise ValueError(f"Unknown interface direction '{self.direction}'. Expected one of: {valid}")
        return DesignInterface(
            block_id=str(self.block_id or "").strip(),
            name=str(self.name or "").strip(),
            direction=direction,
            description=str(self.description or "").strip(),
        )


@dataclass
class PowerDomain:
    """A declared rail and its optional operating envelope.

    A field is only populated when supplied by an authoritative input.  This
    makes unknown limits representable and prevents a nominal rail label from
    being mistaken for a component limit.
    """

    net: str
    v_min: float | None = None
    v_nominal: float | None = None
    v_max: float | None = None
    direction: str | None = None
    i_peak_ma: float | None = None
    i_steady_ma: float | None = None
    sequence_order: int | None = None
    sequence_dependency: str | None = None
    tolerance: float | None = None
    evidence_id: str | None = None

    def normalized(self) -> "PowerDomain":
        direction = str(self.direction or "").strip().lower() or None
        if direction is not None and direction not in _POWER_DIRECTIONS:
            valid = ", ".join(sorted(_POWER_DIRECTIONS))
            raise ValueError(f"Unknown power direction '{self.direction}'. Expected one of: {valid}")
        net = str(self.net or "").strip()
        if not net:
            raise ValueError("Power domain net must be non-empty")
        for key in ("v_min", "v_nominal", "v_max", "i_peak_ma", "i_steady_ma", "tolerance"):
            value = getattr(self, key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Power domain {key} must be a finite number")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"Power domain {key} must be a finite number")
        if self.sequence_order is not None and (
            isinstance(self.sequence_order, bool) or not isinstance(self.sequence_order, int)
        ):
            raise ValueError("Power domain sequence_order must be a non-negative integer")
        if self.v_min is not None and self.v_max is not None and self.v_min > self.v_max:
            raise ValueError("Power domain v_min cannot exceed v_max")
        if self.v_min is not None and self.v_nominal is not None and self.v_nominal < self.v_min:
            raise ValueError("Power domain v_nominal cannot be below v_min")
        if self.v_max is not None and self.v_nominal is not None and self.v_nominal > self.v_max:
            raise ValueError("Power domain v_nominal cannot exceed v_max")
        if self.i_peak_ma is not None and self.i_steady_ma is not None and self.i_peak_ma < self.i_steady_ma:
            raise ValueError("Power domain i_peak_ma cannot be below i_steady_ma")
        for key, value in (
            ("i_peak_ma", self.i_peak_ma),
            ("i_steady_ma", self.i_steady_ma),
            ("tolerance", self.tolerance),
            ("sequence_order", self.sequence_order),
        ):
            if value is not None and value < 0:
                raise ValueError(f"Power domain {key} cannot be negative")
        return PowerDomain(
            net=net,
            v_min=self.v_min,
            v_nominal=self.v_nominal,
            v_max=self.v_max,
            direction=direction,
            i_peak_ma=self.i_peak_ma,
            i_steady_ma=self.i_steady_ma,
            sequence_order=self.sequence_order,
            sequence_dependency=str(self.sequence_dependency or "").strip() or None,
            tolerance=self.tolerance,
            evidence_id=str(self.evidence_id or "").strip() or None,
        )


@dataclass
class DesignBlock:
    id: str
    section: str
    kind: str
    ref: str = ""
    template_type: str = ""
    ic: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    value: str = ""
    description: str = ""
    mpn: str = ""
    required_support: dict[str, Any] = field(default_factory=dict)
    part_bindings: dict[str, Any] = field(default_factory=dict)
    presentation_group: str = ""
    interfaces: list[DesignInterface] = field(default_factory=list)

    def normalized(self) -> "DesignBlock":
        kind = (self.kind or "").strip().lower()
        if kind not in _BLOCK_KINDS:
            inferred = "template" if self.template_type else "component"
            kind = inferred if inferred in _BLOCK_KINDS else "component"
        params = copy.deepcopy(self.params or {})
        required_support = copy.deepcopy(self.required_support or {})
        part_bindings = copy.deepcopy(self.part_bindings or {})
        interfaces = [iface.normalized() for iface in (self.interfaces or [])]
        block_id = str(self.id or "").strip()
        if not block_id:
            primary = self.ref or self.template_type or self.ic or "block"
            block_id = f"{self.section}:{primary}:{_stable_token(self.section, kind, primary)}"
        return DesignBlock(
            id=block_id,
            section=str(self.section or "misc").strip(),
            kind=kind,
            ref=str(self.ref or "").strip(),
            template_type=str(self.template_type or "").strip(),
            ic=str(self.ic or "").strip(),
            params=params,
            value=str(self.value or "").strip(),
            description=str(self.description or "").strip(),
            mpn=str(self.mpn or "").strip(),
            required_support=required_support,
            part_bindings=part_bindings,
            presentation_group=str(self.presentation_group or "").strip(),
            interfaces=interfaces,
        )


@dataclass
class DesignIR:
    metadata: dict[str, Any] = field(default_factory=dict)
    blocks: list[DesignBlock] = field(default_factory=list)
    interfaces: list[DesignInterface] = field(default_factory=list)
    power_domains: list[PowerDomain] = field(default_factory=list)
    approved_overrides: list[dict[str, Any]] = field(default_factory=list)
    pcb_constraints: list[dict[str, Any]] = field(default_factory=list)
    operating_cases: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return design_ir_to_spec(self)


def _normalize_override(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise TypeError("approved_overrides entries must be objects")
    normalized = copy.deepcopy(item)
    normalized["kind"] = str(normalized.get("kind", "")).strip().lower()
    if normalized["kind"] and normalized["kind"] not in _OVERRIDE_KINDS:
        valid = ", ".join(sorted(_OVERRIDE_KINDS))
        raise ValueError(f"Unknown approved override kind '{normalized['kind']}'. Expected one of: {valid}")
    normalized["target"] = str(normalized.get("target", "")).strip()
    return normalized


def _normalize_pcb_constraint(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise TypeError("pcb_constraints entries must be objects")
    normalized = copy.deepcopy(item)
    normalized["kind"] = str(normalized.get("kind", "")).strip().lower()
    if normalized["kind"] and normalized["kind"] not in _PCB_CONSTRAINT_KINDS:
        valid = ", ".join(sorted(_PCB_CONSTRAINT_KINDS))
        raise ValueError(f"Unknown PCB constraint kind '{normalized['kind']}'. Expected one of: {valid}")
    normalized["target"] = str(normalized.get("target", "")).strip()
    return normalized


def _normalize_interface_dict(
    iface: dict[str, Any],
    *,
    block_id: str,
) -> DesignInterface:
    return DesignInterface(
        block_id=str(iface.get("block_id") or block_id or "").strip(),
        name=str(iface.get("name", "")).strip(),
        direction=str(iface.get("direction", "bidirectional")).strip(),
        description=str(iface.get("description", "")).strip(),
    ).normalized()


def _normalize_power_domain(raw: Any) -> PowerDomain:
    """Normalize a canonical rail declaration without filling absent limits."""
    if isinstance(raw, str):
        return PowerDomain(net=raw).normalized()
    if not isinstance(raw, dict):
        raise TypeError("power_domains entries must be objects or net-name strings")
    sequence = raw.get("sequencing") if isinstance(raw.get("sequencing"), dict) else {}
    provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    return PowerDomain(
        net=str(raw.get("net") or raw.get("name") or ""),
        v_min=raw.get("v_min"),
        v_nominal=raw.get("v_nominal", raw.get("voltage")),
        v_max=raw.get("v_max"),
        direction=raw.get("direction"),
        i_peak_ma=raw.get("i_peak_ma", raw.get("max_current_ma")),
        i_steady_ma=raw.get("i_steady_ma"),
        sequence_order=raw.get("sequence_order", sequence.get("order")),
        sequence_dependency=raw.get("sequence_dependency", sequence.get("dependency")),
        tolerance=raw.get("tolerance"),
        evidence_id=raw.get("evidence_id", provenance.get("evidence_id")),
    ).normalized()


def _power_domain_to_spec(domain: PowerDomain) -> dict[str, Any]:
    """Serialize a rail while preserving absence instead of writing nulls."""
    return {key: value for key, value in asdict(domain.normalized()).items() if value is not None}


def _normalize_block(raw: dict[str, Any], *, default_section: str, index: int) -> DesignBlock:
    if not isinstance(raw, dict):
        raise TypeError("blocks entries must be objects")

    kind = str(raw.get("kind", "") or ("template" if raw.get("type") else "component")).strip()
    section = str(raw.get("section", default_section)).strip() or default_section
    template_type = str(raw.get("template_type") or raw.get("type") or "").strip()
    ic = str(raw.get("ic") or raw.get("mpn") or "").strip()
    block_id = str(raw.get("id", "")).strip()
    ref = str(raw.get("ref", "")).strip()
    params = copy.deepcopy(raw.get("params") or {})
    if kind == "template":
        for key, value in raw.items():
            if key not in _BLOCK_RESERVED_KEYS:
                params.setdefault(key, copy.deepcopy(value))
    else:
        # Sprint 41 — component blocks also carry surgical pin overrides
        # (pin_map, pin_nets_extra, power_pins_extra, no_connects,
        # pinout_verified). Stash any of these in params so they round-
        # trip through the canonical IR and reach _resolve_component.
        _COMPONENT_PIN_OVERRIDES = (
            "pin_map",
            "pin_nets_extra",
            "power_pins_extra",
            "no_connects",
            "pinout_verified",
            "power_map",
            "power_reqs",
            "power_pin_defs",
            "dropout_voltage",
            "feedback_vref",
            "feedback_vref_voltage",
            "feedback_vref_provenance",
            "feedback_vref_evidence_id",
            "lcsc",
        )
        for key in _COMPONENT_PIN_OVERRIDES:
            if key in raw and key not in params:
                params[key] = copy.deepcopy(raw[key])
    interfaces = [
        _normalize_interface_dict(iface, block_id=block_id or ref) for iface in raw.get("interfaces", []) or []
    ]
    if not block_id:
        primary = ref or template_type or ic or f"block{index + 1}"
        block_id = f"{section}:{primary}:{_stable_token(section, kind, primary, index)}"
    interfaces = [
        DesignInterface(
            block_id=iface.block_id or block_id,
            name=iface.name,
            direction=iface.direction,
            description=iface.description,
        ).normalized()
        for iface in interfaces
    ]
    return DesignBlock(
        id=block_id,
        section=section,
        kind=kind,
        ref=ref,
        template_type=template_type,
        ic=ic,
        params=params,
        value=str(raw.get("value", "")).strip(),
        description=str(raw.get("description", "")).strip(),
        mpn=str(raw.get("mpn", "")).strip(),
        required_support=copy.deepcopy(raw.get("required_support") or {}),
        part_bindings=copy.deepcopy(raw.get("part_bindings") or {}),
        presentation_group=str(raw.get("presentation_group", "")).strip(),
        interfaces=interfaces,
    ).normalized()


def _normalize_legacy_blocks(spec: dict[str, Any]) -> list[DesignBlock]:
    blocks: list[DesignBlock] = []
    for section_key, items in spec.items():
        if section_key in _METADATA_KEYS or section_key in _CANONICAL_KEYS:
            continue
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            blocks.append(_normalize_block(item, default_section=section_key, index=len(blocks) + idx))
    return blocks


def normalize_design_spec(spec: dict[str, Any]) -> DesignIR:
    """Normalize a legacy or canonical design spec into the canonical IR."""
    if not isinstance(spec, dict):
        raise TypeError("Design spec must be a mapping")

    # Convert wizard-generated specs to engine format if needed
    from .preparser import extract_engine_spec

    operating_cases = copy.deepcopy(spec.get("operating_cases"))
    spec = extract_engine_spec(spec)
    if operating_cases is None:
        operating_cases = copy.deepcopy(spec.get("operating_cases"))

    metadata = _normalize_metadata(spec)
    if isinstance(spec.get("blocks"), list):
        blocks = [
            _normalize_block(block, default_section=str(block.get("section", "misc")), index=idx)
            for idx, block in enumerate(spec.get("blocks", []))
            if isinstance(block, dict)
        ]
    else:
        blocks = _normalize_legacy_blocks(spec)

    block_key_map = {block.ref: block.id for block in blocks if block.ref}

    top_interfaces = []
    for raw in spec.get("interfaces", []) or []:
        if not isinstance(raw, dict):
            continue
        raw_block_id = str(raw.get("block_id") or raw.get("ref") or "").strip()
        resolved_block_id = block_key_map.get(raw_block_id, raw_block_id)
        top_interfaces.append(_normalize_interface_dict(raw, block_id=resolved_block_id))

    merged_blocks = []
    for block in blocks:
        merged = list(block.interfaces)
        for iface in top_interfaces:
            if iface.block_id != block.id:
                continue
            if any(existing.name == iface.name for existing in merged):
                continue
            merged.append(iface)
        merged_blocks.append(
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
                required_support=copy.deepcopy(block.required_support),
                part_bindings=copy.deepcopy(block.part_bindings),
                presentation_group=block.presentation_group,
                interfaces=merged,
            ).normalized()
        )

    all_interfaces = []
    for block in merged_blocks:
        all_interfaces.extend(block.interfaces)

    return DesignIR(
        metadata=metadata,
        blocks=merged_blocks,
        interfaces=all_interfaces,
        power_domains=[_normalize_power_domain(item) for item in (spec.get("power_domains") or [])],
        approved_overrides=[
            _normalize_override(item) for item in (spec.get("approved_overrides") or []) if isinstance(item, dict)
        ],
        pcb_constraints=[
            _normalize_pcb_constraint(item) for item in (spec.get("pcb_constraints") or []) if isinstance(item, dict)
        ],
        operating_cases=operating_cases,
    )


def design_ir_to_spec(ir: DesignIR) -> dict[str, Any]:
    """Convert the canonical IR into a canonical YAML/JSON-serializable spec."""
    metadata = _normalize_metadata(ir.metadata or {})
    blocks = []
    interfaces = []
    for block in ir.blocks:
        item = {
            "id": block.id,
            "section": block.section,
            "kind": block.kind,
            "ref": block.ref,
            "required_support": copy.deepcopy(block.required_support),
            "part_bindings": copy.deepcopy(block.part_bindings),
        }
        if block.kind == "template":
            item["type"] = block.template_type
            if block.params:
                item["params"] = copy.deepcopy(block.params)
        else:
            item["ic"] = block.ic
        if block.value:
            item["value"] = block.value
        if block.description:
            item["description"] = block.description
        if block.mpn:
            item["mpn"] = block.mpn
        if block.presentation_group:
            item["presentation_group"] = block.presentation_group
        if block.interfaces:
            item["interfaces"] = [asdict(iface) for iface in block.interfaces]
            interfaces.extend(asdict(iface) for iface in block.interfaces)
        blocks.append(item)

    spec = dict(metadata)
    spec["blocks"] = blocks
    spec["interfaces"] = interfaces
    spec["power_domains"] = [_power_domain_to_spec(domain) for domain in ir.power_domains]
    spec["approved_overrides"] = [copy.deepcopy(item) for item in ir.approved_overrides]
    spec["pcb_constraints"] = [copy.deepcopy(item) for item in ir.pcb_constraints]
    if ir.operating_cases is not None:
        spec["operating_cases"] = copy.deepcopy(ir.operating_cases)
    return spec


def design_ir_to_engine_spec(ir: DesignIR) -> dict[str, Any]:
    """Convert DesignIR into the legacy section-grouped engine input spec."""
    spec = {
        "project": ir.metadata.get("project", "project"),
        "company": ir.metadata.get("company", ""),
        "version": ir.metadata.get("version", ""),
        "description": ir.metadata.get("description", ""),
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for block in ir.blocks:
        item: dict[str, Any]
        if block.kind == "template":
            item = copy.deepcopy(block.params)
            item["type"] = block.template_type
            # Preserve the explicit ``ic`` selection so templates that
            # support multiple IC variants (e.g. usb_controller's
            # CH340G / CYUSB3014 / FT232) honor the user's choice
            # instead of falling back to the template default.
            if block.ic:
                item["ic"] = block.ic
        else:
            item = {"ic": block.ic}
            # Sprint 41 — propagate surgical pin overrides captured at
            # normalization time into the engine spec so _resolve_component
            # can honor them.
            if block.params:
                item.update(copy.deepcopy(block.params))
        if block.ref:
            item["ref"] = block.ref
        if block.value:
            item["value"] = block.value
        if block.description:
            item["description"] = block.description
        if block.mpn:
            item["mpn"] = block.mpn
        grouped.setdefault(block.section, []).append(item)
    spec.update(grouped)
    # Retain typed electrical intent for template consumers and downstream
    # validators; project-spec resolution explicitly treats this as metadata.
    if ir.power_domains:
        spec["power_domains"] = [_power_domain_to_spec(domain) for domain in ir.power_domains]
    if ir.operating_cases is not None:
        spec["operating_cases"] = copy.deepcopy(ir.operating_cases)
    return spec


def semantic_diff(old_spec: dict[str, Any], new_spec: dict[str, Any]) -> dict[str, Any]:
    """Return a semantic IR-level diff between two design specs."""
    old_ir = normalize_design_spec(old_spec)
    new_ir = normalize_design_spec(new_spec)

    old_blocks = {block.id: block for block in old_ir.blocks}
    new_blocks = {block.id: block for block in new_ir.blocks}
    old_keys = set(old_blocks)
    new_keys = set(new_blocks)

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = []
    for block_id in sorted(old_keys & new_keys):
        before = old_blocks[block_id]
        after = new_blocks[block_id]
        before_dict = asdict(before)
        after_dict = asdict(after)
        changed_fields = sorted(
            key for key in after_dict.keys() | before_dict.keys() if before_dict.get(key) != after_dict.get(key)
        )
        if changed_fields:
            changed.append(
                {
                    "block_id": block_id,
                    "ref": after.ref or before.ref,
                    "kind": after.kind,
                    "fields": changed_fields,
                }
            )

    metadata_changes = sorted(
        key
        for key in (old_ir.metadata.keys() | new_ir.metadata.keys())
        if old_ir.metadata.get(key) != new_ir.metadata.get(key)
    )

    old_interfaces = {(iface.block_id, iface.name, iface.direction) for iface in old_ir.interfaces}
    new_interfaces = {(iface.block_id, iface.name, iface.direction) for iface in new_ir.interfaces}

    diff = {
        "metadata_changed": metadata_changes,
        "added_blocks": added,
        "removed_blocks": removed,
        "changed_blocks": changed,
        "added_interfaces": sorted(new_interfaces - old_interfaces),
        "removed_interfaces": sorted(old_interfaces - new_interfaces),
        "override_count_delta": len(new_ir.approved_overrides) - len(old_ir.approved_overrides),
        "pcb_constraint_count_delta": len(new_ir.pcb_constraints) - len(old_ir.pcb_constraints),
        "summary": {
            "added_blocks": len(added),
            "removed_blocks": len(removed),
            "changed_blocks": len(changed),
            "metadata_changes": len(metadata_changes),
            "added_interfaces": len(new_interfaces - old_interfaces),
            "removed_interfaces": len(old_interfaces - new_interfaces),
        },
    }
    if old_ir.operating_cases is not None or new_ir.operating_cases is not None:
        diff["operating_cases_changed"] = old_ir.operating_cases != new_ir.operating_cases
        diff["summary"]["operating_case_changes"] = int(diff["operating_cases_changed"])
    return diff
