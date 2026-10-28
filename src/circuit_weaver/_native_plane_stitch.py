"""Bounded, candidate-only same-net stitching vias for starved plane thermals.

The caller must refill every zone, save/reload, and run a fresh full native DRC
before accepting any board changed by this helper.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

try:
    from ._native_ground_plane import _outline_bounds
    from ._native_routing_repair import _class_rules, _native_point_vector, _via_width_on_layer
except ImportError:  # Standalone import inside KiCad's bundled Python worker.
    from _native_ground_plane import _outline_bounds
    from _native_routing_repair import _class_rules, _native_point_vector, _via_width_on_layer


_MAX_VIAS = 16
_MAX_GRID_POINTS_PER_PAD = 10_000
_DOMINANT_AREA_RATIO = 10.0


def _uuid(item):
    try:
        value = str(item.m_Uuid.AsString())
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("native item UUID is unavailable") from exc
    if not value:
        raise ValueError("native item UUID is empty")
    return value


def _zone_area_mm2(polygons, index):
    """Compute one filled polygon's area, subtracting its holes."""
    try:
        area_iu2 = abs(float(polygons.Outline(index).Area()))
        for hole_index in range(int(polygons.HoleCount(index))):
            area_iu2 -= abs(float(polygons.Hole(index, hole_index).Area()))
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("native filled polygon area is unavailable") from exc
    area = area_iu2 / 1e12
    if not math.isfinite(area) or area <= 0:
        raise ValueError("native filled polygon has invalid area")
    return area


def _dominant_outline(zone, polygons):
    count = int(polygons.OutlineCount())
    if count < 1:
        raise ValueError("filled copper plane has no polygon outlines")
    areas = [_zone_area_mm2(polygons, index) for index in range(count)]
    ranked = sorted(range(count), key=lambda index: (-areas[index], index))
    if len(ranked) > 1 and areas[ranked[0]] < _DOMINANT_AREA_RATIO * areas[ranked[1]]:
        raise ValueError("filled copper plane has no unambiguous dominant polygon")
    return {
        "index": ranked[0],
        "area_mm2": areas[ranked[0]],
        "areas_mm2": areas,
        "non_dominant_indices": ranked[1:],
    }


def _bbox(item, pcbnew):
    try:
        box = item.GetBoundingBox()
        x0 = pcbnew.ToMM(box.GetX())
        y0 = pcbnew.ToMM(box.GetY())
        return (x0, y0, x0 + pcbnew.ToMM(box.GetWidth()), y0 + pcbnew.ToMM(box.GetHeight()))
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("native copper or silk bounding box is unavailable") from exc


def _distance_to_box(x, y, box):
    x0, y0, x1, y1 = box
    dx = max(x0 - x, 0.0, x - x1)
    dy = max(y0 - y, 0.0, y - y1)
    return math.hypot(dx, dy)


def _netclass_clearances(board, pcbnew, target_clearance):
    """Read all class clearances to build conservative exact-collision bounds."""
    board_path = Path(str(board.GetFileName())).resolve()
    project_path = board_path.with_suffix(".kicad_pro")
    if not project_path.is_file():
        raise ValueError("staged KiCad project rules are unavailable")
    try:
        settings = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("staged KiCad project rules cannot be read") from exc
    classes = settings.get("net_settings", {}).get("classes")
    if not isinstance(classes, list) or not classes:
        raise ValueError("staged project has no net-class rules")
    result = {}
    for row in classes:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
            raise ValueError("staged project has an invalid net-class entry")
        if row["name"] in result:
            raise ValueError("staged project has duplicate net-class names")
        value = row.get("clearance")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise ValueError(f"net-class clearance is unavailable for {row['name']!r}")
        result[row["name"]] = float(value)
    if "Default" not in result:
        raise ValueError("staged project lacks the Default net class")

    def class_name(item):
        getter = getattr(item, "GetNetClassName", None)
        if not callable(getter):
            raise ValueError("native item net-class identity is unavailable")
        name = str(getter())
        if name not in result:
            raise ValueError(f"native item maps to an unknown net class {name!r}")
        return name

    return result, class_name


