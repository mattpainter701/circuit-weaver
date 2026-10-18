"""Conservative native removal of traces completely covered by filled planes.

The caller supplies KiCad's ``pcbnew`` module.  Only unlocked straight
``PCB_TRACK`` objects on the selected net can be removed.  Eligibility and
saved-board coverage are decided by native polygon boolean subtraction of the
track's effective copper shape from the union of filled same-net zone shapes.
"""
from __future__ import annotations

import hashlib
import json
import math

try:
    from ._native_copper_graph import CopperGraph
except ImportError:  # Standalone import inside KiCad's native worker.
    from _native_copper_graph import CopperGraph


def _uuid(item):
    try:
        return str(item.m_Uuid.AsString())
    except (AttributeError, RuntimeError):
        return ""


def _xy_mm(pcbnew, point):
    return round(float(pcbnew.ToMM(point.x)), 6), round(float(pcbnew.ToMM(point.y)), 6)


def _track_geometry(item):
    a, b = item.GetStart(), item.GetEnd()
    return {"start": [int(a.x), int(a.y)], "end": [int(b.x), int(b.y)],
            "layer": int(item.GetLayer()), "width": int(item.GetWidth())}


def _track_from_geometry(board, pcbnew, geometry):
    shape = pcbnew.SHAPE_SEGMENT(
        pcbnew.VECTOR2I(*geometry["start"]), pcbnew.VECTOR2I(*geometry["end"]), int(geometry["width"])
    )
    return shape


def _shape_polygon(shape, pcbnew):
    polygon = pcbnew.SHAPE_POLY_SET()
    error_outside = getattr(pcbnew, "ERROR_OUTSIDE", None)
    if error_outside is not None:
        # One IU avoids unbounded zero-error tessellation while rejecting
        # shapes that depend on a sub-IU contact for coverage.
        shape.TransformToPolygon(polygon, 1, error_outside)
    else:
        # KiCad 8's Python bindings expose TransformToPolygon's error-location
        # parameter without exposing its ERROR_LOC enum/constants.  Construct
        # an explicitly circumscribed capsule instead of guessing the enum's
        # SWIG integer value or accepting an inside approximation.
        _conservative_segment_bbox_polygon(shape, pcbnew, polygon)
    if polygon.OutlineCount() <= 0:
        raise ValueError("KiCad could not polygonize an effective copper shape")
    return polygon


def _conservative_segment_bbox_polygon(shape, pcbnew, polygon):
    """Add an integer-coordinate bounding polygon containing a segment capsule."""
    try:
        segment_shape = shape
        if not hasattr(segment_shape, "GetSeg"):
            segment_shape = shape.Cast()
        segment = segment_shape.GetSeg()
        start, end = segment.A, segment.B
        width = int(segment_shape.GetWidth())
    except (AttributeError, RuntimeError, TypeError) as exc:
        raise ValueError("KiCad lacks ERROR_OUTSIDE and shape is not a straight segment") from exc
    if width <= 0:
        raise ValueError("Cannot conservatively polygonize a segment with nonpositive width")

    x0, y0 = int(start.x), int(start.y)
    x1, y1 = int(end.x), int(end.y)
    # A bounding rectangle is coarser than the capsule but always contains it,
    # including a half-IU radius on odd-width traces.  The containment check
    # may therefore decline some safe KiCad 8 candidates, but cannot accept
    # a shape that extends beyond the filled copper.
    radius = (width + 1) // 2
    left, right = min(x0, x1) - radius, max(x0, x1) + radius
    top, bottom = min(y0, y1) - radius, max(y0, y1) + radius
    corners = ((left, top), (right, top), (right, bottom), (left, bottom))
    chain = pcbnew.SHAPE_LINE_CHAIN()
    for x, y in corners:
        chain.Append(pcbnew.VECTOR2I(x, y))
    chain.SetClosed(True)
    polygon.AddOutline(chain)


def _boolean_mode(pcbnew):
    """Return KiCad 8's explicit polygon boolean mode when it is exposed."""
    return getattr(pcbnew.SHAPE_POLY_SET, "PM_STRICTLY_SIMPLE", None)


def _boolean_add(destination, polygons, pcbnew):
    mode = _boolean_mode(pcbnew)
    if mode is None:
        destination.BooleanAdd(polygons)
    else:
        destination.BooleanAdd(polygons, mode)


