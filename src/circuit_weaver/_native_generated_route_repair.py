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


def prune_generated_width_clearances(board, source_board, pcbnew, raw_report, width_repair_report,
                                     *, max_items=64):
    """Remove eligible widened tracks; retain returned SWIG refs through save/reload.

    The caller must pop ``_native_removed_items`` and keep the references alive
    until native save, reload, and inspection have completed.
    """
    _validate_limit(max_items, "max_items", 64)
    worker, _, _, _ = _source_empty_and_preserved(board, source_board, pcbnew)
    if not isinstance(raw_report, dict):
        raise ValueError("fresh native DRC report must be an object")
    if raw_report.get("truncated") is True or raw_report.get("capped") is True:
        raise ValueError("truncated native DRC report cannot authorize pruning")
    report_body = raw_report.get("drc", raw_report)
    if not isinstance(report_body, dict):
        raise ValueError("fresh native DRC report body must be an object")
    if (report_body.get("truncated") is True or report_body.get("capped") is True
            or report_body.get("native_diagnostics_may_be_truncated") is True):
        raise ValueError("truncated native DRC report cannot authorize pruning")
    native_diagnostics = report_body.get("native_diagnostics")
    if (isinstance(native_diagnostics, dict)
            and native_diagnostics.get("native_diagnostics_may_be_truncated") is True):
        raise ValueError("truncated native diagnostics cannot authorize pruning")
    violations = report_body.get("violations")
    unconnected = report_body.get("unconnected_items")
    parity = report_body.get("schematic_parity")
    if not isinstance(violations, list) or not isinstance(unconnected, list) or not isinstance(parity, list):
        raise ValueError("fresh native DRC report lacks required finding arrays")
    if parity:
        raise ValueError("generated-width pruning requires no schematic-parity findings")
    if not isinstance(width_repair_report, dict) or not isinstance(width_repair_report.get("changed_tracks"), list):
        raise ValueError("width repair report lacks its generated-track whitelist")
    expected_flags = ("source_identity_preserved", "source_layout_preserved", "source_geometry_preserved",
                      "other_copper_preserved", "source_copper_exact_subset_preserved")
    if (width_repair_report.get("status") != "candidate_only_requires_fresh_drc"
            or width_repair_report.get("source_copper_item_count") != 0
            or any(width_repair_report.get(key) is not True for key in expected_flags)):
        raise ValueError("width repair report is not a coherent source-preserving candidate")
    whitelist = {}
    for row in width_repair_report["changed_tracks"]:
        if not isinstance(row, dict):
            raise ValueError("width repair whitelist is malformed")
        uuid, old, new, net = (row.get("uuid"), row.get("before_width_iu"),
                               row.get("after_width_iu"), row.get("net"))
        if (not isinstance(uuid, str) or not uuid or uuid in whitelist or isinstance(old, bool)
                or not isinstance(old, int) or old <= 0 or isinstance(new, bool) or not isinstance(new, int)
                or new <= 0 or old >= new or not isinstance(net, str) or not net):
            raise ValueError("width repair whitelist is malformed")
        whitelist[uuid] = (old, new, net)
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