def _validate_stack(board, pcbnew):
    try:
        stack_obj = board.GetEnabledLayers().CuStack()
        seq = getattr(stack_obj, "Seq", None)
        raw_layers = list(seq() if callable(seq) else stack_obj)
        stack = [int(layer) for layer in raw_layers]
        names = [str(board.GetLayerName(layer)) for layer in stack]
        copper_count = int(board.GetDesignSettings().GetCopperLayerCount())
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("native copper stack cannot be resolved") from exc
    if (len(stack) != 4 or len(set(stack)) != 4 or copper_count != 4
            or len(set(names)) != 4 or names[0] != "F.Cu" or names[-1] != "B.Cu"
            or any(not name.endswith(".Cu") for name in names)):
        raise ValueError("plane stitching supports only a verified four-layer F.Cu/In*.Cu/B.Cu stack")
    return stack, names


def _find_native_targets(board, violations):
    if not isinstance(violations, list):
        raise ValueError("fresh DRC violations must be a list")
    if len(violations) > _MAX_VIAS:
        raise ValueError("thermal error rows exceed the bounded native target count")
    pads = {}
    zones = {}
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            uid = _uuid(pad)
            if uid in pads:
                raise ValueError("native pad UUIDs are not unique")
            pads[uid] = pad
    for zone in board.Zones():
        uid = _uuid(zone)
        if uid in zones:
            raise ValueError("native zone UUIDs are not unique")
        zones[uid] = zone

    found = {}
    for row_index, row in enumerate(violations):
        if (not isinstance(row, dict) or row.get("type") != "starved_thermal"
                or row.get("severity") != "error"):
            raise ValueError("plane stitching requires only native thermal error rows")
        items = row.get("items")
        if not isinstance(items, list) or len(items) != 2 or any(not isinstance(item, dict) for item in items):
            raise ValueError(f"thermal DRC row {row_index} does not identify exactly one pad and one zone")
        zone_items = [item for item in items if item.get("uuid") in zones]
        pad_items = [item for item in items if item.get("uuid") in pads]
        if len(zone_items) != 1 or len(pad_items) != 1:
            raise ValueError(f"thermal DRC row {row_index} UUIDs do not map to one native pad and zone")
        zone_uuid, pad_uuid = str(zone_items[0]["uuid"]), str(pad_items[0]["uuid"])
        zone, pad = zones[zone_uuid], pads[pad_uuid]
        try:
            net_code = int(zone.GetNetCode())
            pad_net_code = int(pad.GetNetCode())
            layer = int(zone.GetLayer())
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("thermal target net or copper layer is unavailable") from exc
        if (net_code <= 0 or pad_net_code != net_code
                or str(pad.GetNetname()) != str(zone.GetNetname())
                or not str(board.GetLayerName(layer)).endswith(".Cu")
                or not pad.IsOnLayer(layer)):
            raise ValueError("thermal row does not identify a same-positive-net pad and copper zone")
        if bool(zone.GetIsRuleArea()):
            raise ValueError("thermal row references a rule-area zone")
        if not zone.IsFilled() or not zone.HasFilledPolysForLayer(layer):
            raise ValueError("thermal row references a zone without current native fill")
        polygons = zone.GetFilledPolysList(layer)
        if int(polygons.OutlineCount()) < 1:
            raise ValueError("thermal row zone has no filled polygon geometry")
        key = (zone_uuid, pad_uuid)
        if key in found:
            raise ValueError("native thermal pad/zone pairs must be unique")
        found[key] = {"zone": zone, "pad": pad, "zone_uuid": zone_uuid, "pad_uuid": pad_uuid,
                      "net": str(zone.GetNetname()), "net_code": net_code, "layer": layer,
                      "layer_name": str(board.GetLayerName(layer)), "polygons": polygons}
    if not found:
        raise ValueError("fresh native DRC contains no resolvable starved thermal rows")
    return list(found.values()), zones, pads


