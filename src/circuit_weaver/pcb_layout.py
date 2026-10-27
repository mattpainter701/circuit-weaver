"""Place and finish routing existing real KiCad boards through native pcbnew."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .autoroute import _prepare_destination, preflight_pcb
from .component_db import ComponentDef, PinDef
from .drc_runner import run_drc
from .evidence import EvidenceLedger
from .kicad_bridge import run_kicad
from .kicad_project_context import inspect_project_footprint_context, stage_project_footprint_context
from .pcb_handoff import _pcb_output_lock, _publish_staged_transaction
from .placement_optimizer import (
    PlacementConfig,
    _is_board_dimension_constraint,
    _position_values,
    optimize_placement,
)

_NATIVE_PLACE_BASE_TIMEOUT_SECONDS = 120
_NATIVE_PLACE_SECONDS_PER_FOOTPRINT = 2
_NATIVE_PLACE_MAX_TIMEOUT_SECONDS = 20 * 60


def _rotated_offset(x: float, y: float, rotation: float) -> tuple[float, float]:
    """Rotate local KiCad coordinates, whose Y axis points down."""
    angle = math.radians(rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    return x * cosine + y * sine, y * cosine - x * sine


def _center_constraints(constraints: list[dict], footprints: list[dict], bounds: dict) -> list[dict]:
    """Translate authored footprint origins to the optimizer's courtyard centers."""
    by_ref = {item["ref"]: item for item in footprints}
    result = copy.deepcopy(constraints)
    for rule in result:
        item = by_ref.get(rule.get("target"))
        x, y = _position_values(rule)
        if item is None or x is None or y is None or rule.get("kind") != "placement":
            continue
        rotation = rule.get("rotation", 0)
        if isinstance(rotation, bool) or not isinstance(rotation, (int, float)) or not math.isfinite(rotation):
            raise ValueError("Fixed placement rotation must be a finite number")
        if item["locked"] and (
            abs(x + bounds["x"] - item["x"]) > 0.001 or abs(y + bounds["y"] - item["y"]) > 0.001
            or abs((rotation - item["rotation"] + 180) % 360 - 180) > 0.001
        ):
            raise ValueError(f"{item['ref']}: design intent conflicts with a locked footprint")
        dx, dy = _rotated_offset(item.get("courtyard_center_x", 0), item.get("courtyard_center_y", 0), rotation)
        rule["x_mm"], rule["y_mm"] = x + dx, y + dy
    return result


def _native_origin(placement: dict, offset: tuple[float, float], bounds: dict) -> tuple[float, float]:
    dx, dy = _rotated_offset(*offset, placement["rotation"])
    x, y = placement["x"] + bounds["x"] - dx, placement["y"] + bounds["y"] - dy
    # Native KiCad DSN export uses tenths of a micrometer for positions.
    # Quantize new movable coordinates before publication so routing can retain
    # their exact native positions (0.1 um = 0.0001 mm).
    # Authored fixed positions and existing board locks retain full precision.
    if not placement.get("constraint_locked"):
        x, y = round(x, 4), round(y, 4)
    return x, y


def _validate_design_outline_constraints(constraints: list[dict], bounds: dict) -> None:
    """Check only placement-stage board-outline declarations against native bounds."""
    board_targets = {"board", "board_outline", "pcb", "outline"}
    for rule in constraints:
        if not isinstance(rule, dict):
            continue
        if not _is_board_dimension_constraint(rule):
            continue
        target = str(rule.get("target") or rule.get("ref") or "").strip().lower()
        nested = rule.get("board") if isinstance(rule.get("board"), dict) else {}
        for axis in ("width", "height"):
            value = rule.get(f"board_{axis}_mm")
            if value is None:
                value = nested.get(f"{axis}_mm")
            if value is None and target in board_targets:
                value = rule.get(f"{axis}_mm")
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or abs(float(value) - bounds[axis]) > 0.1
            ):
                raise ValueError("Design board dimensions do not match the native Edge.Cuts outline")