def _boolean_subtract(destination, subject, clip, pcbnew):
    mode = _boolean_mode(pcbnew)
    if mode is None:
        destination.BooleanSubtract(subject, clip)
    else:
        destination.BooleanSubtract(subject, clip, mode)


def _filled_union(zones, layer, pcbnew, net=None):
    result = pcbnew.SHAPE_POLY_SET()
    count = 0
    for zone in zones:
        if int(zone.GetLayer()) != int(layer):
            continue
        if net is not None and str(zone.GetNetname()) != str(net):
            continue
        if not zone.IsFilled() or not zone.HasFilledPolysForLayer(layer):
            continue
        polygons = zone.GetFilledPolysList(layer)
        if polygons.OutlineCount() <= 0:
            continue
        _boolean_add(result, polygons, pcbnew)
        count += 1
    if not count or result.OutlineCount() <= 0:
        return None
    return result


def _fully_covered(shape_poly, filled_union, pcbnew):
    remainder = pcbnew.SHAPE_POLY_SET()
    _boolean_subtract(remainder, shape_poly, filled_union, pcbnew)
    return remainder.OutlineCount() == 0


def _poly_hash(polygons):
    return hashlib.sha256(polygons.Format(False).encode("utf-8")).hexdigest()


def _zone_config(zone, pcbnew):
    return {
        "clearance_iu": int(zone.GetLocalClearance()),
        "min_thickness_iu": int(zone.GetMinThickness()),
        "thermal_gap_iu": int(zone.GetThermalReliefGap()),
        "thermal_spoke_width_iu": int(zone.GetThermalReliefSpokeWidth()),
        "pad_connection": int(zone.GetPadConnection()),
        "island_removal_mode": int(zone.GetIslandRemovalMode()),
        "min_island_area_iu2": int(zone.GetMinIslandArea()),
        "priority": int(zone.GetAssignedPriority()),
        "layer": int(zone.GetLayer()),
        "net": str(zone.GetNetname()),
    }


def _zone_needs_refill(zone):
    method = getattr(zone, "NeedRefill", None)
    return bool(method()) if method is not None else False


def _zone_row(zone, pcbnew):
    layer = int(zone.GetLayer())
    polys = zone.GetFilledPolysList(layer) if zone.IsFilled() and zone.HasFilledPolysForLayer(layer) else None
    count = int(polys.OutlineCount()) if polys is not None else 0
    islands = [bool(zone.IsIsland(layer, i)) for i in range(count)]
    if any(islands):
        raise ValueError(f"filled {zone.GetNetname()} zone on {zone.GetLayerName()} has floating copper islands")
    area = float(zone.CalculateFilledArea()) / 1e12
    if not zone.IsFilled() or count <= 0 or not math.isfinite(area) or area <= 0:
        raise ValueError(f"{zone.GetNetname()} zone on {zone.GetLayerName()} is not natively filled")
    return {
        "uuid": _uuid(zone), "net": str(zone.GetNetname()), "layer": layer,
        "layer_name": str(zone.GetLayerName()), "config": _zone_config(zone, pcbnew),
        "outline_sha256": hashlib.sha256(zone.Outline().Format(False).encode("utf-8")).hexdigest(),
        "filled_polygon_sha256": _poly_hash(polys), "polygon_count": count,
        "island_count": sum(islands), "area_mm2": round(area, 6),
    }


def _zone_rows(board, pcbnew, net=None):
    zones = [z for z in board.Zones() if net is None or str(z.GetNetname()) == str(net)]
    return sorted((_zone_row(z, pcbnew) for z in zones), key=lambda row: (row["net"], row["layer"], row["uuid"]))


