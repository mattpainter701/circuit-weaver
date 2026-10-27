"""Native, fail-closed ground-plane creation on selected enabled copper layers.

The caller supplies KiCad's pcbnew module.  This module only adds two native
zones and fills them; it never edits existing tracks, vias, footprints, or text.
"""
from __future__ import annotations

import hashlib
import math

try:
    from ._native_copper_graph import CopperGraph
except ImportError:  # Standalone module import inside KiCad's native worker.
    from _native_copper_graph import CopperGraph


def _mm_point(pcbnew, point):
    return float(pcbnew.ToMM(point.x)), float(pcbnew.ToMM(point.y))


def _point(pcbnew, xy):
    return pcbnew.VECTOR2I(pcbnew.FromMM(xy[0]), pcbnew.FromMM(xy[1]))


def _number(options, name, default, *, minimum=0.0):
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return float(value)


def _outline_bounds(board, pcbnew):
    """Accept only a four-segment, axis-aligned closed rectangular Edge.Cuts."""
    segments = []
    for shape in board.GetDrawings():
        if int(shape.GetLayer()) != int(pcbnew.Edge_Cuts):
            continue
        if shape.GetClass() != "PCB_SHAPE" or shape.GetShape() != pcbnew.SHAPE_T_SEGMENT:
            raise ValueError("ground planes currently require a rectangular segment-only Edge.Cuts outline")
        a, b = _mm_point(pcbnew, shape.GetStart()), _mm_point(pcbnew, shape.GetEnd())
        if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6:
            raise ValueError("ground planes currently require an axis-aligned rectangular Edge.Cuts outline")
        segments.append((a, b))
    if len(segments) != 4:
        raise ValueError("ground planes currently require exactly four rectangular Edge.Cuts segments")
    vertices = []
    for a, b in segments:
        vertices.extend((a, b))
    unique = {(round(x, 6), round(y, 6)) for x, y in vertices}
    if len(unique) != 4 or any(sum(v == p for v in vertices) != 2 for p in unique):
        raise ValueError("Edge.Cuts must form one closed rectangle without extra segments")
    xs, ys = [p[0] for p in unique], [p[1] for p in unique]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x1 <= x0 or y1 <= y0 or unique != {(x0, y0), (x0, y1), (x1, y0), (x1, y1)}:
        raise ValueError("Edge.Cuts must form one non-degenerate rectangle")
    expected = {frozenset(((x0, y0), (x1, y0))), frozenset(((x1, y0), (x1, y1))),
                frozenset(((x1, y1), (x0, y1))), frozenset(((x0, y1), (x0, y0)))}
    actual = {frozenset((tuple(round(v, 6) for v in a), tuple(round(v, 6) for v in b))) for a, b in segments}
    if actual != expected:
        raise ValueError("Edge.Cuts must form one closed rectangle")
    return x0, y0, x1, y1


def _pad_map(board):
    """Index logical ref.pad names without discarding their physical members."""
    result = {}
    identities = set()
    references = set()
    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        physical_pads = list(fp.Pads())
        if physical_pads:
            if ref in references:
                raise ValueError(f"duplicate footprint reference {ref}")
            references.add(ref)
        for pad in physical_pads:
            key = (ref, str(pad.GetNumber()))
            pad_id = _native_item_id(pad)
            if pad_id in identities:
                raise ValueError("native pad connectivity identities are not unique")
            identities.add(pad_id)
            result.setdefault(key, []).append(pad)
    for (ref, number), members in result.items():
        nets = {str(pad.GetNetname()) for pad in members}
        if len(nets) != 1:
            raise ValueError(f"physical members of logical pad {ref}.{number} have conflicting assigned nets")
    return result


def _pad_label(reference, pad, *, disambiguate=False):
    label = f"{reference}.{pad.GetNumber()}"
    return f"{label}[{_native_item_id(pad)}]" if disambiguate else label


def _ensure_protected_endpoints_unambiguous(endpoint_groups, index):
    """The trace graph addresses ref.pad logically, so it cannot bound each duplicate member."""
    if any(len(group) > 1 for group in endpoint_groups):
        raise ValueError(
            f"protected_connections[{index}] uses a repeated physical pad endpoint whose trace-only route is ambiguous"
        )


def _trace_route(graph, row):
    route = graph.route(row["from_ref"], row["from_pad"], row["to_ref"], row["to_pad"])
    if route is None:
        raise ValueError(f"protected connection {row['from_ref']}.{row['from_pad']} to "
                         f"{row['to_ref']}.{row['to_pad']} has no existing trace-only route")
    return {"status": "preserved", "trace_only_length_mm": round(float(route["length_mm"]), 6),
            "trace_only_vias": int(route["vias"])}