def _silk_boxes(board, pcbnew):
    layers = {int(pcbnew.F_SilkS), int(pcbnew.B_SilkS)}
    result = []
    for drawing in board.GetDrawings():
        if int(drawing.GetLayer()) in layers:
            result.append(_bbox(drawing, pcbnew))
    for footprint in board.GetFootprints():
        for field in (footprint.Reference(), footprint.Value()):
            if field.IsVisible() and int(field.GetLayer()) in layers:
                result.append(_bbox(field, pcbnew))
        for drawing in footprint.GraphicalItems():
            if int(drawing.GetLayer()) in layers:
                result.append(_bbox(drawing, pcbnew))
    return result


def _drilled_holes(board, pcbnew):
    holes = []
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            drill = pad.GetDrillSize()
            if int(drill.x) > 0 or int(drill.y) > 0:
                pos = pad.GetPosition()
                holes.append(((pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)),
                              max(pcbnew.ToMM(int(drill.x)), pcbnew.ToMM(int(drill.y))) / 2))
    for item in board.GetTracks():
        if str(item.GetClass()) == "PCB_VIA":
            pos = item.GetPosition()
            holes.append(((pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)), pcbnew.ToMM(int(item.GetDrillValue())) / 2))
    return holes


def _collision_index(board, pcbnew, stack, cell_size, target_net_code, target_clearance, class_clearances,
                     class_name_for):
    items = [item for footprint in board.GetFootprints() for item in footprint.Pads()]
    items.extend(list(board.GetTracks()))
    records = []
    index = {}

    def cell_range(box):
        return (range(math.floor(box[0] / cell_size), math.floor(box[2] / cell_size) + 1),
                range(math.floor(box[1] / cell_size), math.floor(box[3] / cell_size) + 1))

    for item in items:
        try:
            net_code = int(item.GetNetCode())
            net_name = str(item.GetNetname())
            item_class = str(item.GetClass())
            box = _bbox(item, pcbnew)
            on_layers = [layer for layer in stack if item.IsOnLayer(layer)]
            if not on_layers:
                continue
            class_name = class_name_for(item)
            pair_clearance = max(target_clearance, class_clearances[class_name],
                                 pcbnew.ToMM(int(board.GetDesignSettings().m_MinClearance)))
        except (AttributeError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            raise ValueError("native obstacle class, layer, or geometry is unavailable") from exc
        record_index = len(records)
        records.append((item, net_code, net_name, item_class, box, on_layers, pair_clearance))
        expanded = (box[0] - cell_size, box[1] - cell_size, box[2] + cell_size, box[3] + cell_size)
        xs, ys = cell_range(expanded)
        for layer in on_layers:
            for cx in xs:
                for cy in ys:
                    index.setdefault((layer, cx, cy), []).append(record_index)
    return records, index, cell_range


def repair(board, pcbnew, violations, *, max_vias=16, search_radius_mm=6.0, grid_step_mm=0.25):
    """Add bounded GND/same-net stitching-via candidates from fresh thermal DRC rows.

    Returned vias are diagnostic candidates only. The caller must refill zones,
    save/reload and run a fresh full native DRC before any acceptance decision.
    """
    try:
        from . import _kicad_worker as worker
    except ImportError:  # Standalone KiCad worker import.
        import _kicad_worker as worker

    if (isinstance(max_vias, bool) or not isinstance(max_vias, int) or not 1 <= max_vias <= _MAX_VIAS):
        raise ValueError(f"max_vias must be an integer from 1 through {_MAX_VIAS}")
    for value, label, minimum, maximum in (
        (search_radius_mm, "search_radius_mm", 0.5, 20.0),
        (grid_step_mm, "grid_step_mm", 0.1, 1.0),
    ):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not minimum <= value <= maximum):
            raise ValueError(f"{label} must be finite and in [{minimum}, {maximum}]")
    half_steps = math.ceil(search_radius_mm / grid_step_mm)
    if (2 * half_steps + 1) ** 2 > _MAX_GRID_POINTS_PER_PAD:
        raise ValueError("search grid exceeds the bounded native point budget")

    targets, all_zones, _ = _find_native_targets(board, violations)
    target_net_codes = {row["net_code"] for row in targets}
    target_net_names = {row["net"] for row in targets}
    if len(target_net_codes) != 1 or len(target_net_names) != 1:
        raise ValueError("one repair call may target only one same-net plane")
    net_code = next(iter(target_net_codes))
    net_name = next(iter(target_net_names))
    if net_name != "GND":
        raise ValueError("plane stitching is currently restricted to the native GND net")
    width, diameter, drill, clearance = _class_rules(board, pcbnew, net_name)
    stack, stack_names = _validate_stack(board, pcbnew)
    bounds = _outline_bounds(board, pcbnew)
    ds = board.GetDesignSettings()
    global_clearance = pcbnew.ToMM(int(ds.m_MinClearance))
    hole_gap = max(clearance, global_clearance, pcbnew.ToMM(int(ds.m_HoleToHoleMin)),
                   pcbnew.ToMM(int(ds.m_HoleClearance)))
    silk_clearance = pcbnew.ToMM(int(ds.m_SilkClearance))
    via_radius = diameter / 2
    edge_margin = via_radius + max(clearance, global_clearance, pcbnew.ToMM(int(ds.m_CopperEdgeClearance)))
    if not (bounds[0] + edge_margin < bounds[2] - edge_margin
            and bounds[1] + edge_margin < bounds[3] - edge_margin):
        raise ValueError("rectangular board leaves no legal stitching-via placement area")

    same_net_zones = []
    for zone in all_zones.values():
        layer_name = str(board.GetLayerName(int(zone.GetLayer())))
        if not layer_name.endswith(".Cu"):
            continue
        if bool(zone.GetIsRuleArea()) or int(zone.GetNetCode()) != net_code:
            raise ValueError("other copper-layer zones or keepouts are not modeled by the stitching collision search")
        same_net_zones.append(zone)
    by_layer = {}
    for zone in same_net_zones:
        layer = int(zone.GetLayer())
        if [enabled for enabled in stack if zone.IsOnLayer(enabled)] != [layer]:
            raise ValueError("plane stitching requires separate single-copper-layer zones")
        if layer in by_layer:
            raise ValueError("same-net plane mapping is ambiguous on a copper layer")
        if not zone.IsFilled() or not zone.HasFilledPolysForLayer(layer):
            raise ValueError("same-net plane lacks current native filled polygons")
        polygons = zone.GetFilledPolysList(layer)
        dominant = _dominant_outline(zone, polygons)
        by_layer[layer] = {"zone": zone, "polygons": polygons, "dominant": dominant,
                           "zone_uuid": _uuid(zone), "layer_name": str(board.GetLayerName(layer))}
    if len(by_layer) < 2:
        raise ValueError("stitching requires multiple filled same-net copper planes")

    # Every DRC target must resolve to its same-net zone and at least one other
    # layer with an unambiguous dominant filled plane.
    for target in targets:
        source = by_layer.get(target["layer"])
        if source is None or source["zone_uuid"] != target["zone_uuid"]:
            raise ValueError("thermal row zone is not a unique filled plane on its native layer")
        if not source["dominant"]["non_dominant_indices"]:
            raise ValueError("thermal target plane has no non-dominant local copper polygon")
        secondary = [row for layer, row in by_layer.items() if layer != target["layer"]]
        if not secondary:
            raise ValueError("thermal target has no same-net plane on another copper layer")

    target_clearances, class_name_for = _netclass_clearances(board, pcbnew, clearance)
    max_clearance = max(target_clearances.values(), default=clearance)
    cell_size = max(2.0, diameter + 2 * max_clearance)
    records, spatial, cell_range = _collision_index(
        board, pcbnew, stack, cell_size, net_code, clearance, target_clearances, class_name_for,
    )
    silk = _silk_boxes(board, pcbnew)
    holes = _drilled_holes(board, pcbnew)

    query = pcbnew.PCB_VIA(board)
    query.SetViaType(pcbnew.VIATYPE_THROUGH)
    front_layer, back_layer = stack[0], stack[-1]
    query.SetLayerPair(front_layer, back_layer)
    query.SetWidth(pcbnew.FromMM(diameter))
    query.SetDrill(pcbnew.FromMM(drill))
    query.SetNetCode(net_code)
    for layer in stack:
        if _via_width_on_layer(query, layer) != int(pcbnew.FromMM(diameter)):
            raise ValueError("native through-via diameter cannot be verified on every copper layer")

    def candidate_clear(x, y, planned):
        point = _native_point_vector(pcbnew, (x, y))
        actual_x, actual_y = pcbnew.ToMM(point.x), pcbnew.ToMM(point.y)
        if not (bounds[0] + edge_margin <= actual_x <= bounds[2] - edge_margin
                and bounds[1] + edge_margin <= actual_y <= bounds[3] - edge_margin):
            return None
        query.SetPosition(point)
        if any(_distance_to_box(actual_x, actual_y, box) <= via_radius + silk_clearance for box in silk):
            return None
        if any(math.hypot(actual_x - hole_point[0], actual_y - hole_point[1])
               <= drill / 2 + hole_radius + hole_gap for hole_point, hole_radius in holes):
            return None
        if any(math.hypot(actual_x - item["x_mm"], actual_y - item["y_mm"])
               <= drill + hole_gap for item in planned):
            return None
        x_cells, y_cells = cell_range((actual_x - cell_size, actual_y - cell_size,
                                       actual_x + cell_size, actual_y + cell_size))
        nearby = set()
        for layer in stack:
            for cx in x_cells:
                for cy in y_cells:
                    nearby.update(spatial.get((layer, cx, cy), ()))
        for item_index in nearby:
            item, other_net, other_name, item_class, item_box, item_layers, pair_clearance = records[item_index]
            if item_class == "PAD":
                # Via-in-pad is forbidden even for an electrically common net.
                clearance_iu = pcbnew.FromMM(max(pair_clearance, clearance))
            elif other_net == net_code:
                # Same-net track contact is electrically valid; existing vias
                # are still excluded by the hole-spacing and shape checks below.
                clearance_iu = 0
            else:
                clearance_iu = pcbnew.FromMM(pair_clearance)
            for layer in stack:
                if layer not in item_layers:
                    continue
                try:
                    candidate_shape = query.GetEffectiveShape(layer)
                    other_shape = (item.GetEffectiveShape(layer)
                                   if item_class in {"PAD", "PCB_VIA"} else item.GetEffectiveShape())
                    if candidate_shape is None or other_shape is None:
                        return None
                    if other_shape.Collide(candidate_shape, clearance_iu):
                        if other_net == net_code and item_class != "PAD":
                            continue
                        return None
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    return None
        return actual_x, actual_y

    def ring_membership(target, outline_index):
        pad = target["pad"]
        center = pad.GetPosition()
        x0, y0 = pcbnew.ToMM(center.x), pcbnew.ToMM(center.y)
        try:
            pad_radius = pcbnew.ToMM(int(pad.GetBoundingRadius()))
            zone = target["zone"]
            base_radius = pad_radius + pcbnew.ToMM(int(zone.GetThermalReliefGap()))
            base_radius += pcbnew.ToMM(int(zone.GetThermalReliefSpokeWidth())) / 2
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("thermal pad ring geometry is unavailable") from exc
        counts = 0
        for extra in (0.0, grid_step_mm, 2 * grid_step_mm, 0.5, 1.0, 2.0, 3.0, 4.0):
            radius = base_radius + extra
            if radius > search_radius_mm:
                continue
            for degrees in range(0, 360, 10):
                angle = math.radians(degrees)
                point = _native_point_vector(
                    pcbnew, (x0 + radius * math.cos(angle), y0 + radius * math.sin(angle)),
                )
                try:
                    if target["polygons"].Contains(point, outline_index):
                        counts += 1
                except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                    raise ValueError("filled copper subpolygon membership is unavailable") from exc
        return counts

    # Associate each non-dominant outline with nearby error-pad thermal rings.
    # This is geometric proximity evidence only, not proof of unique electrical
    # pad ownership. Dedupe shared components before selecting via sites.
    component_rows = {}
    pad_mapping = []
    for target in targets:
        source = by_layer[target["layer"]]
        mapped = []
        for outline_index in source["dominant"]["non_dominant_indices"]:
            count = ring_membership(target, outline_index)
            if count:
                key = (target["zone_uuid"], int(outline_index))
                component_rows.setdefault(key, {
                    "source_zone_uuid": target["zone_uuid"],
                    "source_layer": target["layer_name"],
                    "outline_index": int(outline_index),
                    "area_mm2": source["dominant"]["areas_mm2"][outline_index],
                    "nearby_pad_uuids": [],
                })
                if target["pad_uuid"] not in component_rows[key]["nearby_pad_uuids"]:
                    component_rows[key]["nearby_pad_uuids"].append(target["pad_uuid"])
                mapped.append({"source_zone_uuid": target["zone_uuid"],
                               "outline_index": int(outline_index), "ring_samples": count,
                               "mapping": "nearby_pad_ring_overlap"})
        pad_mapping.append({"pad_uuid": target["pad_uuid"], "zone_uuid": target["zone_uuid"],
                            "nearby_components": mapped})
    components = list(component_rows.values())
    target_pad_ids = sorted({row["pad_uuid"] for row in targets})
    if not components:
        raise ValueError("no non-dominant filled subpolygon overlaps a target pad's thermal ring")
    if len(components) > min(max_vias, len(target_pad_ids)):
        raise ValueError("distinct nearby components exceed the bounded target-pad via cap")

    planned = []
    locations = []
    for component in components:
        nearby_targets = [row for row in targets
                          if row["zone_uuid"] == component["source_zone_uuid"]
                          and row["pad_uuid"] in component["nearby_pad_uuids"]]
        source = next(row for row in by_layer.values() if row["zone_uuid"] == component["source_zone_uuid"])
        secondary_rows = [row for layer, row in by_layer.items()
                          if layer != int(source["zone"].GetLayer())]
        if not secondary_rows:
            raise ValueError("local plane component has no same-net plane on another layer")
        candidate_rows = []
        for target in nearby_targets:
            center = target["pad"].GetPosition()
            x0, y0 = pcbnew.ToMM(center.x), pcbnew.ToMM(center.y)
            n = math.ceil(search_radius_mm / grid_step_mm)
            for ix in range(-n, n + 1):
                for iy in range(-n, n + 1):
                    x = x0 + ix * grid_step_mm
                    y = y0 + iy * grid_step_mm
                    if math.hypot(x - x0, y - y0) > search_radius_mm:
                        continue
                    point = _native_point_vector(pcbnew, (x, y))
                    try:
                        if not source["polygons"].Contains(point, component["outline_index"]):
                            continue
                    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                        raise ValueError("native local-plane membership query failed") from exc
                    secondary_hits = []
                    for secondary in secondary_rows:
                        polys = secondary["polygons"]
                        index = secondary["dominant"]["index"]
                        try:
                            if polys.Contains(point, index):
                                secondary_hits.append(secondary)
                        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                            raise ValueError("native secondary-plane membership query failed") from exc
                    if not secondary_hits:
                        continue
                    clear = candidate_clear(x, y, planned)
                    if clear is None:
                        continue
                    actual_x, actual_y = clear
                    for secondary in secondary_hits:
                        candidate_rows.append((math.hypot(actual_x - x0, actual_y - y0),
                                               actual_x, actual_y, target, secondary))
        if not candidate_rows:
            raise ValueError("no legal via point lies inside both a nearby local and dominant plane polygon")
        candidate_rows.sort(key=lambda row: (row[0], row[1], row[2], row[4]["layer_name"]))
        distance, x, y, closest_target, secondary = candidate_rows[0]
        location = {
            "x_mm": round(x, 6), "y_mm": round(y, 6), "net": net_name,
            "source_zone_uuid": component["source_zone_uuid"],
            "source_layer": component["source_layer"],
            "source_outline_index": component["outline_index"],
            "source_outline_area_mm2": round(component["area_mm2"], 6),
            "target_zone_uuid": secondary["zone_uuid"],
            "target_layer": secondary["layer_name"],
            "nearby_pad_uuids": sorted(component["nearby_pad_uuids"]),
            "nearest_pad_uuid": closest_target["pad_uuid"],
            "distance_from_nearest_pad_mm": round(distance, 6),
            "mapping_is_proximity_not_unique_pad_ownership": True,
        }
        planned.append(location)
        locations.append(location)

    if not locations or len(locations) > max_vias or len(locations) > len(target_pad_ids):
        raise ValueError("candidate via count exceeds the configured or target-pad bound")

    before_copper = worker._copper_state(board)
    before_identity = worker._identity(board)
    before_layout = worker._fixed_layout_state(board)
    before_geometry = worker._ground_plane_source_geometry_state(board, pcbnew)
    before_zones = worker._zone_definition_state(board, pcbnew)
    for location in locations:
        via = pcbnew.PCB_VIA(board)
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(front_layer, back_layer)
        via.SetPosition(_native_point_vector(pcbnew, (location["x_mm"], location["y_mm"])))
        via.SetWidth(pcbnew.FromMM(diameter))
        via.SetDrill(pcbnew.FromMM(drill))
        via.SetNetCode(net_code)
        for layer in stack:
            if _via_width_on_layer(via, layer) != int(pcbnew.FromMM(diameter)):
                raise ValueError("candidate via width differs from derived net-class rules")
        board.Add(via)
        location["via_uuid"] = _uuid(via)
    board.BuildConnectivity()

    if (worker._identity(board) != before_identity
            or worker._fixed_layout_state(board) != before_layout
            or worker._ground_plane_source_geometry_state(board, pcbnew) != before_geometry
            or worker._zone_definition_state(board, pcbnew) != before_zones):
        raise ValueError("stitch candidate changed pad/net identity, layout, source geometry, or zone rules")
    added = worker._copper_state(board) - before_copper
    removed = before_copper - worker._copper_state(board)
    added_rows = [json.loads(raw) for raw, count in added.items() for _ in range(count)]
    if (removed or len(added_rows) != len(locations)
            or any(row.get("type") != "PCB_VIA" or row.get("net") != net_name or row.get("locked")
                   for row in added_rows)):
        raise ValueError("stitch operation changed original copper or added an unexpected copper object")

    return {
        "status": "candidate_only_requires_full_drc",
        "net": net_name,
        "target_pad_uuids": target_pad_ids,
        "target_zone_uuids": sorted({row["zone_uuid"] for row in targets}),
        "added_vias": len(locations),
        "resolved_track_width_mm": width,
        "resolved_via_diameter_mm": diameter,
        "resolved_via_drill_mm": drill,
        "resolved_clearance_mm": clearance,
        "components": components,
        "pad_component_mapping": pad_mapping,
        "locations": locations,
        "requires_zone_refill_save_reload_and_fresh_full_native_drc": True,
    }