def _track_rows(board):
    rows = []
    for item in board.GetTracks():
        is_via = item.GetClass() == "PCB_VIA"
        if is_via:
            p = item.GetPosition()
            try:
                top_width = int(item.GetWidth(item.TopLayer()))
                bottom_width = int(item.GetWidth(item.BottomLayer()))
            except TypeError:  # KiCad 8/9 compatibility.
                top_width = bottom_width = int(item.GetWidth())
            geometry = {"position": [int(p.x), int(p.y)], "top_layer": int(item.TopLayer()),
                        "bottom_layer": int(item.BottomLayer()), "top_width": top_width,
                        "bottom_width": bottom_width,
                        "drill": int(item.GetDrill())}
            length = 0.0
        else:
            geometry = _track_geometry(item)
            a, b = item.GetStart(), item.GetEnd()
            length = math.hypot(float(b.x - a.x), float(b.y - a.y)) / 1_000_000
        rows.append({"uuid": _uuid(item), "class": item.GetClass(), "net": str(item.GetNetname()),
                     "locked": bool(item.IsLocked()), "geometry": geometry, "length_mm": length})
    return sorted(rows, key=lambda row: row["uuid"])


def _track_totals(rows, net=None):
    selected = [row for row in rows if row["class"] == "PCB_TRACK" and (net is None or row["net"] == net)]
    return len(selected), round(sum(row["length_mm"] for row in selected), 6)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _static_board_digest(board, pcbnew):
    try:
        from ._kicad_worker import _ground_plane_source_geometry_state
    except ImportError:  # Standalone native worker import.
        from _kicad_worker import _ground_plane_source_geometry_state

    footprints_and_drawings = _ground_plane_source_geometry_state(board, pcbnew)
    zones = sorted((str(z.GetNetname()), int(z.GetLayer()), str(z.Outline().Format(False)),
                    json.dumps(_zone_config(z, None), sort_keys=True)) for z in board.Zones())
    return _digest({"footprints_and_drawings": footprints_and_drawings, "zones": zones})


def _pad_map(board):
    result = {}
    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        for pad in fp.Pads():
            key = (ref, str(pad.GetNumber()))
            if key in result:
                raise ValueError(f"duplicate physical pad {ref}.{key[1]}")
            result[key] = pad
    return result


def _protected_routes(board, pcbnew, protected_connections):
    pads = _pad_map(board)
    graph = CopperGraph(board, pcbnew)
    rows, frozen = [], set()
    for index, row in enumerate(protected_connections):
        if not isinstance(row, dict) or not all(k in row for k in ("from_ref", "from_pad", "to_ref", "to_pad")):
            raise ValueError(f"protected_connections[{index}] must identify two physical pads")
        keys = ((str(row["from_ref"]), str(row["from_pad"])), (str(row["to_ref"]), str(row["to_pad"])))
        if any(key not in pads for key in keys):
            raise ValueError(f"protected_connections[{index}] names an unknown physical pad")
        first, last = pads[keys[0]], pads[keys[1]]
        net = str(first.GetNetname())
        if not net or str(last.GetNetname()) != net:
            raise ValueError(f"protected_connections[{index}] endpoints must use the same assigned net")
        route = graph.route(*keys[0], *keys[1])
        if route is None:
            raise ValueError(f"protected connection {keys[0]} to {keys[1]} has no existing trace-only route")
        if "max_length_mm" in row:
            maximum = row["max_length_mm"]
            if (isinstance(maximum, bool) or not isinstance(maximum, (int, float))
                    or not math.isfinite(maximum) or maximum <= 0):
                raise ValueError(f"protected_connections[{index}].max_length_mm must be positive and finite")
            if float(route["length_mm"]) > float(maximum) + 1e-6:
                raise ValueError(f"protected connection {keys[0]} to {keys[1]} exceeds max_length_mm")
        route_item_uuids = set(route.get("item_uuids", ()))
        available_uuids = {_uuid(item) for item in graph.items.get(net, ())}
        if not route_item_uuids or not route_item_uuids.issubset(available_uuids):
            raise ValueError(f"could not map protected route {keys[0]} to {keys[1]} to native track/via items")
        frozen.update(route_item_uuids)
        rows.append({"from_ref": keys[0][0], "from_pad": keys[0][1], "to_ref": keys[1][0], "to_pad": keys[1][1],
                     "status": "preserved", "trace_only_length_mm": round(float(route["length_mm"]), 6),
                     "trace_only_vias": int(route["vias"]), "net": net,
                     "frozen_item_uuids": sorted(route_item_uuids)})
    return graph, rows, frozen


