"""Exhaustive, review-only PCB placement artifact pipeline.

The schematic compiler models bypass capacitors and straps as support metadata
on their owning component.  Placement artifacts, however, need one physical
component per assembly reference.  This module bridges those representations
through the canonical assembly manifest and deliberately labels every output
as a heuristic review aid rather than fabrication data.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .assembly_manifest import AssemblyItem, AssemblyManifest, build_assembly_manifest
from .component_db import ComponentDef, PinDef
from .placement_context import build_placement_context, write_placement_context
from .placement_optimizer import PlacementConfig, component_part_number, optimize_placement
from .placement_viewer import generate_viewer
from .svg_placement import export_placement_svg

PLACEMENT_RESULT_FILENAME = "placement_result.json"
PLACEMENT_CONTEXT_FILENAME = "placement_review_context.json"
PLACEMENT_SVG_FILENAME = "placement.svg"
PLACEMENT_HTML_FILENAME = "placement_editor.html"

_ARTIFACT_FILENAMES = {
    "placement_result": PLACEMENT_RESULT_FILENAME,
    "placement_context": PLACEMENT_CONTEXT_FILENAME,
    "placement_svg": PLACEMENT_SVG_FILENAME,
    "placement_html": PLACEMENT_HTML_FILENAME,
}
_PUBLISH_ORDER = (
    "placement_svg",
    "placement_html",
    "placement_context",
    "placement_result",
)
_QUALITY_DEFECT_KEYS = (
    "overlaps",
    "outside_board",
    "missing_parents",
    "support_body_violations",
)
_REFERENCE_PREFIX_RE = re.compile(r"^([A-Za-z]+)")
_HEURISTIC_AUTHORITY = (
    "Heuristic placement proposal for human and AI review only. It is not a routed PCB, "
    "has not been validated against mechanical constraints or part-specific layout rules, "
    "and is not fabrication-ready. Official datasheets and reference layouts are authoritative."
)


class PlacementPipelineError(ValueError):
    """Raised when the assembly inventory cannot be represented faithfully."""


@contextmanager
def _placement_output_lock(output_dir: Path, *, timeout: float = 30.0):
    """Serialize placement publication across processes for one output directory."""
    resolved = output_dir.resolve(strict=False)
    lock_path = resolved.parent / f".{resolved.name or 'placement'}.placement-review.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        acquired = False
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for placement output lock: {lock_path}"
                    ) from exc
                time.sleep(0.05)

        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class PlacementInventory:
    """One canonical assembly manifest and its flat placement components."""

    manifest: AssemblyManifest
    components: list[ComponentDef]

    @property
    def references(self) -> list[str]:
        """Return manifest references in deterministic assembly order."""
        return [item.reference for item in self.manifest.items]


def _ref_prefix(reference: str) -> str:
    match = _REFERENCE_PREFIX_RE.match(reference or "")
    return match.group(1).upper() if match else "U"


def _support_component(item: AssemblyItem) -> ComponentDef:
    pin_nets: dict[str, str] = {}
    pins: list[PinDef] = []
    if item.net1:
        pin_nets["1"] = item.net1
        pins.append(PinDef("1", item.net1, "passive", "L"))
    if item.net2:
        pin_nets["2"] = item.net2
        pins.append(PinDef("2", item.net2, "passive", "R"))

    support_role = str(item.role or item.source_kind or "support").replace("_", " ")
    component = ComponentDef(
        mpn=item.mpn,
        ref_prefix=_ref_prefix(item.reference),
        value=item.value,
        footprint=item.footprint,
        description=f"Generated {support_role} support part for {item.owner_ref}",
        category="passive",
        source_ref=item.reference,
        source_mpn=item.mpn,
        source_value=item.value,
        source_manufacturer=item.manufacturer,
        lcsc_pn=item.lcsc_pn,
        functional_section=item.functional_section,
        block_id=item.block_id,
        pins=pins,
        pin_nets=pin_nets,
    )
    # ComponentDef permits runtime metadata and may also gain declared fields
    # in newer releases.  setattr works for both representations.
    component.placement_parent_ref = item.owner_ref  # type: ignore[attr-defined]
    component.placement_parent_pin = item.owner_pin  # type: ignore[attr-defined]
    component.placement_role = item.role or item.source_kind  # type: ignore[attr-defined]
    component.assembly_source_kind = item.source_kind  # type: ignore[attr-defined]
    has_sourcing_id = bool(str(item.mpn or "").strip() or str(item.lcsc_pn or "").strip())
    component.placement_sourcing_status = (  # type: ignore[attr-defined]
        "identified" if has_sourcing_id else "review_blocked"
    )
    component.placement_sourcing_review_reason = (  # type: ignore[attr-defined]
        ""
        if has_sourcing_id
        else (
            f"Generated {support_role} support {item.reference} has value/footprint only; "
            "assign a manufacturer or supplier part number before placement approval."
        )
    )
    component.placement_geometry_status = (  # type: ignore[attr-defined]
        "estimated" if str(item.footprint or "").strip() else "review_blocked_placeholder"
    )
    component.placement_geometry_review_reason = (  # type: ignore[attr-defined]
        ""
        if item.footprint
        else f"{item.reference} has no footprint; its placement box is a nonphysical placeholder."
    )
    return component


def build_placement_inventory(
    components: Iterable[ComponentDef],
    *,
    include_auto_bypass: bool = True,
    assembly_manifest: AssemblyManifest | None = None,
) -> PlacementInventory:
    """Flatten a compiled design into one ComponentDef per assembly item.

    Primary components are deep-copied from the manifest's prepared inventory,
    retaining their complete pin/power connectivity.  The stable reference
    allocated by the manifest is then applied to the copy.  Bypass and strap
    items become ordinary two-terminal passive ComponentDefs carrying owner,
    role, section, and block metadata for placement affinity and review.
    """
    manifest = assembly_manifest or build_assembly_manifest(
        components,
        include_auto_bypass=include_auto_bypass,
    )
    primary_items = [item for item in manifest.items if item.source_kind == "component"]
    if len(primary_items) != len(manifest.prepared_components):
        raise PlacementPipelineError(
            "Assembly manifest primary-item count does not match its prepared component inventory"
        )

    primary_by_ref: dict[str, ComponentDef] = {}
    for item, prepared in zip(primary_items, manifest.prepared_components):
        component = copy.deepcopy(prepared)
        component.source_ref = item.reference
        component.functional_section = item.functional_section or component.functional_section
        component.block_id = item.block_id or component.block_id
        component.assembly_source_kind = "component"  # type: ignore[attr-defined]
        component.placement_geometry_status = (  # type: ignore[attr-defined]
            "estimated" if str(component.footprint or "").strip() else "review_blocked_placeholder"
        )
        component.placement_geometry_review_reason = (  # type: ignore[attr-defined]
            ""
            if component.footprint
            else (
                f"{item.reference} has no footprint; its placement box is a nonphysical placeholder."
            )
        )
        # Support parts are flattened into their own ComponentDefs below. Keep
        # them off the owner copy or net collectors will falsely attribute the
        # support rail endpoint directly to the IC as well.
        component.bypass_caps = []
        component.straps = []
        primary_by_ref[item.reference] = component

    flat_components: list[ComponentDef] = []
    for item in manifest.items:
        if item.source_kind == "component":
            try:
                flat_components.append(primary_by_ref[item.reference])
            except KeyError as exc:
                raise PlacementPipelineError(
                    f"Assembly primary {item.reference} has no prepared ComponentDef"
                ) from exc
        elif item.source_kind in {"bypass", "strap"}:
            flat_components.append(_support_component(item))
        else:
            raise PlacementPipelineError(
                f"Unsupported assembly source kind for placement: {item.source_kind!r}"
            )

    manifest_refs = [item.reference for item in manifest.items]
    component_refs = [component.source_ref for component in flat_components]
    if component_refs != manifest_refs:
        raise PlacementPipelineError(
            "Flat placement inventory references do not exactly match the assembly manifest"
        )
    from .footprint_lib import KiCadFootprintLibrary

    library = KiCadFootprintLibrary()
    for component in flat_components:
        if not library.footprint_exists(component.footprint):
            continue
        try:
            geometry = library.geometry(component.footprint)
        except ValueError:
            continue
        # Use actual library courtyard/pad extents, enclosing them about the
        # footprint origin. Connectors often use pin 1 rather than body center.
        component.placement_width_mm = geometry.width_mm + 2 * abs(geometry.center_x_mm)
        component.placement_height_mm = geometry.height_mm + 2 * abs(geometry.center_y_mm)
        component.placement_geometry_status = "library_" + geometry.source
    return PlacementInventory(manifest=manifest, components=flat_components)


def _artifact_rows(statuses: dict[str, tuple[str, str]]) -> list[dict]:
    rows = []
    for kind, filename in _ARTIFACT_FILENAMES.items():
        status, reason = statuses[kind]
        rows.append(
            {
                "kind": kind,
                "path": filename,
                "status": status,
                "fabrication_ready": False,
                "reason": reason,
            }
        )
    return rows


def _write_result(payload: dict, output_dir: Path) -> Path:
    path = output_dir / PLACEMENT_RESULT_FILENAME
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _invalidate_owned_artifacts(output_dir: Path) -> None:
    """Remove the prior commit marker first, then every pipeline-owned file."""
    for kind in _ARTIFACT_FILENAMES:
        (output_dir / _ARTIFACT_FILENAMES[kind]).unlink(missing_ok=True)


def _replace_staged_file(source: Path, destination: Path) -> None:
    """Publish one staged artifact with a same-filesystem atomic replacement."""
    source.replace(destination)


def _publish_staged_artifacts(
    staging_dir: Path,
    output_dir: Path,
    kinds: Iterable[str],
) -> None:
    """Publish a coherent set, keeping the result/commit manifest last."""
    requested = set(kinds)
    ordered = [kind for kind in _PUBLISH_ORDER if kind in requested]
    if requested != set(ordered):
        unknown = sorted(requested - set(ordered))
        raise PlacementPipelineError(f"Unknown placement artifact kinds: {unknown}")
    for kind in ordered:
        staged = staging_dir / _ARTIFACT_FILENAMES[kind]
        if not staged.is_file():
            raise PlacementPipelineError(
                f"Staged placement artifact is missing before publication: {staged.name}"
            )
    for kind in ordered:
        filename = _ARTIFACT_FILENAMES[kind]
        _replace_staged_file(staging_dir / filename, output_dir / filename)


def _quality_defects_present(optimizer_result: dict) -> bool:
    quality = optimizer_result.get("quality") or {}
    evaluation = optimizer_result.get("constraint_evaluation") or {}
    return bool(
        any(quality.get(key) for key in _QUALITY_DEFECT_KEYS)
        or evaluation.get("unsupported")
        or evaluation.get("violations")
    )


def _return_payload(payload: dict, output_dir: Path) -> dict:
    result = copy.deepcopy(payload)
    result["artifact_paths"] = {
        kind: str(output_dir / filename) for kind, filename in _ARTIFACT_FILENAMES.items()
    }
    result["generated_artifact_paths"] = {
        row["kind"]: str(output_dir / row["path"])
        for row in payload["artifacts"]
        if row["status"] in {"ready", "review_blocked"}
    }
    return result


def _blocked_payload(
    *,
    project_name: str,
    assembly_item_count: int,
    reason: str,
    optimizer_result: dict | None = None,
    reconciliation: dict | None = None,
) -> dict:
    statuses = {
        "placement_result": ("ready", "Truthful blocked-state record."),
        "placement_context": ("blocked", reason),
        "placement_svg": ("blocked", reason),
        "placement_html": ("blocked", reason),
    }
    return {
        "schema_version": 1,
        "artifact_kind": "placement_review",
        "placement_kind": "heuristic_proposal",
        "project": project_name,
        "status": "blocked",
        "fabrication_ready": False,
        "review_required": True,
        "authority": _HEURISTIC_AUTHORITY,
        "assembly_item_count": assembly_item_count,
        "placement_component_count": 0,
        "artifact_count": 1,
        "expected_artifact_count": len(_ARTIFACT_FILENAMES),
        "generated_artifact_count": 1,
        "blocked_reasons": [reason],
        "reference_reconciliation": reconciliation,
        "optimizer": optimizer_result,
        "placements": {},
        "artifacts": _artifact_rows(statuses),
    }


def _generate_placement_review_staged(
    components: Iterable[ComponentDef],
    target: Path,
    staging_dir: Path,
    *,
    project_name: str = "design",
    config: PlacementConfig | None = None,
    specs_dir: str | Path | None = None,
    constraints: list[dict] | None = None,
    include_auto_bypass: bool = True,
    assembly_manifest: AssemblyManifest | None = None,
) -> dict:
    """Build a placement review in staging and publish its commit manifest last.

    Returns a JSON-compatible status dictionary.  Four deterministic files are
    produced for a valid inventory: ``placement_result.json``,
    ``placement_review_context.json``, ``placement.svg``, and
    ``placement_editor.html``.  Empty or irreconcilable inventories produce a
    blocked ``placement_result.json`` and no misleading visual artifacts.
    """
    inventory = build_placement_inventory(
        components,
        include_auto_bypass=include_auto_bypass,
        assembly_manifest=assembly_manifest,
    )

    if not inventory.components:
        payload = _blocked_payload(
            project_name=project_name,
            assembly_item_count=0,
            reason="Placement review requires at least one physical assembly item.",
        )
        _write_result(payload, staging_dir)
        _publish_staged_artifacts(staging_dir, target, {"placement_result"})
        return _return_payload(payload, target)

    optimizer_result = copy.deepcopy(
        optimize_placement(
            inventory.components,
            config=config,
            specs_dir=specs_dir,
            constraints=constraints,
        )
    )
    placements = optimizer_result.get("placements")
    if not isinstance(placements, dict):
        placements = {}

    manifest_refs = set(inventory.references)
    placement_refs = set(placements)
    missing_refs = sorted(manifest_refs - placement_refs)
    unexpected_refs = sorted(placement_refs - manifest_refs)
    reconciliation = {
        "exact_match": not missing_refs and not unexpected_refs and len(placements) == len(inventory.references),
        "manifest_refs": sorted(manifest_refs),
        "placement_refs": sorted(placement_refs),
        "missing_from_placement": missing_refs,
        "unexpected_in_placement": unexpected_refs,
    }
    if optimizer_result.get("status") != "ok" or not reconciliation["exact_match"]:
        reason = (
            "Placement optimizer did not return a usable result."
            if optimizer_result.get("status") != "ok"
            else "Placement references do not exactly match the exhaustive assembly manifest."
        )
        payload = _blocked_payload(
            project_name=project_name,
            assembly_item_count=len(inventory.references),
            reason=reason,
            optimizer_result=optimizer_result,
            reconciliation=reconciliation,
        )
        _write_result(payload, staging_dir)
        _publish_staged_artifacts(staging_dir, target, {"placement_result"})
        return _return_payload(payload, target)

    quality = dict(optimizer_result.get("quality") or {})
    quality["review_required"] = True
    optimizer_result["quality"] = quality

    board_width = float(optimizer_result.get("board_width_mm", 100.0))
    board_height = float(optimizer_result.get("board_height_mm", 80.0))
    context = build_placement_context(
        inventory.components,
        placements,
        board_width_mm=board_width,
        board_height_mm=board_height,
        constraints=constraints,
        constraint_evaluation=optimizer_result.get("constraint_evaluation"),
        placement_quality=quality,
    )
    write_placement_context(context, staging_dir / PLACEMENT_CONTEXT_FILENAME)

    svg_components = [
        {
            "ref": component.source_ref,
            "value": component.source_value or component.value or component_part_number(component),
            "footprint": component.footprint,
            "category": (
                "placeholder"
                if getattr(component, "placement_geometry_status", "")
                == "review_blocked_placeholder"
                else component.category
            ),
            "width_mm": optimizer_result["placements"][component.source_ref].get("width_mm"),
            "height_mm": optimizer_result["placements"][component.source_ref].get("height_mm"),
        }
        for component in inventory.components
    ]
    title = f"{project_name} PCB Placement Review"
    export_placement_svg(
        svg_components,
        placements,
        board_width,
        board_height,
        output_path=staging_dir / PLACEMENT_SVG_FILENAME,
        title=title,
    )
    generate_viewer(
        inventory.components,
        placements,
        board_width,
        board_height,
        placement_context=context,
        title=title,
        output_path=staging_dir / PLACEMENT_HTML_FILENAME,
    )

    quality_defects = _quality_defects_present(optimizer_result)
    if quality_defects:
        statuses = {
            "placement_result": ("ready", "Truthful blocked-state commit manifest."),
            "placement_context": (
                "review_blocked",
                "Generated for diagnosis, but placement quality defects block approval.",
            ),
            "placement_svg": (
                "review_blocked",
                "Generated for correction, but placement quality defects block approval.",
            ),
            "placement_html": (
                "review_blocked",
                "Generated for correction, but placement quality defects block approval.",
            ),
        }
    else:
        statuses = {
            kind: ("ready", "Review-only heuristic artifact; never fabrication data.")
            for kind in _ARTIFACT_FILENAMES
        }
    review_gate = dict(context.get("review_gate") or {})
    review_blockers = list(review_gate.get("blockers") or [])
    quality["sourcing_review_blockers"] = [
        blocker for blocker in review_blockers if blocker.get("kind") == "sourcing_metadata"
    ]
    quality["geometry_review_blockers"] = [
        blocker for blocker in review_blockers if blocker.get("kind") == "footprint_geometry"
    ]
    quality["placement_review_blockers"] = [
        blocker
        for blocker in review_blockers
        if blocker.get("kind")
        in {
            "placement_overlap",
            "placement_outside_board",
            "placement_support_parent_missing",
            "placement_support_clearance",
            "placement_constraint",
            "placement_constraint_violation",
        }
    ]
    quality["review_required"] = True
    optimizer_result["quality"] = quality
    payload = {
        "schema_version": 1,
        "artifact_kind": "placement_review",
        "placement_kind": "heuristic_proposal",
        "project": project_name,
        "status": "blocked" if quality_defects else "review_required",
        "fabrication_ready": False,
        "review_required": True,
        "authority": _HEURISTIC_AUTHORITY,
        "assembly_item_count": len(inventory.references),
        "placement_component_count": len(placements),
        "artifact_count": len(_ARTIFACT_FILENAMES),
        "expected_artifact_count": len(_ARTIFACT_FILENAMES),
        "generated_artifact_count": len(_ARTIFACT_FILENAMES),
        "blocked_reasons": [str(blocker.get("reason", "")) for blocker in review_blockers],
        "review_gate": review_gate,
        "board": {"width_mm": board_width, "height_mm": board_height},
        "reference_reconciliation": reconciliation,
        "optimizer": optimizer_result,
        "placements": placements,
        "artifacts": _artifact_rows(statuses),
    }
    _write_result(payload, staging_dir)
    _publish_staged_artifacts(staging_dir, target, _ARTIFACT_FILENAMES)
    return _return_payload(payload, target)


def generate_placement_review(
    components: Iterable[ComponentDef],
    output_dir: str | Path,
    *,
    project_name: str = "design",
    config: PlacementConfig | None = None,
    specs_dir: str | Path | None = None,
    constraints: list[dict] | None = None,
    include_auto_bypass: bool = True,
    assembly_manifest: AssemblyManifest | None = None,
) -> dict:
    """Generate and coherently publish exhaustive, review-only placement artifacts.

    The prior placement result is invalidated before optimization starts. Every
    successful visual set is built in a sibling staging directory and the JSON
    result is published last as its commit manifest. Any exception removes all
    pipeline-owned destination files, so callers can never observe a stale
    success result or a mixed-generation placement set.
    """
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    with _placement_output_lock(target):
        _invalidate_owned_artifacts(target)
        staging_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{target.name or 'placement'}.placement-stage-",
                dir=target.parent,
            )
        )
        try:
            return _generate_placement_review_staged(
                components,
                target,
                staging_dir,
                project_name=project_name,
                config=config,
                specs_dir=specs_dir,
                constraints=constraints,
                include_auto_bypass=include_auto_bypass,
                assembly_manifest=assembly_manifest,
            )
        except BaseException as exc:
            try:
                _invalidate_owned_artifacts(target)
            except OSError as cleanup_exc:
                add_note = getattr(exc, "add_note", None)
                if add_note is not None:
                    add_note(f"Placement artifact cleanup also failed: {cleanup_exc}")
            raise
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
