"""Standalone pcbnew worker; stdlib only so it runs in KiCad's bundled Python."""

from __future__ import annotations

import json
import math
import sys

_SILK_CLEARANCE_MM = 0.2
_SILK_EDGE_MARGIN_MM = 0.4
_SILK_SEARCH_STEP_MM = 0.5


def _identity(board):
    result = {}
    for footprint in board.GetFootprints():
        ref = footprint.GetReference()
        if not ref or ref in result:
            raise ValueError("Board footprints require unique, nonempty references")
        result[ref] = sorted((pad.GetNumber(), pad.GetNetname()) for pad in footprint.Pads())
    return result


def _inspect(board, pcbnew):
    footprints = []
    for footprint in board.GetFootprints():
        position = footprint.GetPosition()
        rotation = footprint.GetOrientationDegrees()
        pads = [{"number": p.GetNumber(), "net": p.GetNetname(),
                 "x": pcbnew.ToMM(p.GetPosition().x), "y": pcbnew.ToMM(p.GetPosition().y)}
                for p in footprint.Pads()]
        fields = [footprint.Reference(), footprint.Value(), *list(footprint.GetFields())]
        field_state = []
        seen_fields = set()
        for field in fields:
            if id(field) in seen_fields:
                continue
            seen_fields.add(id(field))
            field_state.append((field, field.GetTextPos(), field.GetTextAngleDegrees()))
        footprint.SetOrientationDegrees(0)
        for row, pad in zip(pads, footprint.Pads()):
            row["local_x"] = pcbnew.ToMM(pad.GetPosition().x - position.x)
            row["local_y"] = pcbnew.ToMM(pad.GetPosition().y - position.y)
        courtyard_layer = pcbnew.B_CrtYd if footprint.GetLayer() == pcbnew.B_Cu else pcbnew.F_CrtYd
        courtyard = footprint.GetCourtyard(courtyard_layer)
        box = courtyard.BBox() if not courtyard.IsEmpty() else footprint.GetBoundingBox(False, False)
        # Enclose geometry about the footprint origin, including off-center
        # THT connectors; a width/height alone cannot describe such parts.
        width = 2 * max(abs(box.GetX() - position.x), abs(box.GetRight() - position.x))
        height = 2 * max(abs(box.GetY() - position.y), abs(box.GetBottom() - position.y))
        # Keep the true courtyard shape available to placement optimizers.
        # Width/height above intentionally remain origin-symmetric for older
        # consumers; off-center connectors can make those legacy values much
        # larger than the actual courtyard rectangle.
        courtyard_width = box.GetWidth()
        courtyard_height = box.GetHeight()
        courtyard_center_x = (box.GetX() + box.GetRight()) // 2 - position.x
        courtyard_center_y = (box.GetY() + box.GetBottom()) // 2 - position.y
        footprint.SetOrientationDegrees(rotation)
        # pcbnew may rotate footprint fields as a side effect of changing the
        # footprint angle. Inspection must not alter their authored positions.
        for field, text_position, text_angle in field_state:
            field.SetTextPos(text_position)
            field.SetTextAngleDegrees(text_angle)
        footprints.append({
            "ref": footprint.GetReference(), "footprint": str(footprint.GetFPID().GetLibItemName()),
            "x": pcbnew.ToMM(position.x), "y": pcbnew.ToMM(position.y), "rotation": rotation,
            "layer": "back" if footprint.GetLayer() == pcbnew.B_Cu else "front",
            "locked": footprint.IsLocked(), "width": pcbnew.ToMM(width), "height": pcbnew.ToMM(height),
            "courtyard_width": pcbnew.ToMM(courtyard_width),
            "courtyard_height": pcbnew.ToMM(courtyard_height),
            "courtyard_center_x": pcbnew.ToMM(courtyard_center_x),
            "courtyard_center_y": pcbnew.ToMM(courtyard_center_y),
            "pads": pads,
        })
    box = board.GetBoardEdgesBoundingBox()
    edges = [d for d in board.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
    rectangular = len(edges) == 1 and edges[0].GetShape() == pcbnew.SHAPE_T_RECT
    if len(edges) == 4 and all(d.GetShape() == pcbnew.SHAPE_T_SEGMENT for d in edges):
        points = [(d.GetStart().x, d.GetStart().y) for d in edges]
        points += [(d.GetEnd().x, d.GetEnd().y) for d in edges]
        unique = set(points)
        rectangular = (
            len(unique) == 4 and len({p[0] for p in unique}) == 2 and len({p[1] for p in unique}) == 2
            and all(points.count(p) == 2 for p in unique)
            and len({tuple(sorted(((d.GetStart().x, d.GetStart().y), (d.GetEnd().x, d.GetEnd().y))))
                     for d in edges}) == 4
            and all(d.GetStart().x == d.GetEnd().x or d.GetStart().y == d.GetEnd().y for d in edges)
        )
    return {"footprints": footprints, "track_count": len(list(board.GetTracks())),
            "zone_count": len(list(board.Zones())),
            "rectangular_outline": rectangular,
            "bounds": {"x": pcbnew.ToMM(box.GetX()), "y": pcbnew.ToMM(box.GetY()),
                       "width": pcbnew.ToMM(box.GetWidth()), "height": pcbnew.ToMM(box.GetHeight())}}


def _box(item, pcbnew, margin_mm=0.0):
    try:
        bounds = item.GetBoundingBox()
    except TypeError:
        bounds = item.GetBoundingBox(False, False)
    margin = pcbnew.FromMM(margin_mm)
    return (bounds.GetX() - margin, bounds.GetY() - margin,
            bounds.GetRight() + margin, bounds.GetBottom() + margin)


def _box_from_bounds(bounds, pcbnew, margin_mm=0.0):
    margin = pcbnew.FromMM(margin_mm)
    return (bounds.GetX() - margin, bounds.GetY() - margin,
            bounds.GetRight() + margin, bounds.GetBottom() + margin)


def _boxes_overlap(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _point_box_distance_sq(point, box):
    dx = max(box[0] - point[0], 0, point[0] - box[2])
    dy = max(box[1] - point[1], 0, point[1] - box[3])
    return dx * dx + dy * dy


def _candidate_associated(center, own_courtyard, ref, obstacles):
    own_distance = _point_box_distance_sq(center, own_courtyard)
    return not any(
        kind == "courtyard" and owner != ref
        and _point_box_distance_sq(center, obstacle) < own_distance
        for kind, owner, obstacle in obstacles
    )


def _courtyard_box(footprint, pcbnew):
    layer = pcbnew.B_CrtYd if footprint.GetLayer() == pcbnew.B_Cu else pcbnew.F_CrtYd
    courtyard = footprint.GetCourtyard(layer)
    if not courtyard.IsEmpty():
        return _box_from_bounds(courtyard.BBox(), pcbnew, _SILK_CLEARANCE_MM)
    # Some library footprints omit courtyards. Use their non-text native
    # footprint geometry as a conservative obstacle rather than treating the
    # missing courtyard as free space.
    return _box(footprint, pcbnew, _SILK_CLEARANCE_MM)


def _silk_obstacles(board, pcbnew):
    obstacles = {pcbnew.F_SilkS: [], pcbnew.B_SilkS: []}
    for footprint in board.GetFootprints():
        silk_layer = pcbnew.B_SilkS if footprint.GetLayer() == pcbnew.B_Cu else pcbnew.F_SilkS
        obstacles[silk_layer].append(("courtyard", footprint.GetReference(), _courtyard_box(footprint, pcbnew)))
        mask_layer = pcbnew.B_Mask if silk_layer == pcbnew.B_SilkS else pcbnew.F_Mask
        for pad in footprint.Pads():
            # Pad bboxes are copper geometry. A small expansion represents the
            # solder-mask opening so reference ink cannot sit on exposed pads.
            if pad.IsOnLayer(silk_layer) or pad.IsOnLayer(mask_layer):
                obstacles[silk_layer].append(("pad_mask", footprint.GetReference(),
                                              _box(pad, pcbnew, _SILK_CLEARANCE_MM)))
        for item in footprint.GraphicalItems():
            if item.GetLayer() == silk_layer:
                obstacles[silk_layer].append(("silkscreen_graphics", footprint.GetReference(),
                                              _box(item, pcbnew, _SILK_CLEARANCE_MM)))
        for field in footprint.GetFields():
            if field.GetTextTypeDescription() == "Reference":
                continue
            if field.GetLayer() == silk_layer and _field_visible(field):
                obstacles[silk_layer].append(("silkscreen_graphics", footprint.GetReference(),
                                              _box(field, pcbnew, _SILK_CLEARANCE_MM)))
    for drawing in board.GetDrawings():
        layer = drawing.GetLayer()
        if layer in obstacles:
            obstacles[layer].append(("silkscreen_graphics", "board", _box(drawing, pcbnew,
                                                                            _SILK_CLEARANCE_MM)))
    return obstacles


def _field_visible(field):
    try:
        return bool(field.IsVisible())
    except AttributeError:
        return True


def _reference_entries(board, pcbnew):
    entries = []
    for footprint in board.GetFootprints():
        field = footprint.Reference()
        layer = field.GetLayer()
        if layer not in (pcbnew.F_SilkS, pcbnew.B_SilkS) or not _field_visible(field):
            continue
        entries.append({"ref": footprint.GetReference(), "footprint": footprint, "field": field,
                        "layer": layer, "box": _box(field, pcbnew, _SILK_CLEARANCE_MM),
                        "locked": bool(footprint.IsLocked() or field.IsLocked())})
    return entries


def _board_text_bounds(board, pcbnew):
    bounds = board.GetBoardEdgesBoundingBox()
    margin = pcbnew.FromMM(_SILK_EDGE_MARGIN_MM)
    return (bounds.GetX() + margin, bounds.GetY() + margin,
            bounds.GetRight() - margin, bounds.GetBottom() - margin)


def _inside_bounds(box, bounds):
    return box[0] >= bounds[0] and box[1] >= bounds[1] and box[2] <= bounds[2] and box[3] <= bounds[3]


def _candidate_centers(courtyard, text_width, text_height, bounds, pcbnew, max_ring=None):
    """Generate deterministic perimeter positions from nearest to farthest."""
    step = pcbnew.FromMM(_SILK_SEARCH_STEP_MM)
    half_width = max(0, (text_width + 1) // 2) + pcbnew.FromMM(_SILK_CLEARANCE_MM)
    half_height = max(0, (text_height + 1) // 2) + pcbnew.FromMM(_SILK_CLEARANCE_MM)
    base = (courtyard[0] - half_width, courtyard[1] - half_height,
            courtyard[2] + half_width, courtyard[3] + half_height)
    center_x = (courtyard[0] + courtyard[2]) // 2
    center_y = (courtyard[1] + courtyard[3]) // 2
    if max_ring is None:
        span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
        max_ring = max(1, int(math.ceil(span / step)))
    seen = set()
    for ring in range(max_ring + 1):
        left, top = base[0] - ring * step, base[1] - ring * step
        right, bottom = base[2] + ring * step, base[3] + ring * step
        xs = {left, right, center_x}
        ys = {top, bottom, center_y}
        x = left
        while x <= right:
            xs.add(x)
            x += step
        y = top
        while y <= bottom:
            ys.add(y)
            y += step
        candidates = {(x, top) for x in xs} | {(x, bottom) for x in xs}
        candidates |= {(left, y) for y in ys} | {(right, y) for y in ys}
        for candidate in sorted(candidates, key=lambda point: (
            _point_box_distance_sq(point, courtyard),
            point[1] >= center_y,
            (point[0] - center_x) ** 2 + (point[1] - center_y) ** 2,
            point[1], point[0],
        )):
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def _tidy_reference_fields(board, pcbnew):
    """Move visible unlocked reference fields clear of native silkscreen obstacles.

    This changes only each unlocked reference field's position and text angle.
    Its text, UUID, layer, size, values, and footprint positions remain intact.
    """
    obstacles = _silk_obstacles(board, pcbnew)
    entries = _reference_entries(board, pcbnew)
    bounds = _board_text_bounds(board, pcbnew)
    moved = 0
    normalized = 0

    # References read cleanly across a board when the movable fields use the
    # same horizontal orientation. Locked fields retain their authored angle.
    for entry in entries:
        if entry["locked"]:
            continue
        angle = float(entry["field"].GetTextAngleDegrees()) % 180.0
        if min(angle, 180.0 - angle) > 0.01:
            entry["field"].SetTextAngleDegrees(0.0)
            normalized += 1
    # Native text bounds depend on angle, so refresh the entries after rotation.
    entries = _reference_entries(board, pcbnew)

    # Locked fields reserve their current positions. Place the most constrained
    # movable fields first, so an easy label cannot claim a neighboring part's
    # only nearby clear slot.
    def nearby_option_count(entry):
        if entry["locked"]:
            return -1
        box = entry["box"]
        width = box[2] - box[0] - 2 * pcbnew.FromMM(_SILK_CLEARANCE_MM)
        height = box[3] - box[1] - 2 * pcbnew.FromMM(_SILK_CLEARANCE_MM)
        own_courtyard = _courtyard_box(entry["footprint"], pcbnew)
        initial_center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        count = 0
        max_local_distance = pcbnew.FromMM(3.0)
        local_ring = int(math.ceil(max_local_distance / pcbnew.FromMM(_SILK_SEARCH_STEP_MM)))
        for center in _candidate_centers(
            own_courtyard, width, height, bounds, pcbnew, max_ring=local_ring
        ):
            if _point_box_distance_sq(center, own_courtyard) > max_local_distance ** 2:
                continue
            dx, dy = center[0] - initial_center[0], center[1] - initial_center[1]
            candidate_box = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
            if not _inside_bounds(candidate_box, bounds):
                continue
            if any(_boxes_overlap(candidate_box, obstacle[2]) for obstacle in obstacles[entry["layer"]]):
                continue
            if not _candidate_associated(center, own_courtyard, entry["ref"], obstacles[entry["layer"]]):
                continue
            count += 1
        return count

    locked_entries = sorted((entry for entry in entries if entry["locked"]), key=lambda entry: entry["ref"])
    movable_entries = sorted(
        (entry for entry in entries if not entry["locked"]),
        key=lambda entry: (nearby_option_count(entry), entry["ref"]),
    )
    ordered = locked_entries + movable_entries
    occupied_refs = {pcbnew.F_SilkS: [], pcbnew.B_SilkS: []}
    for entry in ordered:
        if entry["locked"]:
            occupied_refs[entry["layer"]].append(entry["box"])
            continue
        field = entry["field"]
        pos = field.GetTextPos()
        current_box = entry["box"]
        courtyard = _courtyard_box(entry["footprint"], pcbnew)
        # The courtyard is in obstacles in the same coordinate system. Find
        # its exact obstacle to derive candidates outside the component body.
        width = current_box[2] - current_box[0] - 2 * pcbnew.FromMM(_SILK_CLEARANCE_MM)
        height = current_box[3] - current_box[1] - 2 * pcbnew.FromMM(_SILK_CLEARANCE_MM)
        initial_center = ((current_box[0] + current_box[2]) // 2,
                          (current_box[1] + current_box[3]) // 2)
        # Keep the field's native origin/justification offset while testing
        # translated native bboxes.
        origin_offset = (pos.x - initial_center[0], pos.y - initial_center[1])
        fixed_obstacles = obstacles[entry["layer"]]
        chosen = None
        for center in _candidate_centers(courtyard, width, height, bounds, pcbnew):
            dx, dy = center[0] - initial_center[0], center[1] - initial_center[1]
            candidate_box = (current_box[0] + dx, current_box[1] + dy,
                             current_box[2] + dx, current_box[3] + dy)
            if not _inside_bounds(candidate_box, bounds):
                continue
            if any(_boxes_overlap(candidate_box, obstacle[2]) for obstacle in fixed_obstacles):
                continue
            if any(_boxes_overlap(candidate_box, other_box)
                   for other_box in occupied_refs[entry["layer"]]):
                continue
            # A clear label can still read as belonging to the wrong part when
            # its center is nearer a neighbor's courtyard than its own. Keep
            # searching for a local position that visually associates it with
            # its footprint whenever one is available.
            if not _candidate_associated(center, courtyard, entry["ref"], fixed_obstacles):
                continue
            chosen = (center, (center[0] + origin_offset[0], center[1] + origin_offset[1]))
            break
        if chosen is None:
            occupied_refs[entry["layer"]].append(current_box)
            continue
        center, new_origin = chosen
        if center != initial_center:
            field.SetTextPos(pcbnew.VECTOR2I(new_origin[0], new_origin[1]))
            moved += 1
            delta = (center[0] - initial_center[0], center[1] - initial_center[1])
            entry["box"] = (current_box[0] + delta[0], current_box[1] + delta[1],
                            current_box[2] + delta[0], current_box[3] + delta[1])
        occupied_refs[entry["layer"]].append(entry["box"])
    return {"requested": True, "moved_count": moved, "orientation_normalized_count": normalized}


def _reference_conflict_report(board, pcbnew, moved_count, orientation_normalized_count):
    obstacles = _silk_obstacles(board, pcbnew)
    entries = _reference_entries(board, pcbnew)
    bounds = _board_text_bounds(board, pcbnew)
    conflict_counts = {"courtyard": 0, "pad_mask": 0, "silkscreen_graphics": 0,
                       "reference_reference": 0, "edge": 0, "association": 0}
    conflicted_refs = set()
    for entry in entries:
        box = entry["box"]
        if not _inside_bounds(box, bounds):
            conflict_counts["edge"] += 1
            conflicted_refs.add(entry["ref"])
        for kind, _owner, obstacle in obstacles[entry["layer"]]:
            if _boxes_overlap(box, obstacle):
                conflict_counts[kind] += 1
                conflicted_refs.add(entry["ref"])
        center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        # Reference fields can be authored on either silk layer regardless
        # of footprint side. The footprint's physical courtyard is the
        # correct association anchor even when this field lives on opposite
        # side silk and therefore has no courtyard obstacle on its layer.
        own_courtyard = _courtyard_box(entry["footprint"], pcbnew)
        if not _candidate_associated(center, own_courtyard, entry["ref"], obstacles[entry["layer"]]):
            conflict_counts["association"] += 1
            conflicted_refs.add(entry["ref"])
    for index, first in enumerate(entries):
        for second in entries[index + 1:]:
            if first["layer"] == second["layer"] and _boxes_overlap(first["box"], second["box"]):
                conflict_counts["reference_reference"] += 1
                conflicted_refs.update((first["ref"], second["ref"]))
    return {"requested": True, "moved_count": moved_count,
            "orientation_normalized_count": orientation_normalized_count,
            "unresolved_count": len(conflicted_refs),
            "unresolved_references": sorted(conflicted_refs),
            "remaining_conflict_count": sum(conflict_counts.values()),
            "conflict_counts": conflict_counts}


def execute(request):
    import pcbnew

    board = pcbnew.LoadBoard(request["board"])
    if board is None:
        raise ValueError("KiCad could not load the board")
    identity = _identity(board)
    operation = request["operation"]
    tidy_summary = None
    if operation == "inspect":
        return {"status": "ok", **_inspect(board, pcbnew)}
    if operation == "export_dsn":
        if not pcbnew.ExportSpecctraDSN(board, request["output"]):
            raise ValueError("KiCad Specctra export failed; check the board outline and netlist")
        return {"status": "ok", "version": pcbnew.Version()}
    if operation == "import_ses":
        placements = {f.GetReference(): (f.GetPosition().x, f.GetPosition().y, f.GetOrientationDegrees(), f.GetLayer())
                      for f in board.GetFootprints()}
        if not pcbnew.ImportSpecctraSES(board, request["session"]):
            raise ValueError("KiCad could not import the Specctra session")
        for f in board.GetFootprints():
            if placements[f.GetReference()] != (f.GetPosition().x, f.GetPosition().y,
                                                f.GetOrientationDegrees(), f.GetLayer()):
                raise ValueError(f"Session unexpectedly changed footprint {f.GetReference()}: "
                                 f"{placements[f.GetReference()]} -> "
                                 f"{(f.GetPosition().x, f.GetPosition().y, f.GetOrientationDegrees(), f.GetLayer())}")
    elif operation == "place":
        placements = request["placements"]
        if not placements or set(placements) - set(identity):
            raise ValueError("Placement references do not match the board")
        if len(list(board.GetTracks())) or len(list(board.Zones())):
            raise ValueError("Placement requires an unrouted board without copper zones")
        # Validate every change before any board mutation or save.
        for ref, p in placements.items():
            if not all(isinstance(p.get(k), (int, float)) and math.isfinite(p[k]) for k in ("x", "y", "rotation")):
                raise ValueError(f"{ref}: invalid placement coordinates")
            footprint = board.FindFootprintByReference(ref)
            current_layer = "back" if footprint.GetLayer() == pcbnew.B_Cu else "front"
            if p.get("layer", current_layer) != current_layer:
                raise ValueError("Automatic placement currently preserves each component's board side")
            if footprint.IsLocked() and (
                abs(pcbnew.ToMM(footprint.GetPosition().x) - p["x"]) > 1e-6
                or abs(pcbnew.ToMM(footprint.GetPosition().y) - p["y"]) > 1e-6
                or abs(footprint.GetOrientationDegrees() - p["rotation"]) % 360 > 1e-6
            ):
                raise ValueError(f"{ref}: refusing to move a locked footprint")
        for ref, p in placements.items():
            f = board.FindFootprintByReference(ref)
            target = pcbnew.VECTOR2I(round(p["x"] * 1_000_000), round(p["y"] * 1_000_000))
            current = f.GetPosition()
            current_rotation = float(f.GetOrientationDegrees())
            rotation_delta = abs((current_rotation - float(p["rotation"]) + 180.0) % 360.0 - 180.0)
            if current.x != target.x or current.y != target.y or rotation_delta > 1e-6:
                f.SetPosition(target)
                f.SetOrientationDegrees(p["rotation"])
        if request.get("tidy_references") is True:
            placement_positions = {
                footprint.GetReference(): (footprint.GetPosition().x, footprint.GetPosition().y,
                                           footprint.GetOrientationDegrees(), footprint.GetLayer())
                for footprint in board.GetFootprints()
            }
            tidy_summary = _tidy_reference_fields(board, pcbnew)
            for footprint in board.GetFootprints():
                expected = placement_positions[footprint.GetReference()]
                actual = (footprint.GetPosition().x, footprint.GetPosition().y,
                          footprint.GetOrientationDegrees(), footprint.GetLayer())
                if actual != expected:
                    raise ValueError(f"Reference cleanup unexpectedly moved footprint {footprint.GetReference()}")
    else:
        raise ValueError(f"Unknown KiCad operation: {operation}")
    if _identity(board) != identity:
        raise ValueError("Operation changed pad/net identity")
    if not pcbnew.SaveBoard(request["output"], board):
        raise ValueError("KiCad could not save the board")
    reloaded = pcbnew.LoadBoard(request["output"])
    if reloaded is None or _identity(reloaded) != identity:
        raise ValueError("Saved board failed pad/net identity verification")
    result = {"status": "ok", "version": pcbnew.Version(), **_inspect(reloaded, pcbnew)}
    if tidy_summary is not None:
        result["silkscreen"] = _reference_conflict_report(
            reloaded, pcbnew, tidy_summary["moved_count"],
            tidy_summary["orientation_normalized_count"],
        )
    return result


if __name__ == "__main__":
    try:
        payload = execute(json.load(sys.stdin))
    except Exception as exc:
        payload = {"status": "error", "message": str(exc)}
    print("CW_KICAD_RESULT=" + json.dumps(payload))
    raise SystemExit(0 if payload["status"] == "ok" else 1)