def _connected_pad_map(board, net):
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    pads = _pad_map(board)
    keys = [key for key, pad in pads.items() if str(pad.GetNetname()) == str(net)]
    reachable = {}
    for key in keys:
        pad = pads[key]
        peers = set()
        for connected in connectivity.GetConnectedPads(pad):
            try:
                parent = connected.GetParentFootprint()
                peers.add((str(parent.GetReference()), str(connected.GetNumber())))
            except (AttributeError, RuntimeError):
                continue
        peers.add(key)
        reachable[key] = peers
    return reachable


def _assert_connectivity_not_regressed(before, after):
    for key, peers in before.items():
        if key not in after or not peers.issubset(after[key]):
            lost = sorted(peers - after.get(key, set()))
            raise ValueError(f"native same-net pad connectivity regressed at {key}: {lost}")
    if any(not peers for peers in after.values()):
        raise ValueError("a same-net pad has no native connectivity anchor after plane cleanup")


def _connectivity_json(mapping):
    return {f"{ref}.{pad}": sorted(f"{peer_ref}.{peer_pad}" for peer_ref, peer_pad in peers)
            for (ref, pad), peers in sorted(mapping.items())}


def _check_protected(graph, rows, baseline):
    current = []
    for row, expected in zip(rows, baseline):
        route = graph.route(row["from_ref"], row["from_pad"], row["to_ref"], row["to_pad"])
        if route is None:
            raise ValueError("plane cleanup disconnected a protected trace-only route")
        length, vias = round(float(route["length_mm"]), 6), int(route["vias"])
        if length != expected["trace_only_length_mm"] or vias != expected["trace_only_vias"]:
            raise ValueError("plane cleanup changed a protected trace-only route")
        if not set(row["frozen_item_uuids"]).issubset(set(route.get("item_uuids", ()))):
            raise ValueError("plane cleanup changed copper items on a protected trace-only route")
        current.append({key: row[key] for key in ("from_ref", "from_pad", "to_ref", "to_pad")}
                       | {"status": "preserved", "trace_only_length_mm": length, "trace_only_vias": vias,
                          "protected_path_preserved": True, "net": row["net"],
                          "frozen_item_uuids": row["frozen_item_uuids"]})
    if len(current) != len(baseline):
        raise ValueError("protected route count differs from baseline")
    return current


def _zone_anchors(board, zone):
    connectivity = board.GetConnectivity()
    items = list(connectivity.GetConnectedPads(zone)) + list(connectivity.GetConnectedTracks(zone))
    anchors = [item for item in items if item.GetClass() != "ZONE" and str(item.GetNetname()) == str(zone.GetNetname())]
    if not anchors:
        raise ValueError(f"filled {zone.GetNetname()} zone on {zone.GetLayerName()} has no native copper connection")
    return anchors


def _coverage_for_removed(board, pcbnew, removed, net):
    zones = list(board.Zones())
    for row in removed:
        layer = int(row["geometry"]["layer"])
        coverage = _filled_union(zones, layer, pcbnew, net=net)
        if coverage is None:
            raise ValueError(f"no filled same-layer plane remains for removed trace {row['uuid']}")
        shape = _shape_polygon(_track_from_geometry(board, pcbnew, row["geometry"]), pcbnew)
        if not _fully_covered(shape, coverage, pcbnew):
            raise ValueError(f"refilled planes no longer fully cover removed trace {row['uuid']}")


