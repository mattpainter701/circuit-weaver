"""Remove selected traces already covered by a native copper plane."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Any

from .autoroute import _prepare_destination
from .drc_runner import run_drc
from .evidence import EvidenceLedger
from .kicad_bridge import run_kicad
from .pcb_layout import _check_snapshots, _prepare_settings, _publish, _restore_settings
from .pcb_return_paths import _validate_connections


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"net", "protected_connections"}:
        raise ValueError("Plane-cleanup plan must contain only 'net' and 'protected_connections'")
    net = value["net"]
    if not isinstance(net, str) or not net.strip():
        raise ValueError("net must be a non-empty string")
    connections = _validate_connections(value["protected_connections"])
    seen = set()
    for index, row in enumerate(connections):
        key = (row["from_ref"], row["from_pad"], row["to_ref"], row["to_pad"])
        if key in seen:
            raise ValueError(f"protected_connections[{index}] duplicates a directed endpoint pair")
        seen.add(key)
    return {"net": net.strip(), "protected_connections": connections}


def load_plane_cleanup_plan(path: str | Path) -> dict[str, Any]:
    """Load and validate the exact JSON plan accepted by this operation."""
    return _validate_plan(json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object))


def _finite_number(value: Any, *, minimum: float = 0.0) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= minimum)


def _valid_zone_config(value: Any, *, net: str, layer: int) -> bool:
    required = {
        "clearance_iu", "min_thickness_iu", "thermal_gap_iu", "thermal_spoke_width_iu",
        "pad_connection", "island_removal_mode", "min_island_area_iu2", "priority", "layer", "net",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("net") != net or value.get("layer") != layer:
        return False
    integer_fields = required - {"net"}
    if any(isinstance(value[key], bool) or not isinstance(value[key], int) for key in integer_fields):
        return False
    return (value["clearance_iu"] >= 0 and value["min_thickness_iu"] > 0
            and value["thermal_gap_iu"] >= 0 and value["thermal_spoke_width_iu"] > 0
            and value["pad_connection"] >= 0 and value["island_removal_mode"] >= 0
            and value["min_island_area_iu2"] >= 0 and value["priority"] >= 0
            and value["layer"] >= 0)


def _valid_plane_cleanup_report(report: Any, plan: dict[str, Any]) -> bool:
    """Fail closed unless worker evidence describes a valid deletion-only cleanup."""
    if not isinstance(report, dict) or report.get("status") != "ok" or report.get("net") != plan["net"]:
        return False
    required_true = (
        "existing_filled_planes_verified", "only_covered_unlocked_traces_removed",
        "no_copper_added", "vias_preserved", "nonselected_copper_preserved",
        "locked_copper_preserved", "original_geometry_preserved", "original_board_geometry_preserved",
        "saved_fill_geometry_verified",
        "saved_coverage_verified", "saved_connectivity_verified", "protected_paths_preserved",
        "all_same_net_pad_connectivity_preserved",
    )
    if any(report.get(key) is not True for key in required_true):
        return False
    removed, before, after = (report.get(key) for key in ("removed_count", "track_count_before", "track_count_after"))
    removed_rows = report.get("removed_tracks")
    if (any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in (removed, before, after))
            or not isinstance(removed_rows, list) or len(removed_rows) != removed
            or before - after != removed
            or not _finite_number(report.get("length_removed_mm"))
            or not _finite_number(report.get("track_length_before_mm"))
            or not _finite_number(report.get("track_length_after_mm"))
            or not math.isclose(report["track_length_before_mm"] - report["track_length_after_mm"],
                                report["length_removed_mm"], rel_tol=0, abs_tol=1e-6)):
        return False
    selected_before = report.get("selected_net_track_count_before")
    selected_after = report.get("selected_net_track_count_after")
    selected_length_before = report.get("selected_net_track_length_before_mm")
    selected_length_after = report.get("selected_net_track_length_after_mm")
    if (any(isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in (selected_before, selected_after))
            or selected_before - selected_after != removed
            or not _finite_number(selected_length_before) or not _finite_number(selected_length_after)
            or not math.isclose(selected_length_before - selected_length_after,
                                report["length_removed_mm"], rel_tol=0, abs_tol=1e-6)):
        return False
    uuids = set()
    for row in removed_rows:
        geometry = row.get("geometry") if isinstance(row, dict) else None
        start = geometry.get("start") if isinstance(geometry, dict) else None
        end = geometry.get("end") if isinstance(geometry, dict) else None
        if (not isinstance(row, dict) or not isinstance(row.get("uuid"), str) or not row["uuid"]
                or row["uuid"] in uuids or row.get("net") != plan["net"]
                or row.get("locked") is not False
                or not _finite_number(row.get("centerline_length_mm"), minimum=0.000001)
                or not isinstance(geometry, dict) or not isinstance(start, list) or len(start) != 2
                or not isinstance(end, list) or len(end) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in (*start, *end))
                or start == end
                or isinstance(geometry.get("layer"), bool) or not isinstance(geometry.get("layer"), int)
                or isinstance(geometry.get("width"), bool) or not isinstance(geometry.get("width"), int)
                or geometry["width"] <= 0):
            return False
        uuids.add(row["uuid"])
    zones_before, zones_after = report.get("zones_before"), report.get("zones_after")
    if (not isinstance(zones_before, list) or not zones_before
            or not isinstance(zones_after, list) or len(zones_before) != len(zones_after)):
        return False
    for before_zone, after_zone in zip(zones_before, zones_after):
        for zone in (before_zone, after_zone):
            for key in ("outline_sha256", "filled_polygon_sha256"):
                value = zone.get(key) if isinstance(zone, dict) else None
                if (not isinstance(value, str) or len(value) != 64
                        or any(character not in "0123456789abcdef" for character in value.lower())):
                    return False
        if (not isinstance(before_zone, dict) or not isinstance(after_zone, dict)
                or before_zone.get("net") != plan["net"] or after_zone.get("net") != plan["net"]
                or not isinstance(before_zone.get("uuid"), str) or not before_zone["uuid"]
                or before_zone.get("uuid") != after_zone.get("uuid")
                or before_zone.get("layer") != after_zone.get("layer")
                or isinstance(before_zone.get("layer"), bool) or not isinstance(before_zone.get("layer"), int)
                or not isinstance(before_zone.get("layer_name"), str) or not before_zone["layer_name"]
                or before_zone.get("layer_name") != after_zone.get("layer_name")
                or before_zone.get("config") != after_zone.get("config")
                or not _valid_zone_config(before_zone.get("config"), net=plan["net"],
                                          layer=before_zone.get("layer"))
                or before_zone.get("outline_sha256") != after_zone.get("outline_sha256")
                or isinstance(before_zone.get("polygon_count"), bool)
                or not isinstance(before_zone.get("polygon_count"), int) or before_zone["polygon_count"] < 1
                or isinstance(after_zone.get("polygon_count"), bool)
                or not isinstance(after_zone.get("polygon_count"), int) or after_zone["polygon_count"] < 1
                or isinstance(before_zone.get("island_count"), bool)
                or not isinstance(before_zone.get("island_count"), int) or before_zone["island_count"] != 0
                or isinstance(after_zone.get("island_count"), bool)
                or not isinstance(after_zone.get("island_count"), int) or after_zone["island_count"] != 0
                or not _finite_number(before_zone.get("area_mm2"), minimum=0.000001)
                or not _finite_number(after_zone.get("area_mm2"), minimum=0.000001)):
            return False
    if len({row["uuid"] for row in zones_before}) != len(zones_before):
        return False
    protected = report.get("protected_connections")
    if not isinstance(protected, list) or len(protected) != len(plan["protected_connections"]):
        return False
    for row, connection in zip(protected, plan["protected_connections"]):
        frozen = row.get("frozen_item_uuids") if isinstance(row, dict) else None
        if (not isinstance(row, dict) or row.get("status") != "preserved"
                or any(row.get(key) != connection[key] for key in ("from_ref", "from_pad", "to_ref", "to_pad"))
                or not _finite_number(row.get("trace_only_length_mm"))
                or isinstance(row.get("trace_only_vias"), bool)
                or not isinstance(row.get("trace_only_vias"), int) or row["trace_only_vias"] < 0
                or not isinstance(frozen, list) or not frozen
                or any(not isinstance(item, str) or not item for item in frozen)
                or row.get("protected_path_preserved") is not True):
            return False
        if ("max_length_mm" in connection
                and row["trace_only_length_mm"] > connection["max_length_mm"] + 1e-6):
            return False
        if uuids.intersection(frozen):
            return False
    return True


def cleanup_plane_traces(
    board_path: str | Path,
    output_path: str | Path,
    plan: dict[str, Any],
    *,
    overwrite: bool = False,
    kicad_python_path: str | Path | None = None,
) -> dict[str, Any]:
    """Remove redundant plane-covered traces on a private copy, after native verification and DRC."""
    source, destination = Path(board_path).resolve(), Path(output_path).resolve()
    try:
        plan = _validate_plan(plan)
        if destination.suffix.lower() != ".kicad_pcb":
            raise ValueError("Plane-cleanup output must be a .kicad_pcb file")
        if source == destination:
            raise ValueError("Choose a separate output board to preserve the original PCB")
        if not source.is_file() or source.suffix.lower() != ".kicad_pcb":
            raise ValueError("Input must be an existing .kicad_pcb board")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        error = _prepare_destination(destination, overwrite=overwrite)
        if error:
            raise ValueError(error)
        with tempfile.TemporaryDirectory(prefix=".cw-plane-cleanup-", dir=destination.parent) as folder:
            staged = Path(folder) / destination.name
            snapshots = _prepare_settings(source, staged)
            if snapshots[source] != source_hash:
                raise ValueError("Source board changed during processing")
            input_drc = run_drc(source, evidence_ledger=EvidenceLedger())
            _check_snapshots(snapshots)
            if not input_drc.passed:
                return {"status": "error", "message": "Input board must pass full KiCad DRC before plane cleanup",
                        "drc": input_drc.to_dict()}
            if input_drc.board_sha256 != source_hash:
                raise ValueError("Input DRC evidence does not match the source board bytes")
            changed = run_kicad("cleanup_plane_traces", board=str(source), output=str(staged), plan=plan,
                                python_path=kicad_python_path)
            if changed.get("status") != "ok":
                return changed
            if not staged.is_file():
                return {"status": "error", "message": "KiCad plane-cleanup worker produced no staged board"}
            report = changed.get("plane_cleanup")
            if not _valid_plane_cleanup_report(report, plan):
                return {"status": "error", "message": "Native worker did not verify plane coverage, deletion-only "
                        "copper changes, saved connectivity, and protected paths; output was not published",
                        "plane_cleanup": report}
            silkscreen = changed.get("silkscreen", {})
            if (not isinstance(silkscreen, dict) or silkscreen.get("unresolved_count") != 0
                    or silkscreen.get("remaining_conflict_count") != 0):
                return {"status": "error", "message": "Plane-cleaned board failed native silkscreen verification; "
                        "output was not published", "silkscreen": silkscreen}
            _restore_settings(source, staged, snapshots)
            drc = run_drc(staged, evidence_ledger=EvidenceLedger())
            if not drc.passed:
                return {"status": "error",
                        "message": "Plane-cleaned board failed full KiCad DRC; output was not published",
                        "drc": drc.to_dict()}
            if drc.board_sha256 != hashlib.sha256(staged.read_bytes()).hexdigest():
                raise ValueError("Final DRC evidence does not match the staged plane-cleaned board bytes")
            _publish(staged, destination, snapshots, overwrite)
        return {"status": "ok", "output_path": str(destination), "output_kind": "plane_cleaned_kicad_board",
                "source_sha256": source_hash, "plane_cleanup": report, "silkscreen": silkscreen,
                "drc": {**drc.to_dict(), "board": str(destination)}, "fabrication_ready": False}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "error", "message": str(exc)}