def _design_intent(spec: dict[str, Any] | None, footprints: list[dict], bounds: dict) -> tuple[dict, list[dict]]:
    """Bind optional design intent to real board references, footprints and nets.

    The design supplies roles and constraints only. Native board geometry and
    pad connectivity remain the source of all placement dimensions and costs.
    """
    if spec is None:
        return {}, []
    from .design_loader import compile_design_ir
    from .placement_pipeline import build_placement_inventory

    compiled = compile_design_ir(spec, enrich_parts=False)
    inventory = build_placement_inventory(compiled.components)
    by_ref = {component.source_ref: component for component in inventory.components}
    if set(by_ref) != {item["ref"] for item in footprints}:
        raise ValueError("Design intent references do not exactly match the native board inventory")
    for item in footprints:
        component = by_ref[item["ref"]]
        if component.footprint.split(":")[-1] != item["footprint"].split(":")[-1]:
            raise ValueError(f"{item['ref']}: design footprint does not match the native board")
        actual = {str(pad["number"]): pad["net"] for pad in item["pads"] if pad["number"]}
        expected = {**component.power_pins, **component.pin_nets}
        # Assembly support net1/net2 describe endpoints, not symbol pin order.
        # The schematic compiler may emit the capacitor symbol reversed. Only
        # use unordered endpoints for two-terminal generated supports; preserve
        # exact pad checks for primary components.
        if (getattr(component, "placement_parent_ref", "") and len(actual) == len(expected) == 2
                and sorted(actual.values()) == sorted(expected.values())):
            continue
        for pin, net in expected.items():
            if actual.get(str(pin)) != net:
                raise ValueError(f"{item['ref']}.{pin}: design net does not match the native board")
    constraints = list(compiled.ir.pcb_constraints)
    _validate_design_outline_constraints(constraints, bounds)
    return by_ref, constraints


def _layout_review(placements: dict[str, dict], components: list[ComponentDef], constraints: list[dict],
                   bounds: dict) -> dict[str, Any]:
    """Report assembly readability separately from electrical/manufacturing approval."""
    connectors = []
    warnings = []
    edge_rules = {rule.get("target"): rule for rule in constraints if rule.get("edge")}
    for component in components:
        if component.category not in {"connector", "usb", "debug"}:
            continue
        rule = edge_rules.get(component.source_ref, {})
        explicit = rule.get("edge") in {"top", "bottom", "left", "right"} and "rotation" in rule
        connectors.append({"reference": component.source_ref, "edge": rule.get("edge"),
                           "rotation": placements[component.source_ref]["rotation"],
                           "edge_and_orientation_specified": explicit})
        if not explicit:
            warnings.append(f"{component.source_ref}: specify the mating edge and rotation; "
                            "nearest-edge affinity cannot verify access.")
    area = bounds["width"] * bounds["height"]
    footprint_area = sum(component.placement_width_mm * component.placement_height_mm for component in components)
    occupied_ratio = footprint_area / area if area else 0.0
    if occupied_ratio < 0.1:
        warnings.append("Sparse board: review outline size against enclosure, routing and thermal requirements.")
    return {"status": "review_required" if warnings else "intent_specified",
            "connectors": connectors, "courtyard_area_ratio": round(occupied_ratio, 4),
            "warnings": warnings, "fabrication_ready": False,
            "scope": "Placement intent and readability only; "
                     "part-specific electrical and mechanical review remains required."}


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_modern_project_settings(source: Path) -> None:
    """Reject mutating workflows that would silently ignore legacy KiCad rules."""
    source = source.expanduser().resolve()
    legacy_project = source.with_suffix(".pro")
    modern_project = source.with_suffix(".kicad_pro")
    if legacy_project.is_file() and not modern_project.is_file():
        raise ValueError(
            "Legacy KiCad project settings (.pro) are present without a modern .kicad_pro file; "
            "convert the project rules in KiCad before routing or modifying this board"
        )


def _prepare_settings(source: Path, staged: Path, project_context: str | Path | None = None) -> dict[Path, str | None]:
    _require_modern_project_settings(source)
    snapshots = {source: _hash(source)}
    context_root = Path(project_context).resolve() if project_context is not None else source.resolve().parent
    if context_root.is_file():
        context_root = context_root.parent
    context_inventory = inspect_project_footprint_context(context_root)
    snapshots.update({Path(path): digest for path, digest in context_inventory.get("source_hashes", {}).items()})
    snapshots.update({
        Path(path): digest
        for path, digest in context_inventory.get("library_manifest_sha256", {}).items()
    })
    for path in context_inventory.get("watch_paths", []):
        snapshots.setdefault(Path(path), None)
    stage_project_footprint_context(context_root, staged.parent)
    for suffix in (".kicad_pro", ".kicad_dru"):
        origin = source.with_suffix(suffix)
        if origin.is_file():
            snapshots[origin] = _hash(origin)
            shutil.copy2(origin, staged.with_suffix(suffix))
        else:
            snapshots[origin] = None
    return snapshots


