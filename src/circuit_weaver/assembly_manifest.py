"""Canonical, exhaustive assembly-part manifest.

The schematic renderer historically assigned references to generated support
parts during page layout.  BOM and CPL exporters then recompiled only the
primary components, silently omitting bypass capacitors and straps.  This
module establishes one deterministic assembly inventory that downstream
delivery code can reconcile against a physical PCB.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .component_db import ComponentDef, auto_generate_bypass_caps
from .evidence_policy import validate_output_relative_evidence_manifest

_REFERENCE_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


class AssemblyManifestError(ValueError):
    """Raised when a deterministic, exhaustive manifest cannot be built."""


@dataclass(frozen=True)
class AssemblyItem:
    """One physical item expected in the schematic/BOM/PCB reconciliation."""

    reference: str
    value: str
    footprint: str
    mpn: str = ""
    manufacturer: str = ""
    lcsc_pn: str = ""
    source_kind: str = "component"  # component | bypass | strap
    semantic_key: str = ""
    owner_ref: str = ""
    role: str = ""
    functional_section: str = ""
    block_id: str = ""
    net1: str = ""
    net2: str = ""
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_cpl: bool = False
    evidence_ids: tuple[str, ...] = ()
    owner_pin: str = ""

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["evidence_ids"] = list(self.evidence_ids)
        return payload


@dataclass
class AssemblyManifest:
    """Stable assembly inventory shared by BOM, CPL, and delivery reporting."""

    items: list[AssemblyItem]
    auto_bypass_components: int = 0
    retired_references: list[str] = field(default_factory=list)
    evidence_ids: tuple[str, ...] = ()
    evidence_manifest: str = ""
    schema_version: int = 2
    prepared_components: list[ComponentDef] = field(default_factory=list, repr=False)
    # Full schematic render inventory. Virtual containers stay here so their
    # real support passives can be rendered, while prepared_components and
    # items describe physical assembly parts only.
    render_components: list[ComponentDef] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "item_count": len(self.items),
            "auto_bypass_components": self.auto_bypass_components,
            "retired_references": list(self.retired_references),
            "evidence_ids": list(self.evidence_ids),
            "evidence_manifest": self.evidence_manifest,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AssemblyManifest:
        raw_items = payload.get("items") or []
        if not isinstance(raw_items, list):
            raise AssemblyManifestError("Assembly manifest items must be a list")
        allowed = set(AssemblyItem.__dataclass_fields__)
        items = [
            AssemblyItem(
                **{
                    key: tuple(value) if key == "evidence_ids" and isinstance(value, list) else value
                    for key, value in raw.items()
                    if key in allowed
                }
            )
            for raw in raw_items
            if isinstance(raw, dict)
        ]
        return cls(
            items=items,
            auto_bypass_components=int(payload.get("auto_bypass_components", 0) or 0),
            retired_references=[str(ref) for ref in payload.get("retired_references", []) or []],
            evidence_ids=tuple(str(item) for item in payload.get("evidence_ids", []) or []),
            evidence_manifest=validate_output_relative_evidence_manifest(payload.get("evidence_manifest", "")),
            schema_version=int(payload.get("schema_version", 1) or 1),
        )

    def write_json(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8", newline="")
        return path

    def active_bom_items(self) -> list[AssemblyItem]:
        return [item for item in self.items if not item.dnp and not item.exclude_from_bom]

    def active_cpl_items(self) -> list[AssemblyItem]:
        return [item for item in self.items if not item.dnp and not item.exclude_from_cpl and not item.exclude_from_bom]

    def missing_footprint_refs(self) -> list[str]:
        return sorted(item.reference for item in self.active_cpl_items() if not item.footprint)


class _ReferenceAllocator:
    def __init__(self) -> None:
        self.used: set[str] = set()
        self.counters: dict[str, int] = {}

    def reserve(self, reference: str) -> None:
        ref = (reference or "").strip()
        if not ref:
            return
        if ref in self.used:
            return
        self.used.add(ref)
        match = _REFERENCE_RE.match(ref)
        if match:
            prefix, number = match.groups()
            prefix = prefix.upper()
            self.counters[prefix] = max(self.counters.get(prefix, 0), int(number))

    def allocate(self, prefix: str) -> str:
        normalized = "".join(ch for ch in (prefix or "U").upper() if ch.isalpha()) or "U"
        while True:
            self.counters[normalized] = self.counters.get(normalized, 0) + 1
            candidate = f"{normalized}{self.counters[normalized]}"
            if candidate not in self.used:
                self.used.add(candidate)
                return candidate


def _bypass_prefix(value: str, footprint: str, role: str) -> str:
    value_l = (value or "").lower()
    footprint_l = (footprint or "").lower()
    role_l = (role or "").lower()
    if role_l == "inductor" or "inductor" in footprint_l or value_l.endswith("h"):
        return "L"
    return "C"


def _semantic_key(base: str, counters: dict[str, int]) -> str:
    counters[base] = counters.get(base, 0) + 1
    return f"{base}#{counters[base]}"


def _primary_semantic_base(comp: ComponentDef) -> str:
    source_ref = str(comp.source_ref or "").strip()
    if source_ref:
        # An explicit designator is the user's durable identity even when the
        # value or footprint intentionally changes in a later revision.
        identity = f"ref:{source_ref}"
    else:
        # A design block may legitimately own several same-prefix parts.  The
        # block alone cannot distinguish (for example) its 1 k and 10 k
        # resistors, so include physical identity before assigning occurrence
        # counters.  Truly identical duplicates remain interchangeable.
        identity = "|".join(
            (
                f"block:{str(getattr(comp, 'block_id', '') or '').strip()}",
                f"mpn:{str(comp.source_mpn or comp.mpn or '').strip()}",
                f"value:{str(comp.source_value or comp.value or '').strip()}",
                f"footprint:{str(comp.footprint or '').strip()}",
                f"manufacturer:{str(comp.source_manufacturer or '').strip()}",
                f"section:{str(getattr(comp, 'functional_section', '') or '').strip()}",
            )
        )
    return f"component:{identity}"


def _coerce_previous_manifest(
    value: AssemblyManifest | dict[str, Any] | str | Path | None,
) -> AssemblyManifest | None:
    if value is None:
        return None
    if isinstance(value, AssemblyManifest):
        return value
    if isinstance(value, (str, Path)):
        path = Path(value)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssemblyManifestError(f"Cannot read previous assembly manifest {path}: {exc}") from exc
    if isinstance(value, dict):
        return AssemblyManifest.from_dict(value)
    raise TypeError("previous_manifest must be an AssemblyManifest, JSON object, path, or None")


def build_assembly_manifest(
    components: Iterable[ComponentDef],
    *,
    include_auto_bypass: bool = True,
    previous_manifest: AssemblyManifest | dict[str, Any] | str | Path | None = None,
    evidence_ids_by_reference: Mapping[str, Iterable[str]] | None = None,
    evidence_ids: Iterable[str] = (),
    evidence_manifest: str = "",
) -> AssemblyManifest:
    """Build one exhaustive manifest with stable references.

    Explicit primary references are reserved before any generated reference is
    assigned, preventing a support capacitor from stealing a later BOM ref.
    Components are deep-copied before the centralized auto-bypass policy runs,
    so manifest construction does not mutate the caller's compiled design.
    """
    prepared = copy.deepcopy(list(components))
    evidence_by_reference = {
        str(reference): tuple(str(evidence_id) for evidence_id in identifiers)
        for reference, identifiers in (evidence_ids_by_reference or {}).items()
    }
    manifest_evidence_ids = tuple(str(evidence_id) for evidence_id in evidence_ids)
    evidence_manifest = validate_output_relative_evidence_manifest(evidence_manifest)
    auto_bypass_components = auto_generate_bypass_caps(prepared) if include_auto_bypass else 0

    virtual_components = [bool(getattr(comp, "is_virtual_container", False)) for comp in prepared]
    semantic_counts: dict[str, int] = {}
    primary_semantics: list[str] = []
    support_semantics: list[list[str]] = []
    for comp in prepared:
        owner_key = _semantic_key(_primary_semantic_base(comp), semantic_counts)
        primary_semantics.append(owner_key)
        comp_support: list[str] = []
        for bypass in comp.bypass_caps:
            base = "|".join(
                (
                    "bypass",
                    owner_key,
                    str(bypass.role or ""),
                    str(bypass.pin or ""),
                    str(bypass.net or ""),
                    str(bypass.gnd_net or ""),
                    str(bypass.value or ""),
                    str(bypass.footprint or ""),
                )
            )
            comp_support.append(_semantic_key(base, semantic_counts))
        for strap in comp.straps:
            base = "|".join(
                (
                    "strap",
                    owner_key,
                    str(strap.role or ""),
                    str(strap.pin or ""),
                    str(strap.net or ""),
                    str(strap.rail or ""),
                    str(strap.value or ""),
                    str(strap.footprint or ""),
                )
            )
            comp_support.append(_semantic_key(base, semantic_counts))
        support_semantics.append(comp_support)

    previous = _coerce_previous_manifest(previous_manifest)
    previous_by_key = {
        item.semantic_key: item.reference
        for item in (previous.items if previous is not None and previous.schema_version >= 2 else [])
        if item.semantic_key
    }
    active_semantics = set(primary_semantics)
    active_semantics.update(key for values in support_semantics for key in values)
    retired_references = set(previous.retired_references if previous is not None else [])
    if previous is not None and previous.schema_version >= 2:
        retired_references.update(
            item.reference for item in previous.items if item.semantic_key and item.semantic_key not in active_semantics
        )

    allocator = _ReferenceAllocator()
    seen_explicit: set[str] = set()
    for comp in prepared:
        if comp.source_ref:
            if comp.source_ref in seen_explicit:
                raise AssemblyManifestError(f"Duplicate explicit assembly reference: {comp.source_ref}")
            seen_explicit.add(comp.source_ref)
            allocator.reserve(comp.source_ref)
    for reference in [*previous_by_key.values(), *sorted(retired_references)]:
        allocator.reserve(reference)

    primary_refs: list[str] = []
    for comp, semantic_key, is_virtual in zip(prepared, primary_semantics, virtual_components):
        if is_virtual:
            # The reference names a schematic container, not an assembly
            # primary. Keep it as the support parts' logical owner.
            reference = comp.source_ref or allocator.allocate(comp.ref_prefix)
            allocator.reserve(reference)
            primary_refs.append(reference)
            continue
        prior_ref = previous_by_key.get(semantic_key, "")
        if comp.source_ref:
            reference = comp.source_ref
        elif prior_ref and prior_ref.upper().startswith(comp.ref_prefix.upper()):
            reference = prior_ref
        else:
            reference = allocator.allocate(comp.ref_prefix)
        allocator.reserve(reference)
        primary_refs.append(reference)

    items: list[AssemblyItem] = []
    for comp_index, (comp, owner_ref, owner_key, is_virtual) in enumerate(
        zip(prepared, primary_refs, primary_semantics, virtual_components)
    ):
        # Generation consumes the prepared inventory directly. Persist every
        # allocated designator on that copy so functional-sheet reordering
        # cannot swap support-part identities relative to BOM/placement data.
        comp.source_ref = owner_ref
        if not is_virtual:
            items.append(
                AssemblyItem(
                    reference=owner_ref,
                    value=comp.source_value or comp.value or comp.mpn,
                    footprint=comp.footprint,
                    mpn=comp.source_mpn or comp.mpn,
                    manufacturer=comp.source_manufacturer,
                    lcsc_pn=comp.lcsc_pn,
                    source_kind="component",
                    semantic_key=owner_key,
                    owner_ref=owner_ref,
                    functional_section=getattr(comp, "functional_section", "") or "",
                    block_id=getattr(comp, "block_id", "") or "",
                    evidence_ids=evidence_by_reference.get(owner_ref, ()),
                )
            )

        support_index = 0
        for bypass in comp.bypass_caps:
            semantic_key = support_semantics[comp_index][support_index]
            support_index += 1
            prefix = _bypass_prefix(bypass.value, bypass.footprint, bypass.role)
            prior_ref = previous_by_key.get(semantic_key, "")
            support_ref = prior_ref if prior_ref.upper().startswith(prefix.upper()) else allocator.allocate(prefix)
            allocator.reserve(support_ref)
            setattr(bypass, "source_ref", support_ref)
            items.append(
                AssemblyItem(
                    reference=support_ref,
                    value=bypass.value,
                    footprint=bypass.footprint,
                    source_kind="bypass",
                    semantic_key=semantic_key,
                    owner_ref=owner_ref,
                    role=bypass.role,
                    owner_pin=str(bypass.pin or ""),
                    functional_section=getattr(comp, "functional_section", "") or "",
                    block_id=getattr(comp, "block_id", "") or "",
                    net1=bypass.net,
                    net2=bypass.gnd_net,
                    evidence_ids=evidence_by_reference.get(support_ref, ()),
                )
            )

        for strap in comp.straps:
            semantic_key = support_semantics[comp_index][support_index]
            support_index += 1
            prior_ref = previous_by_key.get(semantic_key, "")
            support_ref = prior_ref if prior_ref.upper().startswith("R") else allocator.allocate("R")
            allocator.reserve(support_ref)
            setattr(strap, "source_ref", support_ref)
            items.append(
                AssemblyItem(
                    reference=support_ref,
                    value=strap.value,
                    footprint=strap.footprint,
                    source_kind="strap",
                    semantic_key=semantic_key,
                    owner_ref=owner_ref,
                    role=strap.role,
                    owner_pin=str(strap.pin or ""),
                    functional_section=getattr(comp, "functional_section", "") or "",
                    block_id=getattr(comp, "block_id", "") or "",
                    net1=strap.net,
                    net2=strap.rail,
                    evidence_ids=evidence_by_reference.get(support_ref, ()),
                )
            )

    references = [item.reference for item in items]
    if len(references) != len(set(references)):
        raise AssemblyManifestError("Assembly reference allocation produced duplicates")

    return AssemblyManifest(
        items=items,
        auto_bypass_components=auto_bypass_components,
        retired_references=sorted(retired_references),
        evidence_ids=manifest_evidence_ids,
        evidence_manifest=evidence_manifest,
        prepared_components=[comp for comp, is_virtual in zip(prepared, virtual_components) if not is_virtual],
        render_components=prepared,
    )


def coerce_assembly_manifest(
    value: AssemblyManifest | Iterable[AssemblyItem] | Iterable[ComponentDef],
) -> AssemblyManifest:
    """Normalize manifest/items/components for backward-compatible exporters."""
    if isinstance(value, AssemblyManifest):
        return value
    materialized = list(value)
    if not materialized:
        return AssemblyManifest(items=[])
    if all(isinstance(item, AssemblyItem) for item in materialized):
        return AssemblyManifest(items=list(materialized))
    if all(isinstance(item, ComponentDef) for item in materialized):
        return build_assembly_manifest(materialized)
    raise TypeError("Expected AssemblyManifest, AssemblyItem iterable, or ComponentDef iterable")
