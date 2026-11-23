"""Bounded, candidate-only native same-net routing repair.

This helper adds copper to a board with no copper zones. Its output is never
accepted until the caller runs a fresh full native DRC.
"""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path


def _via_width_on_layer(via, layer_id):
    """Read a native via width across KiCad SWIG signatures and fail closed."""
    try:
        raw_width = via.GetWidth(layer_id)
    except TypeError:
        # KiCad 8 exposes PCB_TRACK.GetWidth() without a layer argument;
        # KiCad 10's PCB_VIA binding requires one.
        raw_width = via.GetWidth()
    try:
        if isinstance(raw_width, bool):
            raise ValueError
        width = int(raw_width)
        finite_width = float(raw_width)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("native via width is not a valid positive integer") from exc
    if not math.isfinite(finite_width) or width <= 0 or finite_width != width:
        raise ValueError("native via width is not a valid positive integer")
    return width


def _native_point_vector(pcbnew, point):
    """Round query and emitted coordinates back to their nearest native IU."""
    scale = pcbnew.FromMM(1.0)
    return pcbnew.VECTOR2I(int(round(point[0] * scale)), int(round(point[1] * scale)))


def _edge_margin_iu(copper_width_iu, edge_clearance_iu):
    """Minimum centerline/center offset from a straight board edge, in native IU."""
    for name, value in (("copper width", copper_width_iu), ("edge clearance", edge_clearance_iu)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative native IU integer")
    return edge_clearance_iu + (copper_width_iu + 1) // 2


def _segment_inside_edge_margin(start, end, outline, margin_iu):
    """Check copper geometry against an axis-aligned rectangular board edge."""
    x0, y0, x1, y1 = outline
    sx, sy = start
    ex, ey = end
    return (min(sx, ex) >= x0 + margin_iu and max(sx, ex) <= x1 - margin_iu
            and min(sy, ey) >= y0 + margin_iu and max(sy, ey) <= y1 - margin_iu)


def _native_npth_hole_obstacles(board, pcbnew):
    """Return exact native NPTH drill shapes and the board hole clearance.

    NPTH pad copper geometry is not a safe proxy for the drill: footprint pad
    size may be larger, smaller, or otherwise unrelated to the actual hole.
    Use KiCad's effective hole geometry so circles and slots retain their real
    shape. Missing geometry or rule APIs fail closed before searching.
    """
    footprints = list(board.GetFootprints())
    npth_attribute = getattr(pcbnew, "PAD_ATTRIB_NPTH", None)
    if npth_attribute is None:
        for footprint in footprints:
            for pad in footprint.Pads():
                try:
                    drill = pad.GetDrillSize()
                except AttributeError:
                    # Lightweight legacy adapters may not expose drill data;
                    # no drilled-pad evidence means there is no NPTH to index.
                    continue
                try:
                    if int(drill.x) > 0 or int(drill.y) > 0:
                        raise ValueError("native NPTH attribute API is unavailable")
                except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("native pad drill geometry is unavailable") from exc
        return [], 0, 0

    holes = []
    for footprint in footprints:
        for pad in footprint.Pads():
            try:
                if int(pad.GetAttribute()) != int(npth_attribute):
                    continue
                drill = pad.GetDrillSize()
                raw_drill_x, raw_drill_y = drill.x, drill.y
                if isinstance(raw_drill_x, bool) or isinstance(raw_drill_y, bool):
                    raise ValueError("native NPTH drill dimensions are invalid")
                drill_x, drill_y = int(raw_drill_x), int(raw_drill_y)
                if (float(raw_drill_x) != drill_x or float(raw_drill_y) != drill_y
                        or drill_x < 0 or drill_y < 0):
                    raise ValueError("native NPTH drill dimensions are invalid")
                if drill_x <= 0 or drill_y <= 0:
                    raise ValueError("native NPTH drill dimensions are invalid")
                hole_shape = pad.GetEffectiveHoleShape()
            except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("native NPTH drill geometry is unavailable") from exc
            if hole_shape is None:
                raise ValueError("native NPTH drill geometry is unavailable")
            holes.append(hole_shape)

    if not holes:
        return [], 0, 0
    try:
        settings = board.GetDesignSettings()
        raw_clearance = settings.m_HoleClearance
        raw_hole_to_hole = settings.m_HoleToHoleMin
        if isinstance(raw_clearance, bool) or isinstance(raw_hole_to_hole, bool):
            raise ValueError
        clearance_iu = int(raw_clearance)
        hole_to_hole_iu = int(raw_hole_to_hole)
        if (clearance_iu < 0 or hole_to_hole_iu < 0
                or float(raw_clearance) != clearance_iu
                or float(raw_hole_to_hole) != hole_to_hole_iu):
            raise ValueError
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("native hole-clearance rule is unavailable or invalid") from exc
    return holes, clearance_iu, hole_to_hole_iu


def _collides_with_npth_hole(candidate_shape, holes, clearance_iu):
    """Check copper geometry against native NPTH drill shapes and hole rule."""
    for hole_shape in holes:
        try:
            if hole_shape.Collide(candidate_shape, clearance_iu):
                return True
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("native NPTH hole-clearance geometry check failed") from exc
    return False


def _native_silkscreen_obstacles(board, pcbnew, bbox):
    """Index exact visible footprint silk geometry for via-clearance checks.

    Bounding boxes are only a broad phase. The final decision uses KiCad's
    effective silk shape against the candidate via's native copper shape.
    """
    silk_to_copper = {
        int(pcbnew.F_SilkS): int(pcbnew.F_Cu),
        int(pcbnew.B_SilkS): int(pcbnew.B_Cu),
    }
    try:
        raw_clearance = board.GetDesignSettings().m_SilkClearance
        if isinstance(raw_clearance, bool):
            raise ValueError
        clearance_iu = int(raw_clearance)
        if clearance_iu < 0 or float(raw_clearance) != clearance_iu:
            raise ValueError
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("native silkscreen clearance is unavailable or invalid") from exc

    obstacles = []

    def add(item, layer):
        if layer not in silk_to_copper:
            return
        try:
            shape = item.GetEffectiveShape()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("native silkscreen effective shape is unavailable") from exc
        if shape is None:
            raise ValueError("native silkscreen effective shape is unavailable")
        obstacles.append((silk_to_copper[layer], bbox(item), shape))

    for footprint in board.GetFootprints():
        for field in (footprint.Reference(), footprint.Value()):
            if field.IsVisible():
                add(field, int(field.GetLayer()))
        for drawing in footprint.GraphicalItems():
            add(drawing, int(drawing.GetLayer()))
    return obstacles, clearance_iu


def _collides_with_silkscreen(point_mm, via_query, obstacles, diameter_mm, clearance_iu, pcbnew):
    """Use boxes as broad phase, then effective native shape collision."""
    radius = diameter_mm / 2
    clearance_mm = pcbnew.ToMM(clearance_iu)
    x, y = point_mm
    via_query.SetPosition(_native_point_vector(pcbnew, point_mm))
    for copper_layer, box, silk_shape in obstacles:
        if not (
            box[0] - radius - clearance_mm <= x <= box[2] + radius + clearance_mm
            and box[1] - radius - clearance_mm <= y <= box[3] + radius + clearance_mm
        ):
            continue
        try:
            candidate = via_query.GetEffectiveShape(copper_layer)
            if candidate is None:
                raise ValueError
            if silk_shape.Collide(candidate, clearance_iu):
                return True
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("native silkscreen/via collision check failed") from exc
    return False


def _validated_search_grid_phase(value, search_mode):
    """Accept only the original lattice phase or deterministic half-cell offsets."""
    if value is None:
        phase = (0.0, 0.0)
    else:
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError("search grid phase must contain two half-cell offsets")
        normalized = []
        for component in value:
            if isinstance(component, bool) or not isinstance(component, (int, float)):
                raise ValueError("search grid phase must contain two half-cell offsets")
            try:
                finite = math.isfinite(component)
            except (OverflowError, TypeError, ValueError):
                finite = False
            if not finite or float(component) not in {0.0, 0.5}:
                raise ValueError("search grid phase offsets must be 0 or 0.5")
            normalized.append(float(component))
        phase = (normalized[0], normalized[1])
    if search_mode == "standard" and phase != (0.0, 0.0):
        raise ValueError("phased grid is supported only for bidirectional search")
    return phase


def _native_hole_bbox_mm(hole_shape, clearance_iu, pcbnew):
    """Get a native drill-shape broad-phase box expanded by hole clearance."""
    try:
        box = hole_shape.BBox(clearance_iu)
        coordinates = tuple(pcbnew.ToMM(int(value)) for value in (
            box.GetX(), box.GetY(), box.GetX() + box.GetWidth(), box.GetY() + box.GetHeight(),
        ))
        if not all(math.isfinite(value) for value in coordinates):
            raise ValueError
        if coordinates[0] > coordinates[2] or coordinates[1] > coordinates[3]:
            raise ValueError
        return coordinates
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("native NPTH clearance bounding geometry is unavailable") from exc


def _astar_search(start, goal, layer_count, target_layers, origins, bounds, step, max_expanded_states,
                  clear_segment, can_change_layer):
    """Search lattice states and return a route path plus real expansion count."""
    def coordinate(node):
        return start[0] + node[0] * step, start[1] + node[1] * step

    queue = []
    distance = {node: 0.0 for node in origins}
    previous = {}
    sequence = 0
    for node in sorted(origins):
        heapq.heappush(queue, (math.dist(start, goal), 0.0, sequence, node))
        sequence += 1
    directions = [(x, y) for x in (-1, 0, 1) for y in (-1, 0, 1) if x or y]
    end = None
    expanded = 0
    while queue and expanded < max_expanded_states:
        _, queued_distance, _, node = heapq.heappop(queue)
        if queued_distance > distance.get(node, float("inf")) + 1e-9:
            continue
        point = coordinate(node)
        expanded += 1
        if node[2] in target_layers and clear_segment(point, goal, node[2]):
            end = node
            break
        neighbors = []
        for dx, dy in directions:
            candidate = (node[0] + dx, node[1] + dy, node[2])
            target = coordinate(candidate)
            if (bounds[0] <= target[0] <= bounds[2] and bounds[1] <= target[1] <= bounds[3]
                    and clear_segment(point, target, node[2])):
                neighbors.append((candidate, math.hypot(dx, dy) * step))
        if can_change_layer(point):
            neighbors.extend(((node[0], node[1], layer), 4.0)
                             for layer in range(layer_count) if layer != node[2])
        for candidate, cost in neighbors:
            score = distance[node] + cost
            if score + 1e-9 < distance.get(candidate, float("inf")):
                distance[candidate] = score
                previous[candidate] = node
                heapq.heappush(queue, (score + math.dist(coordinate(candidate), goal), score,
                                       sequence, candidate))
                sequence += 1
    if end is None:
        return None, expanded
    path = [end]
    while path[-1] not in origins:
        path.append(previous[path[-1]])
    path.reverse()
    return path, expanded


def _bidirectional_astar_search(start, goal, layer_count, target_layers, origins, bounds, step, max_expanded_states,
                                  clear_segment, can_change_layer, grid_phase=None):
    """Meet two A* fronts on one start-anchored lattice under a shared cap.

    Reverse roots are the floor/ceil lattice points nearest the physical goal
    that have a legal straight connector to it. Both directions use the same
    native segment and layer-change predicates and the same movement costs.
    This is a feasibility candidate search; first-meeting termination does not
    claim globally minimum path length.
    """
    phase_x, phase_y = _validated_search_grid_phase(grid_phase, "bidirectional")
    lattice_origin = (start[0] + phase_x * step, start[1] + phase_y * step)

    def coordinate(node):
        return lattice_origin[0] + node[0] * step, lattice_origin[1] + node[1] * step

    start_layers = {node[2] for node in origins}
    x_start_indices = sorted({math.floor(-phase_x), math.ceil(-phase_x)})
    y_start_indices = sorted({math.floor(-phase_y), math.ceil(-phase_y)})
    forward_roots = set()
    forward_distance = {}
    reverse_distance = {}
    forward_previous = {}
    reverse_next = {}
    forward_queue, reverse_queue = [], []
    sequence = 0
    if (phase_x, phase_y) == (0.0, 0.0):
        # Preserve the original phase-zero frontier exactly: the physical
        # start is already the lattice origin, so no connector query is needed.
        forward_roots = set(origins)
        forward_distance = {node: 0.0 for node in origins}
        for node in sorted(origins):
            point = coordinate(node)
            heapq.heappush(forward_queue, (math.dist(point, goal), 0.0, sequence, node))
            sequence += 1
    else:
        for layer in sorted(start_layers):
            for ix in x_start_indices:
                for iy in y_start_indices:
                    node = (ix, iy, layer)
                    point = coordinate(node)
                    if (not (bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3])
                            or not clear_segment(start, point, layer)):
                        continue
                    connector_cost = math.dist(start, point)
                    if connector_cost + 1e-9 >= forward_distance.get(node, float("inf")):
                        continue
                    forward_roots.add(node)
                    forward_distance[node] = connector_cost
                    heapq.heappush(forward_queue,
                                   (connector_cost + math.dist(point, goal),
                                    connector_cost, sequence, node))
                    sequence += 1
    if not forward_queue:
        return None, 0

    xratio = (goal[0] - lattice_origin[0]) / step
    yratio = (goal[1] - lattice_origin[1]) / step
    xindices = sorted({math.floor(xratio), math.ceil(xratio)})
    yindices = sorted({math.floor(yratio), math.ceil(yratio)})
    for layer in sorted(target_layers):
        for ix in xindices:
            for iy in yindices:
                node = (ix, iy, layer)
                point = coordinate(node)
                if (not (bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3])
                        or not clear_segment(point, goal, layer)):
                    continue
                connector_cost = math.dist(point, goal)
                if connector_cost + 1e-9 >= reverse_distance.get(node, float("inf")):
                    continue
                reverse_distance[node] = connector_cost
                heapq.heappush(reverse_queue,
                               (connector_cost + math.dist(point, start),
                                connector_cost, sequence, node))
                sequence += 1
    if not reverse_queue:
        return None, 0

    directions = [(x, y) for x in (-1, 0, 1) for y in (-1, 0, 1) if x or y]
    expanded = 0
    meeting = None

    def reconstruct(node):
        left = [node]
        while left[-1] not in forward_roots:
            left.append(forward_previous[left[-1]])
        left.reverse()
        right = []
        cursor = node
        while cursor in reverse_next:
            cursor = reverse_next[cursor]
            right.append(cursor)
        return left + right

    def pop_live(queue, distances):
        while queue:
            priority, queued_distance, _, node = heapq.heappop(queue)
            if queued_distance <= distances.get(node, float("inf")) + 1e-9:
                return priority, queued_distance, node
        return None

    while forward_queue and reverse_queue and expanded < max_expanded_states:
        # Compare each side's current f score; sequence numbers keep ties stable.
        fscore = forward_queue[0][0]
        rscore = reverse_queue[0][0]
        forward_turn = fscore <= rscore
        queue = forward_queue if forward_turn else reverse_queue
        distances = forward_distance if forward_turn else reverse_distance
        current = pop_live(queue, distances)
        if current is None:
            if forward_turn:
                forward_queue.clear()
            else:
                reverse_queue.clear()
            continue
        _, current_distance, node = current
        expanded += 1
        other = reverse_distance if forward_turn else forward_distance
        if node in other:
            meeting = node
            break
        point = coordinate(node)
        neighbors = []
        for dx, dy in directions:
            candidate = (node[0] + dx, node[1] + dy, node[2])
            target = coordinate(candidate)
            if (bounds[0] <= target[0] <= bounds[2] and bounds[1] <= target[1] <= bounds[3]
                    and clear_segment(point, target, node[2])):
                neighbors.append((candidate, math.hypot(dx, dy) * step))
        if can_change_layer(point):
            neighbors.extend(((node[0], node[1], layer), 4.0)
                             for layer in range(layer_count) if layer != node[2])
        for candidate, cost in neighbors:
            score = current_distance + cost
            if score + 1e-9 >= distances.get(candidate, float("inf")):
                continue
            distances[candidate] = score
            if forward_turn:
                forward_previous[candidate] = node
                heuristic = math.dist(coordinate(candidate), goal)
            else:
                reverse_next[candidate] = node
                heuristic = math.dist(coordinate(candidate), start)
            heapq.heappush(queue, (score + heuristic, score, sequence, candidate))
            sequence += 1
            if candidate in other:
                meeting = candidate
                break
        if meeting is not None:
            break
    if meeting is None:
        return None, expanded
    return reconstruct(meeting), expanded