def _check_snapshots(snapshots: dict[Path, str | None]) -> None:
    from .kicad_project_context import footprint_library_tree_sha256
    for source, digest in snapshots.items():
        if source.is_dir():
            current = footprint_library_tree_sha256(source)
        else:
            current = _hash(source) if source.exists() else None
        if current != digest:
            raise ValueError("Source board, session, or project settings changed during processing")


def _restore_settings(source: Path, staged: Path, snapshots: dict[Path, str | None]) -> None:
    _check_snapshots(snapshots)
    for suffix in (".kicad_pro", ".kicad_dru"):
        origin, target = source.with_suffix(suffix), staged.with_suffix(suffix)
        if origin.is_file():
            shutil.copy2(origin, target)
        elif target.exists():
            # Discard defaults/UI state emitted by native SaveBoard. The
            # source had no project here; validate with the same defaults.
            target.unlink()


def _publish(staged: Path, destination: Path, snapshots: dict[Path, str | None], overwrite: bool) -> None:
    publications = [(staged, destination)]
    for suffix in (".kicad_pro", ".kicad_dru"):
        source = staged.with_suffix(suffix)
        if source.is_file():
            publications.append((source, destination.with_suffix(suffix)))
    with _pcb_output_lock(destination.parent):
        _check_snapshots(snapshots)
        for _, target in publications:
            error = _prepare_destination(target, overwrite=overwrite)
            if error:
                raise ValueError(error)
            if target == destination:
                for suffix in (".kicad_pro", ".kicad_dru"):
                    if not staged.with_suffix(suffix).exists() and target.with_suffix(suffix).exists():
                        raise ValueError("Output has unrelated project settings; choose a fresh output name")
        _publish_staged_transaction(staged.parent, publications)


def _design_min_component_gap(spec: dict[str, Any] | None) -> float:
    """Return the effective default or authored board gap seen by the optimizer."""
    minimum = PlacementConfig().min_component_gap_mm
    if not isinstance(spec, dict):
        return minimum
    constraints = spec.get("pcb_constraints", [])
    if not isinstance(constraints, list):
        return minimum
    board_targets = {"board", "board_outline", "pcb", "outline"}
    for rule in constraints:
        if not isinstance(rule, dict):
            continue
        kind = str(rule.get("kind") or rule.get("type") or "").strip().lower()
        target = str(rule.get("target") or rule.get("ref") or "").strip().lower()
        has_board_keys = (target in board_targets or "board_width_mm" in rule
                          or "board_height_mm" in rule or isinstance(rule.get("board"), dict))
        if kind != "placement" or not has_board_keys:
            continue
        value = rule.get("min_component_gap_mm")
        if isinstance(value, bool):
            continue
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value >= 0:
            # _build_constraint_plan applies board gap rules in input order;
            # mirror its last-valid-rule behavior for the attempt diagnostics.
            minimum = value
    return minimum


def _native_reference_gap_floor(footprints: list[dict[str, Any]]) -> float:
    """Estimate a useful spacing floor from native, clearance-padded reference boxes."""
    heights = []
    for footprint in footprints:
        size = footprint.get("reference_size_mm")
        if (isinstance(size, (list, tuple)) and len(size) == 2
                and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and value > 0 for value in size)):
            heights.append(float(size[1]))
    # Native reference boxes already include 0.2 mm silk clearance on every
    # side. Add another 0.4 mm between labels and bodies as placement room.
    return max(heights, default=0.1) + 0.4


def _effective_min_component_gap(
    design_spec: dict[str, Any] | None, reference_gap_floor: float,
    feedback_min_component_gap_mm: float | None = None,
) -> float:
    """Return the maximum authored, native-label, and retry spacing floor."""
    candidates = [_design_min_component_gap(design_spec), reference_gap_floor]
    if feedback_min_component_gap_mm is not None:
        candidates.append(feedback_min_component_gap_mm)
    return max(candidates)


