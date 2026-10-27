"""Bounded, candidate-only native same-net routing repair.

This helper adds copper to a board with no copper zones. Its output is never
accepted until the caller runs a fresh full native DRC.
"""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path


def _uuid(item):
    return str(item.m_Uuid.AsString())


def _class_rules(board, pcbnew, net_name):
    """Resolve the native net class through the staged project settings."""
    pads = [pad for fp in board.GetFootprints() for pad in fp.Pads() if str(pad.GetNetname()) == net_name]
    if not pads:
        raise ValueError("repair net has no native pad from which to resolve its net class")
    names = {str(pad.GetNetClassName()) for pad in pads}
    if len(names) != 1 or not next(iter(names)):
        raise ValueError("repair net maps to inconsistent or unavailable native net classes")
    class_name = next(iter(names))
    board_path = Path(str(board.GetFileName())).resolve()
    project_path = board_path.with_suffix(".kicad_pro")
    if not project_path.is_file():
        raise ValueError("staged KiCad project settings are unavailable for net-class rules")
    settings = json.loads(project_path.read_text(encoding="utf-8"))
    classes = settings.get("net_settings", {}).get("classes")
    if not isinstance(classes, list):
        raise ValueError("project net-class definitions are unavailable")
    matches = [row for row in classes if isinstance(row, dict) and row.get("name") == class_name]
    if len(matches) != 1:
        raise ValueError("native net class has no unique project rule definition")
    row = matches[0]

    def mm(name):
        value = row.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"net-class {name} is missing or invalid")
        return float(value)

    ds = board.GetDesignSettings()

    def native_min(name):
        value = int(getattr(ds, name))
        if value < 0:
            raise ValueError(f"native board constraint {name} is invalid")
        return pcbnew.ToMM(value)

    width = max(mm("track_width"), native_min("m_TrackMinWidth"))
    diameter = max(mm("via_diameter"), native_min("m_ViasMinSize"))
    drill = max(mm("via_drill"), native_min("m_MinThroughDrill"))
    clearance = max(mm("clearance"), native_min("m_MinClearance"))
    if drill >= diameter:
        raise ValueError("resolved through-via drill is not smaller than its diameter")
    return width, diameter, drill, clearance