def _native_item_id(item):
    try:
        value = item.m_Uuid.AsString()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("native connectivity item UUID is unavailable") from exc
    if not isinstance(value, str) or not value:
        raise ValueError("native connectivity item UUID is empty or invalid")
    return value


def _connected_copper_neighbors(connectivity, pad, filled_zones):
    """Walk native copper neighbors and prove contact with a filled zone."""
    pads = getattr(connectivity, "GetConnectedPads", None)
    tracks = getattr(connectivity, "GetConnectedTracks", None)
    if not callable(pads) or not callable(tracks):
        raise ValueError("native copper-neighbor connectivity queries are unavailable")
    net = str(pad.GetNetname())
    anchors = {}
    for zone in filled_zones:
        if str(zone.GetNetname()) != net:
            continue
        for item in list(pads(zone)) + list(tracks(zone)):
            if str(item.GetNetname()) == net and str(item.GetClass()) != "ZONE":
                anchors.setdefault(_native_item_id(item), []).append(zone)
    pending, visited, members, reached_zones = [pad], set(), [], {}
    while pending:
        item = pending.pop()
        item_id = _native_item_id(item)
        if item_id in visited or str(item.GetNetname()) != net:
            continue
        # Never bridge distinct fill islands through their common ZONE parent.
        if str(item.GetClass()) == "ZONE":
            continue
        visited.add(item_id)
        members.append(item)
        for zone in anchors.get(item_id, ()):
            reached_zones[_native_item_id(zone)] = zone
        pending.extend(list(pads(item)) + list(tracks(item)))
    return members + list(reached_zones.values())


def _connected_copper_items(search, pad, connectivity=None, filled_zones=()):
    """Adapt modern flags, KiCad 9 vectors, and KiCad 8 native neighbors."""
    try:
        return list(search(pad))
    except TypeError:
        # KiCad 8 requires an explicit std::initializer_list<KICAD_T>;
        # newer bindings provide the all-conductor default themselves.
        import pcbnew

        names = ("PCB_PAD_T", "PCB_TRACE_T", "PCB_ARC_T", "PCB_VIA_T", "PCB_ZONE_T")
        try:
            item_types = [getattr(pcbnew, name) for name in names]
        except AttributeError as exc:
            raise ValueError("native conductive item-type filter is unavailable") from exc
        try:
            return list(search(pad, item_types, False))
        except TypeError:
            # KiCad 8 exposes initializer_list without a usable Python
            # conversion. Its stable one-argument neighbor APIs remain native.
            return _connected_copper_neighbors(connectivity, pad, filled_zones)


def _unconnected_plane_pads_by_net(board, nets, *, rebuild=True):
    """Find pads whose native transitive cluster reaches no filled net zone.

    BOARD_ITEM.IsConnected() is a type test, not a graph query. KiCad's
    GetConnectedItems() searches connectivity clusters with zones included.
    Cache pad members of each result so a routed net needs one native search.
    Final whole-board DRC separately rejects opens between distinct clusters.
    """
    if rebuild:
        board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    search = getattr(connectivity, "GetConnectedItems", None)
    if not callable(search):
        raise ValueError("native transitive connectivity query is unavailable")
    result = {net: [] for net in nets}
    zone_ids = {net: set() for net in result}
    filled_zones = []
    for zone in board.Zones():
        net = str(zone.GetNetname())
        if (net in zone_ids and not zone.GetIsRuleArea()
                and str(board.GetLayerName(int(zone.GetLayer()))).endswith(".Cu") and zone.IsFilled()
                and zone.HasFilledPolysForLayer(zone.GetLayer())):
            zone_ids[net].add(_native_item_id(zone))
            filled_zones.append(zone)
    cached = {net: {} for net in result}
    _pad_map(board)  # Validate identity and net consistency for every physical pad.
    pad_ids = set()
    logical_counts = {}
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            key = (str(footprint.GetReference()), str(pad.GetNumber()))
            logical_counts[key] = logical_counts.get(key, 0) + 1
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            net = str(pad.GetNetname())
            if net not in result:
                continue
            pad_id = _native_item_id(pad)
            if pad_id in pad_ids:
                raise ValueError("native pad connectivity identities are not unique")
            pad_ids.add(pad_id)
            if pad_id not in cached[net]:
                members = _connected_copper_items(search, pad, connectivity, filled_zones)
                same_net = [item for item in members if str(item.GetNetname()) == net]
                ids = {_native_item_id(item) for item in same_net}
                attached = bool(ids & zone_ids[net])
                cached[net][pad_id] = attached
                for item in same_net:
                    if str(item.GetClass()) == "PAD":
                        cached[net][_native_item_id(item)] = attached
            if not cached[net][pad_id]:
                key = (str(footprint.GetReference()), str(pad.GetNumber()))
                result[net].append(_pad_label(key[0], pad, disambiguate=logical_counts[key] > 1))
    return {net: sorted(pads) for net, pads in result.items()}