def _native_place_timeout_seconds(footprint_count: int) -> int:
    """Bound KiCad's native placement/silkscreen pass according to board size."""
    if isinstance(footprint_count, bool) or not isinstance(footprint_count, int) or footprint_count < 0:
        raise ValueError("footprint_count must be a non-negative integer")
    return min(
        _NATIVE_PLACE_MAX_TIMEOUT_SECONDS,
        _NATIVE_PLACE_BASE_TIMEOUT_SECONDS + footprint_count * _NATIVE_PLACE_SECONDS_PER_FOOTPRINT,
    )


def _place_pcb_once(
    board_path: str | Path, output_path: str | Path, *, iterations: int = 5000, seed: int = 0,
    overwrite: bool = False, kicad_python_path: str | Path | None = None,
    design_spec: dict[str, Any] | None = None, tidy_references: bool = False,
    feedback_min_component_gap_mm: float | None = None,
    expected_snapshots: dict[Path, str | None] | None = None,
) -> dict[str, Any]:
    """Place an unrouted rectangular board, preserving all real pads and nets."""
    source, destination = Path(board_path).resolve(), Path(output_path).resolve()
    try:
        if not isinstance(tidy_references, bool):
            raise ValueError("tidy_references must be a boolean")
        if (feedback_min_component_gap_mm is not None
                and (isinstance(feedback_min_component_gap_mm, bool)
                     or not isinstance(feedback_min_component_gap_mm, (int, float))
                     or not math.isfinite(feedback_min_component_gap_mm)
                     or feedback_min_component_gap_mm < 0)):
            raise ValueError("feedback_min_component_gap_mm must be a finite non-negative number")
        if destination.suffix.lower() != ".kicad_pcb":
            raise ValueError("Placement output must be a .kicad_pcb file")
        if source == destination:
            raise ValueError("Choose a separate output board to preserve the original PCB")
        _require_modern_project_settings(source)
        if expected_snapshots is not None:
            _check_snapshots(expected_snapshots)
        preflight = preflight_pcb(source)
        if not preflight["routable"]:
            raise ValueError(preflight["reason"])
        error = _prepare_destination(destination, overwrite=overwrite)
        if error:
            raise ValueError(error)
        original_snapshots = {
            path: _hash(path) if path.exists() else None
            for path in (source, source.with_suffix(".kicad_pro"), source.with_suffix(".kicad_dru"))
        }
        if expected_snapshots is not None:
            original_snapshots.update(expected_snapshots)
        inspection = run_kicad("inspect", board=str(source), python_path=kicad_python_path)
        if inspection["status"] != "ok":
            return inspection
        if not inspection["rectangular_outline"]:
            raise ValueError("Automatic placement currently requires a closed rectangular Edge.Cuts outline")
        if inspection["track_count"] or inspection["zone_count"]:
            raise ValueError("Automatic placement requires an unrouted board without copper zones")
        bounds = inspection["bounds"]
        reference_gap_floor = _native_reference_gap_floor(inspection["footprints"])
        intent, constraints = _design_intent(design_spec, inspection["footprints"], bounds)
        authored_constraints = constraints
        constraints = _center_constraints(constraints, inspection["footprints"], bounds)
        authored_gap = _design_min_component_gap(design_spec)
        fallback_gap = max(authored_gap, feedback_min_component_gap_mm or 0.0)
        preferred_gap = authored_gap
        if tidy_references:
            preferred_gap = _effective_min_component_gap(
                design_spec, reference_gap_floor, feedback_min_component_gap_mm)
            constraints.append({"kind": "placement", "target": "board",
                                "min_component_gap_mm": preferred_gap})
        elif feedback_min_component_gap_mm is not None:
            constraints.append({"kind": "placement", "target": "board",
                                "min_component_gap_mm": feedback_min_component_gap_mm})
        effective_gap = preferred_gap if tidy_references else (
            feedback_min_component_gap_mm if feedback_min_component_gap_mm is not None else authored_gap)
        components = []
        center_offsets = {}
        # Native iteration order may depend on regenerated KiCad UUIDs. A seed
        # must describe the same search when physical/electrical input is equal.
        for item in sorted(inspection["footprints"], key=lambda footprint: footprint["ref"]):
            design_component = intent.get(item["ref"])
            component = ComponentDef(
                mpn=item["ref"], source_ref=item["ref"], footprint=item["footprint"],
                category=(design_component.category if design_component else
                          "connector" if item["ref"].startswith("J") else "other"),
                pins=[PinDef(p["number"], p["number"], "passive", "L") for p in item["pads"]],
                pin_nets={p["number"]: p["net"] for p in item["pads"] if p["net"]},
            )
            component.placement_width_mm = item.get("courtyard_width", item["width"])
            component.placement_height_mm = item.get("courtyard_height", item["height"])
            center_x, center_y = item.get("courtyard_center_x", 0), item.get("courtyard_center_y", 0)
            center_offsets[item["ref"]] = (center_x, center_y)
            component.placement_layer = item["layer"]
            component.placement_geometry_status = "board_geometry"
            component.placement_reference_size_mm = item.get("reference_size_mm")
            component.placement_native_footprint_id = item.get("footprint_id")
            component.placement_native_surface_copper_layer_id = item.get(
                "native_surface_copper_layer_id"
            )
            component.placement_native_surface_copper_side = item.get(
                "native_surface_copper_side"
            )
            component.placement_native_board_copper_layer_ids = item.get(
                "native_board_copper_layer_ids"
            )
            component.placement_native_board_copper_layer_inventory_complete = item.get(
                "native_board_copper_layer_inventory_complete"
            )
            component.placement_native_known_copper_layer_ids = item.get(
                "native_known_copper_layer_ids"
            )
            component.placement_native_known_copper_layer_inventory_complete = item.get(
                "native_known_copper_layer_inventory_complete"
            )
            component.placement_native_footprint_is_smd = item.get("native_footprint_is_smd")
            component.placement_native_opposite_courtyard_known = item.get(
                "native_opposite_courtyard_known"
            )
            component.placement_native_opposite_courtyard_present = item.get(
                "native_opposite_courtyard_present"
            )
            inspected_pad_geometry = item.get("pad_geometry") or []
            inspected_pads = item.get("pads") or []
            # Retain each native pad's net even if one geometry accessor fails;
            # the optimizer must count that endpoint as ambiguous, not omit it.
            component.placement_native_pad_geometry = [
                {**(inspected_pad_geometry[index] if index < len(inspected_pad_geometry) else {}),
                 "number": pad.get("number", f"__missing_pad_{index}"),
                 "net": pad.get("net", ""),
                 "known_shape": bool(index < len(inspected_pad_geometry)
                                      and inspected_pad_geometry[index].get("known_shape") is True),
                 "surface_shape_known": bool(index < len(inspected_pad_geometry)
                                              and inspected_pad_geometry[index].get("surface_shape_known") is True),
                 "native_roundrect": bool(index < len(inspected_pad_geometry)
                                           and inspected_pad_geometry[index].get("native_roundrect") is True)}
                for index, pad in enumerate(inspected_pads)
            ]
            if len(inspected_pad_geometry) != len(inspected_pads):
                component.placement_native_pad_geometry.append({
                    "number": "__inventory_mismatch__", "net": "", "known_shape": False,
                    "surface_shape_known": False, "native_roundrect": False,
                })
            component.placement_native_reference_locked = item.get("reference_locked")
            if design_component:
                component.functional_section = design_component.functional_section
                component.block_id = design_component.block_id
                component.placement_parent_ref = getattr(design_component, "placement_parent_ref", "")
                component.placement_parent_pin = getattr(design_component, "placement_parent_pin", "")
                component.placement_role = getattr(design_component, "placement_role", "")
            component.placement_pad_offsets = {}
            pin_offsets = {}
            for pad in item["pads"]:
                number = pad["number"]
                # Duplicate pad numbers identify several physical lands. Keep
                # the net-based fallback instead of picking an arbitrary land.
                pin_offsets[number] = (
                    None if number in pin_offsets else
                    (pad["local_x"] - center_x, pad["local_y"] - center_y)
                )
                if pad["net"]:
                    component.placement_pad_offsets.setdefault(pad["net"], []).append(
                        (pad["local_x"] - center_x, pad["local_y"] - center_y))
            component.placement_pin_offsets = {number: offset for number, offset in pin_offsets.items()
                                               if offset is not None}
            components.append(component)
            if item["locked"]:
                dx, dy = _rotated_offset(center_x, center_y, item["rotation"])
                constraints.append({"kind": "placement", "target": item["ref"],
                                    "x_mm": item["x"] + dx - bounds["x"],
                                    "y_mm": item["y"] + dy - bounds["y"],
                                    "rotation": item["rotation"], "layer": item["layer"]})
        placement_config = PlacementConfig(board_width_mm=bounds["width"], board_height_mm=bounds["height"],
                                           iterations=iterations, seed=seed)
        gap_searches = []
        result = optimize_placement(components, config=placement_config, constraints=constraints)
        if tidy_references:
            gap_searches.append({"min_component_gap_mm": effective_gap, "iterations": iterations, "seed": seed,
                                 "review_required": bool(result.get("quality", {}).get("review_required")),
                                 "reason": "native_reference_gap_preference"})
        if any(abs(result[f"board_{axis}_mm"] - bounds[axis]) > 0.1 for axis in ("width", "height")):
            raise ValueError("Design placement constraints cannot resize the native board outline")
        if (tidy_references and result["quality"]["review_required"]
                and preferred_gap > fallback_gap):
            # The native-derived floor is a preferred seed constraint. If it
            # makes optimizer geometry infeasible, retry once at the exact
            # authored/feedback floor; every ordinary placement and native gate
            # still has to pass before publication.
            effective_gap = fallback_gap
            constraints.append({"kind": "placement", "target": "board",
                                "min_component_gap_mm": effective_gap})
            fallback_result = optimize_placement(components, config=placement_config, constraints=constraints)
            gap_searches.append({"min_component_gap_mm": effective_gap, "iterations": iterations, "seed": seed,
                                 "review_required": bool(fallback_result.get("quality", {}).get("review_required")),
                                 "reason": "native_floor_optimizer_review_required"})
            result = fallback_result
            if any(abs(result[f"board_{axis}_mm"] - bounds[axis]) > 0.1 for axis in ("width", "height")):
                raise ValueError("Design placement constraints cannot resize the native board outline")
        if tidy_references:
            result["native_reference_gap_floor_mm"] = reference_gap_floor
            result["preferred_min_component_gap_mm"] = preferred_gap
            result["effective_min_component_gap_mm"] = effective_gap
            result["gap_search_attempts"] = gap_searches
        if result["quality"]["review_required"]:
            return {"status": "error", "message": "No valid placement found within the board constraints",
                    "placement": result}
        placements = {}
        for ref, placement in result["placements"].items():
            x, y = _native_origin(placement, center_offsets[ref], bounds)
            placements[ref] = {**placement, "x": x, "y": y,
                               "courtyard_center_x_mm": placement["x"] + bounds["x"],
                               "courtyard_center_y_mm": placement["y"] + bounds["y"]}
        result["placements"] = placements
        result["coordinate_origin"] = "kicad_board"
        result["movable_coordinate_resolution_mm"] = 0.0001
        result["native_reference_gap_floor_mm"] = reference_gap_floor
        result["design_intent_sha256"] = (hashlib.sha256(json.dumps(design_spec, sort_keys=True).encode()).hexdigest()
                                           if design_spec is not None else None)
        result["layout_review"] = _layout_review(placements, components, authored_constraints, bounds)
        with tempfile.TemporaryDirectory(prefix=".cw-place-", dir=destination.parent) as folder:
            staged = Path(folder) / destination.name
            snapshots = _prepare_settings(source, staged)
            snapshots.update(original_snapshots)
            applied = run_kicad("place", board=str(source), output=str(staged), placements=placements,
                                tidy_references=tidy_references,
                                python_path=kicad_python_path,
                                timeout=_native_place_timeout_seconds(len(placements)))
            if applied["status"] != "ok":
                return {**applied, "placement": result} if tidy_references else applied
            if tidy_references:
                result["silkscreen"] = applied.get("silkscreen", {})
            _restore_settings(source, staged, snapshots)
            drc = run_drc(staged, evidence_ledger=EvidenceLedger(), project_context=source.parent)
            # An unrouted board necessarily has unconnected pads. All other
            # DRC errors still prevent publication, including pad clearances.
            blockers = [f for f in drc.findings if f.severity == "blocker" and f.rule_id != "CW-DRC-002"]
            if drc.status != "ok" or blockers:
                failure = {"status": "error", "message": "Placed board failed physical DRC", "drc": drc.to_dict()}
                return {**failure, "placement": result} if tidy_references else failure
            _publish(staged, destination, snapshots, overwrite)
        return {"status": "ok", "output_path": str(destination), "output_kind": "placed_kicad_board",
                "placement": result, "drc": {**drc.to_dict(), "board": str(destination)}, "fabrication_ready": False,
                "message": "Real footprints placed and physical DRC checked; the board still requires routing"}
    except (OSError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}


