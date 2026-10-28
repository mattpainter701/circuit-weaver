"""Authoritative schematic-to-PCB handoff on real KiCad footprints.

The placement preview is never read as a source of footprint or pad data.  An
approved placement supplies coordinates only; every physical footprint is
freshly resolved from a local `.kicad_mod` and passes the T247 identity guard
before rendering begins.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .component_db import ComponentDef
from .drc_runner import DrcResult, run_drc
from .evidence import EvidenceLedger, EvidenceSource
from .footprint_lib import FootprintGeometry, KiCadFootprintLibrary
from .identity import (
    IdentityHandoffBundle,
    IdentityHandoffResult,
    IdentityRecord,
    require_identity_handoff,
)
from .manufacturing_readiness import (
    READINESS_FILENAME,
    ManufacturingReadinessInputs,
    assess_manufacturing_readiness,
)
from .pcb_constraints import (
    ConstraintCompilation,
    PcbConstraintConflictError,
    render_kicad_dru_report,
)
from .pcb_contracts import (
    AUTHORITATIVE_GENERATOR,
    PcbArtifactKind,
    PcbConstraint,
    inspect_pcb_artifact,
    require_fresh_authoritative_target,
    validate_pcb_constraint,
)
from .pcb_export import _SETUP, _edge_cuts_rect, _kicad_string, _safe_project_filename

BOARD_MANIFEST_SCHEMA = "circuit-weaver-authoritative-board/v1"


class PcbHandoffError(ValueError):
    """The authoritative board cannot be emitted without weakening a gate."""


class PcbDrcBlocked(PcbHandoffError):
    """Exact staged board bytes failed the authoritative DRC gate."""


class PcbHandoffTransactionError(PcbHandoffError):
    """Transactional publication failed and prior artifacts were restored."""


@dataclass(frozen=True)
class AuthoritativeHandoffResult:
    board_path: str
    board_rules_path: str
    board_manifest_path: str
    evidence_manifest_path: str
    drc_report_path: str
    drc_findings_path: str
    drc_evidence_id: str
    manufacturing_readiness_path: str
    board_provenance_evidence_id: str
    identity_guard_ids: tuple[str, ...]
    footprint_snapshot: Mapping[str, str]
    pad_count: int


@contextmanager
def _pcb_output_lock(output_dir: Path, *, timeout: float = 30.0):
    """Serialize one board's staged DRC and multi-artifact commit."""

    resolved = output_dir.resolve(strict=False)
    lock_path = resolved.parent / f".{resolved.name or 'pcb'}.pcb-handoff.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
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
                    raise TimeoutError(f"timed out waiting for PCB handoff lock: {lock_path}") from exc
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


def _publish_staged_transaction(
    staging: Path,
    publications: Sequence[tuple[Path, Path]],
) -> None:
    """Replace a coherent artifact set and restore every prior byte on failure."""

    backup = staging / ".rollback"
    backup.mkdir()
    prior: dict[Path, Path | None] = {}
    for _source, destination in publications:
        if destination.is_file():
            saved = backup / destination.name
            shutil.copy2(destination, saved)
            prior[destination] = saved
        else:
            prior[destination] = None
    try:
        for source, destination in publications:
            source.replace(destination)
    except OSError as exc:
        for destination, saved in prior.items():
            if saved is None:
                destination.unlink(missing_ok=True)
            else:
                shutil.copy2(saved, destination)
        raise PcbHandoffTransactionError("PCB handoff publication failed; prior artifacts restored") from exc


@dataclass(frozen=True)
class PlacementApproval:
    """A time-bounded approval bound to the exact placement coordinates."""

    id: str
    placement_sha256: str
    approved_at: str
    expires_at: str


