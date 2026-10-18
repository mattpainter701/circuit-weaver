"""Add verified native copper ground planes to a copied KiCad board."""

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

_OPTIONS = {
    "edge_clearance_mm", "clearance_mm", "min_thickness_mm",
    "thermal_gap_mm", "thermal_spoke_width_mm",
}
_PLAN_KEYS = {"net", "layers", "protected_connections", *_OPTIONS}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PLAN_KEYS:
        raise ValueError("Ground-plane plan must contain only 'net', 'layers', 'protected_connections', "
                         "'edge_clearance_mm', 'clearance_mm', 'min_thickness_mm', 'thermal_gap_mm', "
                         "and 'thermal_spoke_width_mm'")
    net = value["net"]
    if not isinstance(net, str) or not net.strip():
        raise ValueError("net must be a non-empty string")
    layers = value["layers"]
    if not isinstance(layers, list) or layers != ["F.Cu", "B.Cu"]:
        raise ValueError("layers must be exactly ['F.Cu', 'B.Cu'] for the supported two-layer board")
    options = {}
    for key in _OPTIONS:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item <= 0:
            raise ValueError(f"{key} must be a positive finite number")
        options[key] = float(item)
    raw_connections = value["protected_connections"]
    if not isinstance(raw_connections, list):
        raise ValueError("protected_connections must be an array")
    connections = _validate_connections(raw_connections) if raw_connections else []
    seen = set()
    for index, row in enumerate(connections):
        key = (row["from_ref"], row["from_pad"], row["to_ref"], row["to_pad"])
        if key in seen:
            raise ValueError(f"protected_connections[{index}] duplicates a directed endpoint pair")
        seen.add(key)
    return {"net": net.strip(), "layers": list(layers), "protected_connections": connections, **options}


def load_ground_plane_plan(path: str | Path) -> dict[str, Any]:
    """Load the strict JSON plan used by the public ground-plane command."""
    return _validate_plan(json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object))


def _valid_zone_stats(rows: Any, layers: list[str], plan: dict[str, Any], filled_area_mm2: float) -> bool:
    """Validate the ordered native per-layer fill evidence returned by the worker."""
    if not isinstance(rows, list) or len(rows) != len(layers):
        return False
    areas = []
    layer_ids = []
    outline_hashes = []
    for row, layer in zip(rows, layers):
        required_fields = {
            "layer_id", "layer_name", "area_mm2", "polygon_count", "island_count", "polygon_sha256",
            "outline_sha256", "net", "clearance_mm", "min_thickness_mm", "thermal_gap_mm",
            "thermal_spoke_width_mm", "pad_connection", "island_removal_mode",
        }
        if not isinstance(row, dict) or not required_fields <= set(row):
            return False
        area, polygon_count, island_count = row["area_mm2"], row["polygon_count"], row["island_count"]
        fingerprint = row["polygon_sha256"]
        outline_hash = row["outline_sha256"]
        layer_id = row["layer_id"]
        if (row["layer_name"] != layer or isinstance(layer_id, bool) or not isinstance(layer_id, int)
                or isinstance(area, bool) or not isinstance(area, (int, float))
                or not math.isfinite(area) or area <= 0
                or isinstance(polygon_count, bool) or not isinstance(polygon_count, int) or polygon_count < 1
                or isinstance(island_count, bool) or not isinstance(island_count, int) or island_count != 0
                or not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint.lower())
                or not isinstance(outline_hash, str) or len(outline_hash) != 64
                or any(character not in "0123456789abcdef" for character in outline_hash.lower())
                or row["net"] != plan["net"]
                or any(isinstance(row[key], bool) or not isinstance(row[key], (int, float))
                       or not math.isfinite(row[key]) or not math.isclose(row[key], plan[key], rel_tol=0, abs_tol=1e-9)
                       for key in ("clearance_mm", "min_thickness_mm", "thermal_gap_mm", "thermal_spoke_width_mm"))
                or row["pad_connection"] != 1 or isinstance(row["pad_connection"], bool)
                or row["island_removal_mode"] != 0 or isinstance(row["island_removal_mode"], bool)):
            return False
        layer_ids.append(layer_id)
        outline_hashes.append(outline_hash)
        areas.append(float(area))
    return (len(set(layer_ids)) == len(layer_ids)
            and len(set(outline_hashes)) == 1
            and math.isclose(round(sum(areas), 6), filled_area_mm2, rel_tol=0.0, abs_tol=1e-6))