def _connected_ground_pads(board, net, *, allow_unconnected=False):
    unconnected = _unconnected_plane_pads_by_net(board, [str(net)])[str(net)]
    if unconnected and not allow_unconnected:
        raise ValueError("filled ground plane leaves pads unconnected: " + ", ".join(unconnected))
    return unconnected


def _copper_layer_ids(board, pcbnew):
    """Resolve actual enabled copper layer names from this board's native stack."""
    stack = board.GetEnabledLayers().CuStack()
    seq = getattr(stack, "Seq", None)
    values = seq() if callable(seq) else stack
    result = {}
    for raw in values:
        layer_id = int(raw)
        name = str(board.GetLayerName(layer_id))
        if name in result or not name.endswith(".Cu"):
            raise ValueError("board exposes an invalid or duplicate enabled copper layer name")
        result[name] = layer_id
    if len(result) != int(board.GetDesignSettings().GetCopperLayerCount()):
        raise ValueError("board copper layer inventory is incomplete")
    return result


def _zone_pad_anchors(board, zone):
    connectivity = board.GetConnectivity()
    # These one-argument queries are stable across KiCad 8, 9, and 10. The
    # track vector includes vias; pads are returned separately.
    connected = list(connectivity.GetConnectedPads(zone)) + list(connectivity.GetConnectedTracks(zone))
    anchors = [item for item in connected if item.GetClass() != "ZONE"
               and str(item.GetNetname()) == str(zone.GetNetname())]
    if not anchors:
        layer_name = board.GetLayerName(int(zone.GetLayer()))
        raise ValueError(f"filled {zone.GetNetname()} zone on {layer_name} has no native copper connection")
    return anchors


def _zone_stats(zone, pcbnew, layer_name):
    layer = int(zone.GetLayer())
    polys = zone.GetFilledPolysList(layer)
    count = int(polys.OutlineCount())
    islands = [bool(zone.IsIsland(layer, index)) for index in range(count)]
    if any(islands):
        raise ValueError(f"filled {zone.GetNetname()} zone on {zone.GetLayerName()} has floating copper islands")
    area = float(zone.CalculateFilledArea()) / 1e12
    if not math.isfinite(area) or area <= 0:
        raise ValueError(f"filled {zone.GetNetname()} zone on {zone.GetLayerName()} has no positive area")
    polygon_text = polys.Format(False)
    outline_text = zone.Outline().Format(False)
    return {"layer_id": layer, "layer_name": layer_name, "area_mm2": round(area, 6), "polygon_count": count,
            "island_count": sum(islands),
            "polygon_sha256": hashlib.sha256(polygon_text.encode("utf-8")).hexdigest(),
            "outline_sha256": hashlib.sha256(outline_text.encode("utf-8")).hexdigest(),
            "net": str(zone.GetNetname()),
            "clearance_mm": round(float(pcbnew.ToMM(zone.GetLocalClearance())), 6),
            "min_thickness_mm": round(float(pcbnew.ToMM(zone.GetMinThickness())), 6),
            "thermal_gap_mm": round(float(pcbnew.ToMM(zone.GetThermalReliefGap())), 6),
            "thermal_spoke_width_mm": round(float(pcbnew.ToMM(zone.GetThermalReliefSpokeWidth())), 6),
            "pad_connection": int(zone.GetPadConnection()),
            "island_removal_mode": int(zone.GetIslandRemovalMode())}


def _ground_zones(board, pcbnew, net, layers):
    layer_ids = _copper_layer_ids(board, pcbnew)
    if any(name not in layer_ids for name in layers):
        raise ValueError("requested ground-plane layer is not enabled copper on this board")
    found = {}
    for zone in board.Zones():
        if str(zone.GetNetname()) != str(net):
            continue
        layer = int(zone.GetLayer())
        if layer in found:
            raise ValueError(f"multiple {net} zones found on one requested layer")
        found[layer] = zone
    wanted = {layer_ids[layer] for layer in layers}
    if set(found) != wanted:
        raise ValueError(f"expected exactly one {net} zone on each of {', '.join(layers)}")
    return [found[layer_ids[name]] for name in layers]


