"""Remove explicitly reported, router-generated dangling tracks or vias."""

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
    """Remove only unique unlocked copper named by fresh KiCad dangling warnings.

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

    requested: dict[str, dict[str, Any]] = {}
    for row in warning_rows:
        if not isinstance(row, dict) or row.get("severity") != "warning":
            raise ValueError("Only fresh KiCad dangling-copper warning rows can select copper")
        warning_type = row.get("type")
        if warning_type not in {"track_dangling", "via_dangling"}:
            raise ValueError("Only fresh KiCad track_dangling or via_dangling warnings can select copper")
        items = row.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("Each dangling-copper warning must identify exactly one native item")
        item = items[0]
        uuid = item.get("uuid")
        pos = item.get("pos")
        if not isinstance(uuid, str) or not uuid or uuid in requested or not isinstance(pos, dict):
            raise ValueError("Dangling-copper warning UUID or position is missing or duplicated")
        point = []
        for axis in ("x", "y"):
            value = pos.get(axis)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Dangling-copper position coordinates must be finite numbers")
            point.append(round(float(value) * 1_000_000))
        requested[uuid] = {"kind": warning_type, "position": (point[0], point[1])}

    by_uuid: dict[str, list[Any]] = {}
    for item in board.GetTracks():
        getter = getattr(item, "m_Uuid", None)
        uuid = getter.AsString() if getter is not None and callable(getattr(getter, "AsString", None)) else ""
        if uuid:
            by_uuid.setdefault(str(uuid), []).append(item)

    to_remove = []
    removed_tracks = []
    removed_vias = []
    for uuid, selection in requested.items():
        matches = by_uuid.get(uuid, [])
        if len(matches) != 1:
            raise ValueError("A reported dangling UUID must resolve to exactly one native board object")
        item = matches[0]
        if bool(item.IsLocked()):
            raise ValueError("Refusing to remove locked router-generated copper")
        kind = selection["kind"]
        expected_position = selection["position"]
        net = str(item.GetNetname())
        if not net:
            raise ValueError("Dangling native copper is missing net identity")
        if kind == "track_dangling":
            if type(item).__name__ != "PCB_TRACK" or hasattr(item, "GetDrillValue"):
                raise ValueError("track_dangling warning did not identify an ordinary PCB_TRACK")
            layer_id = int(item.GetLayer())
            layer_name = str(board.GetLayerName(layer_id))
            if layer_id not in enabled_copper_layer_ids:
                raise ValueError("Dangling warning track is not on an enabled copper layer")
            start = (int(item.GetStart().x), int(item.GetStart().y))
            end = (int(item.GetEnd().x), int(item.GetEnd().y))
            # KiCad DRC JSON coordinates are rounded to 4 decimal millimeters.
            if not any(abs(endpoint[0] - expected_position[0]) <= 50
                       and abs(endpoint[1] - expected_position[1]) <= 50
                       for endpoint in (start, end)):
                raise ValueError("Dangling warning endpoint does not match the named native track")
            width = int(item.GetWidth())
            if width <= 0 or not layer_name:
                raise ValueError("Dangling native track is missing valid copper width or layer identity")
            removed_tracks.append({
                "uuid": uuid, "net": net, "layer": layer_name,
                "start_iu": list(start), "end_iu": list(end), "width_iu": width, "locked": False,
            })
        else:
            if type(item).__name__ != "PCB_VIA":
                raise ValueError("via_dangling warning did not identify an ordinary PCB_VIA")
            top, bottom = int(item.TopLayer()), int(item.BottomLayer())
            if top == bottom or top not in enabled_copper_layer_ids or bottom not in enabled_copper_layer_ids:
                raise ValueError("Dangling via span does not match enabled copper layers")
            position = item.GetPosition()
            native_position = (int(position.x), int(position.y))
            if (abs(native_position[0] - expected_position[0]) > 50
                    or abs(native_position[1] - expected_position[1]) > 50):
                raise ValueError("Dangling warning position does not match the named native via")
            try:
                widths = (int(item.GetWidth(top)), int(item.GetWidth(bottom)))
            except TypeError:
                width = int(item.GetWidth())
                widths = (width, width)
            drill_getter = getattr(item, "GetDrillValue", None)
            if not callable(drill_getter):
                drill_getter = getattr(item, "GetDrill", None)
            if not callable(drill_getter):
                raise ValueError("Dangling native via has no supported drill-size accessor")
            drill = int(drill_getter())
            if min(widths) <= 0 or drill <= 0 or drill >= min(widths):
                raise ValueError("Dangling native via is missing valid annular width or drill dimensions")
            top_name, bottom_name = str(board.GetLayerName(top)), str(board.GetLayerName(bottom))
            via_type_getter = getattr(item, "GetViaType", None)
            removed_vias.append({
                "uuid": uuid, "net": net, "layer": f"{top_name}-{bottom_name}",
                "position_iu": list(native_position),
                "top_layer_id": top, "bottom_layer_id": bottom,
                "top_layer": top_name, "bottom_layer": bottom_name,
                "widths_iu": list(widths), "drill_iu": drill,
                "via_type": int(via_type_getter()) if callable(via_type_getter) else None,
                "locked": False,
            })
        to_remove.append(item)

    for item in to_remove:
        board.Remove(item)
    removed_count = len(removed_tracks) + len(removed_vias)
    return {
        "status": "candidate_only_requires_fresh_drc",
        # Keep the legacy count track-only; the aggregate explicitly covers
        # both tracks and vias for the shared host-side cleanup budget.
        "removed_count": len(removed_tracks),
        "removed_track_count": len(removed_tracks),
        "removed_via_count": len(removed_vias),
        "removed_item_count": removed_count,
        "removed_tracks": removed_tracks,
        "removed_vias": removed_vias,
        "original_copper_subset_required": True,
        "_native_removed_items": to_remove,
    }