def _placement_digest(placements: Mapping[str, Any]) -> str:
    canonical = {
        str(reference): {
            "x_mm": normalized[0],
            "y_mm": normalized[1],
            "rotation_deg": normalized[2],
            "layer": normalized[3],
        }
        for reference, raw in sorted(placements.items())
        for normalized in (_placement_tuple(raw),)
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def approve_placements(
    placements: Mapping[str, Any],
    *,
    approval_id: str,
    approved_at: str,
    expires_at: str,
) -> PlacementApproval:
    """Create a placement approval tied to content and an explicit validity window."""

    approval = PlacementApproval(
        id=str(approval_id).strip(),
        placement_sha256=_placement_digest(placements),
        approved_at=approved_at,
        expires_at=expires_at,
    )
    _require_current_placement_approval(approval, placements)
    return approval


def _parse_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PcbHandoffError(f"placement approval {field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PcbHandoffError(f"placement approval {field} must carry a UTC offset")
    return parsed.astimezone(timezone.utc)


def _require_current_placement_approval(
    approval: PlacementApproval,
    placements: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    if not approval.id:
        raise PcbHandoffError("authoritative handoff requires a placement approval ID")
    approved = _parse_utc(approval.approved_at, "approved_at")
    expires = _parse_utc(approval.expires_at, "expires_at")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires <= approved or current < approved or current >= expires:
        raise PcbHandoffError("placement approval is stale or outside its validity window")
    if approval.placement_sha256 != _placement_digest(placements):
        raise PcbHandoffError("placement approval does not match the approved placement state")


@dataclass(frozen=True)
class _PreparedFootprint:
    component: ComponentDef
    reference: str
    library_text: str
    library_hash: str
    geometry: FootprintGeometry
    pad_numbers: tuple[str, ...]
    pad_to_symbol: Mapping[str, str]
    guard: IdentityHandoffResult
    placement: tuple[float, float, float, str]
    footprint_uuid: str


def _require_compiled_constraints(
    compiled: Iterable[PcbConstraint] | ConstraintCompilation,
) -> ConstraintCompilation:
    """Reject missing/invalid/conflicting constraints before physical rendering."""

    unverified_requirements: tuple[dict[str, Any], ...] = ()
    if isinstance(compiled, ConstraintCompilation):
        constraints = compiled.require_ready()
        unverified_requirements = compiled.unverified_requirements
    else:
        constraints = tuple(compiled)
    if not constraints:
        raise PcbHandoffError("authoritative handoff requires compiled board constraints")
    conflict_ids: set[str] = set()
    for constraint in constraints:
        validate_pcb_constraint(constraint)
        if constraint.conflicts:
            conflict_ids.add(constraint.id)
    if conflict_ids:
        rendered = ", ".join(sorted(conflict_ids))
        raise PcbConstraintConflictError(
            f"PCB constraint conflicts must be resolved before mutation: {rendered}"
        )
    return ConstraintCompilation(tuple(constraints), unverified_requirements=unverified_requirements)


def _placement_tuple(raw: Any) -> tuple[float, float, float, str]:
    if isinstance(raw, Mapping):
        x = raw.get("x_mm", raw.get("x"))
        y = raw.get("y_mm", raw.get("y"))
        rotation = raw.get("rotation_deg", raw.get("rotation", 0.0))
        layer = raw.get("layer", "F.Cu")
    elif isinstance(raw, (tuple, list)) and len(raw) >= 2:
        x, y = raw[0], raw[1]
        rotation = raw[2] if len(raw) > 2 else 0.0
        layer = raw[3] if len(raw) > 3 else "F.Cu"
    else:
        raise PcbHandoffError("approved placement must provide x/y coordinates")
    try:
        normalized = (float(x), float(y), float(rotation), str(layer))
    except (TypeError, ValueError) as exc:
        raise PcbHandoffError("approved placement coordinates must be numeric") from exc
    if normalized[3] not in {"F.Cu", "B.Cu", "top", "bottom"}:
        raise PcbHandoffError(f"unsupported placement layer: {normalized[3]!r}")
    layer_name = {"top": "F.Cu", "bottom": "B.Cu"}.get(normalized[3], normalized[3])
    return normalized[0], normalized[1], normalized[2], layer_name


def _pad_numbers(text: str) -> tuple[str, ...]:
    numbers = {
        match.group(1) or match.group(2)
        for match in re.finditer(r"\(pad\s+(?:\"([^\"]*)\"|([^\s()]+))", text)
        if (match.group(1) or match.group(2))
    }
    return tuple(sorted(numbers))


def _selected_identity(bundle: IdentityHandoffBundle) -> IdentityRecord:
    selected = [
        assertion.identity
        for assertion in bundle.assertions
        if assertion.identity.manufacturer == bundle.manufacturer
        and assertion.identity.mpn == bundle.mpn
        and assertion.identity.package_suffix == bundle.package_suffix
        and assertion.identity.symbol_ref == bundle.symbol_ref
        and assertion.identity.footprint_ref == bundle.footprint_ref
    ]
    if not selected:
        raise PcbHandoffError("identity bundle does not contain its selected exact identity")
    first = selected[0]
    if any(item != first for item in selected[1:]):
        raise PcbHandoffError("identity bundle selected sources disagree")
    return first


def _preflight_component(
    component: ComponentDef,
    *,
    placement: Any,
    bundle: IdentityHandoffBundle,
    footprint_library: KiCadFootprintLibrary,
    project_name: str,
) -> _PreparedFootprint:
    reference = str(component.source_ref or "").strip()
    if not reference:
        raise PcbHandoffError("authoritative handoff requires stable source references")
    if not component.footprint:
        raise PcbHandoffError(f"{reference}: authoritative handoff requires a resolved footprint")
    selected_mpn = component.source_mpn or component.mpn
    if bundle.mpn != selected_mpn or bundle.footprint_ref != component.footprint:
        raise PcbHandoffError(f"{reference}: identity bundle does not match the selected component/footprint")

    # T249.5 reuses T247 verbatim. Do not duplicate or soften this call.
    guard = require_identity_handoff(
        bundle.assertions,
        bundle.reconciliation,
        manufacturer=bundle.manufacturer,
        mpn=bundle.mpn,
        package_suffix=bundle.package_suffix,
        symbol_ref=bundle.symbol_ref,
        footprint_ref=bundle.footprint_ref,
    )
    identity = _selected_identity(bundle)
    library_text = footprint_library.read(component.footprint)
    actual_pads = _pad_numbers(library_text)
    expected_pads = tuple(sorted(identity.footprint_pads))
    if actual_pads != expected_pads:
        raise PcbHandoffError(
            f"{reference}: resolved footprint pads {actual_pads!r} do not match T247 identity {expected_pads!r}"
        )
    pad_to_symbol = {item.footprint_pad: item.symbol_pin for item in identity.pin_pad_map}
    connected_symbol_pins = set(component.pin_nets) | set(component.power_pins) | set(component.explicit_no_connects)
    missing_connectivity = sorted(set(identity.symbol_pins) - connected_symbol_pins)
    if missing_connectivity:
        raise PcbHandoffError(
            f"{reference}: authoritative connectivity is missing symbol pins {missing_connectivity!r}"
        )
    geometry = footprint_library.geometry(component.footprint)
    if geometry.source == "heuristic" or geometry.confidence == "heuristic":
        raise PcbHandoffError(
            f"{reference}: authoritative handoff refuses heuristic or placeholder footprint geometry"
        )
    library_hash = hashlib.sha256(library_text.encode("utf-8")).hexdigest()
    footprint_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"circuit-weaver:{project_name}:{reference}"))
    return _PreparedFootprint(
        component=component,
        reference=reference,
        library_text=library_text,
        library_hash=library_hash,
        geometry=geometry,
        pad_numbers=actual_pads,
        pad_to_symbol=pad_to_symbol,
        guard=guard,
        placement=_placement_tuple(placement),
        footprint_uuid=footprint_uuid,
    )


def _matching_close(text: str, start: int) -> int:
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise PcbHandoffError("unbalanced footprint S-expression")


def _direct_children(text: str) -> list[str]:
    opening = re.match(r"\s*\(footprint\s+(?:\"[^\"]*\"|[^\s()]+)", text)
    if opening is None:
        raise PcbHandoffError("resolved footprint is not a KiCad footprint S-expression")
    outer_end = _matching_close(text, text.index("("))
    children: list[str] = []
    index = opening.end()
    while index < outer_end:
        if text[index] != "(":
            index += 1
            continue
        end = _matching_close(text, index)
        children.append(text[index : end + 1])
        index = end + 1
    return children


def _child_keyword(block: str) -> str:
    match = re.match(r"\(\s*([^\s()]+)", block)
    return match.group(1) if match else ""


def _stable_child_uuids(block: str, *, seed: str) -> str:
    counter = 0

    def replace_uuid(_match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        value = uuid.uuid5(uuid.NAMESPACE_URL, f"{seed}:child:{counter}")
        return f"(uuid {value})"

    return re.sub(r"\(uuid\s+[0-9a-fA-F-]+\)", replace_uuid, block)


def _render_pad(block: str, prepared: _PreparedFootprint, net_numbers: Mapping[str, int], index: int) -> str:
    number_match = re.match(r"\(pad\s+(?:\"([^\"]*)\"|([^\s()]+))", block)
    if number_match is None:
        raise PcbHandoffError(f"{prepared.reference}: malformed library pad")
    pad_number = number_match.group(1) or number_match.group(2)
    if not pad_number:
        return _stable_child_uuids(block, seed=f"{prepared.footprint_uuid}:mechanical-pad:{index}")
    if pad_number not in prepared.pad_to_symbol:
        raise PcbHandoffError(f"{prepared.reference}: pad {pad_number!r} has no T247 pin mapping")
    symbol_pin = prepared.pad_to_symbol[pad_number]
    net_name = prepared.component.pin_nets.get(symbol_pin) or prepared.component.power_pins.get(symbol_pin)
    explicit_nc = symbol_pin in prepared.component.explicit_no_connects
    if not net_name and not explicit_nc:
        raise PcbHandoffError(f"{prepared.reference}: pad {pad_number!r} has no authoritative net or explicit NC")
    if re.search(r"\(net\s+", block):
        raise PcbHandoffError(f"{prepared.reference}: library pad unexpectedly carries a board net")
    rendered = _stable_child_uuids(block, seed=f"{prepared.footprint_uuid}:pad:{index}")
    if net_name:
        rendered = rendered[:-1] + f' (net {net_numbers[net_name]} "{_kicad_string(net_name)}"))'
    return rendered


def _render_authoritative_footprint(prepared: _PreparedFootprint, net_numbers: Mapping[str, int]) -> str:
    x, y, rotation, layer = prepared.placement
    children: list[str] = []
    pad_index = 0
    for child_index, child in enumerate(_direct_children(prepared.library_text), start=1):
        keyword = _child_keyword(child)
        if keyword in {"version", "generator", "generator_version", "layer", "at", "property"}:
            continue
        if keyword == "fp_text" and re.match(r"\(fp_text\s+(?:reference|value)\b", child):
            continue
        if keyword == "pad":
            pad_index += 1
            child = _render_pad(child, prepared, net_numbers, pad_index)
        else:
            child = _stable_child_uuids(
                child,
                seed=f"{prepared.footprint_uuid}:{keyword}:{child_index}",
            )
        children.append(child)

    lines = [
        f'  (footprint "{_kicad_string(prepared.component.footprint)}"',
        f'    (layer "{layer}")',
        f"    (uuid {prepared.footprint_uuid})",
        f"    (at {x:.6f} {y:.6f} {rotation:.6f})",
        f'    (property "Reference" "{_kicad_string(prepared.reference)}" (at 0 -2 0)',
        "      (effects (font (size 1 1) (thickness 0.15)))",
        "    )",
        f'    (property "Value" "{_kicad_string(prepared.component.value or prepared.component.mpn)}" (at 0 2 0)',
        "      (effects (font (size 1 1) (thickness 0.15)))",
        "    )",
    ]
    lines.extend("    " + line.replace("\n", "\n    ") for line in children)
    lines.append("  )")
    return "\n".join(lines)


def _layer_table(copper_layers: int) -> str:
    if copper_layers not in {2, 4}:
        raise PcbHandoffError("authoritative handoff currently supports exactly 2 or 4 copper layers")
    copper = ['    (0 "F.Cu" signal)']
    if copper_layers == 4:
        copper.extend(['    (4 "In1.Cu" power)', '    (6 "In2.Cu" power)'])
    copper.append('    (2 "B.Cu" signal)')
    user = [
        '    (9 "F.Adhes" user "F.Adhesive")',
        '    (11 "B.Adhes" user "B.Adhesive")',
        '    (13 "F.Paste" user)',
        '    (15 "B.Paste" user)',
        '    (5 "F.SilkS" user "F.Silkscreen")',
        '    (7 "B.SilkS" user "B.Silkscreen")',
        '    (1 "F.Mask" user)',
        '    (3 "B.Mask" user)',
        '    (17 "Dwgs.User" user "User.Drawings")',
        '    (19 "Cmts.User" user "User.Comments")',
        '    (21 "Eco1.User" user "User.Eco1")',
        '    (23 "Eco2.User" user "User.Eco2")',
        '    (25 "Edge.Cuts" user)',
        '    (27 "Margin" user)',
        '    (31 "F.CrtYd" user "F.Courtyard")',
        '    (29 "B.CrtYd" user "B.Courtyard")',
        '    (35 "F.Fab" user)',
        '    (33 "B.Fab" user)',
        '    (39 "User.1" user)',
        '    (41 "User.2" user)',
        '    (43 "User.3" user)',
        '    (45 "User.4" user)',
    ]
    return "\n".join(("  (layers", *copper, *user, "  )"))


def _semantic_changes(previous: Mapping[str, Any] | None, current: list[dict[str, Any]]) -> list[dict[str, str]]:
    previous_rows = {
        str(item.get("reference")): item
        for item in (previous or {}).get("components", [])
        if isinstance(item, Mapping)
    }
    changes: list[dict[str, str]] = []
    for item in current:
        old = previous_rows.pop(item["reference"], None)
        if old is None:
            action = "added"
        elif old.get("footprint") != item["footprint"]:
            action = "remapped"
        elif old.get("placement") != item["placement"]:
            action = "moved"
        else:
            action = "unchanged"
        changes.append({"reference": item["reference"], "action": action})
    changes.extend({"reference": ref, "action": "removed"} for ref in sorted(previous_rows))
    return sorted(changes, key=lambda item: item["reference"])


def generate_authoritative_board(
    components: Iterable[ComponentDef],
    approved_placements: Mapping[str, Any],
    identity_handoffs: Mapping[str, IdentityHandoffBundle],
    output_dir: str | Path,
    *,
    project_name: str,
    placement_approval: PlacementApproval,
    board_constraints: Iterable[PcbConstraint] | ConstraintCompilation,
    footprint_library: KiCadFootprintLibrary | None = None,
    evidence_ledger: EvidenceLedger | None = None,
    preview_path: str | Path | None = None,
    board_width_mm: float = 100.0,
    board_height_mm: float = 80.0,
    copper_layers: int = 2,
    approved_drc_overrides: Mapping[str, str] | None = None,
) -> AuthoritativeHandoffResult:
    """Freshly emit a real pad-bearing board after all fail-closed preflight gates."""

    materialized = tuple(components)
    if not materialized:
        raise PcbHandoffError("authoritative handoff requires at least one component")
    if board_width_mm <= 0 or board_height_mm <= 0:
        raise PcbHandoffError("board dimensions must be positive")

    output = Path(output_dir)
    safe_name = _safe_project_filename(project_name)
    target = require_fresh_authoritative_target(preview_path, output / f"{safe_name}.kicad_pcb")
    manifest_target = output / f"{safe_name}_board_manifest.json"
    rules_target = output / f"{safe_name}.kicad_dru"
    evidence_target = output / "evidence_manifest.json"
    drc_report_target = output / f"{safe_name}_drc.json"
    drc_findings_target = output / f"{safe_name}_drc_findings.json"
    readiness_target = output / READINESS_FILENAME
    references = [str(component.source_ref or "").strip() for component in materialized]
    if any(not item for item in references) or len(references) != len(set(references)):
        raise PcbHandoffError("authoritative handoff references must be non-empty and unique")
    if set(approved_placements) != set(references):
        raise PcbHandoffError("approved placement references must exactly match authoritative components")
    if set(identity_handoffs) != set(references):
        raise PcbHandoffError("identity handoff references must exactly match authoritative components")
    _require_current_placement_approval(placement_approval, approved_placements)
    compilation = _require_compiled_constraints(board_constraints)
    constraints = compilation.constraints

    library = footprint_library or KiCadFootprintLibrary()
    prepared = tuple(
        _preflight_component(
            component,
            placement=approved_placements[reference],
            bundle=identity_handoffs[reference],
            footprint_library=library,
            project_name=safe_name,
        )
        for component, reference in zip(materialized, references)
    )

    net_names = sorted(
        {
            net
            for item in materialized
            for net in (*item.pin_nets.values(), *item.power_pins.values())
            if net
        }
    )
    net_numbers = {name: index + 1 for index, name in enumerate(net_names)}
    # Rendering begins only after every component has passed T247 and library/pad preflight.
    footprints = [_render_authoritative_footprint(item, net_numbers) for item in prepared]
    board_parts = [
        f'(kicad_pcb (version 20240108) (generator "{AUTHORITATIVE_GENERATOR}")',
        "  (general (thickness 1.6) (legacy_teardrops no))",
        _layer_table(copper_layers),
        _SETUP,
        '  (net 0 "")',
        *(f'  (net {number} "{_kicad_string(name)}")' for name, number in net_numbers.items()),
        *footprints,
        _edge_cuts_rect(0.0, 0.0, board_width_mm, board_height_mm),
        ")",
    ]
    board_text = "\n".join(board_parts)
    rules_render = render_kicad_dru_report(compilation)
    rules_text = str(rules_render["text"])
    inspection = inspect_pcb_artifact(target, board_text)
    if inspection.kind is not PcbArtifactKind.AUTHORITATIVE:
        raise PcbHandoffError("authoritative board failed the frozen artifact contract")

    snapshot = {item.component.footprint: item.library_hash for item in prepared}
    snapshot_hash = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    guard_ids = tuple(sorted(item.guard.id for item in prepared))
    ledger = evidence_ledger or EvidenceLedger()
    provenance_claim = json.dumps(
        {
            "placement_approval_id": placement_approval.id,
            "fp_lib_snapshot_sha256": snapshot_hash,
            "identity_guard_ids": list(guard_ids),
            "board_constraint_ids": [item.id for item in constraints],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    provenance_id = ledger.record(
        subject_ref="tool:pcb_handoff",
        claim=provenance_claim,
        kind="tool_result",
        source=EvidenceSource(doc_id="pcb-handoff", extraction_method="authoritative-board"),
        confidence="verified",
        freshness="current",
    )

    component_rows = [
        {
            "reference": item.reference,
            "uuid": item.footprint_uuid,
            "footprint": item.component.footprint,
            "pad_count": len(item.pad_numbers),
            "placement": {
                "x_mm": item.placement[0],
                "y_mm": item.placement[1],
                "rotation_deg": item.placement[2],
                "layer": item.placement[3],
            },
            "geometry": {
                "width_mm": item.geometry.width_mm,
                "height_mm": item.geometry.height_mm,
                "source": item.geometry.source,
            },
            "identity_guard_id": item.guard.id,
        }
        for item in prepared
    ]
    board_hash = hashlib.sha256(board_text.encode("utf-8")).hexdigest()
    rules_hash = hashlib.sha256(rules_text.encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": BOARD_MANIFEST_SCHEMA,
        "artifact_kind": "authoritative_board",
        "project": safe_name,
        "board": target.name,
        "board_sha256": board_hash,
        "board_rules": rules_target.name,
        "board_rules_sha256": rules_hash,
        "board_provenance_evidence_id": provenance_id,
        "placement_approval_id": placement_approval.id,
        "placement_approval_sha256": placement_approval.placement_sha256,
        "fp_lib_snapshot": snapshot,
        "fp_lib_snapshot_sha256": snapshot_hash,
        "identity_guard_ids": list(guard_ids),
        "board_constraint_ids": [item.id for item in constraints],
        "board_constraint_rendering": {key: value for key, value in rules_render.items() if key != "text"},
        "unverified_board_requirements": [
            {**dict(item), "verification_status": "not_verified"}
            for item in compilation.unverified_requirements
        ],
        "components": component_rows,
        "copper_layers": copper_layers,
    }

    output.mkdir(parents=True, exist_ok=True)
    drc_result: DrcResult | None = None
    with _pcb_output_lock(output):
        staging = Path(tempfile.mkdtemp(prefix=f".{safe_name}.pcb-handoff-", dir=output.parent))
        try:
            staged_board = staging / target.name
            staged_manifest = staging / manifest_target.name
            staged_rules = staging / rules_target.name
            staged_drc_report = staging / drc_report_target.name
            staged_drc_findings = staging / drc_findings_target.name
            staged_readiness = staging / readiness_target.name
            staged_board.write_text(board_text, encoding="utf-8", newline="")
            staged_rules.write_text(rules_text, encoding="utf-8", newline="")

            drc_result = run_drc(
                staged_board,
                evidence_ledger=ledger,
                constraints=constraints,
                approved_overrides=approved_drc_overrides,
                **({"project_context": footprint_library._project_root}
                   if getattr(footprint_library, "_project_root", None) is not None else {}),
            )
            if drc_result.status != "ok":
                raise PcbDrcBlocked(f"authoritative DRC could not complete: {drc_result.failure_reason}")
            if drc_result.blocker_count:
                raise PcbDrcBlocked(
                    f"authoritative DRC found {drc_result.blocker_count} unapproved blocker(s)"
                )
            if drc_result.board_sha256 != board_hash:
                raise PcbDrcBlocked("DRC result does not name the exact staged board bytes")

            previous: Mapping[str, Any] | None = None
            if manifest_target.is_file():
                try:
                    loaded = json.loads(manifest_target.read_text(encoding="utf-8"))
                    previous = loaded if isinstance(loaded, Mapping) else None
                except json.JSONDecodeError:
                    previous = None
            manifest["semantic_changes"] = _semantic_changes(previous, component_rows)
            manifest["drc"] = {
                "status": drc_result.status,
                "passed": drc_result.passed,
                "blocker_count": drc_result.blocker_count,
                "tool_version": drc_result.tool_version,
                "evidence_id": drc_result.evidence_id,
                "board_sha256": drc_result.board_sha256,
                "report": drc_report_target.name,
                "findings": drc_findings_target.name,
            }
            readiness = assess_manufacturing_readiness(
                ManufacturingReadinessInputs(
                    identity_complete=True,
                    placement_approved=True,
                    routing_complete=False,
                    erc_passed=False,
                    drc_completed=True,
                    drc_passed=True,
                    bom_cpl_reconciled=False,
                    fabrication_artifacts_valid=False,
                ),
                evidence_records=ledger.to_manifest()["records"],
            )
            manifest["manufacturing_readiness"] = readiness.to_dict()
            staged_readiness.write_text(
                json.dumps(readiness.to_dict(), indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                encoding="utf-8",
                newline="",
            )
            staged_drc_report.write_text(
                json.dumps(drc_result.raw_report, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                encoding="utf-8",
                newline="",
            )
            staged_drc_findings.write_text(
                json.dumps(
                    [finding.to_dict() for finding in drc_result.findings],
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=True,
                )
                + "\n",
                encoding="utf-8",
                newline="",
            )
            staged_manifest.write_text(
                json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                encoding="utf-8",
                newline="",
            )
            staged_evidence = ledger.write(staging)
            _publish_staged_transaction(
                staging,
                (
                    (staged_evidence, evidence_target),
                    (staged_drc_report, drc_report_target),
                    (staged_drc_findings, drc_findings_target),
                    (staged_readiness, readiness_target),
                    (staged_rules, rules_target),
                    (staged_manifest, manifest_target),
                    (staged_board, target),
                ),
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    if drc_result is None:
        raise PcbHandoffTransactionError("DRC transaction completed without a result")

    return AuthoritativeHandoffResult(
        board_path=str(target),
        board_rules_path=str(rules_target),
        board_manifest_path=str(manifest_target),
        evidence_manifest_path=str(evidence_target),
        drc_report_path=str(drc_report_target),
        drc_findings_path=str(drc_findings_target),
        drc_evidence_id=drc_result.evidence_id,
        manufacturing_readiness_path=str(readiness_target),
        board_provenance_evidence_id=provenance_id,
        identity_guard_ids=guard_ids,
        footprint_snapshot=dict(snapshot),
        pad_count=inspection.pad_count,
    )