def _search_with_fallback(start, goal, layer_count, target_layers, origins, bounds, grid_step_mm,
                          max_expanded_states, clear_segment, can_change_layer):
    """Search long spans on a coarse grid first, sharing one hard state budget."""
    requested_step = float(grid_step_mm)
    long_span_coarse = requested_step < 0.5 and math.dist(start, goal) > 128 * requested_step
    searches = ([(0.5, False), (0.5, True), (requested_step, False)]
                if long_span_coarse else [(requested_step, False)])
    if requested_step > 0.125:
        searches.append((0.125, False))
    remaining = max_expanded_states
    expanded = 0
    for attempt, (step, reverse) in enumerate(searches):
        if remaining <= 0:
            break
        if long_span_coarse and attempt < 2:
            budget = min(remaining, max(1, max_expanded_states // 4))
        else:
            budget = remaining if attempt == len(searches) - 1 else max(1, remaining // 2)
        search_start, search_goal = (goal, start) if reverse else (start, goal)
        search_origins = ({(0, 0, layer) for layer in target_layers} if reverse else origins)
        search_targets = {node[2] for node in origins} if reverse else target_layers
        path, used = _astar_search(
            search_start, search_goal, layer_count, search_targets, search_origins,
            bounds, step, budget, clear_segment, can_change_layer,
        )
        expanded += used
        remaining -= used
        if path is not None:
            return path, step, expanded, search_start, search_goal, reverse
    return None, None, expanded, start, goal, False


def _physical_search_points(path, step, origin, goal, reverse, stack, exact_start=None):
    """Resolve a lattice on its own origin before restoring original direction."""
    points = [((origin[0] + n[0] * step, origin[1] + n[1] * step), stack[n[2]]) for n in path]
    if exact_start is not None:
        points.insert(0, (exact_start, stack[path[0][2]]))
    points.append((goal, stack[path[-1][2]]))
    if reverse:
        points.reverse()
    return _collapse_collinear_points(points)

def _collapse_collinear_points(points):
    """Merge only forward, exactly collinear same-layer grid segments."""
    result = []
    for point in points:
        if len(result) >= 2 and result[-2][1] == result[-1][1] == point[1]:
            a, b, c = result[-2][0], result[-1][0], point[0]
            ab = (b[0] - a[0], b[1] - a[1])
            bc = (c[0] - b[0], c[1] - b[1])
            cross = ab[0] * bc[1] - ab[1] * bc[0]
            dot = ab[0] * bc[0] + ab[1] * bc[1]
            if abs(cross) <= 1e-12 and dot > 0:
                result[-1] = point
                continue
        result.append(point)
    return result


def _uuid(item):
    return str(item.m_Uuid.AsString())


def _connected_component_anchor_vertices(board, seed_item, source_items_by_uuid, net_code, net_name,
                                         stack, pcbnew, opposite_point, *, limit=32):
    """Return bounded endpoints of the seed's verified native copper component."""
    if isinstance(net_code, bool) or not isinstance(net_code, int) or net_code <= 0:
        raise ValueError("native connected copper net code must be positive")
    if not isinstance(net_name, str) or not net_name:
        raise ValueError("native connected copper net name is unavailable")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("native connected copper anchor limit is invalid")
    connectivity = board.GetConnectivity()
    search = getattr(connectivity, "GetConnectedItems", None)
    if not callable(search):
        raise ValueError("native transitive connectivity query is unavailable")
    try:
        from ._native_ground_plane import _connected_copper_items
    except ImportError:
        try:
            from circuit_weaver._native_ground_plane import _connected_copper_items
        except ImportError:
            from _native_ground_plane import _connected_copper_items
    try:
        members = list(_connected_copper_items(search, seed_item, connectivity, ()))
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("native connected copper cluster is unavailable") from exc
    seed_uuid = _uuid(seed_item)
    if seed_uuid not in source_items_by_uuid:
        raise ValueError("selected DRC item is absent from the native source inventory")
    canonical_seed = source_items_by_uuid[seed_uuid]
    members_by_uuid = {}
    for query_item in members:
        try:
            query_class = str(query_item.GetClass())
            item_uuid = _uuid(query_item)
            query_net_code = int(query_item.GetNetCode())
            query_net_name = str(query_item.GetNetname())
        except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("native connected copper member identity is unavailable") from exc
        if not item_uuid:
            raise ValueError("native connected copper member UUID is empty")
        if query_class == "ZONE":
            raise ValueError("native connected copper cluster unexpectedly contains a zone")
        item = source_items_by_uuid.get(item_uuid)
        if item is None:
            raise ValueError("native connected copper query returned an unknown source UUID")
        try:
            item_class = str(item.GetClass())
            item_net_code = int(item.GetNetCode())
            item_net_name = str(item.GetNetname())
        except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("canonical native connected copper item is unavailable") from exc
        if (query_net_code != item_net_code or query_net_name != item_net_name
                or item_net_code != net_code or item_net_name != net_name):
            raise ValueError("native connected copper cluster contains a foreign net")
        if item_uuid not in members_by_uuid:
            members_by_uuid[item_uuid] = item
    # The board inventory is authoritative for the selected DRC item. Some
    # KiCad bindings return a base wrapper or omit this item from the query.
    # Add only this known seed; never infer any other contact geometrically.
    members_by_uuid.setdefault(seed_uuid, canonical_seed)

    anchors = {}
    for item_uuid, item in sorted(members_by_uuid.items()):
        item_class = str(item.GetClass())
        try:
            if item_class == "PAD" or item_class == "PCB_VIA":
                positions = [item.GetPosition()]
            elif item_class in {"PCB_TRACK", "PCB_ARC"}:
                positions = [item.GetStart(), item.GetEnd()]
            else:
                raise ValueError("native connected copper cluster has an unsupported item class")
            for native_position in positions:
                native_iu = (int(native_position.x), int(native_position.y))
                xy = (pcbnew.ToMM(native_iu[0]), pcbnew.ToMM(native_iu[1]))
                for layer in stack:
                    if item.IsOnLayer(layer):
                        key = (native_iu, int(layer))
                        candidate = {
                            "uuid": item_uuid,
                            "point": xy,
                            "layer": int(layer),
                            "item_class": item_class,
                        }
                        previous = anchors.get(key)
                        if (previous is None or item_uuid == seed_uuid
                                or (previous["uuid"] != seed_uuid and item_uuid < previous["uuid"])):
                            anchors[key] = candidate
        except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("native connected copper anchor geometry is unavailable") from exc
    ordered = sorted(
        anchors.values(),
        key=lambda anchor: (
            math.dist(anchor["point"], opposite_point),
            anchor["point"], anchor["layer"], anchor["uuid"],
        ),
    )
    selected_vertices = [anchor for anchor in ordered if anchor["uuid"] == seed_uuid][:limit]
    selected_keys = {(anchor["uuid"], anchor["layer"], anchor["point"]) for anchor in selected_vertices}
    result = list(selected_vertices)
    for anchor in ordered:
        key = (anchor["uuid"], anchor["layer"], anchor["point"])
        if key not in selected_keys:
            result.append(anchor)
            selected_keys.add(key)
        if len(result) >= limit:
            break
    if not result:
        raise ValueError("native connected copper cluster has no supported endpoint anchors")
    return result, set(members_by_uuid)


def _choose_connected_anchor_pair(first_anchors, last_anchors):
    """Choose one deterministic compatible anchor pair without retrying search."""
    pairs = []
    for first in first_anchors:
        for last in last_anchors:
            gap = math.dist(first["point"], last["point"])
            via_penalty = 0.0 if first["layer"] == last["layer"] else 4.0
            pairs.append((gap + via_penalty, gap, first["uuid"], first["layer"], first["point"],
                          last["uuid"], last["layer"], last["point"], first, last))
    if not pairs:
        raise ValueError("native connected copper components have no compatible anchor pair")
    selected = min(pairs)
    return selected[-2], selected[-1]


def _bidirectional_connected_anchor_search(start_anchors, goal_anchors, stack, bounds, step,
                                           max_expanded_states, clear_segment, can_change_layer,
                                           grid_phase=None):
    """Search once from bounded verified copper-component anchor sets.

    Every seeded lattice connector is checked against native copper geometry.
    The returned endpoint records identify the concrete anchors used by the
    reconstructed candidate; all roots share one lattice and one state cap.
    """
    phase_x, phase_y = _validated_search_grid_phase(grid_phase, "bidirectional")
    if isinstance(max_expanded_states, bool) or not isinstance(max_expanded_states, int) or max_expanded_states <= 0:
        raise ValueError("connected-anchor search state cap is invalid")
    if not isinstance(stack, (list, tuple)) or not stack:
        raise ValueError("connected-anchor search copper stack is unavailable")
    if not isinstance(start_anchors, (list, tuple)) or not start_anchors:
        raise ValueError("connected-anchor search start set is empty")
    if not isinstance(goal_anchors, (list, tuple)) or not goal_anchors:
        raise ValueError("connected-anchor search goal set is empty")
    stack_index = {layer: index for index, layer in enumerate(stack)}

    def normalize(rows):
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("connected-anchor search received an invalid anchor")
            uid, layer, point = row.get("uuid"), row.get("layer"), row.get("point")
            if (not isinstance(uid, str) or not uid or isinstance(layer, bool)
                    or not isinstance(layer, int) or layer not in stack_index):
                raise ValueError("connected-anchor search anchor identity/layer is invalid")
            if (not isinstance(point, (tuple, list)) or len(point) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                           for v in point)):
                raise ValueError("connected-anchor search anchor point is invalid")
            result.append({"uuid": uid, "layer": layer, "point": (float(point[0]), float(point[1]))})
        return sorted(result, key=lambda row: (row["point"], row["layer"], row["uuid"]))

    starts = normalize(start_anchors)
    goals = normalize(goal_anchors)
    origin_anchor = starts[0]
    lattice_origin = (origin_anchor["point"][0] + phase_x * step,
                      origin_anchor["point"][1] + phase_y * step)

    def coordinate(node):
        return lattice_origin[0] + node[0] * step, lattice_origin[1] + node[1] * step

    def seed_roots(anchors):
        roots, distances, anchor_by_root, queue = set(), {}, {}, []
        sequence = 0
        for anchor in anchors:
            point, layer_id = anchor["point"], anchor["layer"]
            ratio_x = (point[0] - lattice_origin[0]) / step
            ratio_y = (point[1] - lattice_origin[1]) / step
            x_indices = sorted({math.floor(ratio_x), math.ceil(ratio_x)})
            y_indices = sorted({math.floor(ratio_y), math.ceil(ratio_y)})
            layer = stack_index[layer_id]
            for ix in x_indices:
                for iy in y_indices:
                    node = (ix, iy, layer)
                    grid_point = coordinate(node)
                    if not (bounds[0] <= grid_point[0] <= bounds[2]
                            and bounds[1] <= grid_point[1] <= bounds[3]):
                        continue
                    connector_cost = math.dist(point, grid_point)
                    if connector_cost > 1e-9 and not clear_segment(point, grid_point, layer_id):
                        continue
                    previous = distances.get(node)
                    candidate_key = (connector_cost, anchor["point"], anchor["layer"], anchor["uuid"])
                    current_anchor = anchor_by_root.get(node)
                    current_key = ((previous, current_anchor["point"], current_anchor["layer"],
                                    current_anchor["uuid"]) if current_anchor is not None else None)
                    if previous is not None and (connector_cost > previous + 1e-9
                                                  or (abs(connector_cost - previous) <= 1e-9
                                                      and candidate_key >= current_key)):
                        continue
                    roots.add(node)
                    distances[node] = connector_cost
                    anchor_by_root[node] = anchor
                    heapq.heappush(queue, (connector_cost, sequence, node))
                    sequence += 1
        return roots, distances, anchor_by_root, queue

    forward_roots, forward_distance, forward_anchor, forward_queue = seed_roots(starts)
    reverse_roots, reverse_distance, reverse_anchor, reverse_queue = seed_roots(goals)
    if not forward_queue or not reverse_queue:
        return None, 0, None, None, lattice_origin
    forward_previous, reverse_next = {}, {}
    sequence = 0
    forward_goal_points = [coordinate(node) for node in reverse_roots]
    reverse_start_points = [coordinate(node) for node in forward_roots]
    forward_queue = [(cost + min(math.dist(coordinate(node), p) for p in forward_goal_points),
                      cost, seq, node) for cost, seq, node in forward_queue]
    reverse_queue = [(cost + min(math.dist(coordinate(node), p) for p in reverse_start_points),
                      cost, seq, node) for cost, seq, node in reverse_queue]
    heapq.heapify(forward_queue)
    heapq.heapify(reverse_queue)

    def reconstruct(node):
        left = [node]
        while left[-1] not in forward_roots:
            left.append(forward_previous[left[-1]])
        start_root = left[-1]
        left.reverse()
        right, cursor = [], node
        while cursor not in reverse_roots:
            cursor = reverse_next[cursor]
            right.append(cursor)
        return left + right, forward_anchor[start_root], reverse_anchor[cursor]

    def pop_live(queue, distances):
        while queue:
            priority, queued_distance, _, node = heapq.heappop(queue)
            if queued_distance <= distances.get(node, float("inf")) + 1e-9:
                return priority, queued_distance, node
        return None

    directions = [(x, y) for x in (-1, 0, 1) for y in (-1, 0, 1) if x or y]
    expanded, meeting = 0, None
    while forward_queue and reverse_queue and expanded < max_expanded_states:
        forward_turn = forward_queue[0][0] <= reverse_queue[0][0]
        queue = forward_queue if forward_turn else reverse_queue
        distances = forward_distance if forward_turn else reverse_distance
        current = pop_live(queue, distances)
        if current is None:
            queue.clear()
            continue
        _, current_distance, node = current
        expanded += 1
        other = reverse_distance if forward_turn else forward_distance
        if node in other:
            meeting = node
            break
        point = coordinate(node)
        neighbors = []
        for dx, dy in directions:
            candidate = (node[0] + dx, node[1] + dy, node[2])
            target = coordinate(candidate)
            if (bounds[0] <= target[0] <= bounds[2] and bounds[1] <= target[1] <= bounds[3]
                    and clear_segment(point, target, stack[node[2]])):
                neighbors.append((candidate, math.hypot(dx, dy) * step))
        if can_change_layer(point):
            neighbors.extend(((node[0], node[1], layer), 4.0)
                             for layer in range(len(stack)) if layer != node[2])
        for candidate, cost in neighbors:
            score = current_distance + cost
            if score + 1e-9 >= distances.get(candidate, float("inf")):
                continue
            distances[candidate] = score
            if forward_turn:
                forward_previous[candidate] = node
                heuristic = min(math.dist(coordinate(candidate), p) for p in forward_goal_points)
            else:
                reverse_next[candidate] = node
                heuristic = min(math.dist(coordinate(candidate), p) for p in reverse_start_points)
            heapq.heappush(queue, (score + heuristic, score, sequence, candidate))
            sequence += 1
            if candidate in other:
                meeting = candidate
                break
        if meeting is not None:
            break
    if meeting is None:
        return None, expanded, None, None, lattice_origin
    path, start_anchor, goal_anchor = reconstruct(meeting)
    return path, expanded, start_anchor, goal_anchor, lattice_origin


def _physical_connected_search_points(path, step, lattice_origin, start_anchor, goal_anchor, stack):
    """Restore exact native anchor endpoints around one shared lattice path."""
    if not path or not isinstance(start_anchor, dict) or not isinstance(goal_anchor, dict):
        raise ValueError("connected-anchor search result is incomplete")
    points = [(start_anchor["point"], start_anchor["layer"])]
    for node in path:
        candidate = ((lattice_origin[0] + node[0] * step,
                      lattice_origin[1] + node[1] * step), stack[node[2]])
        if points[-1] != candidate:
            points.append(candidate)
    final_anchor = (goal_anchor["point"], goal_anchor["layer"])
    if points[-1] != final_anchor:
        points.append(final_anchor)
    return points


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


def repair_one(board, pcbnew, drc_report, *, pair_index=0, max_expanded_states=150000,
               grid_step_mm=0.25, search_mode="standard", grid_phase=None,
               connected_endpoints=False):
    """Add one bounded A* candidate for a fresh DRC explicit UUID pair."""
    import _kicad_worker as worker
    try:
        from _native_board_rules import native_edge_clearance
    except ImportError:  # package-level pure tests; KiCad worker uses same-folder import
        from ._native_board_rules import native_edge_clearance

    if list(board.Zones()):
        raise ValueError("same-net repair requires a board without zones")
    if not isinstance(search_mode, str) or search_mode not in {"standard", "bidirectional"}:
        raise ValueError("repair search mode is invalid")
    if not isinstance(connected_endpoints, bool):
        raise ValueError("connected_endpoints must be a boolean")
    if connected_endpoints and search_mode != "bidirectional":
        raise ValueError("connected copper anchors require bidirectional search")
    grid_phase = _validated_search_grid_phase(grid_phase, search_mode)
    if isinstance(pair_index, bool) or not isinstance(pair_index, int) or pair_index < 0:
        raise ValueError("pair_index must be a nonnegative integer")
    if (
        isinstance(max_expanded_states, bool)
        or not isinstance(max_expanded_states, int)
        or max_expanded_states <= 0
        or isinstance(grid_step_mm, bool)
        or not isinstance(grid_step_mm, (int, float))
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
    npth_holes, hole_clearance_iu, hole_to_hole_iu = _native_npth_hole_obstacles(board, pcbnew)
    npth_attribute = getattr(pcbnew, "PAD_ATTRIB_NPTH", None)
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
    edge = native_edge_clearance(board, pcbnew)

    def point(p):
        return pcbnew.ToMM(p.x), pcbnew.ToMM(p.y)

    def vec(p):
        return _native_point_vector(pcbnew, p)

    def ends(item):
        if type(item).__name__ in {"PCB_TRACK", "PCB_ARC"}:
            return [point(item.GetStart()), point(item.GetEnd())]
        return [point(item.GetPosition())]

    start, goal = min(((a, b) for a in ends(first) for b in ends(last)), key=lambda ab: math.dist(*ab))
    original_start, original_goal = start, goal
    stack, complete = worker._native_copper_layer_stack(board, pcbnew)
    if not complete:
        raise ValueError("native copper stack cannot be verified")
    connected_anchor_report = None
    if connected_endpoints:
        board.BuildConnectivity()
        first_anchors, first_cluster_ids = _connected_component_anchor_vertices(
            board, first, by_uuid, net, net_name, stack, pcbnew, original_goal,
        )
        last_anchors, last_cluster_ids = _connected_component_anchor_vertices(
            board, last, by_uuid, net, net_name, stack, pcbnew, original_start,
        )
        if first_cluster_ids & last_cluster_ids:
            raise ValueError("native DRC endpoint clusters overlap despite an unconnected finding")
        connected_start_anchors, connected_goal_anchors = first_anchors, last_anchors
        connected_anchor_report = {
            "verified": True,
            "from_uuid": _uuid(first),
            "to_uuid": _uuid(last),
            "from_cluster_item_count": len(first_cluster_ids),
            "to_cluster_item_count": len(last_cluster_ids),
            "anchor_vertex_limit_per_cluster": 32,
            "search_strategy": "single_shared_bidirectional_multiroot",
        }
    edge_layer = int(pcbnew.Edge_Cuts)
    edges = [item for item in board.GetDrawings() if int(item.GetLayer()) == edge_layer]
    bounds_iu = worker._rectangular_outline_bounds_iu(edges, pcbnew)
    if bounds_iu is None:
        raise ValueError("repair supports only a verified rectangular board outline")
    edge_iu = edge["value_iu"]
    track_width_iu = int(pcbnew.FromMM(width))
    track_edge_margin_iu = _edge_margin_iu(track_width_iu, edge_iu)
    # Search bounds only constrain the track lattice. Candidate vias are
    # checked individually in clear_segment using their larger radius.
    margin = track_edge_margin_iu
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

    npth_hole_index = {}
    for hole_index, hole_shape in enumerate(npth_holes):
        # Expand the drill geometry itself by the configured hole clearance;
        # route candidates separately include their own copper half-width.
        hole_box = _native_hole_bbox_mm(hole_shape, max(hole_clearance_iu, hole_to_hole_iu), pcbnew)
        for layer_id in stack:
            for x, y in cells(hole_box):
                npth_hole_index.setdefault((layer_id, x, y), set()).add(hole_index)

    for item in items:
        is_pad = type(item).__name__ == "PAD"
        if (is_pad and npth_attribute is not None
                and int(item.GetAttribute()) == int(npth_attribute)):
            npth_drill_size = item.GetDrillSize()
            if int(npth_drill_size.x) > 0 or int(npth_drill_size.y) > 0:
                # NPTHs are modeled by their actual drill shape below. Their
                # nominal pad copper size must not create a second obstacle.
                continue
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
    if any(_via_width_on_layer(via_query, layer_id) != pcbnew.FromMM(diameter) for layer_id in stack):
        raise ValueError("native via diameter cannot be verified on every enabled copper layer")

    def candidates(layer, box):
        found = set()
        for x, y in cells(box):
            found.update(index.get((layer, x, y), ()))
        return found

    def candidate_holes(layer, box):
        found = set()
        for x, y in cells(box):
            found.update(npth_hole_index.get((layer, x, y), ()))
        return found

    def clear_segment(a, b, layer, copper_width=width, avoid_pads=False):
        start_iu, end_iu = vec(a), vec(b)
        segment_width_iu = int(pcbnew.FromMM(copper_width))
        edge_margin_iu = _edge_margin_iu(segment_width_iu, edge_iu)
        if not _segment_inside_edge_margin(start_iu, end_iu, (x0, y0, x0 + w, y0 + h), edge_margin_iu):
            return False
        query.SetStart(start_iu)
        query.SetEnd(end_iu)
        query.SetWidth(segment_width_iu)
        query.SetLayer(layer)
        candidate_via = a == b and copper_width == diameter
        if candidate_via:
            via_query.SetPosition(start_iu)
            shape = via_query.GetEffectiveShape(layer)
        else:
            shape = query.GetEffectiveShape()
        radius = copper_width / 2
        box = (min(a[0], b[0]) - radius, min(a[1], b[1]) - radius, max(a[0], b[0]) + radius, max(a[1], b[1]) + radius)
        nearby_npth_holes = candidate_holes(layer, box) if npth_holes else set()
        if any(_collides_with_npth_hole(shape, [npth_holes[n]], hole_clearance_iu)
               for n in nearby_npth_holes):
            return False
        if candidate_via and nearby_npth_holes:
            try:
                via_hole_shape = via_query.GetEffectiveHoleShape()
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                raise ValueError("native candidate via drill geometry is unavailable") from exc
            if via_hole_shape is None:
                raise ValueError("native candidate via drill geometry is unavailable")
            if any(_collides_with_npth_hole(via_hole_shape, [npth_holes[n]], hole_to_hole_iu)
                   for n in nearby_npth_holes):
                return False
        for n in candidates(layer, box):
            other_net, is_pad, other_shape = records[n]
            if other_net == net and not (avoid_pads and is_pad):
                continue
            distance = 0 if other_net == net and avoid_pads and is_pad else pcbnew.FromMM(clearance)
            if other_shape.Collide(shape, distance):
                return False
        return True

    silk_obstacles, silk_clearance_iu = _native_silkscreen_obstacles(board, pcbnew, bbox)
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
        if _collides_with_silkscreen(
            p, via_query, silk_obstacles, diameter, silk_clearance_iu, pcbnew
        ):
            via_cache[key] = False
            return False
        if any(math.dist(p, c) < drill / 2 + r + hole_gap for c, r in holes):
            via_cache[key] = False
            return False
        okay = all(clear_segment(p, p, layer_id, diameter, avoid_pads=True) for layer_id in stack)
        via_cache[key] = okay
        return okay

    origins = {(0, 0, i) for i, layer_id in enumerate(stack) if first.IsOnLayer(layer_id)}
    target_layers = {i for i, layer_id in enumerate(stack) if last.IsOnLayer(layer_id)}
    if not connected_endpoints and (not origins or not target_layers):
        raise ValueError("endpoints do not occupy supported copper layers")
    if connected_endpoints:
        path, expanded, selected_start_anchor, selected_goal_anchor, search_origin = (
            _bidirectional_connected_anchor_search(
                connected_start_anchors, connected_goal_anchors, stack, bounds,
                grid_step_mm, max_expanded_states,
                lambda a, b, layer: clear_segment(a, b, layer), can_change_layer,
                grid_phase=grid_phase,
            )
        )
        step = float(grid_step_mm)
        search_goal = selected_goal_anchor["point"] if selected_goal_anchor else goal
        reverse = False
        if selected_start_anchor is not None and selected_goal_anchor is not None:
            connected_anchor_report.update({
                "from_anchor_uuid": selected_start_anchor["uuid"],
                "from_anchor_point_mm": list(selected_start_anchor["point"]),
                "from_anchor_layer": selected_start_anchor["layer"],
                "to_anchor_uuid": selected_goal_anchor["uuid"],
                "to_anchor_point_mm": list(selected_goal_anchor["point"]),
                "to_anchor_layer": selected_goal_anchor["layer"],
                "search_grid_origin_mm": list(search_origin),
                "search_state_budget": max_expanded_states,
            })
    elif search_mode == "bidirectional":
        search_origin = (
            start[0] + grid_phase[0] * grid_step_mm,
            start[1] + grid_phase[1] * grid_step_mm,
        )
        path, expanded = _bidirectional_astar_search(
            start, goal, len(stack), target_layers, origins, bounds,
            grid_step_mm, max_expanded_states,
            lambda a, b, layer: clear_segment(a, b, stack[layer]), can_change_layer,
            grid_phase=grid_phase,
        )
        step, search_goal, reverse = float(grid_step_mm), goal, False
    else:
        path, step, expanded, search_origin, search_goal, reverse = _search_with_fallback(
            start, goal, len(stack), target_layers,
            origins, bounds, grid_step_mm, max_expanded_states,
            lambda a, b, layer: clear_segment(a, b, stack[layer]), can_change_layer,
        )
    if path is None:
        raise RuntimeError(f"no collision-free repair within {expanded} native search states")
    if connected_endpoints:
        points = _physical_connected_search_points(
            path, step, search_origin, selected_start_anchor, selected_goal_anchor, stack,
        )
    else:
        exact_start = start if search_mode == "bidirectional" and grid_phase != (0.0, 0.0) else None
        points = _physical_search_points(path, step, search_origin, search_goal, reverse, stack,
                                        exact_start=exact_start)
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
                if any(_via_width_on_layer(via, layer_id) != pcbnew.FromMM(diameter) for layer_id in stack):
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
        "search_mode": search_mode,
        **({
            "requested_grid_phase": list(grid_phase),
            "search_grid_phase": list(grid_phase),
            "search_grid_origin_mm": list(search_origin),
        } if search_mode == "bidirectional" else {}),
        "requested_grid_step_mm": float(grid_step_mm),
        "search_grid_step_mm": step,
        "search_direction": (
            "bidirectional" if search_mode == "bidirectional" else ("reverse" if reverse else "forward")
        ),
        **({"connected_endpoint_anchors": connected_anchor_report} if connected_anchor_report else {}),
        "search_state_budget": max_expanded_states,
        "resolved_track_width_mm": width,
        "resolved_via_diameter_mm": diameter,
        "resolved_via_drill_mm": drill,
        "resolved_clearance_mm": clearance,
        "native_npth_hole_rules": {
            "status": "verified" if npth_holes else "not_required",
            "hole_clearance_iu": hole_clearance_iu if npth_holes else None,
            "hole_clearance_mm": pcbnew.ToMM(hole_clearance_iu) if npth_holes else None,
            "hole_to_hole_min_iu": hole_to_hole_iu if npth_holes else None,
            "hole_to_hole_min_mm": pcbnew.ToMM(hole_to_hole_iu) if npth_holes else None,
            "source": "BOARD_DESIGN_SETTINGS.m_HoleClearance/m_HoleToHoleMin",
        },
        "native_edge_clearance": {"status": "ok", **edge},
    }