def add_ground_plane(
    board_path: str | Path,
    output_path: str | Path,
    plan: dict[str, Any],
    *,
    overwrite: bool = False,
    kicad_python_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create GND zones on a private board copy; publish only after native verification and DRC."""
    source, destination = Path(board_path).resolve(), Path(output_path).resolve()
    try:
        plan = _validate_plan(plan)
        if destination.suffix.lower() != ".kicad_pcb":
            raise ValueError("Ground-plane output must be a .kicad_pcb file")
        if source == destination:
            raise ValueError("Choose a separate output board to preserve the original PCB")
        if not source.is_file() or source.suffix.lower() != ".kicad_pcb":
            raise ValueError("Input must be an existing .kicad_pcb board")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        error = _prepare_destination(destination, overwrite=overwrite)
        if error:
            raise ValueError(error)
        with tempfile.TemporaryDirectory(prefix=".cw-ground-plane-", dir=destination.parent) as folder:
            staged = Path(folder) / destination.name
            snapshots = _prepare_settings(source, staged)
            if snapshots[source] != source_hash:
                raise ValueError("Source board changed during processing")
            input_drc = run_drc(source, evidence_ledger=EvidenceLedger())
            _check_snapshots(snapshots)
            if not input_drc.passed:
                return {"status": "error",
                        "message": "Input board must pass full KiCad DRC before adding ground planes",
                        "drc": input_drc.to_dict()}
            if input_drc.board_sha256 != source_hash:
                raise ValueError("Input DRC evidence does not match the source board bytes")
            changed = run_kicad("add_ground_plane", board=str(source), output=str(staged), plan=plan,
                                python_path=kicad_python_path)
            if changed.get("status") != "ok":
                return changed
            if not staged.is_file():
                return {"status": "error", "message": "KiCad ground-plane worker produced no staged board"}
            report = changed.get("ground_plane")
            rows = report.get("protected_connections") if isinstance(report, dict) else None
            required = (
                "tracks_preserved", "original_footprints_and_text_preserved", "original_board_geometry_preserved",
                "saved_fill_geometry_verified", "saved_connectivity_verified",
            )
            filled_area = report.get("filled_area_mm2") if isinstance(report, dict) else None
            track_count_before = report.get("track_count_before") if isinstance(report, dict) else None
            track_count_after = report.get("track_count_after") if isinstance(report, dict) else None
            island_count = report.get("island_count") if isinstance(report, dict) else None
            zone_stats = report.get("zone_stats") if isinstance(report, dict) else None
            connected_copper_counts = report.get("zone_connected_copper_counts") if isinstance(report, dict) else None
            if (not isinstance(report, dict) or report.get("status") != "ok"
                    or any(report.get(key) is not True for key in required)
                    or report.get("net") != plan["net"] or report.get("layers") != plan["layers"]
                    or report.get("filled_zones") != len(plan["layers"])
                    or report.get("added_zones") != len(plan["layers"])
                    or isinstance(filled_area, bool) or not isinstance(filled_area, (int, float))
                    or not math.isfinite(filled_area) or filled_area <= 0
                    or isinstance(track_count_before, bool) or not isinstance(track_count_before, int)
                    or track_count_before < 0
                    or isinstance(track_count_after, bool) or not isinstance(track_count_after, int)
                    or track_count_before != track_count_after
                    or isinstance(island_count, bool) or not isinstance(island_count, int) or island_count != 0
                    or not isinstance(zone_stats, list) or len(zone_stats) != len(plan["layers"])
                    or not isinstance(connected_copper_counts, list)
                    or len(connected_copper_counts) != len(plan["layers"])
                    or any(isinstance(count, bool) or not isinstance(count, int) or count < 1
                           for count in connected_copper_counts)
                    or not _valid_zone_stats(zone_stats, plan["layers"], plan, filled_area)
                    or not isinstance(rows, list) or len(rows) != len(plan["protected_connections"])
                    or any(not isinstance(row, dict) or row.get("status") != "preserved"
                           or any(row.get(key) != connection[key] for key in (
                               "from_ref", "from_pad", "to_ref", "to_pad",
                           ))
                           or isinstance(row.get("trace_only_length_mm"), bool)
                           or not isinstance(row.get("trace_only_length_mm"), (int, float))
                           or not math.isfinite(row["trace_only_length_mm"])
                           or row["trace_only_length_mm"] < 0
                           or isinstance(row.get("trace_only_vias"), bool)
                           or not isinstance(row.get("trace_only_vias"), int)
                           or row["trace_only_vias"] < 0
                           for row, connection in zip(rows, plan["protected_connections"]))):
                return {"status": "error", "message": "Native worker did not verify ground-plane fill, connectivity, "
                        "track preservation, and every protected connection; output was not published",
                        "ground_plane": report}
            silkscreen = changed.get("silkscreen", {})
            if (not isinstance(silkscreen, dict) or silkscreen.get("unresolved_count") != 0
                    or silkscreen.get("remaining_conflict_count") != 0):
                return {"status": "error", "message": "Ground-plane board failed native silkscreen verification; "
                        "output was not published", "silkscreen": silkscreen}
            _restore_settings(source, staged, snapshots)
            drc = run_drc(staged, evidence_ledger=EvidenceLedger())
            if not drc.passed:
                return {"status": "error",
                        "message": "Ground-plane board failed full KiCad DRC; output was not published",
                        "drc": drc.to_dict()}
            if drc.board_sha256 != hashlib.sha256(staged.read_bytes()).hexdigest():
                raise ValueError("Final DRC evidence does not match the staged ground-plane board bytes")
            _publish(staged, destination, snapshots, overwrite)
        return {"status": "ok", "output_path": str(destination), "output_kind": "ground_plane_kicad_board",
                "source_sha256": source_hash, "ground_plane": report, "silkscreen": silkscreen,
                "drc": {**drc.to_dict(), "board": str(destination)}, "fabrication_ready": False}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "error", "message": str(exc)}