def _create_zone(board, pcbnew, net_item, layer, bounds, options):
    x0, y0, x1, y1 = bounds
    edge_clearance = _number(options, "edge_clearance_mm", 0.5)
    if (x1 - x0) <= 2 * edge_clearance or (y1 - y0) <= 2 * edge_clearance:
        raise ValueError("edge_clearance_mm leaves no usable copper area")
    zone = pcbnew.ZONE(board)
    zone.SetLayer(layer)
    zone.SetNet(net_item)
    zone.SetLocalClearance(pcbnew.FromMM(_number(options, "clearance_mm", 0.25)))
    zone.SetMinThickness(pcbnew.FromMM(_number(options, "min_thickness_mm", 0.2, minimum=0.01)))
    zone.SetThermalReliefGap(pcbnew.FromMM(_number(options, "thermal_gap_mm", 0.25)))
    zone.SetThermalReliefSpokeWidth(pcbnew.FromMM(_number(options, "thermal_spoke_width_mm", 0.3, minimum=0.01)))
    zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    zone.SetMinIslandArea(0)
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    outline = zone.Outline()
    outline.NewOutline()
    for p in ((x0 + edge_clearance, y0 + edge_clearance),
              (x1 - edge_clearance, y0 + edge_clearance),
              (x1 - edge_clearance, y1 - edge_clearance),
              (x0 + edge_clearance, y1 - edge_clearance)):
        outline.Append(_point(pcbnew, p))
    board.Add(zone)
    return zone


def apply(board, pcbnew, *, net="GND", layers=("F.Cu", "B.Cu"), options=None,
          protected_connections=(), pre_route=False):
    """Add and fill one ground zone on each requested enabled copper layer.

    The board must have a rectangular outline, enabled copper layers, no
    existing zones, and an existing named net. Existing copper objects are preserved.
    """
    options = dict(options or {})
    if not isinstance(pre_route, bool):
        raise ValueError("pre_route must be a boolean")
    if list(board.Zones()):
        raise ValueError("native ground planes require a board without existing zones")
    layer_ids = _copper_layer_ids(board, pcbnew)
    if (not isinstance(layers, (tuple, list)) or not layers
            or any(not isinstance(name, str) or name not in layer_ids for name in layers)
            or len(layers) != len(set(layers))):
        raise ValueError("layers must contain unique enabled copper layer names from this board")
    bounds = _outline_bounds(board, pcbnew)
    net_item = next((item for item in board.GetNetInfo().NetsByName().values()
                     if str(item.GetNetname()) == str(net)), None)
    if net_item is None or int(net_item.GetNetCode()) <= 0:
        raise ValueError(f"board has no assigned {net!r} net")
    pads = _pad_map(board)
    rows = []
    trace_graph = CopperGraph(board, pcbnew)
    for index, row in enumerate(protected_connections):
        if not isinstance(row, dict) or not all(k in row for k in ("from_ref", "from_pad", "to_ref", "to_pad")):
            raise ValueError(f"protected_connections[{index}] must identify two physical pads")
        keys = ((str(row["from_ref"]), str(row["from_pad"])), (str(row["to_ref"]), str(row["to_pad"])))
        if any(key not in pads for key in keys):
            raise ValueError(f"protected_connections[{index}] names an unknown physical pad")
        first_net, last_net = str(pads[keys[0]][0].GetNetname()), str(pads[keys[1]][0].GetNetname())
        if not first_net or first_net != last_net:
            raise ValueError(f"protected_connections[{index}] endpoints must use the same assigned net")
        _ensure_protected_endpoints_unambiguous((pads[keys[0]], pads[keys[1]]), index)
        route = _trace_route(trace_graph, {"from_ref": keys[0][0], "from_pad": keys[0][1],
                                           "to_ref": keys[1][0], "to_pad": keys[1][1]})
        if "max_length_mm" in row:
            maximum = row["max_length_mm"]
            if (
                isinstance(maximum, bool)
                or not isinstance(maximum, (int, float))
                or not math.isfinite(maximum)
                or maximum <= 0
            ):
                raise ValueError(f"protected_connections[{index}].max_length_mm must be positive and finite")
            if route["trace_only_length_mm"] > maximum + 1e-6:
                raise ValueError(f"protected connection {keys[0]} to {keys[1]} exceeds max_length_mm")
        rows.append({"from_ref": keys[0][0], "from_pad": keys[0][1],
                     "to_ref": keys[1][0], "to_pad": keys[1][1], **route})
    before = sum(1 for _ in board.GetTracks())
    zones = [_create_zone(board, pcbnew, net_item, layer_ids[name], bounds, options) for name in layers]
    filler = pcbnew.ZONE_FILLER(board)
    if not filler.Fill(board.Zones()):
        raise ValueError("KiCad native zone fill failed")
    if any(not zone.IsFilled() or not zone.HasFilledPolysForLayer(zone.GetLayer()) for zone in zones):
        raise ValueError("KiCad did not retain filled polygons for every ground zone")
    after = sum(1 for _ in board.GetTracks())
    if before != after:
        raise ValueError("ground-plane fill changed the existing track/via inventory")
    unconnected_ground_pads = _connected_ground_pads(board, net, allow_unconnected=pre_route)
    zones = _ground_zones(board, pcbnew, net, layers)
    stats = [_zone_stats(zone, pcbnew, name) for zone, name in zip(zones, layers)]
    return {"status": "ok", "net": str(net), "layers": list(layers), "added_zones": len(zones),
            "filled_zones": sum(bool(zone.IsFilled()) for zone in zones),
            "filled_area_mm2": round(sum(row["area_mm2"] for row in stats), 6),
            "island_count": sum(row["island_count"] for row in stats), "zone_stats": stats,
            "track_count_before": before, "track_count_after": after,
            "tracks_preserved": True, "protected_connections": rows,
            "unconnected_ground_pads": unconnected_ground_pads,
            "routing_complete": not pre_route, "requires_route": bool(pre_route)}


