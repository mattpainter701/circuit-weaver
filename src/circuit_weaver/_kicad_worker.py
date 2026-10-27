"""Standalone pcbnew worker; stdlib only so it runs in KiCad's bundled Python."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import Counter

_SILK_CLEARANCE_MM = 0.2
_SILK_EDGE_MARGIN_MM = 0.4
_SILK_SEARCH_STEP_MM = 0.5


def _placement_envelope(footprint, courtyard):
    """Never let an undersized courtyard hide pads or other body geometry."""
    boxes = [footprint.GetBoundingBox(False, False)]
    if not courtyard.IsEmpty():
        boxes.append(courtyard.BBox())
    return (min(box.GetX() for box in boxes), min(box.GetY() for box in boxes),
            max(box.GetRight() for box in boxes), max(box.GetBottom() for box in boxes))


def _native_is_roundrect(pad, pcbnew):
    try:
        return int(pad.GetShape()) == int(pcbnew.PAD_SHAPE_ROUNDRECT)
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
        return False


def _native_roundrect_radius_ratio(pad, pcbnew):
    """Return a validated native roundrect ratio, or None when unavailable."""
    try:
        shape = int(pad.GetShape())
        roundrect = int(pcbnew.PAD_SHAPE_ROUNDRECT)
        if shape != roundrect:
            return None
        getter = getattr(pad, "GetRoundRectRadiusRatio", None)
        if not callable(getter):
            getter = getattr(pad, "GetRoundrectRadiusRatio", None)
        if not callable(getter):
            return None
        ratio = float(getter())
        return ratio if math.isfinite(ratio) and 0.0 <= ratio <= 0.5 else None
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
        return None


def _native_surface_shape_known(pad, pcbnew):
    """Known solid occupancy geometry, separate from legacy known_shape."""
    try:
        shape = int(pad.GetShape())
        ordinary = {
            int(getattr(pcbnew, name)) for name in (
                "PAD_SHAPE_CIRCLE", "PAD_SHAPE_RECT", "PAD_SHAPE_OVAL")
            if hasattr(pcbnew, name)
        }
        return shape in ordinary or _native_roundrect_radius_ratio(pad, pcbnew) is not None
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
        return False


def _opposite_courtyard_state(footprint, pcbnew):
    """Return (known, present) for the courtyard on the other PCB surface."""
    try:
        layer = pcbnew.F_CrtYd if footprint.GetLayer() == pcbnew.B_Cu else pcbnew.B_CrtYd
        courtyard = footprint.GetCourtyard(layer)
        polygon_present = not courtyard.IsEmpty()
        graphics = list(footprint.GraphicalItems())
        graphics_on_layer = [item for item in graphics if int(item.GetLayer()) == int(layer)]
        return True, polygon_present or bool(graphics_on_layer)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False, None


def _native_copper_layer_inventory(board, pcbnew):
    """Return the enabled copper stack and whether the API inventory is complete."""
    known_ids, known_complete = _native_known_copper_layer_inventory(pcbnew)
    try:
        stack = board.GetEnabledLayers().CuStack()
        seq = getattr(stack, "Seq", None)
        values = seq() if callable(seq) else stack
        layer_ids = sorted(set(int(layer) for layer in values))
        complete = (known_complete
                    and len(layer_ids) == int(board.GetCopperLayerCount())
                    and set(layer_ids) <= set(known_ids)
                    and {int(pcbnew.F_Cu), int(pcbnew.B_Cu)} <= set(known_ids))
        return layer_ids, complete
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return [], False


def _native_copper_layer_stack(board, pcbnew):
    """Return enabled copper IDs in native physical stack order, fail-closed."""
    known_ids, known_complete = _native_known_copper_layer_inventory(pcbnew)
    try:
        stack = board.GetEnabledLayers().CuStack()
        seq = getattr(stack, "Seq", None)
        layer_ids = [int(layer) for layer in (seq() if callable(seq) else stack)]
        complete = (known_complete and len(layer_ids) == len(set(layer_ids))
                    and len(layer_ids) == int(board.GetCopperLayerCount())
                    and set(layer_ids) <= set(known_ids)
                    and {int(pcbnew.F_Cu), int(pcbnew.B_Cu)} <= set(layer_ids))
        return layer_ids, complete
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return [], False


def _native_via_layer_span(via, copper_stack_ids, stack_complete):
    """Expand a via's physical end layers through the enabled copper stack.

    ``TopLayer``/``BottomLayer`` identify the span endpoints; they do not list
    every internal layer crossed by a through via. Preserve the board's native
    physical order (which is not numeric layer-ID order) and fail closed when
    either endpoint or the stack cannot be verified.
    """
    if not stack_complete or not isinstance(copper_stack_ids, (list, tuple)):
        return None
    try:
        stack = [int(layer) for layer in copper_stack_ids]
        if len(stack) < 2 or len(stack) != len(set(stack)):
            return None
        top, bottom = int(via.TopLayer()), int(via.BottomLayer())
        if top == bottom or top not in stack or bottom not in stack:
            return None
        top_index, bottom_index = stack.index(top), stack.index(bottom)
        first, last = sorted((top_index, bottom_index))
        span = stack[first:last + 1]
        return span if len(span) >= 2 else None
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
        return None


def _native_pad_is_plated_through(pad, pcbnew):
    """Return exact PAD_ATTRIB_PTH evidence, or None if the API cannot prove it."""
    try:
        pth_attribute = getattr(pcbnew, "PAD_ATTRIB_PTH", None)
        if pth_attribute is None:
            return None
        attribute = int(pad.GetAttribute())
        if attribute == int(pth_attribute):
            return True
        other_attributes = {
            int(value) for name in ("PAD_ATTRIB_SMD", "PAD_ATTRIB_CONN", "PAD_ATTRIB_NPTH")
            if (value := getattr(pcbnew, name, None)) is not None
        }
        if attribute not in other_attributes:
            return None
        return False
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
        return None


def _native_pad_enabled_copper_layers(pad_layer_ids, enabled_layer_ids, enabled_inventory_complete):
    """Intersect a pad's known copper-layer set with the board-enabled stack."""
    if (not enabled_inventory_complete or not isinstance(pad_layer_ids, (list, tuple))
            or not isinstance(enabled_layer_ids, (list, tuple))):
        return None
    if any(not isinstance(layer, int) or isinstance(layer, bool)
           for layer in (*pad_layer_ids, *enabled_layer_ids)):
        return None
    return sorted(set(pad_layer_ids) & set(enabled_layer_ids))


def _native_stack_state(board, pcbnew):
    layer_ids, complete = _native_copper_layer_stack(board, pcbnew)
    if not complete:
        raise ValueError("native copper stack inventory is incomplete")
    layers = [{"id": layer_id, "name": str(board.GetLayerName(layer_id))}
              for layer_id in layer_ids]
    names = [row["name"] for row in layers]
    if (any(not name.endswith(".Cu") for name in names)
            or len(names) != len(set(names))):
        raise ValueError("native copper stack has invalid or duplicate layer names")
    return {"count": len(layers), "layers": layers}


def _zone_definition_state(board, pcbnew):
    """Fingerprint zone outlines/settings separately from mutable fill geometry."""
    rows = []
    for zone in board.Zones():
        try:
            uuid = str(zone.m_Uuid.AsString())
            outline = zone.Outline().Format(False)
            layer_id = int(zone.GetLayer())
            layer_name = str(board.GetLayerName(layer_id))
            row = {
                "id": uuid,
                "net": str(zone.GetNetname()),
                "layer_id": layer_id,
                "layer_name": layer_name,
                "layer_ids": sorted(int(layer) for layer in zone.GetLayerSet().Seq()),
                "rule_area": _zone_is_rule_area(zone),
                "outline_sha256": hashlib.sha256(outline.encode("utf-8")).hexdigest(),
                "settings": {
                    "clearance_iu": int(zone.GetLocalClearance()),
                    "min_thickness_iu": int(zone.GetMinThickness()),
                    "thermal_gap_iu": int(zone.GetThermalReliefGap()),
                    "thermal_spoke_width_iu": int(zone.GetThermalReliefSpokeWidth()),
                    "pad_connection": int(zone.GetPadConnection()),
                    "island_removal_mode": int(zone.GetIslandRemovalMode()),
                    "min_island_area_iu2": int(zone.GetMinIslandArea()),
                    "priority": int(zone.GetAssignedPriority()),
                },
            }
            if row["rule_area"]:
                row["keepout"] = {name: bool(getattr(zone, name)()) for name in (
                    "GetDoNotAllowTracks", "GetDoNotAllowVias", "GetDoNotAllowPads",
                    "GetDoNotAllowZoneFills", "GetDoNotAllowFootprints")}
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("native zone definition could not be captured completely") from exc
        rows.append(row)
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("native zone identities are not unique")
    return sorted(rows, key=lambda row: row["id"])


def _zone_is_rule_area(zone):
    for name in ("GetIsRuleArea", "IsRuleArea"):
        member = getattr(zone, name, None)
        if callable(member):
            return bool(member())
        if member is not None:
            return bool(member)
    return False


def _zone_fill_state(board, pcbnew):
    rows = []
    keepout_count = 0
    non_copper_count = 0
    for zone in board.Zones():
        if _zone_is_rule_area(zone):
            keepout_count += 1
            continue
        layer = int(zone.GetLayer())
        layer_name = str(board.GetLayerName(layer))
        if not layer_name.endswith(".Cu"):
            non_copper_count += 1
            continue
        if sorted(int(value) for value in zone.GetLayerSet().Seq()) != [layer]:
            raise ValueError("saved fill verification currently requires single-layer copper zones")
        if not zone.IsFilled() or not zone.HasFilledPolysForLayer(layer):
            raise ValueError(f"zone {zone.GetNetname()} on {layer_name} has no saved fill")
        polygons = zone.GetFilledPolysList(layer)
        polygon_count = int(polygons.OutlineCount())
        islands = sum(bool(zone.IsIsland(layer, index)) for index in range(polygon_count))
        area_mm2 = float(zone.CalculateFilledArea()) / 1e12
        if not math.isfinite(area_mm2) or area_mm2 < 0:
            raise ValueError("native zone fill has invalid area")
        rows.append({
            "id": str(zone.m_Uuid.AsString()), "net": str(zone.GetNetname()),
            "layer_name": layer_name, "area_mm2": round(area_mm2, 6),
            "island_count": islands,
            "polygon_sha256": hashlib.sha256(polygons.Format(False).encode("utf-8")).hexdigest(),
        })
    return {"rows": sorted(rows, key=lambda row: row["id"]),
            "fillable_zone_count": len(rows), "keepout_zone_count": keepout_count,
            "non_copper_zone_count": non_copper_count}