def apply(board, pcbnew, *, net="GND", protected_connections=()):
    """Remove only unlocked selected-net straight tracks fully inside filled planes."""
    net = str(net)
    all_zones = list(board.Zones())
    target_zones = [zone for zone in all_zones if str(zone.GetNetname()) == net]
    if not target_zones:
        raise ValueError(f"board has no existing {net!r} filled zone")
    for zone in target_zones:
        if (_zone_needs_refill(zone) or not zone.IsFilled()
                or not zone.HasFilledPolysForLayer(zone.GetLayer())):
            raise ValueError(f"existing {net} zone on {zone.GetLayerName()} has no current filled polygons")

    graph_before, protected_rows, frozen = _protected_routes(board, pcbnew, protected_connections)
    before_connected = _connected_pad_map(board, net)
    before_tracks = _track_rows(board)
    before_zone_rows = _zone_rows(board, pcbnew, net)
    static_before = _static_board_digest(board, pcbnew)
    all_count_before, all_length_before = _track_totals(before_tracks)
    selected_count_before, selected_length_before = _track_totals(before_tracks, net)

    same_net_zones = [zone for zone in all_zones if str(zone.GetNetname()) == net]
    unions = {}
    for zone in target_zones:
        layer = int(zone.GetLayer())
        unions.setdefault(layer, _filled_union(same_net_zones, layer, pcbnew, net=net))
    removals = []
    for item in list(board.GetTracks()):
        if item.GetClass() != "PCB_TRACK" or str(item.GetNetname()) != net or item.IsLocked():
            continue
        uid = _uuid(item)
        if not uid or uid in frozen:
            continue
        coverage = unions.get(int(item.GetLayer()))
        if coverage is None:
            continue
        full_shape = _shape_polygon(item.GetEffectiveShape(item.GetLayer()), pcbnew)
        if not _fully_covered(full_shape, coverage, pcbnew):
            continue
        geometry = _track_geometry(item)
        a, b = item.GetStart(), item.GetEnd()
        removals.append({"uuid": uid, "geometry": geometry, "locked": False, "net": net,
                         "layer_name": str(item.GetLayerName()),
                         "centerline_length_mm": round(math.hypot(float(b.x-a.x), float(b.y-a.y))/1_000_000, 6),
                         "effective_shape_sha256": _poly_hash(full_shape)})

    removed_items = []
    for row in removals:
        item = next((candidate for candidate in board.GetTracks() if _uuid(candidate) == row["uuid"]), None)
        if item is None:
            raise ValueError(f"candidate trace {row['uuid']} disappeared before removal")
        removed_items.append(item)
        board.Remove(item)

    if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
        raise ValueError("KiCad native zone refill failed after trace cleanup")
    if any(_zone_needs_refill(zone) or not zone.IsFilled()
           or not zone.HasFilledPolysForLayer(zone.GetLayer()) for zone in all_zones):
        raise ValueError("KiCad did not retain filled polygons for every existing zone")
    after_tracks = _track_rows(board)
    expected_after_ids = {row["uuid"] for row in before_tracks} - {row["uuid"] for row in removals}
    if {row["uuid"] for row in after_tracks} != expected_after_ids:
        raise ValueError("plane cleanup/refill changed copper objects beyond the reported trace removals")
    for row in after_tracks:
        original = next(item for item in before_tracks if item["uuid"] == row["uuid"])
        if row != original:
            raise ValueError(f"plane cleanup changed preserved copper object {row['uuid']}")
    if _static_board_digest(board, pcbnew) != static_before:
        raise ValueError("plane cleanup changed footprint, drawing, zone outline, or zone configuration data")

    after_connected = _connected_pad_map(board, net)
    _assert_connectivity_not_regressed(before_connected, after_connected)
    graph_after = CopperGraph(board, pcbnew)
    verified_protected = _check_protected(graph_after, protected_rows, protected_rows)
    for zone in target_zones:
        _zone_anchors(board, zone)
    _coverage_for_removed(board, pcbnew, removals, net)
    after_zone_rows = _zone_rows(board, pcbnew, net)
    all_count_after, all_length_after = _track_totals(after_tracks)
    selected_count_after, selected_length_after = _track_totals(after_tracks, net)
    if selected_count_before - selected_count_after != len(removals):
        raise ValueError("removed trace count does not match selected-net inventory change")
    return {
        "status": "ok", "net": net,
        "track_count_before": all_count_before, "track_count_after": all_count_after,
        "track_length_before_mm": all_length_before, "track_length_after_mm": all_length_after,
        "selected_net_track_count_before": selected_count_before,
        "selected_net_track_count_after": selected_count_after,
        "selected_net_track_length_before_mm": selected_length_before,
        "selected_net_track_length_after_mm": selected_length_after,
        "length_removed_mm": round(selected_length_before - selected_length_after, 6),
        "removed_count": len(removals), "removed_tracks": removals,
        "zones_before": before_zone_rows, "zones_after": after_zone_rows,
        "protected_connections": verified_protected,
        "protected_item_uuids": sorted(frozen),
        "all_same_net_pad_connectivity_preserved": True,
        "pad_connectivity_before": _connectivity_json(before_connected),
        "pad_connectivity_after": _connectivity_json(after_connected),
        "nonselected_locked_and_via_copper_preserved": True,
        "refilled": True, "refilled_coverage_verified": True,
        "static_board_sha256": static_before, "coverage_polygon_precision_iu": 1,
        "copper_before_sha256": _digest(before_tracks), "copper_after_sha256": _digest(after_tracks),
        # The worker retains these SWIG wrappers through save/reload to avoid
        # KiCad's native ownership issue for just-removed BOARD_ITEM objects.
        "_native_removed_items": removed_items,
    }