def place_pcb(
    board_path: str | Path, output_path: str | Path, *, iterations: int = 5000, seed: int = 0,
    overwrite: bool = False, kicad_python_path: str | Path | None = None,
    design_spec: dict[str, Any] | None = None, tidy_references: bool = False,
) -> dict[str, Any]:
    """Place a board, retrying a small deterministic seed set for tidy silk."""
    if not isinstance(tidy_references, bool):
        return {"status": "error", "message": "tidy_references must be a boolean"}
    if isinstance(seed, bool) or not isinstance(seed, int):
        return {"status": "error", "message": "seed must be an integer"}
    if not tidy_references:
        return _place_pcb_once(
            board_path, output_path, iterations=iterations, seed=seed, overwrite=overwrite,
            kicad_python_path=kicad_python_path, design_spec=design_spec, tidy_references=False,
        )

    source, destination = Path(board_path).resolve(), Path(output_path).resolve()
    try:
        if destination.suffix.lower() != ".kicad_pcb":
            raise ValueError("Placement output must be a .kicad_pcb file")
        if source == destination:
            raise ValueError("Choose a separate output board to preserve the original PCB")
        error = _prepare_destination(destination, overwrite=overwrite)
        if error:
            raise ValueError(error)
        original_snapshots = {
            path: _hash(path) if path.exists() else None
            for path in (source, source.with_suffix(".kicad_pro"), source.with_suffix(".kicad_dru"))
        }
        attempts: list[dict[str, Any]] = []
        best_result: dict[str, Any] | None = None
        best_score = math.inf
        base_gap = _design_min_component_gap(design_spec)
        feedback_gap: float | None = None
        with tempfile.TemporaryDirectory(prefix=".cw-place-retries-", dir=destination.parent) as folder:
            root = Path(folder)
            for attempt_index, attempt_seed in enumerate((seed, seed + 1, seed + 2), start=1):
                _check_snapshots(original_snapshots)
                candidate_dir = root / f"attempt-{attempt_index}"
                candidate_dir.mkdir()
                candidate_path = candidate_dir / destination.name
                candidate = _place_pcb_once(
                    source, candidate_path, iterations=iterations, seed=attempt_seed, overwrite=False,
                    kicad_python_path=kicad_python_path, design_spec=design_spec, tidy_references=True,
                    feedback_min_component_gap_mm=feedback_gap,
                    expected_snapshots=original_snapshots,
                )
                # A concurrent source/project change invalidates the entire search,
                # even if a candidate happened to pass before it was observed.
                _check_snapshots(original_snapshots)
                silk = candidate.get("placement", {}).get("silkscreen")
                has_native_report = (isinstance(silk, dict) and
                                     "unresolved_count" in silk and "remaining_conflict_count" in silk)
                raw_unresolved = silk.get("unresolved_count") if isinstance(silk, dict) else None
                raw_remaining = silk.get("remaining_conflict_count") if isinstance(silk, dict) else None
                unresolved = raw_unresolved if type(raw_unresolved) is int and raw_unresolved >= 0 else None
                remaining = raw_remaining if type(raw_remaining) is int and raw_remaining >= 0 else None
                label_clear = (has_native_report and type(unresolved) is int and unresolved == 0
                               and type(remaining) is int and remaining == 0)
                success = candidate.get("status") == "ok" and label_clear
                placement = candidate.get("placement")
                reported_gap = placement.get("effective_min_component_gap_mm") if isinstance(placement, dict) else None
                applied_gap = (reported_gap if isinstance(reported_gap, (int, float))
                               and not isinstance(reported_gap, bool) and math.isfinite(reported_gap)
                               and reported_gap >= 0 else None)
                attempts.append({"attempt": attempt_index, "seed": attempt_seed, "iterations": iterations,
                                 "min_component_gap_mm": (applied_gap if applied_gap is not None else
                                                           feedback_gap if feedback_gap is not None else base_gap),
                                 "status": "accepted" if success else "rejected",
                                 "candidate_status": candidate.get("status", "error"),
                                 "unresolved_count": unresolved, "remaining_conflict_count": remaining})
                reason = None
                if not has_native_report:
                    reason = "native_silkscreen_report_missing"
                elif unresolved is None or remaining is None:
                    reason = "native_silkscreen_count_invalid"
                attempts[-1]["rejection_reason"] = None if success else reason or "candidate_failed_checks"
                score = (unresolved + remaining) if unresolved is not None and remaining is not None else math.inf
                if best_result is None or score < best_score:
                    best_result, best_score = candidate, score
                if not success:
                    # Retry only when placement/DRC passed and a complete native
                    # report proves that labels alone need more spacing.
                    if candidate.get("status") != "ok" or not has_native_report:
                        break
                    if unresolved is None or remaining is None:
                        break
                    if not label_clear:
                        current_gap = (applied_gap if applied_gap is not None else
                                       feedback_gap if feedback_gap is not None else base_gap)
                        # The native estimate may already have proved infeasible
                        # and fallen back to a smaller physical gap. Jumping to
                        # that same estimate skips feasible intermediate spacing
                        # and can terminate the retry before native label checks.
                        feedback_gap = max(current_gap + 0.5, base_gap)
                    continue
                _publish(candidate_path, destination, original_snapshots, overwrite)
                candidate["output_path"] = str(destination)
                candidate["drc"] = {**candidate["drc"], "board": str(destination)}
                candidate["placement_attempts"] = attempts
                candidate["selected_attempt"] = attempt_index
                candidate["selected_seed"] = attempt_seed
                candidate["requested_seed"] = seed
                candidate["iterations_per_attempt"] = iterations
                return candidate
        report = best_result or {"status": "error", "message": "No placement candidates were produced"}
        report = copy.deepcopy(report)
        report.pop("output_path", None)
        report_silk = report.get("placement", {}).get("silkscreen")
        if isinstance(report_silk, dict):
            for key in ("unresolved_count", "remaining_conflict_count"):
                value = report_silk.get(key)
                if type(value) is not int or value < 0:
                    report_silk[key] = None
        if isinstance(report.get("drc"), dict):
            report["drc"].pop("board", None)
        return {"status": "error",
                "message": ("No candidate passed physical DRC and native reference-label checks; "
                            "output was not published"),
                "placement_attempts": attempts, "best_candidate": report,
                "iterations_per_attempt": iterations}
    except (OSError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}