def _zone_connected_counts(board):
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    result = []
    for zone in board.Zones():
        layer_name = str(board.GetLayerName(int(zone.GetLayer())))
        if _zone_is_rule_area(zone) or not layer_name.endswith(".Cu"):
            continue
        connected = list(connectivity.GetConnectedPads(zone)) + list(connectivity.GetConnectedTracks(zone))
        count = sum(1 for item in connected
                    if item.GetClass() != "ZONE" and str(item.GetNetname()) == str(zone.GetNetname()))
        result.append({"id": str(zone.m_Uuid.AsString()), "connected_copper_count": count})
    return sorted(result, key=lambda row: row["id"])


def _refill_imported_zones(board, pcbnew, original_definitions, original_stack):
    if (_zone_definition_state(board, pcbnew) != original_definitions
            or _native_stack_state(board, pcbnew) != original_stack):
        raise ValueError("SES import changed existing zone definitions or copper stackup")
    native_zones = board.Zones()
    if not pcbnew.ZONE_FILLER(board).Fill(native_zones):
        raise ValueError("KiCad native zone refill failed after SES import")
    if (_zone_definition_state(board, pcbnew) != original_definitions
            or _native_stack_state(board, pcbnew) != original_stack):
        raise ValueError("zone refill changed existing zone definitions or copper stackup")
    fill_state = _zone_fill_state(board, pcbnew)
    return {"status": "pending_saved_verification", "zone_definitions": original_definitions,
            "copper_stack": original_stack, **fill_state,
            "connected_copper": _zone_connected_counts(board)}


def _verify_saved_imported_zones(board, pcbnew, report):
    if (_zone_definition_state(board, pcbnew) != report["zone_definitions"]
            or _native_stack_state(board, pcbnew) != report["copper_stack"]):
        raise ValueError("saved SES board changed zone definitions or copper stackup")
    saved_fill_state = _zone_fill_state(board, pcbnew)
    if saved_fill_state != {key: report[key] for key in (
            "rows", "fillable_zone_count", "keepout_zone_count", "non_copper_zone_count")}:
        raise ValueError("saved SES zone-fill geometry differs from fresh native refill")
    connected = _zone_connected_counts(board)
    try:
        from ._native_ground_plane import _unconnected_plane_pads_by_net
    except ImportError:  # Standalone import from KiCad's bundled Python.
        from _native_ground_plane import _unconnected_plane_pads_by_net
    # _zone_connected_counts already rebuilt connectivity. Native cluster
    # membership, rather than the BOARD_ITEM type flag, proves plane attachment.
    unconnected_pads_by_net = _unconnected_plane_pads_by_net(
        board, sorted({row["net"] for row in saved_fill_state["rows"]}), rebuild=False,
    )
    unconnected_ground_pads = unconnected_pads_by_net.get("GND", [])
    report.update({"status": "verified", "saved_fill_geometry_verified": True,
                   "zone_definitions_preserved": True, "copper_stackup_preserved": True,
                   "copper_layers_preserved": True,
                   "zone_count": saved_fill_state["fillable_zone_count"],
                   "total_zone_count": len(report["zone_definitions"]),
                   "zones": saved_fill_state["rows"], "connected_copper": connected,
                   "unconnected_ground_pads": unconnected_ground_pads,
                   "unconnected_pads_by_net": unconnected_pads_by_net})
    return report


def _native_known_copper_layer_inventory(pcbnew):
    """Return all copper IDs, proving enum symbols match KiCad's full copper mask."""
    try:
        enum_ids = {
            int(getattr(pcbnew, name))
            for name in dir(pcbnew)
            if (name in {"F_Cu", "B_Cu"} or (name.startswith("In") and name.endswith("_Cu")))
            and isinstance(getattr(pcbnew, name, None), int)
        }
        all_copper_ids = sorted(set(int(layer) for layer in pcbnew.LSET.AllCuMask().Seq()))
        complete = bool(all_copper_ids) and set(all_copper_ids) == enum_ids
        return all_copper_ids, complete
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return [], False


def _identity(board):
    result = {}
    for footprint in board.GetFootprints():
        ref = footprint.GetReference()
        if not ref or ref in result:
            raise ValueError("Board footprints require unique, nonempty references")
        result[ref] = sorted((pad.GetNumber(), pad.GetNetname()) for pad in footprint.Pads())
    return result


def _copper_state(board):
    """Native copper identities and geometry; additions may not replace originals."""
    rows = []
    for item in board.GetTracks():
        row = {"type": type(item).__name__, "uuid": item.m_Uuid.AsString(),
               "net": item.GetNetname(), "locked": item.IsLocked()}
        if hasattr(item, "GetDrillValue"):
            try:
                widths = (item.GetWidth(item.TopLayer()), item.GetWidth(item.BottomLayer()))
            except TypeError:
                widths = (item.GetWidth(), item.GetWidth())
            row.update(position=(item.GetPosition().x, item.GetPosition().y),
                       drill=item.GetDrillValue(), top=int(item.TopLayer()), bottom=int(item.BottomLayer()),
                       via_type=int(item.GetViaType()), widths=widths)
        else:
            row.update(start=(item.GetStart().x, item.GetStart().y),
                       end=(item.GetEnd().x, item.GetEnd().y), layer=int(item.GetLayer()), width=item.GetWidth())
        rows.append(json.dumps(row, sort_keys=True))
    return Counter(rows)


def _copper_state_for_nets(state, nets, *, include_selected=True, locked=None):
    selected = set(nets)
    result = Counter()
    for raw, count in state.items():
        row = json.loads(raw)
        if ((row["net"] in selected) == include_selected
                and (locked is None or row["locked"] is locked)):
            result[raw] = count
    return result


def _fixed_layout_state(board):
    """Footprint and text state that a copper-only operation must preserve."""
    rows = {}
    for footprint in board.GetFootprints():
        fields = [footprint.Reference(), footprint.Value(), *list(footprint.GetFields())]
        text = sorted((field.GetText(), field.GetTextPos().x, field.GetTextPos().y,
                       field.GetTextAngleDegrees(), field.GetTextSize().x, field.GetTextSize().y,
                       int(field.GetLayer()), _field_visible(field), field.IsLocked()) for field in fields)
        rows[footprint.GetReference()] = (
            footprint.GetPosition().x, footprint.GetPosition().y, footprint.GetOrientationDegrees(),
            int(footprint.GetLayer()), footprint.IsLocked(), text,
        )
    return rows


def _vec3_state(value):
    return tuple(getattr(value, axis, None) for axis in ("x", "y", "z"))


def _ground_plane_graphic_state(item, pcbnew):
    """Capture supported native drawing geometry; fail closed on unknown item types."""
    kind = type(item).__name__
    uuid = item.m_Uuid.AsString()
    if kind == "PCB_SHAPE":
        shape = item.GetShapeStr()
        if item.GetShape() == pcbnew.SHAPE_T_SEGMENT:
            geometry = ((item.GetStart().x, item.GetStart().y), (item.GetEnd().x, item.GetEnd().y))
        elif shape == "Rect":
            geometry = tuple((point.x, point.y) for point in item.GetRectCorners())
        elif shape == "Polygon":
            geometry = item.GetPolyShape().Format(False)
        else:
            raise ValueError(f"Ground-plane preservation does not support existing {shape} drawings")
        return (kind, uuid, shape, int(item.GetLayer()), geometry, item.GetWidth(),
                int(item.GetFillMode()), item.IsLocked(), item.GetNetname())
    if kind == "PCB_TEXT":
        position, size = item.GetTextPos(), item.GetTextSize()
        return (kind, uuid, item.GetText(), (position.x, position.y), item.GetTextAngleDegrees(),
                (size.x, size.y), item.GetTextThickness(), int(item.GetLayer()), item.IsMirrored(),
                item.GetHorizJustify(), item.GetVertJustify(), item.IsLocked())
    raise ValueError(f"Ground-plane preservation does not support existing drawing type {kind}")


def _ground_plane_source_geometry_state(board, pcbnew):
    """Fingerprint footprints, pad copper, 3D models and board/footprint drawings."""
    footprints = []
    for footprint in board.GetFootprints():
        position = footprint.GetPosition()
        pads = []
        for pad in footprint.Pads():
            pad_position, size, drill = pad.GetPosition(), pad.GetSize(), pad.GetDrillSize()
            pad_shapes = []
            for layer in pad.GetLayerSet().Seq():
                shape = pad.GetEffectiveShape(int(layer))
                if shape is None:
                    raise ValueError(
                        f"Cannot fingerprint native pad shape {footprint.GetReference()}.{pad.GetNumber()}"
                    )
                pad_shapes.append((int(layer), shape.Format()))
            pads.append((str(pad.GetNumber()), str(pad.GetNetname()), pad.GetNetCode(),
                         (pad_position.x, pad_position.y), pad.GetOrientationDegrees(),
                         (size.x, size.y), (drill.x, drill.y), int(pad.GetShape()),
                         int(pad.GetAttribute()), pad.GetLayerSet().FmtHex(), pad.IsLocked(),
                         pad.GetLocalClearance(), pad.GetLocalSolderMaskMargin(),
                         pad.GetLocalSolderPasteMargin(), pad.GetLocalSolderPasteMarginRatio(),
                         pad.GetThermalGap(), pad.GetThermalSpokeAngleDegrees(), tuple(pad_shapes)))
        models = []
        for model in footprint.Models():
            models.append((model.m_Filename, _vec3_state(model.m_Offset), _vec3_state(model.m_Rotation),
                           _vec3_state(model.m_Scale), model.m_Opacity, model.m_Show))
        graphics = sorted((_ground_plane_graphic_state(item, pcbnew) for item in footprint.GraphicalItems()),
                          key=repr)
        footprints.append((str(footprint.GetReference()), str(footprint.GetValueAsString()),
                           footprint.GetFPIDAsString(),
                           footprint.GetPath().AsString(), (position.x, position.y),
                           footprint.GetOrientationDegrees(), int(footprint.GetLayer()),
                           int(footprint.GetAttributes()), footprint.IsLocked(), tuple(sorted(pads)),
                           tuple(sorted(models, key=repr)), tuple(graphics)))
    drawings = sorted((_ground_plane_graphic_state(item, pcbnew) for item in board.GetDrawings()), key=repr)
    return tuple(sorted(footprints, key=repr)), tuple(drawings)