def repair_one(board, pcbnew, drc_report, *, pair_index=0, max_expanded_states=150000, grid_step_mm=0.25):
    """Add one bounded A* candidate for a fresh DRC explicit UUID pair."""
    import _kicad_worker as worker

    if list(board.Zones()):
        raise ValueError("same-net repair requires a board without zones")
    if isinstance(pair_index, bool) or not isinstance(pair_index, int) or pair_index < 0:
        raise ValueError("pair_index must be a nonnegative integer")
    if (
        isinstance(max_expanded_states, bool)
        or not isinstance(max_expanded_states, int)
        or max_expanded_states <= 0
        or not math.isfinite(grid_step_mm)
        or grid_step_mm <= 0
    ):
        raise ValueError("repair search bounds are invalid")
    raw = drc_report
    if isinstance(raw, (str, Path)):
        raw = json.loads(Path(raw).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("unconnected_items"), list):
        raise ValueError("fresh DRC report lacks unconnected_items")
    def pair_distance(row):
        try:
            entries = row["items"]
            if len(entries) != 2:
                raise ValueError("expected two items")
            coordinates = [(entry["pos"]["x"], entry["pos"]["y"]) for entry in entries]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for point in coordinates for v in point):
                raise ValueError("nonfinite position")
            return math.dist(*coordinates)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("DRC pair has invalid endpoint positions") from exc

    rows = sorted(raw["unconnected_items"], key=pair_distance)
    if pair_index >= len(rows):
        raise ValueError("pair_index is outside the fresh DRC unconnected-item list")
    pair = rows[pair_index].get("items") if isinstance(rows[pair_index], dict) else None
    if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(x, dict) for x in pair):
        raise ValueError("selected DRC item is not an explicit two-item pair")
    items = list(board.GetTracks()) + [pad for fp in board.GetFootprints() for pad in fp.Pads()]
    by_uuid = {_uuid(item): item for item in items}
    if len(by_uuid) != len(items):
        raise ValueError("native track/pad UUIDs are not unique")
    selected = []
    for entry in pair:
        item = by_uuid.get(entry.get("uuid"))
        if item is None:
            raise ValueError("fresh DRC pair UUID is absent from the loaded board")
        if "net" in entry and str(item.GetNetname()) != str(entry["net"]):
            raise ValueError("fresh DRC pair net does not match the native item")
        selected.append(item)
    first, last = selected
    net = int(first.GetNetCode())
    if net <= 0 or net != int(last.GetNetCode()) or str(first.GetNetname()) != str(last.GetNetname()):
        raise ValueError("repair pair must belong to the same positive native net")
    net_name = str(first.GetNetname())
    width, diameter, drill, clearance = _class_rules(board, pcbnew, net_name)

    def point(p):
        return pcbnew.ToMM(p.x), pcbnew.ToMM(p.y)

    def vec(p):
        return pcbnew.VECTOR2I(pcbnew.FromMM(p[0]), pcbnew.FromMM(p[1]))

    def ends(item):
        if type(item).__name__ in {"PCB_TRACK", "PCB_ARC"}:
            return [point(item.GetStart()), point(item.GetEnd())]
        return [point(item.GetPosition())]

    start, goal = min(((a, b) for a in ends(first) for b in ends(last)), key=lambda ab: math.dist(*ab))
    stack, complete = worker._native_copper_layer_stack(board, pcbnew)
    if not complete:
        raise ValueError("native copper stack cannot be verified")
    edge_layer = int(pcbnew.Edge_Cuts)
    edges = [item for item in board.GetDrawings() if int(item.GetLayer()) == edge_layer]
    bounds_iu = worker._rectangular_outline_bounds_iu(edges, pcbnew)
    if bounds_iu is None:
        raise ValueError("repair supports only a verified rectangular board outline")
    margin = pcbnew.FromMM(max(width, diameter) / 2 + clearance)
    x0, y0, w, h = bounds_iu
    bounds = (
        pcbnew.ToMM(x0 + margin),
        pcbnew.ToMM(y0 + margin),
        pcbnew.ToMM(x0 + w - margin),
        pcbnew.ToMM(y0 + h - margin),
    )
    if not (bounds[0] < bounds[2] and bounds[1] < bounds[3]):
        raise ValueError("board outline leaves no legal repair search area")

    cell_size = max(2.0, diameter + clearance * 2)
    records, index = [], {}

    def bbox(item):
        box = item.GetBoundingBox()
        return tuple(
            pcbnew.ToMM(v) for v in (box.GetX(), box.GetY(), box.GetX() + box.GetWidth(), box.GetY() + box.GetHeight())
        )

    def cells(box):
        for x in range(math.floor(box[0] / cell_size), math.floor(box[2] / cell_size) + 1):
            for y in range(math.floor(box[1] / cell_size), math.floor(box[3] / cell_size) + 1):
                yield x, y

    for item in items:
        is_pad = type(item).__name__ == "PAD"
        box = bbox(item)
        for layer in stack:
            if not item.IsOnLayer(layer):
                continue
            shape = (
                item.GetEffectiveShape(layer)
                if is_pad or type(item).__name__ == "PCB_VIA"
                else item.GetEffectiveShape()
            )
            number = len(records)
            records.append((int(item.GetNetCode()), is_pad, shape))
            for x, y in cells((box[0] - clearance, box[1] - clearance, box[2] + clearance, box[3] + clearance)):
                index.setdefault((layer, x, y), []).append(number)
    query = pcbnew.PCB_TRACK(board)
    via_query = pcbnew.PCB_VIA(board)
    via_query.SetViaType(pcbnew.VIATYPE_THROUGH)
    via_query.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    via_query.SetWidth(pcbnew.FromMM(diameter))
    via_query.SetDrill(pcbnew.FromMM(drill))
    via_query.SetNetCode(net)
    if any(via_query.GetWidth(layer_id) != pcbnew.FromMM(diameter) for layer_id in stack):
        raise ValueError("native via diameter cannot be verified on every enabled copper layer")

    def candidates(layer, box):
        found = set()
        for x, y in cells(box):
            found.update(index.get((layer, x, y), ()))
        return found

    def clear_segment(a, b, layer, copper_width=width, avoid_pads=False):
        query.SetStart(vec(a))
        query.SetEnd(vec(b))
        query.SetWidth(pcbnew.FromMM(copper_width))
        query.SetLayer(layer)
        if a == b and copper_width == diameter:
            via_query.SetPosition(vec(a))
            shape = via_query.GetEffectiveShape(layer)
        else:
            shape = query.GetEffectiveShape()
        radius = copper_width / 2
        box = (min(a[0], b[0]) - radius, min(a[1], b[1]) - radius, max(a[0], b[0]) + radius, max(a[1], b[1]) + radius)
        for n in candidates(layer, box):
            other_net, is_pad, other_shape = records[n]
            if other_net == net and not (avoid_pads and is_pad):
                continue
            distance = 0 if other_net == net and avoid_pads and is_pad else pcbnew.FromMM(clearance)
            if other_shape.Collide(shape, distance):
                return False
        return True

    silk_boxes = []
    for fp in board.GetFootprints():
        for text in (fp.Reference(), fp.Value()):
            if text.IsVisible() and int(text.GetLayer()) in (int(pcbnew.F_SilkS), int(pcbnew.B_SilkS)):
                silk_boxes.append(bbox(text))
        for drawing in fp.GraphicalItems():
            if int(drawing.GetLayer()) in (int(pcbnew.F_SilkS), int(pcbnew.B_SilkS)):
                silk_boxes.append(bbox(drawing))
    holes = [
        (point(t.GetPosition()), pcbnew.ToMM(t.GetDrillValue()) / 2) for t in items if type(t).__name__ == "PCB_VIA"
    ]
    hole_gap = max(clearance, pcbnew.ToMM(int(board.GetDesignSettings().m_HoleToHoleMin)))
    through = [t for t in items if type(t).__name__ == "PCB_VIA" and int(t.GetNetCode()) == net]
    through += [
        p
        for p in (item for item in items if type(item).__name__ == "PAD")
        if int(p.GetNetCode()) == net and p.GetDrillSize().x > 0 and all(p.IsOnLayer(layer) for layer in stack)
    ]

    def existing_transition(p):
        return any(t.GetEffectiveShape(stack[0]).Collide(vec(p), 0) for t in through)

    via_cache = {}

    def can_change_layer(p):
        key = tuple(p)
        if key in via_cache:
            return via_cache[key]
        if existing_transition(p):
            via_cache[key] = True
            return True
        radius = diameter / 2 + clearance
        if any(
            box[0] - radius < p[0] < box[2] + radius and box[1] - radius < p[1] < box[3] + radius for box in silk_boxes
        ):
            via_cache[key] = False
            return False
        if any(math.dist(p, c) < drill / 2 + r + hole_gap for c, r in holes):
            via_cache[key] = False
            return False
        okay = all(clear_segment(p, p, layer_id, diameter, avoid_pads=True) for layer_id in stack)
        via_cache[key] = okay
        return okay

    step = float(grid_step_mm)
    def coordinate(node):
        return start[0] + node[0] * step, start[1] + node[1] * step

    origins = {(0, 0, i) for i, layer_id in enumerate(stack) if first.IsOnLayer(layer_id)}
    if not origins or not any(last.IsOnLayer(layer_id) for layer_id in stack):
        raise ValueError("endpoints do not occupy supported copper layers")
    queue = []
    dist = {n: 0.0 for n in origins}
    prev = {}
    count = 0
    for n in sorted(origins):
        heapq.heappush(queue, (math.dist(start, goal), count, n))
        count += 1
    directions = [(x, y) for x in (-1, 0, 1) for y in (-1, 0, 1) if x or y]
    end = None
    expanded = 0
    while queue and expanded < max_expanded_states:
        _, _, node = heapq.heappop(queue)
        p = coordinate(node)
        expanded += 1
        if (
            last.IsOnLayer(stack[node[2]])
            and math.dist(p, goal) <= step * 1.5
            and clear_segment(p, goal, stack[node[2]])
        ):
            end = node
            break
        neighbors = []
        for dx, dy in directions:
            cand = (node[0] + dx, node[1] + dy, node[2])
            q = coordinate(cand)
            if (
                bounds[0] <= q[0] <= bounds[2]
                and bounds[1] <= q[1] <= bounds[3]
                and clear_segment(p, q, stack[node[2]])
            ):
                neighbors.append((cand, math.hypot(dx, dy) * step))
        if can_change_layer(p):
            neighbors.extend(((node[0], node[1], i), 4.0) for i in range(len(stack)) if i != node[2])
        for cand, cost in neighbors:
            score = dist[node] + cost
            if score + 1e-9 < dist.get(cand, float("inf")):
                dist[cand] = score
                prev[cand] = node
                heapq.heappush(queue, (score + math.dist(coordinate(cand), goal), count, cand))
                count += 1
    if end is None:
        raise RuntimeError(f"no collision-free repair within {expanded} native search states")
    path = [end]
    while path[-1] not in origins:
        path.append(prev[path[-1]])
    path.reverse()
    points = [(coordinate(n), stack[n[2]]) for n in path] + [(goal, stack[end[2]])]
    added_tracks = added_vias = 0
    for (a, la), (b, lb) in zip(points, points[1:]):
        if la != lb:
            if not existing_transition(a):
                via = pcbnew.PCB_VIA(board)
                via.SetViaType(pcbnew.VIATYPE_THROUGH)
                via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                via.SetPosition(vec(a))
                via.SetWidth(pcbnew.FromMM(diameter))
                via.SetDrill(pcbnew.FromMM(drill))
                if any(via.GetWidth(layer_id) != pcbnew.FromMM(diameter) for layer_id in stack):
                    raise ValueError("new native via diameter differs from its resolved net class")
                via.SetNetCode(net)
                board.Add(via)
                added_vias += 1
            continue
        if vec(a) == vec(b):
            continue
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(vec(a))
        track.SetEnd(vec(b))
        track.SetWidth(pcbnew.FromMM(width))
        track.SetLayer(la)
        track.SetNetCode(net)
        board.Add(track)
        added_tracks += 1
    if not added_tracks:
        raise RuntimeError("repair produced no new tracks")
    board.BuildConnectivity()
    return {
        "status": "candidate_only_requires_full_drc",
        "net": net_name,
        "from_uuid": _uuid(first),
        "to_uuid": _uuid(last),
        "added_tracks": added_tracks,
        "added_vias": added_vias,
        "expanded_states": expanded,
        "resolved_track_width_mm": width,
        "resolved_via_diameter_mm": diameter,
        "resolved_via_drill_mm": drill,
        "resolved_clearance_mm": clearance,
    }