def import_routing_session(
    board_path: str | Path, session_path: str | Path, output_path: str | Path, *,
    overwrite: bool = False, kicad_python_path: str | Path | None = None,
    expected_hashes: dict[Path, str | None] | None = None,
) -> dict[str, Any]:
    """Import routes into a separate PCB and publish only after native KiCad DRC."""
    source, session, destination = Path(board_path).resolve(), Path(session_path).resolve(), Path(output_path).resolve()
    try:
        if destination.suffix.lower() != ".kicad_pcb":
            raise ValueError("Routed board output must be a .kicad_pcb file")
        if source == destination:
            raise ValueError("Choose a separate output board to preserve the original PCB")
        if expected_hashes:
            _check_snapshots(expected_hashes)
        error = _prepare_destination(destination, overwrite=overwrite)
        if error:
            raise ValueError(error)
        with tempfile.TemporaryDirectory(prefix=".cw-import-", dir=destination.parent) as folder:
            staged = Path(folder) / destination.name
            snapshots = _prepare_settings(source, staged)
            snapshots.update(expected_hashes or {})
            snapshots[session] = _hash(session)
            imported = run_kicad("import_ses", board=str(source), session=str(session), output=str(staged),
                                 python_path=kicad_python_path)
            if imported["status"] != "ok":
                return imported
            zone_refill = imported.get("zone_refill")
            if imported.get("zone_count", 0) and (
                not isinstance(zone_refill, dict) or zone_refill.get("status") != "verified"
                or zone_refill.get("saved_fill_geometry_verified") is not True
            ):
                return {"status": "error", "message": "Imported routing lacks verified fresh zone fills"}
            _restore_settings(source, staged, snapshots)
            drc = run_drc(staged, evidence_ledger=EvidenceLedger(), project_context=source.parent)
            if not drc.passed:
                return {"status": "error", "message": "Imported routing failed KiCad DRC; board was not published",
                        "drc": drc.to_dict()}
            _publish(staged, destination, snapshots, overwrite)
        return {"status": "ok", "output_path": str(destination), "output_kind": "routed_kicad_board",
                "drc": {**drc.to_dict(), "board": str(destination)},
                **({"zone_refill": zone_refill} if zone_refill is not None else {}),
                "routing_complete": True, "fabrication_ready": False}
    except (OSError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}