def _rectangular_outline_bounds_iu(edges, pcbnew):
    """Use Edge.Cuts centerlines, never the stroke-inflated drawing box."""
    if len(edges) == 1 and edges[0].GetShape() == pcbnew.SHAPE_T_RECT:
        points = [(point.x, point.y) for point in (edges[0].GetStart(), edges[0].GetEnd())]
    elif len(edges) == 4 and all(edge.GetShape() == pcbnew.SHAPE_T_SEGMENT for edge in edges):
        points = [(point.x, point.y) for edge in edges for point in (edge.GetStart(), edge.GetEnd())]
        unique = set(points)
        if not (
            len(unique) == 4 and len({p[0] for p in unique}) == 2 and len({p[1] for p in unique}) == 2
            and all(points.count(p) == 2 for p in unique)
            and len({tuple(sorted(((edge.GetStart().x, edge.GetStart().y),
                                   (edge.GetEnd().x, edge.GetEnd().y)))) for edge in edges}) == 4
            and all(edge.GetStart().x == edge.GetEnd().x or edge.GetStart().y == edge.GetEnd().y
                    for edge in edges)
        ):
            return None
    else:
        return None
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


def _inspect(board, pcbnew, include_route_geometry=False):
    footprints = []
    tracks, vias = [], []
    copper_layer_ids, copper_layer_inventory_complete = _native_copper_layer_inventory(board, pcbnew)
    copper_stack_ids, copper_stack_complete = _native_copper_layer_stack(board, pcbnew)
    board_copper_layers = []
    if copper_stack_complete:
        try:
            board_copper_layers = [{"id": layer_id, "name": str(board.GetLayerName(layer_id))}
                             for layer_id in copper_stack_ids]
            names = [row["name"] for row in board_copper_layers]
            copper_stack_complete = (all(name.endswith(".Cu") for name in names)
                                     and len(names) == len(set(names)))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            board_copper_layers = []
            copper_stack_complete = False
    if not copper_stack_complete:
        board_copper_layers = []
    known_copper_layer_ids, known_copper_layer_inventory_complete = (
        _native_known_copper_layer_inventory(pcbnew)
    )
    known_copper_layer_id_set = set(known_copper_layer_ids)
    enabled_copper_layer_id_set = set(copper_layer_ids) if copper_layer_inventory_complete else None
    pad_connectivity_inventory_complete = True
    for footprint in board.GetFootprints():
        position = footprint.GetPosition()
        rotation = footprint.GetOrientationDegrees()
        reference_field = footprint.Reference()
        reference_size_mm = None
        pads = []
        for pad in footprint.Pads():
            try:
                layers = sorted(int(layer) for layer in pad.GetLayerSet().Seq())
                copper_layers = sorted(set(layers) & known_copper_layer_id_set)
                attribute = int(pad.GetAttribute())
            except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
                layers = None
                copper_layers = None
                attribute = None
            enabled_copper_layers = _native_pad_enabled_copper_layers(
                copper_layers, list(enabled_copper_layer_id_set or ()),
                enabled_copper_layer_id_set is not None and copper_layer_inventory_complete,
            )
            plated_through = _native_pad_is_plated_through(pad, pcbnew)
            if plated_through is None or enabled_copper_layers is None:
                pad_connectivity_inventory_complete = False
            pads.append({
                "number": pad.GetNumber(),
                "net": pad.GetNetname(),
                "x": pcbnew.ToMM(pad.GetPosition().x),
                "y": pcbnew.ToMM(pad.GetPosition().y),
                "layers": layers,
                "copper_layers": copper_layers,
                "enabled_copper_layers": enabled_copper_layers,
                "attribute": attribute,
                "plated_through": plated_through,
            })
        pad_geometry = []
        for pad in footprint.Pads():
            try:
                layers = sorted(int(layer) for layer in pad.GetLayerSet().Seq())
                copper_layers = sorted(set(layers) & known_copper_layer_id_set)
                local_orientation = (float(pad.GetOrientationDegrees()) - float(rotation)) % 360.0
                pad_geometry.append({
                    "number": pad.GetNumber(),
                    "net": pad.GetNetname(),
                    "shape": int(pad.GetShape()),
                    "attribute": int(pad.GetAttribute()),
                    "size_iu": [int(pad.GetSize().x), int(pad.GetSize().y)],
                    "drill_iu": [int(pad.GetDrillSize().x), int(pad.GetDrillSize().y)],
                    "drill_shape": int(pad.GetDrillShape()),
                    "layers": layers,
                    "copper_layers": copper_layers,
                    "local_orientation_deg": round(local_orientation, 6),
                    "offset_iu": [int(pad.GetOffset().x), int(pad.GetOffset().y)],
                    "local_position_iu": [
                        round((pad.GetPosition().x - position.x) * math.cos(math.radians(rotation))
                                  - (pad.GetPosition().y - position.y) * math.sin(math.radians(rotation))),
                        round((pad.GetPosition().x - position.x) * math.sin(math.radians(rotation))
                                  + (pad.GetPosition().y - position.y) * math.cos(math.radians(rotation))),
                    ],
                    "known_shape": int(pad.GetShape()) in {
                        int(getattr(pcbnew, name)) for name in (
                            "PAD_SHAPE_CIRCLE", "PAD_SHAPE_RECT", "PAD_SHAPE_OVAL")
                        if hasattr(pcbnew, name)
                    },
                    "native_roundrect": _native_is_roundrect(pad, pcbnew),
                    "roundrect_radius_ratio": _native_roundrect_radius_ratio(pad, pcbnew),
                    "surface_shape_known": _native_surface_shape_known(pad, pcbnew),
                })
            except (AttributeError, TypeError, ValueError):
                pad_geometry.append({"number": pad.GetNumber(), "net": pad.GetNetname(),
                                     "known_shape": False, "surface_shape_known": False,
                                     "native_roundrect": False, "roundrect_radius_ratio": None})
        fields = [footprint.Reference(), footprint.Value(), *list(footprint.GetFields())]
        field_state = []
        seen_fields = set()
        for field in fields:
            if id(field) in seen_fields:
                continue
            seen_fields.add(id(field))
            field_state.append((field, field.GetTextPos(), field.GetTextAngleDegrees()))
        footprint.SetOrientationDegrees(0)
        if (not footprint.IsLocked() and not reference_field.IsLocked()
                and _field_visible(reference_field)):
            # The placement solver needs the horizontal silk footprint of the
            # designator, independent of its authored angle and footprint
            # rotation. Include the same margin used by the native silk
            # cleanup so this size can be used as a conservative label box.
            reference_field.SetTextAngleDegrees(0.0)
            reference_box = _box(reference_field, pcbnew, _SILK_CLEARANCE_MM)
            reference_size_mm = [
                pcbnew.ToMM(reference_box[2] - reference_box[0]),
                pcbnew.ToMM(reference_box[3] - reference_box[1]),
            ]
        for row, pad in zip(pads, footprint.Pads()):
            row["local_x"] = pcbnew.ToMM(pad.GetPosition().x - position.x)
            row["local_y"] = pcbnew.ToMM(pad.GetPosition().y - position.y)
        courtyard_layer = pcbnew.B_CrtYd if footprint.GetLayer() == pcbnew.B_Cu else pcbnew.F_CrtYd
        courtyard = footprint.GetCourtyard(courtyard_layer)
        left, top, right, bottom = _placement_envelope(footprint, courtyard)
        # Two-sided placement is permitted only when absence is explicit.
        opposite_courtyard_known, opposite_courtyard_present = _opposite_courtyard_state(
            footprint, pcbnew
        )
        native_surface_side = (
            "back" if footprint.GetLayer() == pcbnew.B_Cu
            else "front" if footprint.GetLayer() == pcbnew.F_Cu
            else None
        )
        try:
            native_footprint_is_smd = (
                int(footprint.GetAttributes()) == int(pcbnew.FP_SMD)
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            native_footprint_is_smd = None
        # Enclose geometry about the footprint origin, including off-center
        # THT connectors; a width/height alone cannot describe such parts.
        width = 2 * max(abs(left - position.x), abs(right - position.x))
        height = 2 * max(abs(top - position.y), abs(bottom - position.y))
        # Retain the legacy courtyard field names for the conservative envelope.
        # Authored courtyards can be smaller than the physical pads/body.
        # Width/height above intentionally remain origin-symmetric for older
        # consumers; off-center connectors can make those legacy values much
        # larger than the actual envelope rectangle.
        courtyard_width = right - left
        courtyard_height = bottom - top
        courtyard_center_x = (left + right) // 2 - position.x
        courtyard_center_y = (top + bottom) // 2 - position.y
        footprint.SetOrientationDegrees(rotation)
        # pcbnew may rotate footprint fields as a side effect of changing the
        # footprint angle. Inspection must not alter their authored positions.
        for field, text_position, text_angle in field_state:
            field.SetTextPos(text_position)
            field.SetTextAngleDegrees(text_angle)
        footprints.append({
            "ref": footprint.GetReference(), "footprint": str(footprint.GetFPID().GetLibItemName()),
            "footprint_id": footprint.GetFPIDAsString(), "pad_geometry": pad_geometry,
            "x": pcbnew.ToMM(position.x), "y": pcbnew.ToMM(position.y), "rotation": rotation,
            "layer": "back" if footprint.GetLayer() == pcbnew.B_Cu else "front",
            "native_surface_copper_side": native_surface_side,
            "native_surface_copper_layer_id": int(footprint.GetLayer()),
            "native_board_copper_layer_ids": sorted(copper_layer_ids),
            "native_board_copper_layer_inventory_complete": copper_layer_inventory_complete,
            "native_known_copper_layer_ids": known_copper_layer_ids,
            "native_known_copper_layer_inventory_complete": known_copper_layer_inventory_complete,
            "native_footprint_is_smd": native_footprint_is_smd,
            "locked": footprint.IsLocked(), "reference_locked": reference_field.IsLocked(),
            "width": pcbnew.ToMM(width), "height": pcbnew.ToMM(height),
            "courtyard_width": pcbnew.ToMM(courtyard_width),
            "courtyard_height": pcbnew.ToMM(courtyard_height),
            "courtyard_center_x": pcbnew.ToMM(courtyard_center_x),
            "courtyard_center_y": pcbnew.ToMM(courtyard_center_y),
            "native_opposite_courtyard_known": opposite_courtyard_known,
            "native_opposite_courtyard_present": opposite_courtyard_present,
            "reference_size_mm": reference_size_mm,
            "pads": pads,
        })
    # Export centerline geometry in millimetres for read-only route metrics.
    # Copper shapes are deliberately not converted into inferred connectivity.
    if include_route_geometry:
        for item in board.GetTracks():
            net = str(item.GetNetname())
            kind_name = type(item).__name__.upper()
            if "VIA" in kind_name:
                try:
                    layer_span = _native_via_layer_span(item, copper_stack_ids, copper_stack_complete)
                    vias.append({
                        "net": net,
                        "x_mm": pcbnew.ToMM(item.GetPosition().x),
                        "y_mm": pcbnew.ToMM(item.GetPosition().y),
                        "layer_ids": layer_span,
                        "layer_span_complete": layer_span is not None,
                        "kind": "via",
                    })
                except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
                    vias.append({"net": net, "kind": "unknown", "layer_ids": None,
                                 "layer_span_complete": False})
                continue
            kind = "arc" if "ARC" in kind_name else "segment"
            try:
                tracks.append({
                    "net": net,
                    "start_mm": [pcbnew.ToMM(item.GetStart().x), pcbnew.ToMM(item.GetStart().y)],
                    "end_mm": [pcbnew.ToMM(item.GetEnd().x), pcbnew.ToMM(item.GetEnd().y)],
                    "layer_id": int(item.GetLayer()),
                    "kind": kind,
                })
            except (AttributeError, RuntimeError, TypeError, ValueError):
                tracks.append({"net": net, "kind": "unknown"})
    box = board.GetBoardEdgesBoundingBox()
    edges = [d for d in board.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
    outline_bounds = _rectangular_outline_bounds_iu(edges, pcbnew)
    # Nonrectangular inputs retain their diagnostic box, but placement rejects
    # them through rectangular_outline. Verified rectangles use the actual
    # cut boundary independent of line width and direction.
    bounds = outline_bounds or (box.GetX(), box.GetY(), box.GetWidth(), box.GetHeight())
    result = {"footprints": footprints,
            "track_count": len(list(board.GetTracks())), "zone_count": len(list(board.Zones())),
            "copper_layer_count": len(board_copper_layers) if copper_stack_complete else None,
            "copper_layer_names": [row["name"] for row in board_copper_layers] if copper_stack_complete else None,
            "copper_layers": board_copper_layers if copper_stack_complete else None,
            "copper_layer_inventory_complete": copper_stack_complete,
            "pad_connectivity_inventory_complete": pad_connectivity_inventory_complete,
            "rectangular_outline": outline_bounds is not None,
            "bounds": dict(zip(("x", "y", "width", "height"), (pcbnew.ToMM(value) for value in bounds)))}
    if include_route_geometry:
        zone_nets = []
        zone_net_inventory_complete = True
        for zone in board.Zones():
            try:
                zone_net = zone.GetNetname()
                if not isinstance(zone_net, str) or not zone_net:
                    zone_net_inventory_complete = False
                else:
                    zone_nets.append(zone_net)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                zone_net_inventory_complete = False
        result.update(tracks=tracks, vias=vias)
        result.update(
            zone_nets=zone_nets,
            zone_net_inventory_complete=(
                zone_net_inventory_complete and len(zone_nets) == result["zone_count"]
            ),
        )
    return result


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


def _reference_class(ref):
    """Return the alphabetic designator prefix used to distinguish peers."""
    prefix = ""
    for char in ref:
        if char.isdigit():
            break
        prefix += char
    return prefix.casefold() if prefix else ref.casefold()


class _AabbIndex:
    """Deterministic uniform-grid broadphase for native integer-coordinate boxes.

    It only returns possible matches. Callers must retain their exact geometry
    predicate (`_boxes_overlap` / `_point_box_distance_sq`) as the authority.
    Items covering too many cells are kept in a small overflow list so large
    board graphics cannot explode index construction time or memory.
    """

    _MAX_CELLS_PER_ITEM = 256

    def __init__(self, boxes, cell_size):
        self.boxes = boxes
        self.cell_size = max(1, int(cell_size))
        self.cells = {}
        self.overflow = []
        for index, box in enumerate(boxes):
            keys = self._keys(box, self._MAX_CELLS_PER_ITEM)
            if keys is None:
                self.overflow.append(index)
                continue
            for key in keys:
                self.cells.setdefault(key, []).append(index)

    def _keys(self, box, limit):
        size = self.cell_size
        x0, y0 = box[0] // size, box[1] // size
        x1, y1 = box[2] // size, box[3] // size
        count = (x1 - x0 + 1) * (y1 - y0 + 1)
        if count > limit:
            return None
        return ((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))

    def query(self, box):
        keys = self._keys(box, self._MAX_CELLS_PER_ITEM)
        if keys is None:
            # Wide queries are uncommon (normal reference text is small); a
            # deterministic full scan is the safe broadphase fallback.
            return range(len(self.boxes))
        found = set(self.overflow)
        for key in keys:
            found.update(self.cells.get(key, ()))
        return sorted(found)


def _silk_indexes(obstacles, pcbnew):
    cell = pcbnew.FromMM(5.0)
    all_indexes = {}
    peer_indexes = {}
    for layer, rows in obstacles.items():
        all_indexes[layer] = _AabbIndex([row[2] for row in rows], cell)
        peers = {}
        for row in rows:
            kind, owner, box = row
            if kind == "courtyard":
                peers.setdefault(_reference_class(owner), []).append((owner, box))
        peer_indexes[layer] = {
            ref_class: (rows, _AabbIndex([box for _owner, box in rows], cell))
            for ref_class, rows in peers.items()
        }
    return all_indexes, peer_indexes


def _candidate_associated(center, own_courtyard, ref, obstacles, peer_index=None):
    own_distance = _point_box_distance_sq(center, own_courtyard)
    own_class = _reference_class(ref)
    if peer_index is None:
        candidates = ((owner, obstacle) for kind, owner, obstacle in obstacles
                      if kind == "courtyard" and _reference_class(owner) == own_class)
    else:
        rows, index = peer_index.get(own_class, ([], None))
        if index is None:
            return True
        radius = math.isqrt(own_distance) + 1
        query = (center[0] - radius, center[1] - radius,
                 center[0] + radius, center[1] + radius)
        candidates = (rows[i] for i in index.query(query))
    return not any(owner != ref and _point_box_distance_sq(center, obstacle) < own_distance
                   for owner, obstacle in candidates)


def _association_margin(center, own_courtyard, ref, obstacles, peer_index=None):
    """Return the distance advantage over the nearest same-class courtyard."""
    own_distance = math.sqrt(_point_box_distance_sq(center, own_courtyard))
    if peer_index is None:
        peers = ((owner, box) for kind, owner, box in obstacles
                 if kind == "courtyard" and _reference_class(owner) == _reference_class(ref))
    else:
        rows, _index = peer_index.get(_reference_class(ref), ([], None))
        peers = iter(rows)
    peer_distances = [math.sqrt(_point_box_distance_sq(center, box))
                      for owner, box in peers if owner != ref]
    return min(peer_distances, default=math.inf) - own_distance


def _choose_associated_candidate(candidates, own_courtyard, ref, obstacles,
                                 target_margin, max_extra_distance, peer_index=None):
    """Prefer a clearer local association while retaining hard association."""
    if not candidates:
        return None
    baseline_distance = math.sqrt(_point_box_distance_sq(candidates[0], own_courtyard))
    max_distance = baseline_distance + max_extra_distance
    eligible = []
    for order, center in enumerate(candidates):
        distance = math.sqrt(_point_box_distance_sq(center, own_courtyard))
        if distance > max_distance or not _candidate_associated(
            center, own_courtyard, ref, obstacles, peer_index
        ):
            continue
        margin_shortfall = max(0.0, target_margin - _association_margin(
            center, own_courtyard, ref, obstacles, peer_index
        ))
        # Cap the soft cost and the search radius so a clearer label stays local.
        score = distance + min(max_extra_distance, margin_shortfall)
        eligible.append((score, order, center))
    return min(eligible)[2] if eligible else candidates[0]


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
    obstacle_indexes, peer_indexes = _silk_indexes(obstacles, pcbnew)
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
            rows = obstacles[entry["layer"]]
            if any(_boxes_overlap(candidate_box, rows[i][2])
                   for i in obstacle_indexes[entry["layer"]].query(candidate_box)):
                continue
            if not _candidate_associated(center, own_courtyard, entry["ref"], rows,
                                         peer_indexes[entry["layer"]]):
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
    occupied_indexes = {layer: _AabbIndex([], pcbnew.FromMM(5.0))
                        for layer in (pcbnew.F_SilkS, pcbnew.B_SilkS)}

    def reserve_reference(layer, box):
        occupied_refs[layer].append(box)
        occupied_indexes[layer] = _AabbIndex(occupied_refs[layer], pcbnew.FromMM(5.0))

    for entry in ordered:
        if entry["locked"]:
            reserve_reference(entry["layer"], entry["box"])
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
        obstacle_index = obstacle_indexes[entry["layer"]]
        peer_index = peer_indexes[entry["layer"]]
        chosen = None
        for center in _candidate_centers(courtyard, width, height, bounds, pcbnew):
            dx, dy = center[0] - initial_center[0], center[1] - initial_center[1]
            candidate_box = (current_box[0] + dx, current_box[1] + dy,
                             current_box[2] + dx, current_box[3] + dy)
            if not _inside_bounds(candidate_box, bounds):
                continue
            if any(_boxes_overlap(candidate_box, fixed_obstacles[i][2])
                   for i in obstacle_index.query(candidate_box)):
                continue
            if any(_boxes_overlap(candidate_box, occupied_refs[entry["layer"]][i])
                   for i in occupied_indexes[entry["layer"]].query(candidate_box)):
                continue
            # A clear label can still read as belonging to the wrong part when
            # its center is nearer a neighbor's courtyard than its own. Keep
            # searching for a local position that visually associates it with
            # its footprint whenever one is available.
            if not _candidate_associated(center, courtyard, entry["ref"], fixed_obstacles, peer_index):
                continue
            chosen = (center, (center[0] + origin_offset[0], center[1] + origin_offset[1]))
            break
        if chosen is not None:
            # The first clear slot remains the fallback. Inspect only a bounded
            # neighborhood around it for a slot with a more readable peer margin.
            text_height = max(1, height)
            max_extra = int(round(0.75 * text_height))
            baseline_center = chosen[0]
            baseline_distance = math.sqrt(_point_box_distance_sq(baseline_center, courtyard))
            step = pcbnew.FromMM(_SILK_SEARCH_STEP_MM)
            local_ring = int(math.ceil((baseline_distance + max_extra) / step)) + 2
            valid_centers = [baseline_center]
            seen_centers = {baseline_center}
            max_local_distance = baseline_distance + max_extra
            for center in _candidate_centers(
                courtyard, width, height, bounds, pcbnew, max_ring=local_ring
            ):
                if center in seen_centers:
                    continue
                seen_centers.add(center)
                if math.sqrt(_point_box_distance_sq(center, courtyard)) > max_local_distance:
                    continue
                dx, dy = center[0] - initial_center[0], center[1] - initial_center[1]
                candidate_box = (current_box[0] + dx, current_box[1] + dy,
                                 current_box[2] + dx, current_box[3] + dy)
                if not _inside_bounds(candidate_box, bounds):
                    continue
                if any(_boxes_overlap(candidate_box, fixed_obstacles[i][2])
                       for i in obstacle_index.query(candidate_box)):
                    continue
                if any(_boxes_overlap(candidate_box, occupied_refs[entry["layer"]][i])
                       for i in occupied_indexes[entry["layer"]].query(candidate_box)):
                    continue
                if not _candidate_associated(center, courtyard, entry["ref"], fixed_obstacles, peer_index):
                    continue
                valid_centers.append(center)
            preferred = _choose_associated_candidate(
                valid_centers, courtyard, entry["ref"], fixed_obstacles,
                target_margin=text_height, max_extra_distance=max_extra,
                peer_index=peer_index,
            )
            if preferred != baseline_center:
                chosen = (preferred, (preferred[0] + origin_offset[0],
                                      preferred[1] + origin_offset[1]))
        if chosen is None:
            reserve_reference(entry["layer"], current_box)
            continue
        center, new_origin = chosen
        if center != initial_center:
            field.SetTextPos(pcbnew.VECTOR2I(new_origin[0], new_origin[1]))
            moved += 1
            delta = (center[0] - initial_center[0], center[1] - initial_center[1])
            entry["box"] = (current_box[0] + delta[0], current_box[1] + delta[1],
                            current_box[2] + delta[0], current_box[3] + delta[1])
        reserve_reference(entry["layer"], entry["box"])
    return {"requested": True, "moved_count": moved, "orientation_normalized_count": normalized}


def _reference_conflict_report(board, pcbnew, moved_count, orientation_normalized_count):
    obstacles = _silk_obstacles(board, pcbnew)
    obstacle_indexes, peer_indexes = _silk_indexes(obstacles, pcbnew)
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
        rows = obstacles[entry["layer"]]
        for index in obstacle_indexes[entry["layer"]].query(box):
            kind, _owner, obstacle = rows[index]
            if _boxes_overlap(box, obstacle):
                conflict_counts[kind] += 1
                conflicted_refs.add(entry["ref"])
        center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        # Reference fields can be authored on either silk layer regardless
        # of footprint side. The footprint's physical courtyard is the
        # correct association anchor even when this field lives on opposite
        # side silk and therefore has no courtyard obstacle on its layer.
        own_courtyard = _courtyard_box(entry["footprint"], pcbnew)
        if not _candidate_associated(center, own_courtyard, entry["ref"], rows,
                                     peer_indexes[entry["layer"]]):
            conflict_counts["association"] += 1
            conflicted_refs.add(entry["ref"])
    by_layer = {pcbnew.F_SilkS: [], pcbnew.B_SilkS: []}
    for entry in entries:
        by_layer[entry["layer"]].append(entry)
    for layer_entries in by_layer.values():
        index = _AabbIndex([entry["box"] for entry in layer_entries], pcbnew.FromMM(5.0))
        for first_index, first in enumerate(layer_entries):
            for second_index in index.query(first["box"]):
                if second_index <= first_index:
                    continue
                second = layer_entries[second_index]
                if _boxes_overlap(first["box"], second["box"]):
                    conflict_counts["reference_reference"] += 1
                    conflicted_refs.update((first["ref"], second["ref"]))
    return {"requested": True, "moved_count": moved_count,
            "orientation_normalized_count": orientation_normalized_count,
            "unresolved_count": len(conflicted_refs),
            "unresolved_references": sorted(conflicted_refs),
            "remaining_conflict_count": sum(conflict_counts.values()),
            "conflict_counts": conflict_counts}


def _reference_conflict_signature(board, pcbnew):
    """Return exact native label-conflict identities for no-new-conflict checks."""
    signature = Counter()
    obstacles = _silk_obstacles(board, pcbnew)
    obstacle_indexes, peer_indexes = _silk_indexes(obstacles, pcbnew)
    entries = _reference_entries(board, pcbnew)
    bounds = _board_text_bounds(board, pcbnew)
    by_layer = {pcbnew.F_SilkS: [], pcbnew.B_SilkS: []}
    for entry in entries:
        ref, layer, box = entry["ref"], entry["layer"], entry["box"]
        if not _inside_bounds(box, bounds):
            signature[("edge", ref)] += 1
        rows = obstacles[layer]
        for index in obstacle_indexes[layer].query(box):
            kind, owner, obstacle = rows[index]
            if _boxes_overlap(box, obstacle):
                signature[(kind, ref, owner, tuple(obstacle))] += 1
        center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        courtyard = _courtyard_box(entry["footprint"], pcbnew)
        if not _candidate_associated(center, courtyard, ref, rows, peer_indexes[layer]):
            signature[("association", ref)] += 1
        by_layer[layer].append(entry)
    for layer_entries in by_layer.values():
        index = _AabbIndex([entry["box"] for entry in layer_entries], pcbnew.FromMM(5.0))
        for first_index, first in enumerate(layer_entries):
            for second_index in index.query(first["box"]):
                if second_index <= first_index:
                    continue
                second = layer_entries[second_index]
                if _boxes_overlap(first["box"], second["box"]):
                    refs = tuple(sorted((first["ref"], second["ref"])))
                    signature[("reference_reference", *refs)] += 1
    return signature


def _reference_repair_preservation_state(board, pcbnew, movable_refs=()):
    """Capture all footprint/pad geometry and non-position reference properties."""
    footprints = {}
    text = {}
    for footprint in board.GetFootprints():
        ref = footprint.GetReference()
        pads = []
        for pad in footprint.Pads():
            position = pad.GetPosition()
            layers = pad.GetLayerSet()
            layer_state = layers.FmtBin() if hasattr(layers, "FmtBin") else str(layers)
            pads.append((pad.GetNumber(), pad.GetNetname(), position.x, position.y,
                         int(pad.GetOrientationDegrees()), layer_state, pad.IsLocked()))
        position = footprint.GetPosition()
        uuid = footprint.m_Uuid.AsString() if hasattr(footprint, "m_Uuid") else None
        footprints[ref] = (uuid, position.x, position.y, float(footprint.GetOrientationDegrees()),
                           int(footprint.GetLayer()), bool(footprint.IsLocked()), tuple(sorted(pads)))
        for field in footprint.GetFields():
            field_position = field.GetTextPos()
            size = field.GetTextSize()
            thickness = field.GetTextThickness()
            key = (ref, field.GetTextTypeDescription(), field.GetText())
            uuid = field.m_Uuid.AsString() if hasattr(field, "m_Uuid") else None
            is_reference = field.GetTextTypeDescription() == "Reference"
            text[key] = (
                None if is_reference and ref in movable_refs else (field_position.x, field_position.y),
                float(field.GetTextAngleDegrees()), size.x, size.y, thickness,
                int(field.GetLayer()), _field_visible(field), bool(field.IsLocked()), uuid,
            )
    return footprints, text, _copper_state(board)


def _bounded_reference_field_repair(board, pcbnew, baseline_report):
    """Try a bounded native-clear reassignment of only conflicting labels.

    The routine uses the ordinary tidy algorithm's candidate generator,
    association rule, obstacle inventory, and complete conflict reporter.
    It fails closed on locked fields, oversized neighborhoods, empty domains,
    search limits, new conflicts, or any changed footprint/pad/copper state.
    """
    from _native_reference_repair import apply_candidate_positions, solve_bounded_assignment

    max_group_size = 8
    max_ring = 24
    max_move_mm = 8.0
    max_options = 96
    max_center_evaluations = 25000
    max_search_nodes = 50000
    active_refs = set(baseline_report.get("unresolved_references", []))
    if not active_refs:
        return {"status": "not_needed", "reason": "no_native_conflict_references"}
    entries = {entry["ref"]: entry for entry in _reference_entries(board, pcbnew)}
    if active_refs - entries.keys():
        return {"status": "not_applied", "reason": "conflicted_reference_field_missing"}

    # Partition only actual overlapping labels into neighborhoods. A label
    # blocked by a static obstacle forms a singleton neighborhood.
    graph = {ref: set() for ref in active_refs}
    for ref in sorted(active_refs):
        first = entries[ref]
        for other in sorted(active_refs):
            if other <= ref:
                continue
            second = entries[other]
            if first["layer"] == second["layer"] and _boxes_overlap(first["box"], second["box"]):
                graph[ref].add(other)
                graph[other].add(ref)
    groups = []
    pending = set(active_refs)
    while pending:
        seed = min(pending)
        component = set()
        stack = [seed]
        while stack:
            ref = stack.pop()
            if ref in component:
                continue
            component.add(ref)
            stack.extend(sorted(graph[ref] - component, reverse=True))
        pending -= component
        groups.append(sorted(component))
    if any(len(group) > max_group_size for group in groups):
        return {"status": "not_applied", "reason": "conflict_neighborhood_too_large",
                "group_sizes": [len(group) for group in groups], "max_group_size": max_group_size}

    original_positions = {
        ref: (entry["field"].GetTextPos().x, entry["field"].GetTextPos().y)
        for ref, entry in entries.items()
    }
    identity_before = _identity(board)
    baseline_signature = _reference_conflict_signature(board, pcbnew)
    static_obstacles = _silk_obstacles(board, pcbnew)
    obstacle_indexes, peer_indexes = _silk_indexes(static_obstacles, pcbnew)
    bounds = _board_text_bounds(board, pcbnew)
    move_limit = pcbnew.FromMM(max_move_mm)
    candidate_evaluations = 0
    search_nodes = 0
    option_counts = {}
    chosen_all = {}
    # Virtual reservations let later groups see earlier assignments while no
    # field mutates until every conflict group has a bounded solution.
    virtual_boxes = {ref: entry["box"] for ref, entry in entries.items()}

    for group in groups:
        if any(entries[ref]["locked"] for ref in group):
            return {"status": "not_applied", "reason": "conflict_neighborhood_contains_locked_reference",
                    "groups": groups, "candidate_center_evaluations": candidate_evaluations,
                    "search_nodes": search_nodes}
        fixed_by_layer = {pcbnew.F_SilkS: [], pcbnew.B_SilkS: []}
        for ref, entry in entries.items():
            if ref not in group:
                fixed_by_layer[entry["layer"]].append(virtual_boxes[ref])
        options_by_ref = {}
        group_entries = {ref: entries[ref] for ref in group}
        for ref in group:
            entry = entries[ref]
            field, current, layer = entry["field"], virtual_boxes[ref], entry["layer"]
            courtyard = _courtyard_box(entry["footprint"], pcbnew)
            width = current[2] - current[0] - 2 * pcbnew.FromMM(_SILK_CLEARANCE_MM)
            height = current[3] - current[1] - 2 * pcbnew.FromMM(_SILK_CLEARANCE_MM)
            initial_center = ((current[0] + current[2]) // 2, (current[1] + current[3]) // 2)
            position = field.GetTextPos()
            origin_offset = (position.x - initial_center[0], position.y - initial_center[1])
            centers = set(_candidate_centers(
                courtyard, width, height, bounds, pcbnew, max_ring=max_ring,
            ))
            centers.add(initial_center)
            viable = []
            for center in centers:
                if candidate_evaluations >= max_center_evaluations:
                    return {"status": "budget_exhausted", "reason": "candidate_center_budget_exhausted",
                            "groups": groups, "candidate_center_evaluations": candidate_evaluations,
                            "candidate_center_budget": max_center_evaluations,
                            "search_nodes": search_nodes, "search_node_budget": max_search_nodes}
                candidate_evaluations += 1
                dx, dy = center[0] - initial_center[0], center[1] - initial_center[1]
                if dx * dx + dy * dy > move_limit * move_limit:
                    continue
                box = (current[0] + dx, current[1] + dy, current[2] + dx, current[3] + dy)
                if not _inside_bounds(box, bounds):
                    continue
                rows = static_obstacles[layer]
                if any(_boxes_overlap(box, rows[index][2])
                       for index in obstacle_indexes[layer].query(box)):
                    continue
                if any(_boxes_overlap(box, fixed_box)
                       for fixed_box in fixed_by_layer[layer]):
                    continue
                if not _candidate_associated(center, courtyard, ref, rows, peer_indexes[layer]):
                    continue
                viable.append({
                    "center": center,
                    "box": box,
                    "pos": (center[0] + origin_offset[0], center[1] + origin_offset[1]),
                    "move_sq": dx * dx + dy * dy,
                    "own_distance_sq": _point_box_distance_sq(center, courtyard),
                })
            viable.sort(key=lambda row: (row["move_sq"], row["own_distance_sq"],
                                         row["center"][1], row["center"][0]))
            options_by_ref[ref] = viable[:max_options]
        option_counts.update({ref: len(rows) for ref, rows in options_by_ref.items()})
        if any(not rows for rows in options_by_ref.values()):
            return {"status": "not_applied", "reason": "empty_candidate_domain",
                    "groups": groups, "candidate_options_by_ref": option_counts,
                    "candidate_center_evaluations": candidate_evaluations,
                    "search_nodes": search_nodes}
        result = solve_bounded_assignment(
            options_by_ref,
            layer_by_ref={ref: group_entries[ref]["layer"] for ref in group},
            fixed_boxes_by_layer=fixed_by_layer,
            max_nodes=max_search_nodes - search_nodes,
        )
        search_nodes += result["search_nodes"]
        if result["status"] != "solved":
            return {"status": result["status"], "reason": "bounded_assignment_failed",
                    "groups": groups, "candidate_options_by_ref": option_counts,
                    "candidate_center_evaluations": candidate_evaluations,
                    "candidate_center_budget": max_center_evaluations,
                    "search_nodes": search_nodes, "search_node_budget": max_search_nodes}
        for ref, candidate in result["assignment"].items():
            chosen_all[ref] = candidate
            virtual_boxes[ref] = candidate["box"]

    fields_by_ref = {ref: entry["field"] for ref, entry in entries.items()}
    preservation_before = _reference_repair_preservation_state(board, pcbnew, chosen_all)
    candidate_report = None

    def validate_candidate():
        nonlocal candidate_report
        candidate_report = _reference_conflict_report(board, pcbnew, 0, 0)
        candidate_signature = _reference_conflict_signature(board, pcbnew)
        new_conflicts = candidate_signature - baseline_signature
        improved = candidate_report["remaining_conflict_count"] < baseline_report["remaining_conflict_count"]
        no_new_conflicts = not new_conflicts
        preserved = (
            _identity(board) == identity_before
            and _reference_repair_preservation_state(board, pcbnew, chosen_all) == preservation_before
        )
        return improved and no_new_conflicts and preserved

    applied = apply_candidate_positions(
        fields_by_ref, chosen_all, lambda x, y: pcbnew.VECTOR2I(x, y), validate_candidate,
    )
    if not applied["applied"]:
        return {"status": "rolled_back", "reason": applied["reason"], "groups": groups,
                "candidate_options_by_ref": option_counts,
                "candidate_center_evaluations": candidate_evaluations,
                "candidate_center_budget": max_center_evaluations,
                "search_nodes": search_nodes, "search_node_budget": max_search_nodes,
                "before_conflict_count": baseline_report["remaining_conflict_count"],
                "candidate_conflict_count": (candidate_report or {}).get("remaining_conflict_count")}
    moved_refs = sorted(ref for ref, candidate in chosen_all.items()
                        if tuple(candidate["pos"]) != original_positions[ref])
    return {
        "status": "accepted",
        "reason": applied["reason"],
        "groups": groups,
        "moved_refs": moved_refs,
        "candidate_options_by_ref": option_counts,
        "candidate_center_evaluations": candidate_evaluations,
        "candidate_center_budget": max_center_evaluations,
        "search_nodes": search_nodes,
        "search_node_budget": max_search_nodes,
        "max_group_size": max_group_size,
        "candidate_ring_cap": max_ring,
        "max_move_mm": max_move_mm,
        "max_candidates_per_reference": max_options,
        "before_conflict_count": baseline_report["remaining_conflict_count"],
        "after_conflict_count": candidate_report["remaining_conflict_count"],
        "before_conflict_categories": dict(baseline_report["conflict_counts"]),
        "after_conflict_categories": dict(candidate_report["conflict_counts"]),
        "no_new_native_conflicts": True,
        "pad_net_identity_preserved": True,
        "footprints_and_pad_geometry_preserved": True,
        "nonreference_text_fields_preserved": True,
        "copper_preserved": True,
    }


def execute(request):
    import pcbnew

    board = pcbnew.LoadBoard(request["board"])
    if board is None:
        raise ValueError("KiCad could not load the board")
    identity = _identity(board)
    reference_positions_before_tidy = None
    operation = request["operation"]
    tidy_summary = None
    label_repair = None
    refinement = None
    cleanup = None
    plane_cleanup = None
    ground_plane = None
    cleanup_removed_items = []
    plane_cleanup_removed_items = []
    original_copper = None
    original_layout = None
    original_board_geometry = None
    zone_refill = None
    if operation == "inspect":
        return {"status": "ok", **_inspect(board, pcbnew,
                                             include_route_geometry=bool(request.get("include_route_geometry")))}
    if operation == "export_dsn":
        if not pcbnew.ExportSpecctraDSN(board, request["output"]):
            raise ValueError("KiCad Specctra export failed; check the board outline and netlist")
        return {"status": "ok", "version": pcbnew.Version()}
    if operation == "import_ses":
        placements = {f.GetReference(): (f.GetPosition().x, f.GetPosition().y, f.GetOrientationDegrees(), f.GetLayer())
                      for f in board.GetFootprints()}
        original_zone_definitions = _zone_definition_state(board, pcbnew) if list(board.Zones()) else None
        original_copper_stack = _native_stack_state(board, pcbnew) if original_zone_definitions else None
        if not pcbnew.ImportSpecctraSES(board, request["session"]):
            raise ValueError("KiCad could not import the Specctra session")
        for f in board.GetFootprints():
            if placements[f.GetReference()] != (f.GetPosition().x, f.GetPosition().y,
                                                f.GetOrientationDegrees(), f.GetLayer()):
                raise ValueError(f"Session unexpectedly changed footprint {f.GetReference()}: "
                                 f"{placements[f.GetReference()]} -> "
                                 f"{(f.GetPosition().x, f.GetPosition().y, f.GetOrientationDegrees(), f.GetLayer())}")
        if original_zone_definitions is not None:
            zone_refill = _refill_imported_zones(
                board, pcbnew, original_zone_definitions, original_copper_stack,
            )
    elif operation == "refine_returns":
        from _native_return_paths import refine

        original_copper = _copper_state(board)
        original_layout = _fixed_layout_state(board)
        refinement = refine(board, pcbnew, request.get("connections"))
        if original_copper - _copper_state(board) or original_layout != _fixed_layout_state(board):
            raise ValueError("Return refinement changed original copper, footprints or text")
    elif operation == "cleanup_copper":
        from _native_copper_cleanup import cleanup as cleanup_selected_copper

        cleanup_nets = request.get("nets")
        cleanup_connections = request.get("connections")
        if (not isinstance(cleanup_nets, list) or not cleanup_nets
                or any(not isinstance(net, str) or not net for net in cleanup_nets)
                or len(cleanup_nets) != len(set(cleanup_nets))):
            raise ValueError("Copper cleanup requires a nonempty unique list of net names")
        if not isinstance(cleanup_connections, list) or not cleanup_connections:
            raise ValueError("Copper cleanup requires protected connections")
        original_copper = _copper_state(board)
        original_layout = _fixed_layout_state(board)
        cleanup = cleanup_selected_copper(board, pcbnew, cleanup_nets, cleanup_connections)
        cleanup_removed_items = cleanup.pop("_native_removed_items", [])
        if cleanup.get("status") == "refused":
            raise ValueError(cleanup.get("reason") or "Native copper cleanup refused this board")
        if _identity(board) != identity or original_layout != _fixed_layout_state(board):
            raise ValueError("Copper cleanup changed pad/net identity, footprints or text")
        current_copper = _copper_state(board)
        cleanup["unselected_copper_preserved"] = (
            _copper_state_for_nets(original_copper, cleanup_nets, include_selected=False)
            == _copper_state_for_nets(current_copper, cleanup_nets, include_selected=False)
        )
        cleanup["locked_copper_preserved"] = (
            _copper_state_for_nets(original_copper, set(), include_selected=False, locked=True)
            == _copper_state_for_nets(current_copper, set(), include_selected=False, locked=True)
        )
        cleanup["copper_changes_limited_to_selected_nets"] = cleanup["unselected_copper_preserved"]
        if not cleanup["unselected_copper_preserved"] or not cleanup["locked_copper_preserved"]:
            raise ValueError("Copper cleanup modified unselected or locked copper")
    elif operation == "add_ground_plane":
        from _native_ground_plane import apply as apply_ground_plane

        plan = request.get("plan")
        if not isinstance(plan, dict):
            raise ValueError("Ground-plane operation requires a validated plan")
        original_copper = _copper_state(board)
        original_layout = _fixed_layout_state(board)
        original_board_geometry = _ground_plane_source_geometry_state(board, pcbnew)
        ground_plane = apply_ground_plane(
            board, pcbnew, net=plan["net"], layers=tuple(plan["layers"]),
            options={key: plan[key] for key in (
                "edge_clearance_mm", "clearance_mm", "min_thickness_mm", "thermal_gap_mm",
                "thermal_spoke_width_mm",
            )},
            protected_connections=plan["protected_connections"],
            pre_route=request.get("pre_route", False),
        )
        if not isinstance(ground_plane, dict) or ground_plane.get("status") != "ok":
            raise ValueError("Native ground-plane operation did not return a successful report")
        if _identity(board) != identity or original_layout != _fixed_layout_state(board):
            raise ValueError("Ground-plane operation changed pad/net identity, footprints or text")
        if original_board_geometry != _ground_plane_source_geometry_state(board, pcbnew):
            raise ValueError("Ground-plane operation changed source footprint, pad, model, drawing or outline geometry")
        if original_copper != _copper_state(board):
            raise ValueError("Ground-plane operation removed or changed existing tracks or vias")
        ground_plane["tracks_preserved"] = True
        ground_plane["original_footprints_and_text_preserved"] = True
        ground_plane["original_board_geometry_preserved"] = True
    elif operation == "cleanup_plane_traces":
        from _native_plane_cleanup import apply as cleanup_covered_plane_traces

        plan = request.get("plan")
        if (not isinstance(plan, dict) or not isinstance(plan.get("net"), str) or not plan["net"]
                or not isinstance(plan.get("protected_connections"), list)):
            raise ValueError("Plane-cleanup operation requires a validated plan")
        original_copper = _copper_state(board)
        original_layout = _fixed_layout_state(board)
        original_board_geometry = _ground_plane_source_geometry_state(board, pcbnew)
        plane_cleanup = cleanup_covered_plane_traces(
            board, pcbnew, net=plan["net"], protected_connections=plan["protected_connections"],
        )
        if not isinstance(plane_cleanup, dict) or plane_cleanup.get("status") != "ok":
            raise ValueError("Native plane-cleanup operation did not verify a valid result")
        plane_cleanup_removed_items = plane_cleanup.pop("_native_removed_items", [])
        if (_identity(board) != identity or original_layout != _fixed_layout_state(board)
                or original_board_geometry != _ground_plane_source_geometry_state(board, pcbnew)):
            raise ValueError(
                "Plane cleanup changed pad/net identity, layout, footprint, pad, model, drawing or outline geometry"
            )
        before_copper, after_copper = original_copper, _copper_state(board)
        added = after_copper - before_copper
        removed = before_copper - after_copper
        if added:
            raise ValueError("Plane cleanup added or mutated native copper")
        removed_rows = [json.loads(raw) for raw, count in removed.items() for _ in range(count)]
        if (any(row["type"] != "PCB_TRACK" or row["net"] != plan["net"] or row["locked"]
                for row in removed_rows)
                or len(removed_rows) != plane_cleanup.get("removed_count")):
            raise ValueError("Plane cleanup removed a via, locked trace, or nonselected copper item")
        reported = plane_cleanup.get("removed_tracks")
        if not isinstance(reported, list) or len(reported) != len(removed_rows):
            raise ValueError("Plane-cleanup report does not identify every removed native trace")
        actual_by_uuid = {row["uuid"]: row for row in removed_rows}
        if len(actual_by_uuid) != len(removed_rows):
            raise ValueError("Native trace removal did not produce unique UUIDs")
        for report_row in reported:
            actual = actual_by_uuid.get(report_row.get("uuid")) if isinstance(report_row, dict) else None
            geometry = report_row.get("geometry") if isinstance(report_row, dict) else None
            if (actual is None or report_row.get("net") != actual["net"] or report_row.get("locked") is not False
                    or geometry != {"start": actual["start"], "end": actual["end"],
                                    "layer": actual["layer"], "width": actual["width"]}):
                raise ValueError("Plane-cleanup report removal geometry does not match the native copper diff")
        if {row["uuid"] for row in reported} != set(actual_by_uuid):
            raise ValueError("Plane-cleanup report UUIDs do not match the native copper diff")
        plane_cleanup["existing_filled_planes_verified"] = True
        plane_cleanup["only_covered_unlocked_traces_removed"] = True
        plane_cleanup["no_copper_added"] = True
        plane_cleanup["vias_preserved"] = True
        plane_cleanup["nonselected_copper_preserved"] = True
        plane_cleanup["locked_copper_preserved"] = True
        plane_cleanup["original_geometry_preserved"] = True
        plane_cleanup["original_board_geometry_preserved"] = True
    elif operation == "repair_open":
        from _native_routing_repair import repair_one

        original_copper = _copper_state(board)
        original_layout = _fixed_layout_state(board)
        original_board_geometry = _ground_plane_source_geometry_state(board, pcbnew)
        repair = repair_one(
            board, pcbnew, request.get("drc_report"),
            pair_index=request.get("pair_index", 0),
        )
        if (_identity(board) != identity or original_layout != _fixed_layout_state(board)
                or original_board_geometry != _ground_plane_source_geometry_state(board, pcbnew)):
            raise ValueError("Same-net repair changed pad/net identity, layout or original board geometry")
        added = _copper_state(board) - original_copper
        removed = original_copper - _copper_state(board)
        added_rows = [json.loads(raw) for raw, count in added.items() for _ in range(count)]
        if (removed or not added_rows or any(row["net"] != repair["net"] or row["locked"]
                or row["type"] not in {"PCB_TRACK", "PCB_VIA"} for row in added_rows)
                or sum(row["type"] == "PCB_TRACK" for row in added_rows) != repair["added_tracks"]
                or sum(row["type"] == "PCB_VIA" for row in added_rows) != repair["added_vias"]):
            raise ValueError("Same-net repair modified existing copper or reported invalid additions")
        repair["source_identity_preserved"] = True
        repair["original_footprints_and_text_preserved"] = True
        repair["original_board_geometry_preserved"] = True
        repair["existing_copper_preserved"] = True
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
            reference_positions_before_tidy = {
                footprint.GetReference(): (footprint.Reference().GetTextPos().x,
                                           footprint.Reference().GetTextPos().y)
                for footprint in board.GetFootprints()
            }
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
            tidy_report = _reference_conflict_report(
                board, pcbnew, tidy_summary["moved_count"],
                tidy_summary["orientation_normalized_count"],
            )
            if tidy_report["remaining_conflict_count"]:
                label_repair = _bounded_reference_field_repair(board, pcbnew, tidy_report)
    else:
        raise ValueError(f"Unknown KiCad operation: {operation}")
    if _identity(board) != identity:
        raise ValueError("Operation changed pad/net identity")
    if not pcbnew.SaveBoard(request["output"], board):
        raise ValueError("KiCad could not save the board")
    reloaded = pcbnew.LoadBoard(request["output"])
    if reloaded is None or _identity(reloaded) != identity:
        raise ValueError("Saved board failed pad/net identity verification")
    if zone_refill is not None:
        zone_refill = _verify_saved_imported_zones(reloaded, pcbnew, zone_refill)
    if refinement is not None and (original_copper - _copper_state(reloaded)
                                   or original_layout != _fixed_layout_state(reloaded)):
        raise ValueError("Saved return refinement failed original geometry preservation")
    if cleanup is not None:
        from _native_copper_cleanup import verify as verify_cleanup_after_reload

        saved_copper = _copper_state(board)
        if saved_copper != _copper_state(reloaded):
            raise ValueError("Saved cleanup changed native copper during board reload")
        if (_identity(reloaded) != identity
                or original_layout != _fixed_layout_state(reloaded)):
            raise ValueError("Saved cleanup failed pad/net, footprint or text preservation")
        if (_copper_state_for_nets(original_copper, request["nets"], include_selected=False)
                != _copper_state_for_nets(_copper_state(reloaded), request["nets"], include_selected=False)):
            cleanup["unselected_copper_preserved"] = False
            raise ValueError("Saved cleanup changed unselected copper")
        reloaded_copper = _copper_state(reloaded)
        if (_copper_state_for_nets(original_copper, set(), include_selected=False, locked=True)
                != _copper_state_for_nets(reloaded_copper, set(), include_selected=False, locked=True)):
            cleanup["locked_copper_preserved"] = False
            raise ValueError("Saved cleanup changed locked copper")
        cleanup["original_footprints_and_text_preserved"] = True
        cleanup["copper_changes_limited_to_selected_nets"] = cleanup["unselected_copper_preserved"]
        verification = verify_cleanup_after_reload(
            reloaded, pcbnew, request["connections"], cleanup.get("protected_path_limits_mm", {}),
            nets=request["nets"],
        )
        original_rows = cleanup.get("protected_connections", [])
        verified_rows = verification.get("protected_connections", [])
        if not isinstance(original_rows, list) or len(original_rows) != len(verified_rows):
            raise ValueError("Saved cleanup report does not cover every protected connection")
        for original_row, verified_row in zip(original_rows, verified_rows):
            verified_row["baseline_length_mm"] = original_row.get("baseline_length_mm")
            verified_row["limit_mm"] = original_row.get("limit_mm")
        verification["protected_paths_preserved"] = all(
            row.get("protected_path_preserved") is True for row in verified_rows
        )
        cleanup.update(verification)
        cleanup["locked_copper_preserved"] = True
        cleanup["unselected_copper_preserved"] = True
        cleanup["original_footprints_and_text_preserved"] = True
        if not cleanup.get("all_pads_connected") or not cleanup.get("protected_paths_preserved"):
            raise ValueError("Saved cleanup failed native pad connectivity or protected-path verification")
    if operation == "repair_open":
        saved_copper = _copper_state(reloaded)
        if (original_copper - saved_copper or _identity(reloaded) != identity
                or original_layout != _fixed_layout_state(reloaded)
                or original_board_geometry != _ground_plane_source_geometry_state(reloaded, pcbnew)):
            raise ValueError("Saved repair changed original copper, source geometry or pad/net identity")
        delta = saved_copper - original_copper
        delta_rows = [json.loads(raw) for raw, count in delta.items() for _ in range(count)]
        if (len(delta_rows) != repair["added_tracks"] + repair["added_vias"]
                or any(row["net"] != repair["net"] or row["locked"]
                       or row["type"] not in {"PCB_TRACK", "PCB_VIA"} for row in delta_rows)):
            raise ValueError("Saved repair additions do not match the candidate report")
        repair.update({"source_identity_preserved": True,
                       "original_footprints_and_text_preserved": True,
                       "original_board_geometry_preserved": True,
                       "existing_copper_preserved": True})
    result = {"status": "ok", "version": pcbnew.Version(), **_inspect(reloaded, pcbnew)}
    if zone_refill is not None:
        result["zone_refill"] = zone_refill
    if tidy_summary is not None:
        result["silkscreen"] = _reference_conflict_report(
            reloaded, pcbnew, tidy_summary["moved_count"],
            tidy_summary["orientation_normalized_count"],
        )
        if label_repair is not None:
            final_positions = {
                footprint.GetReference(): (footprint.Reference().GetTextPos().x,
                                           footprint.Reference().GetTextPos().y)
                for footprint in reloaded.GetFootprints()
            }
            label_repair["final_moved_refs"] = sorted(
                ref for ref, position in final_positions.items()
                if reference_positions_before_tidy.get(ref) != position
            )
            result["silkscreen"]["moved_count"] = len(label_repair["final_moved_refs"])
            result["silkscreen"]["local_repair"] = label_repair
    if refinement is not None:
        result["refinement"] = {**refinement, "original_copper_preserved": True,
                                "original_footprints_and_text_preserved": True}
        result["silkscreen"] = _reference_conflict_report(reloaded, pcbnew, 0, 0)
    if cleanup is not None:
        cleanup["original_footprints_and_text_preserved"] = True
        result["cleanup"] = cleanup
        result["silkscreen"] = _reference_conflict_report(reloaded, pcbnew, 0, 0)
        # Keep SWIG wrappers alive through save, reload, and native inspection.
        # The list is local only and never enters the JSON response.
        cleanup_removed_items.clear()
    if operation == "repair_open":
        result["route_repair"] = repair
    if ground_plane is not None:
        from _native_ground_plane import verify as verify_ground_plane_after_reload

        if (_copper_state(board) != _copper_state(reloaded)
                or original_layout != _fixed_layout_state(reloaded)
                or _identity(reloaded) != identity):
            raise ValueError("Saved ground-plane board changed tracks, vias, footprints, text or pad/net identity")
        if original_board_geometry != _ground_plane_source_geometry_state(reloaded, pcbnew):
            raise ValueError(
                "Saved ground-plane board changed source footprint, pad, model, drawing or outline geometry"
            )
        verification = verify_ground_plane_after_reload(
            reloaded, pcbnew, ground_plane, net=request["plan"]["net"],
            layers=tuple(request["plan"]["layers"]),
            protected_connections=request["plan"]["protected_connections"],
            pre_route=request.get("pre_route", False),
        )
        if not isinstance(verification, dict) or verification.get("status") != "ok":
            raise ValueError("Saved ground-plane board failed fill geometry or connectivity verification")
        ground_plane.update(verification)
        ground_plane["tracks_preserved"] = True
        ground_plane["original_footprints_and_text_preserved"] = True
        ground_plane["original_board_geometry_preserved"] = True
        result["ground_plane"] = ground_plane
        result["silkscreen"] = _reference_conflict_report(reloaded, pcbnew, 0, 0)
    if plane_cleanup is not None:
        from _native_plane_cleanup import verify as verify_plane_cleanup_after_reload

        if (_identity(reloaded) != identity or original_layout != _fixed_layout_state(reloaded)
                or original_board_geometry != _ground_plane_source_geometry_state(reloaded, pcbnew)):
            raise ValueError("Saved plane-cleanup board changed identity or original geometry")
        saved_copper = _copper_state(reloaded)
        if saved_copper - original_copper:
            raise ValueError("Saved plane-cleanup board added or mutated native copper")
        saved_removed = original_copper - saved_copper
        saved_removed_rows = [json.loads(raw) for raw, count in saved_removed.items() for _ in range(count)]
        if (any(row["type"] != "PCB_TRACK" or row["net"] != request["plan"]["net"] or row["locked"]
                for row in saved_removed_rows)
                or len(saved_removed_rows) != plane_cleanup.get("removed_count")):
            raise ValueError("Saved plane-cleanup board removed a via, locked trace, or nonselected copper item")
        saved_by_uuid = {row["uuid"]: row for row in saved_removed_rows}
        if (len(saved_by_uuid) != len(saved_removed_rows)
                or set(saved_by_uuid) != {row["uuid"] for row in plane_cleanup["removed_tracks"]}):
            raise ValueError("Saved plane-cleanup removed UUIDs do not match the native report")
        for report_row in plane_cleanup["removed_tracks"]:
            actual = saved_by_uuid[report_row["uuid"]]
            geometry = report_row.get("geometry")
            if (report_row.get("net") != actual["net"] or report_row.get("locked") is not False
                    or geometry != {"start": actual["start"], "end": actual["end"],
                                    "layer": actual["layer"], "width": actual["width"]}):
                raise ValueError("Saved plane-cleanup removed geometry does not match the native report")
        verification = verify_plane_cleanup_after_reload(
            reloaded, pcbnew, plane_cleanup, net=request["plan"]["net"],
            protected_connections=request["plan"]["protected_connections"],
        )
        if not isinstance(verification, dict) or verification.get("status") != "ok":
            raise ValueError("Saved plane-cleanup board failed native fill, coverage or connectivity verification")
        plane_cleanup.update(verification)
        plane_cleanup.update({
            "existing_filled_planes_verified": True,
            "only_covered_unlocked_traces_removed": True,
            "no_copper_added": True,
            "vias_preserved": True,
            "nonselected_copper_preserved": True,
            "locked_copper_preserved": True,
            "original_geometry_preserved": True,
            "original_board_geometry_preserved": True,
        })
        result["plane_cleanup"] = plane_cleanup
        result["silkscreen"] = _reference_conflict_report(reloaded, pcbnew, 0, 0)
        plane_cleanup_removed_items.clear()
    return result


if __name__ == "__main__":
    try:
        # Retain the app for the native worker's lifetime; create no window.
        if sys.platform == "win32":
            import wx
            _native_wx_app = wx.GetApp() or wx.App(False)
            _native_wx_log = wx.LogStderr()
            wx.Log.SetActiveTarget(_native_wx_log)
        payload = execute(json.load(sys.stdin))
    except Exception as exc:
        payload = {"status": "error", "message": str(exc)}
    print("CW_KICAD_RESULT=" + json.dumps(payload))
    raise SystemExit(0 if payload["status"] == "ok" else 1)
