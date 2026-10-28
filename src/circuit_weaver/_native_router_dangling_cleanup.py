"""Remove explicitly reported, router-generated dangling track objects."""

from __future__ import annotations

import math
from typing import Any


def remove_reported_tracks(
    board: Any,
    pcbnew: Any,
    warning_rows: Any,
    *,
    max_tracks: int = 64,
    enabled_copper_layer_ids: set[int] | frozenset[int],
) -> dict[str, Any]:
    """Remove only unique unlocked tracks named by fresh KiCad dangling warnings.

    The caller owns the source-board and copper-preservation checks. This
    operation validates each warning's native endpoint against the named PCB
    track, and reports the exact geometry removed for host-side DRC gating.
    """
    if isinstance(max_tracks, bool) or not isinstance(max_tracks, int) or not 1 <= max_tracks <= 64:
        raise ValueError("Dangling-track cleanup limit must be from 1 through 64")
    if (not isinstance(enabled_copper_layer_ids, (set, frozenset)) or not enabled_copper_layer_ids
            or any(isinstance(layer, bool) or not isinstance(layer, int) for layer in enabled_copper_layer_ids)):
        raise ValueError("Dangling-track cleanup requires a complete native copper-layer inventory")
    if not isinstance(warning_rows, list) or not warning_rows or len(warning_rows) > max_tracks:
        raise ValueError("Dangling-track cleanup requires a bounded nonempty warning list")

    requested: dict[str, tuple[int, int]] = {}
    for row in warning_rows:
        if not isinstance(row, dict) or row.get("type") != "track_dangling" or row.get("severity") != "warning":
            raise ValueError("Only fresh KiCad track_dangling warning rows can select tracks")
        items = row.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("Each dangling-track warning must identify exactly one native item")
        item = items[0]
        uuid = item.get("uuid")
        pos = item.get("pos")
        if not isinstance(uuid, str) or not uuid or uuid in requested or not isinstance(pos, dict):
            raise ValueError("Dangling-track warning UUID or endpoint is missing or duplicated")
        point = []
        for axis in ("x", "y"):
            value = pos.get(axis)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Dangling-track endpoint coordinates must be finite numbers")
            point.append(round(float(value) * 1_000_000))
        requested[uuid] = (point[0], point[1])

    by_uuid: dict[str, list[Any]] = {}
    for item in board.GetTracks():
        getter = getattr(item, "m_Uuid", None)
        uuid = getter.AsString() if getter is not None and callable(getattr(getter, "AsString", None)) else ""
        if uuid:
            by_uuid.setdefault(str(uuid), []).append(item)

    to_remove = []
    removed_rows = []
    for uuid, expected_endpoint in requested.items():
        matches = by_uuid.get(uuid, [])
        if len(matches) != 1:
            raise ValueError("A reported dangling UUID must resolve to exactly one native board object")
        item = matches[0]
        if type(item).__name__ != "PCB_TRACK" or hasattr(item, "GetDrillValue"):
            raise ValueError("Dangling warning did not identify an ordinary PCB_TRACK")
        if bool(item.IsLocked()):
            raise ValueError("Refusing to remove a locked router-generated track")
        layer_id = int(item.GetLayer())
        layer_name = str(board.GetLayerName(layer_id))
        if layer_id not in enabled_copper_layer_ids:
            raise ValueError("Dangling warning track is not on a copper layer")
        start = (int(item.GetStart().x), int(item.GetStart().y))
        end = (int(item.GetEnd().x), int(item.GetEnd().y))
        # Coordinates in KiCad JSON are rounded to 4 decimal millimeters.
        if not any(
            abs(endpoint[0] - expected_endpoint[0]) <= 50
            and abs(endpoint[1] - expected_endpoint[1]) <= 50
            for endpoint in (start, end)
        ):
            raise ValueError("Dangling warning endpoint does not match the named native track")
        width = int(item.GetWidth())
        net = str(item.GetNetname())
        if width <= 0 or not net:
            raise ValueError("Dangling native track is missing valid copper width or net identity")
        removed_rows.append({
            "uuid": uuid,
            "net": net,
            "layer": layer_name,
            "start_iu": list(start),
            "end_iu": list(end),
            "width_iu": width,
            "locked": False,
        })
        to_remove.append(item)

    for item in to_remove:
        board.Remove(item)
    return {
        "status": "candidate_only_requires_fresh_drc",
        "removed_count": len(removed_rows),
        "removed_tracks": removed_rows,
        "original_copper_subset_required": True,
        "_native_removed_items": to_remove,
    }
