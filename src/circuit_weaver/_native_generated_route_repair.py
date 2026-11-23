"""In-memory repair helpers for generated, source-empty routing candidates.

These functions only prepare a candidate. The caller must save, reload, and run
a fresh full native DRC before accepting any result.
"""

from __future__ import annotations

import json


def _worker():
    try:
        import _kicad_worker as worker
    except ImportError:
        from . import _kicad_worker as worker
    return worker


def _validate_limit(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")


def _track_rows(board):
    rows = []
    for item in board.GetTracks():
        kind = type(item).__name__
        uuid = str(item.m_Uuid.AsString())
        if not uuid:
            raise ValueError("native copper item has no UUID")
        rows.append((item, kind, uuid))
    return rows


def _copper_rows_by_uuid(copper_state):
    """Index one exact copper snapshot and reject ambiguous native identities."""
    result = {}
    for raw, count in copper_state.items():
        try:
            row = json.loads(raw)
            uuid = row.get("uuid")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("native copper snapshot row is malformed") from exc
        if (not isinstance(uuid, str) or not uuid or isinstance(count, bool)
                or not isinstance(count, int) or count != 1 or uuid in result):
            raise ValueError("native copper snapshot UUIDs must be unique")
        result[uuid] = (raw, row)
    return result


def _source_empty_and_preserved(board, source_board, pcbnew):
    worker = _worker()
    if list(source_board.GetTracks()):
        raise ValueError("generated-route repair requires a source board with no tracks or vias")
    identity = worker._identity(source_board)
    layout = worker._fixed_layout_state(source_board)
    geometry = worker._ground_plane_source_geometry_state(source_board, pcbnew)
    if (worker._identity(board) != identity or worker._fixed_layout_state(board) != layout
            or worker._ground_plane_source_geometry_state(board, pcbnew) != geometry):
        raise ValueError("candidate changed source identity, fixed layout, or source geometry")
    return worker, identity, layout, geometry


def _metadata(worker, board, source_board, pcbnew):
    worker, identity, layout, geometry = _source_empty_and_preserved(board, source_board, pcbnew)
    return {
        "status": "candidate_only_requires_fresh_drc",
        "source_identity_preserved": True,
        "source_layout_preserved": True,
        "source_geometry_preserved": True,
        "other_copper_preserved": True,
        "source_copper_exact_subset_preserved": True,
        "source_copper_item_count": 0,
    }


def repair_generated_widths(board, source_board, pcbnew, *, max_tracks=4096):
    """Widen undersized generated straight tracks to board/net-class minima."""
    _validate_limit(max_tracks, "max_tracks", 4096)
    worker, _, _, _ = _source_empty_and_preserved(board, source_board, pcbnew)
    try:
        from _native_routing_repair import _class_rules
    except ImportError:
        from ._native_routing_repair import _class_rules
    rows = _track_rows(board)
    plans = []
    target_by_net = {}
    for item, kind, uuid in rows:
        if kind == "PCB_VIA":
            continue
        if kind != "PCB_TRACK":
            # Curved/unknown items are permitted only if already compliant.
            try:
                width = int(item.GetWidth())
                net_name = str(item.GetNetname())
                minimum = max(int(board.GetDesignSettings().m_TrackMinWidth),
                              int(pcbnew.FromMM(_class_rules(board, pcbnew, net_name)[0])))
            except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("unsupported copper item cannot be safely checked") from exc
            if width < minimum:
                raise ValueError("undersized non-straight generated copper is unsupported")
            continue
        try:
            width = item.GetWidth()
            if isinstance(width, bool) or int(width) <= 0 or float(width) != int(width):
                raise ValueError
            width = int(width)
            net_code = int(item.GetNetCode())
            net_name = str(item.GetNetname())
            locked = item.IsLocked()
        except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("generated track metadata is invalid") from exc
        if not net_name or net_code <= 0:
            raise ValueError("generated track must belong to a known positive-code net")
        if not isinstance(locked, bool):
            raise ValueError("generated track lock state is invalid")
        if net_name not in target_by_net:
            netclass_mm = _class_rules(board, pcbnew, net_name)[0]
            raw_minimum = board.GetDesignSettings().m_TrackMinWidth
            if isinstance(raw_minimum, bool) or int(raw_minimum) < 0:
                raise ValueError("native track minimum is invalid")
            minimum_iu = int(raw_minimum)
            class_iu = int(pcbnew.FromMM(netclass_mm))
            if class_iu <= 0:
                raise ValueError("resolved net-class track width is invalid")
            target_by_net[net_name] = max(minimum_iu, class_iu)
        target = target_by_net[net_name]
        if width < target:
            if locked:
                raise ValueError("locked generated track is undersized")
            plans.append((item, uuid, net_name, width, target))

    if len(plans) > max_tracks:
        raise ValueError("generated width repair count exceeds the configured bound")

    before = worker._copper_state(board)
    expected = before.copy()
    snapshot_by_uuid = _copper_rows_by_uuid(before)
    for _, uuid, net_name, old, new in plans:
        snapshot_entry = snapshot_by_uuid.get(uuid)
        if snapshot_entry is None:
            raise ValueError("generated track UUID is not unique in the copper snapshot")
        old_row, row = snapshot_entry
        if row.get("net") != net_name or row.get("width") != old:
            raise ValueError("generated track snapshot does not match native metadata")
        expected[old_row] -= 1
        if not expected[old_row]:
            del expected[old_row]
        row["width"] = new
        expected[json.dumps(row, sort_keys=True)] += 1
    changed = []
    try:
        for item, uuid, net_name, old, new in plans:
            item.SetWidth(new)
            changed.append((item, old))
        if worker._copper_state(board) != expected:
            raise ValueError("width repair changed copper fields beyond planned track widths")
        _source_empty_and_preserved(board, source_board, pcbnew)
    except Exception as exc:
        for item, old in reversed(changed):
            try:
                item.SetWidth(old)
            except Exception:
                pass
        if isinstance(exc, ValueError):
            raise
        raise ValueError("native track width mutation failed") from exc
    result = _metadata(worker, board, source_board, pcbnew)
    result.update({
        "changed_track_count": len(plans),
        "changed_tracks": [{"uuid": uuid, "net": net, "before_width_iu": old,
                            "after_width_iu": new} for _, uuid, net, old, new in plans],
        "unchanged_copper_item_count": len(rows) - len(plans),
    })
    return result


def _report_body(raw_report):
    if not isinstance(raw_report, dict) or raw_report.get("truncated") is True or raw_report.get("capped") is True:
        raise ValueError("fresh native DRC report is malformed or incomplete")
    body = raw_report.get("drc", raw_report)
    if not isinstance(body, dict) or body.get("truncated") is True or body.get("capped") is True:
        raise ValueError("fresh native DRC report is malformed or incomplete")
    native_diagnostics = body.get("native_diagnostics")
    if (body.get("native_diagnostics_may_be_truncated") is True
            or (isinstance(native_diagnostics, dict)
                and native_diagnostics.get("native_diagnostics_may_be_truncated") is True)):
        raise ValueError("truncated native DRC diagnostics cannot authorize generated-copper repair")
    return body


def _width_whitelist(width_repair_report, *, require_count=True, require_metadata=True):
    expected_flags = ("source_identity_preserved", "source_layout_preserved", "source_geometry_preserved",
                      "other_copper_preserved", "source_copper_exact_subset_preserved")
    if (not isinstance(width_repair_report, dict)
            or (require_metadata and width_repair_report.get("status") != "candidate_only_requires_fresh_drc")
            or (require_metadata and width_repair_report.get("source_copper_item_count") != 0)
            or (require_metadata and any(width_repair_report.get(key) is not True for key in expected_flags))
            or not isinstance(width_repair_report.get("changed_tracks"), list)
            or (require_count and (type(width_repair_report.get("changed_track_count")) is not int
                or width_repair_report.get("changed_track_count") != len(width_repair_report["changed_tracks"])) )):
        raise ValueError("width repair report is not a coherent source-preserving candidate")
    whitelist = {}
    for row in width_repair_report["changed_tracks"]:
        if not isinstance(row, dict):
            raise ValueError("width repair whitelist is malformed")
        uuid, old, new, net = (row.get("uuid"), row.get("before_width_iu"),
                               row.get("after_width_iu"), row.get("net"))
        if (not isinstance(uuid, str) or not uuid or uuid in whitelist or isinstance(old, bool)
                or not isinstance(old, int) or old <= 0 or isinstance(new, bool) or not isinstance(new, int)
                or new <= old or not isinstance(net, str) or not net):
            raise ValueError("width repair whitelist is malformed")
        whitelist[uuid] = (old, new, net)
    return whitelist


def _point(item, getter):
    value = getattr(item, getter)()
    return int(value.x), int(value.y)


def _point_on_segment(point, start, end):
    cross = ((point[0] - start[0]) * (end[1] - start[1])
             - (point[1] - start[1]) * (end[0] - start[0]))
    return (cross == 0 and min(start[0], end[0]) <= point[0] <= max(start[0], end[0])
            and min(start[1], end[1]) <= point[1] <= max(start[1], end[1]))


def _shape_touches_point(item, kind, layer, point, pcbnew):
    try:
        shape = item.GetEffectiveShape(layer) if kind in {"PAD", "PCB_VIA"} else item.GetEffectiveShape()
        if shape is None:
            raise ValueError("native copper shape is unavailable")
        return bool(shape.Collide(pcbnew.VECTOR2I(*point), 0))
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("native elbow contact geometry could not be checked") from exc


def _violation_item_uuids(violation):
    if not isinstance(violation, dict) or violation.get("type") != "clearance":
        raise ValueError("bend adjustment requires clearance-only native DRC findings")
    raw_items = violation.get("items")
    if not isinstance(raw_items, list):
        raw_items = [violation.get("item1"), violation.get("item2")]
    result = []
    for item in raw_items:
        if isinstance(item, str):
            uuid = item
        elif isinstance(item, dict):
            uuid = item.get("uuid") or item.get("id")
        else:
            uuid = None
        if not isinstance(uuid, str) or not uuid:
            raise ValueError("native clearance finding lacks stable item UUIDs")
        result.append(uuid)
    if len(result) != 2 or result[0] == result[1]:
        raise ValueError("native clearance finding must identify exactly two distinct items")
    return result


def _candidate_deltas():
    # 10–35.4 um deterministic lattice, matching the successful bounded native probe.
    candidates = {
        (x, y)
        for step in (10_000, 15_000, 25_000)
        for x in (-step, 0, step)
        for y in (-step, 0, step)
        if x or y
    }
    return sorted(candidates, key=lambda point: (point[0] ** 2 + point[1] ** 2, point))


def adjust_generated_width_bends(board, source_board, pcbnew, raw_report, width_repair_report,
                                 *, max_tracks=64):
    """Try bounded endpoint moves on exact degree-two elbows implicated in width DRC.

    This only prepares a candidate. Host acceptance always requires a fresh full native DRC.
    """
    _validate_limit(max_tracks, "max_tracks", 64)
    worker, _, _, _ = _source_empty_and_preserved(board, source_board, pcbnew)
    body = _report_body(raw_report)
    violations = body.get("violations")
    unconnected = body.get("unconnected_items")
    parity = body.get("schematic_parity")
    if not isinstance(violations, list) or not isinstance(unconnected, list) or not isinstance(parity, list):
        raise ValueError("fresh native DRC report lacks required finding arrays")
    if unconnected or parity:
        raise ValueError("bend adjustment requires no unconnected or schematic-parity findings")
    whitelist = _width_whitelist(width_repair_report)
    try:
        from _native_routing_repair import _class_rules
    except ImportError:
        from ._native_routing_repair import _class_rules
    if not violations or not whitelist:
        result = _metadata(worker, board, source_board, pcbnew)
        result.update({"adjusted_bend_count": 0, "adjusted_bends": [], "adjusted_track_count": 0})
        return result

    rows = _track_rows(board)
    before = worker._copper_state(board)
    snapshot_by_uuid = _copper_rows_by_uuid(before)
    by_uuid = {uuid: (item, kind) for item, kind, uuid in rows}
    if len(by_uuid) != len(rows):
        raise ValueError("candidate contains duplicate native copper UUIDs")
    pads = [pad for footprint in board.GetFootprints() for pad in footprint.Pads()]
    object_by_uuid = dict(by_uuid)
    for pad in pads:
        try:
            uuid = str(pad.m_Uuid.AsString())
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("native pad has no stable UUID") from exc
        if not uuid or uuid in object_by_uuid:
            raise ValueError("native copper UUIDs are ambiguous")
        object_by_uuid[uuid] = (pad, type(pad).__name__)

    # Clearance findings identify the foreign pad/via and the widened route segment.
    blocker_to_tracks = {}
    for violation in violations:
        first, second = _violation_item_uuids(violation)
        eligible = [uuid for uuid in (first, second) if uuid in whitelist]
        if len(eligible) != 1:
            continue
        track_uuid = eligible[0]
        blocker_uuid = second if first == track_uuid else first
        blocker_pair = object_by_uuid.get(blocker_uuid)
        track_pair = by_uuid.get(track_uuid)
        if blocker_pair is None or track_pair is None:
            raise ValueError("clearance finding references absent generated copper")
        blocker, blocker_kind = blocker_pair
        track, track_kind = track_pair
        if blocker_kind not in {"PAD", "PCB_VIA"} or track_kind != "PCB_TRACK":
            continue
        if (str(track.GetNetname()) != whitelist[track_uuid][2]
                or int(blocker.GetNetCode()) == int(track.GetNetCode())):
            raise ValueError("clearance finding track does not match its width whitelist")
        blocker_to_tracks.setdefault(blocker_uuid, set()).add(track_uuid)

    plans = []
    selected_tracks = set()
    for blocker_uuid, track_uuids in sorted(blocker_to_tracks.items()):
        if len(track_uuids) != 2:
            continue
        pair = [by_uuid[uuid][0] for uuid in sorted(track_uuids)]
        try:
            net_names = {str(track.GetNetname()) for track in pair}
            net_codes = {int(track.GetNetCode()) for track in pair}
            layers = {int(track.GetLayer()) for track in pair}
            locked = [track.IsLocked() for track in pair]
            endpoints = [(_point(track, "GetStart"), _point(track, "GetEnd")) for track in pair]
        except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
            continue
        if (len(net_names) != 1 or len(net_codes) != 1 or len(layers) != 1 or any(locked)
                or any(type(value) is not bool for value in locked)
                or any(int(track.GetWidth()) != whitelist[uuid][1]
                       for track, uuid in zip(pair, sorted(track_uuids)))):
            continue
        joint_set = set(endpoints[0]).intersection(endpoints[1])
        if len(joint_set) != 1:
            continue
        joint = next(iter(joint_set))
        outer_points = [edge[1] if edge[0] == joint else edge[0] for edge in endpoints]
        if any(edge[0] == edge[1] for edge in endpoints) or outer_points[0] == outer_points[1]:
            continue
        net_name, net_code, layer = next(iter(net_names)), next(iter(net_codes)), next(iter(layers))
        # Do not move a branched or pin/via-attached point. The candidate elbow must be
        # represented by exactly these two straight generated tracks.
        branched = False
        for item, kind, uuid in rows:
            if uuid in track_uuids:
                continue
            if kind == "PCB_TRACK":
                if int(item.GetLayer()) != layer:
                    continue
                start, end = _point(item, "GetStart"), _point(item, "GetEnd")
                if (_point_on_segment(joint, start, end)
                        or _shape_touches_point(item, kind, layer, joint, pcbnew)):
                    branched = True
                    break
            elif kind == "PCB_VIA" and item.IsOnLayer(layer):
                if (_point(item, "GetPosition") == joint
                        or _shape_touches_point(item, kind, layer, joint, pcbnew)):
                    branched = True
                    break
        if branched:
            continue
        for pad in pads:
            if pad.IsOnLayer(layer) and (
                _point(pad, "GetPosition") == joint
                or _shape_touches_point(pad, "PAD", layer, joint, pcbnew)
            ):
                branched = True
                break
        if branched:
            continue
        if selected_tracks.intersection(track_uuids):
            continue
        try:
            clearance_mm = _class_rules(board, pcbnew, net_name)[3]
            clearance_iu = int(pcbnew.FromMM(clearance_mm)) + 1000
        except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("native elbow clearance rule is unavailable") from exc
        obstacles = []
        for item, kind, uuid in rows:
            if uuid in track_uuids or int(item.GetNetCode()) == net_code or not item.IsOnLayer(layer):
                continue
            shape = item.GetEffectiveShape(layer) if kind == "PCB_VIA" else item.GetEffectiveShape()
            if shape is None:
                raise ValueError("native foreign copper shape is unavailable")
            obstacles.append(shape)
        for pad in pads:
            if int(pad.GetNetCode()) == net_code or not pad.IsOnLayer(layer):
                continue
            shape = pad.GetEffectiveShape(layer)
            if shape is None:
                raise ValueError("native foreign pad shape is unavailable")
            obstacles.append(shape)

        ends = [0 if edge[0] == joint else 1 for edge in endpoints]
        adjusted = None
        for dx, dy in _candidate_deltas():
            candidate = (joint[0] + dx, joint[1] + dy)
            if candidate in outer_points:
                continue
            try:
                for track, end in zip(pair, ends):
                    setter = track.SetStart if end == 0 else track.SetEnd
                    setter(pcbnew.VECTOR2I(*candidate))
                track_shapes = [track.GetEffectiveShape() for track in pair]
                if any(shape is None for shape in track_shapes):
                    raise ValueError("native candidate elbow shape is unavailable")
                clear = all(not obstacle.Collide(shape, clearance_iu)
                            for shape in track_shapes for obstacle in obstacles)
            except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
                for track, end in zip(pair, ends):
                    (track.SetStart if end == 0 else track.SetEnd)(pcbnew.VECTOR2I(*joint))
                raise ValueError("native elbow candidate geometry could not be checked") from exc
            if clear:
                adjusted = candidate
                for track, end in zip(pair, ends):
                    (track.SetStart if end == 0 else track.SetEnd)(pcbnew.VECTOR2I(*joint))
                break
            for track, end in zip(pair, ends):
                (track.SetStart if end == 0 else track.SetEnd)(pcbnew.VECTOR2I(*joint))
        if adjusted is not None:
            plans.append({
                "tracks": pair, "track_uuids": sorted(track_uuids), "net": net_name,
                "layer": layer, "old": joint, "new": adjusted,
                "blocker_uuids": [blocker_uuid], "ends": ends,
            })
            selected_tracks.update(track_uuids)

    touched = {uuid for plan in plans for uuid in plan["track_uuids"]}
    if len(touched) > max_tracks:
        raise ValueError("generated bend adjustment exceeds the configured track bound")
    expected = before.copy()
    mutated = []
    for plan in plans:
        for track, uuid, end in zip(plan["tracks"], plan["track_uuids"], plan["ends"]):
            entry = snapshot_by_uuid.get(uuid)
            if entry is None:
                raise ValueError("adjusted track UUID is not unique in the copper snapshot")
            raw, row = entry
            if (row.get("type") != "PCB_TRACK" or row.get("locked") is not False
                    or row.get("net") != plan["net"] or row.get("layer") != plan["layer"]):
                raise ValueError("adjusted track snapshot does not match the native elbow")
            expected[raw] -= 1
            if not expected[raw]:
                del expected[raw]
            updated = dict(row)
            updated["start" if end == 0 else "end"] = plan["new"]
            expected[json.dumps(updated, sort_keys=True)] += 1
    try:
        for plan in plans:
            for track, end in zip(plan["tracks"], plan["ends"]):
                (track.SetStart if end == 0 else track.SetEnd)(pcbnew.VECTOR2I(*plan["new"]))
                mutated.append((track, end, plan["old"]))
        if worker._copper_state(board) != expected:
            raise ValueError("bend adjustment changed copper beyond the planned endpoints")
        _source_empty_and_preserved(board, source_board, pcbnew)
        result = _metadata(worker, board, source_board, pcbnew)
    except Exception as exc:
        for track, end, point in reversed(mutated):
            (track.SetStart if end == 0 else track.SetEnd)(pcbnew.VECTOR2I(*point))
        if isinstance(exc, ValueError):
            raise
        raise ValueError("native bend adjustment integrity check failed") from exc
    bends = [{
        "track_uuids": plan["track_uuids"], "net": plan["net"], "layer": plan["layer"],
        "old_position_iu": list(plan["old"]), "new_position_iu": list(plan["new"]),
        "delta_iu": [plan["new"][0] - plan["old"][0], plan["new"][1] - plan["old"][1]],
        "blocker_uuids": plan["blocker_uuids"],
    } for plan in plans]
    result.update({"adjusted_bend_count": len(bends), "adjusted_bends": bends,
                   "adjusted_track_count": len(touched)})
    return result


def prune_generated_width_clearances(board, source_board, pcbnew, raw_report, width_repair_report,
                                     *, max_items=64):
    """Remove eligible widened tracks; retain returned SWIG refs through save/reload.

    The caller must pop ``_native_removed_items`` and keep the references alive
    until native save, reload, and inspection have completed.
    """
    _validate_limit(max_items, "max_items", 64)
    worker, _, _, _ = _source_empty_and_preserved(board, source_board, pcbnew)
    report_body = _report_body(raw_report)
    violations = report_body.get("violations")
    unconnected = report_body.get("unconnected_items")
    parity = report_body.get("schematic_parity")
    if not isinstance(violations, list) or not isinstance(unconnected, list) or not isinstance(parity, list):
        raise ValueError("fresh native DRC report lacks required finding arrays")
    if parity:
        raise ValueError("generated-width pruning requires no schematic-parity findings")
    whitelist = _width_whitelist(width_repair_report, require_count=False, require_metadata=False)
    by_uuid = {uuid: (item, kind) for item, kind, uuid in _track_rows(board)}
    if len(by_uuid) != len(_track_rows(board)):
        raise ValueError("candidate contains duplicate native copper UUIDs")
    selected = set()
    for violation in violations:
        if not isinstance(violation, dict) or violation.get("type") != "clearance":
            raise ValueError("pruning requires a clearance-only fresh DRC report")
        raw_ids = violation.get("items")
        if not isinstance(raw_ids, list):
            raw_ids = [violation.get("item1"), violation.get("item2")]
        ids = set()
        for entry in raw_ids:
            if isinstance(entry, str):
                ids.add(entry)
            elif isinstance(entry, dict):
                value = entry.get("uuid") or entry.get("id")
                if isinstance(value, str):
                    ids.add(value)
        eligible = ids.intersection(whitelist)
        if not eligible:
            raise ValueError("clearance finding does not implicate a widened generated track")
        selected.update(eligible)
    if not selected:
        raise ValueError("native DRC report has no eligible generated-width clearance tracks")
    if len(selected) > max_items:
        raise ValueError("eligible generated-track removals exceed the configured bound")
    removals = []
    for uuid in sorted(selected):
        if uuid not in by_uuid:
            raise ValueError("whitelisted generated track is absent from the candidate")
        item, kind = by_uuid[uuid]
        if (kind != "PCB_TRACK" or item.IsLocked() or int(item.GetWidth()) != whitelist[uuid][1]
                or str(item.GetNetname()) != whitelist[uuid][2]):
            raise ValueError("only unlocked, currently widened straight tracks may be pruned")
        removals.append((item, uuid, str(item.GetNetname()), int(item.GetWidth())))
    before = worker._copper_state(board)
    expected = before.copy()
    snapshot_by_uuid = _copper_rows_by_uuid(before)
    for _, uuid, _, _ in removals:
        snapshot_entry = snapshot_by_uuid.get(uuid)
        if snapshot_entry is None:
            raise ValueError("pruned track is not unique in the copper snapshot")
        raw, _ = snapshot_entry
        expected[raw] -= 1
        if not expected[raw]:
            del expected[raw]
    removed = []
    try:
        for item, _, _, _ in removals:
            board.Remove(item)
            removed.append(item)
        if worker._copper_state(board) != expected:
            raise ValueError("pruning changed copper beyond the selected generated tracks")
        _source_empty_and_preserved(board, source_board, pcbnew)
    except Exception as exc:
        for item in removed:
            try:
                board.Add(item)
            except Exception:
                pass
        if isinstance(exc, ValueError):
            raise
        raise ValueError("native generated-track removal failed") from exc
    result = _metadata(worker, board, source_board, pcbnew)
    result.update({"removed_track_count": len(removals), "removed_tracks": [
        {"uuid": uuid, "net": net, "width_iu": width} for _, uuid, net, width in removals
    ], "remaining_clearance_count_requires_fresh_drc": True,
        "_native_removed_items": [item for item, _, _, _ in removals]})
    return result