def verify(board, pcbnew, expected_report, *, net="GND", layers=("F.Cu", "B.Cu"),
           protected_connections=(), pre_route=False):
    """Verify native zone fill and connectivity after saving and reloading."""
    zones = _ground_zones(board, pcbnew, net, layers)
    if any(not zone.IsFilled() or not zone.HasFilledPolysForLayer(zone.GetLayer()) for zone in zones):
        raise ValueError("saved board does not contain filled polygons for every ground zone")
    stats = [_zone_stats(zone, pcbnew, name) for zone, name in zip(zones, layers)]
    if stats != expected_report.get("zone_stats"):
        raise ValueError("saved filled-polygon geometry differs from the native fill report")
    anchors = [_zone_pad_anchors(board, zone) for zone in zones]
    unconnected_ground_pads = _connected_ground_pads(board, net, allow_unconnected=pre_route)
    track_count = sum(1 for _ in board.GetTracks())
    if expected_report.get("track_count_before") != track_count:
        raise ValueError("saved board track/via inventory count differs from source")
    graph = CopperGraph(board, pcbnew)
    pads = _pad_map(board) if protected_connections else {}
    checked = []
    for index, row in enumerate(protected_connections):
        keys = (str(row["from_ref"]), str(row["from_pad"]), str(row["to_ref"]), str(row["to_pad"]))
        if any(key not in pads for key in ((keys[0], keys[1]), (keys[2], keys[3]))):
            raise ValueError("saved board protected path names an unknown physical pad")
        _ensure_protected_endpoints_unambiguous(
            (pads[(keys[0], keys[1])], pads[(keys[2], keys[3])]), index)
        route = _trace_route(graph, dict(zip(("from_ref", "from_pad", "to_ref", "to_pad"), keys)))
        if index >= len(expected_report.get("protected_connections", [])):
            raise ValueError("saved board has more protected paths than the native report")
        original = expected_report["protected_connections"][index]
        if (route["trace_only_length_mm"] != original.get("trace_only_length_mm")
                or route["trace_only_vias"] != original.get("trace_only_vias")):
            raise ValueError("saved board changed a protected trace-only path")
        checked.append({"from_ref": keys[0], "from_pad": keys[1], "to_ref": keys[2], "to_pad": keys[3], **route})
    if len(checked) != len(expected_report.get("protected_connections", [])):
        raise ValueError("saved board protected-path count differs from the native report")
    return {"status": "ok", "net": str(net), "layers": list(layers), "filled_zones": len(zones),
            "filled_area_mm2": round(sum(row["area_mm2"] for row in stats), 6),
            "island_count": sum(row["island_count"] for row in stats),
            "zone_connected_copper_counts": [len(items) for items in anchors], "tracks_preserved": True,
            "saved_fill_geometry_verified": True, "saved_connectivity_verified": True,
            "protected_connections": checked, "unconnected_ground_pads": unconnected_ground_pads,
            "routing_complete": not pre_route, "requires_route": bool(pre_route)}