def verify(board, pcbnew, report, *, net="GND", protected_connections=()):
    """Verify exact saved zone geometry, coverage, connectivity, and route metrics."""
    if not isinstance(report, dict) or report.get("status") != "ok" or report.get("net") != str(net):
        raise ValueError("plane-cleanup report is invalid for the requested net")
    if _static_board_digest(board, pcbnew) != report.get("static_board_sha256"):
        raise ValueError("saved board changed footprint, drawing, zone outline, or zone configuration data")
    zones = _zone_rows(board, pcbnew, net)
    if zones != report.get("zones_after"):
        raise ValueError("saved filled plane geometry or configuration differs from cleanup report")
    for zone in board.Zones():
        if str(zone.GetNetname()) == str(net):
            if _zone_needs_refill(zone):
                raise ValueError(f"saved {net} zone on {zone.GetLayerName()} is marked for refill")
            _zone_anchors(board, zone)
    _coverage_for_removed(board, pcbnew, report.get("removed_tracks", []), net)
    after_tracks = _track_rows(board)
    if _digest(after_tracks) != report.get("copper_after_sha256"):
        raise ValueError("saved board copper inventory differs from plane-cleanup report")
    after_connectivity = _connected_pad_map(board, net)
    before_connectivity = {}
    for key, values in report.get("pad_connectivity_before", {}).items():
        ref, pad = key.rsplit(".", 1)
        before_connectivity[(ref, pad)] = {tuple(value.rsplit(".", 1)) for value in values}
    _assert_connectivity_not_regressed(before_connectivity, after_connectivity)
    graph = CopperGraph(board, pcbnew)
    expected_rows = report.get("protected_connections", [])
    if len(expected_rows) != len(protected_connections):
        raise ValueError("saved board protected-path count differs from cleanup report")
    if protected_connections:
        current = []
        for spec, expected in zip(protected_connections, expected_rows):
            endpoints = (str(spec["from_ref"]), str(spec["from_pad"]),
                         str(spec["to_ref"]), str(spec["to_pad"]))
            if endpoints != (expected.get("from_ref"), expected.get("from_pad"),
                             expected.get("to_ref"), expected.get("to_pad")):
                raise ValueError("saved board protected paths differ from cleanup report")
            route = graph.route(spec["from_ref"], spec["from_pad"], spec["to_ref"], spec["to_pad"])
            if route is None:
                raise ValueError("saved board disconnected a protected trace-only route")
            length, vias = round(float(route["length_mm"]), 6), int(route["vias"])
            if length != expected["trace_only_length_mm"] or vias != expected["trace_only_vias"]:
                raise ValueError("saved board changed a protected trace-only route")
            if not set(expected.get("frozen_item_uuids", [])).issubset(set(route.get("item_uuids", ()))):
                raise ValueError("saved board changed copper items on a protected trace-only route")
            current.append({**expected, "trace_only_length_mm": length, "trace_only_vias": vias,
                            "protected_path_preserved": True})
    else:
        current = []
    removed_ids = {row["uuid"] for row in report.get("removed_tracks", [])}
    if removed_ids & {row["uuid"] for row in after_tracks}:
        raise ValueError("saved board still contains a reported removed trace")
    present_ids = {row["uuid"] for row in after_tracks}
    if not set(report.get("protected_item_uuids", [])).issubset(present_ids):
        raise ValueError("saved board is missing a copper item from a protected route")
    return {
        "status": "ok", "net": str(net), "saved_fill_geometry_verified": True,
        "saved_coverage_verified": True, "saved_connectivity_verified": True,
        "protected_paths_preserved": True, "protected_connections": current,
        "removed_trace_count": len(removed_ids), "track_count": len(after_tracks),
        "saved_copper_sha256": _digest(after_tracks),
    }
