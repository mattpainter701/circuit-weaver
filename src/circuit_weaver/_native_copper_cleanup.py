"""Removal-only, fail-closed native copper cleanup engine.

The caller supplies KiCad's ``pcbnew`` module so this module can be imported by
the worker's native interpreter without importing KiCad into the application
runtime.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter

try:
    from ._native_copper_graph import CopperGraph, distance, point_mm, shape_collides
except ImportError:  # Standalone module import inside KiCad's native worker.
    from _native_copper_graph import CopperGraph, distance, point_mm, shape_collides

GEOMETRY_TOLERANCE_MM = 1e-4


def _uuid(item):
    try:
        return str(item.m_Uuid.AsString())
    except (AttributeError, RuntimeError):
        try:
            return str(item.GetUuid().AsString())
        except (AttributeError, RuntimeError):
            # This fallback is only used to stabilize ordering on older bindings.
            return f"{type(item).__name__}:{id(item)}"


def _is_via(item):
    return "VIA" in type(item).__name__.upper()


def _locked(item):
    try:
        return bool(item.IsLocked())
    except (AttributeError, RuntimeError):
        return False


def _item_signature(item, pcbnew):
    net = str(item.GetNetname())
    if _is_via(item):
        x, y = point_mm(pcbnew, item.GetPosition())
        top, bottom = int(item.TopLayer()), int(item.BottomLayer())
        try:
            widths = (int(item.GetWidth(top)), int(item.GetWidth(bottom)))
        except TypeError:
            width = int(item.GetWidth())
            widths = (width, width)
        drill = int(item.GetDrillValue()) if hasattr(item, "GetDrillValue") else int(item.GetDrill())
        return ("via", net, round(x, 6), round(y, 6),
                top, bottom, widths, drill,
                int(item.GetViaType()) if hasattr(item, "GetViaType") else None)
    a, b = point_mm(pcbnew, item.GetStart()), point_mm(pcbnew, item.GetEnd())
    ends = tuple(sorted((tuple(round(v, 6) for v in a), tuple(round(v, 6) for v in b))))
    return ("track", net, int(item.GetLayer()), ends,
            round(float(pcbnew.ToMM(item.GetWidth())), 6))


def _copper_inventory(board, pcbnew, only_nets=None, excluded_uuids=None):
    result = Counter()
    excluded_uuids = set(excluded_uuids or ())
    for item in board.GetTracks():
        if _uuid(item) in excluded_uuids:
            continue
        net = str(item.GetNetname())
        if only_nets is None or net in only_nets:
            result[_item_signature(item, pcbnew)] += 1
    return result


def _unselected_inventory(board, pcbnew, selected_nets, excluded_uuids=None):
    result = Counter()
    excluded_uuids = set(excluded_uuids or ())
    for item in board.GetTracks():
        if _uuid(item) in excluded_uuids:
            continue
        net = str(item.GetNetname())
        if net not in selected_nets:
            result[_item_signature(item, pcbnew)] += 1
    return result


def _fingerprint(counter):
    payload = json.dumps(sorted((repr(key), value) for key, value in counter.items()),
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _protected_rows(graph, connections, limits=None):
    rows = []
    for index, connection in enumerate(connections):
        a_ref, a_pad = str(connection["from_ref"]), str(connection["from_pad"])
        b_ref, b_pad = str(connection["to_ref"]), str(connection["to_pad"])
        route = graph.route(a_ref, a_pad, b_ref, b_pad)
        key = f"{a_ref}.{a_pad}->{b_ref}.{b_pad}"
        if limits is not None and key in limits:
            limit = float(limits[key])
        elif connection.get("max_length_mm") is not None:
            limit = float(connection["max_length_mm"])
        else:
            limit = None
        rows.append({"index": index, "from_ref": a_ref, "from_pad": a_pad,
                     "to_ref": b_ref, "to_pad": b_pad,
                     "status": "connected" if route else "disconnected",
                     "baseline_length_mm": None if route is None else route["length_mm"],
                     "final_length_mm": None if route is None else route["length_mm"],
                     "limit_mm": limit,
                     "protected_path_preserved": route is not None and
                         (limit is None or route["length_mm"] <= limit + GEOMETRY_TOLERANCE_MM)})
    return rows


def _limits_from_baseline(connections, rows):
    limits = {}
    for connection, row in zip(connections, rows):
        if row["status"] != "connected":
            continue
        key = f"{row['from_ref']}.{row['from_pad']}->{row['to_ref']}.{row['to_pad']}"
        baseline_cap = row["baseline_length_mm"]
        caller_cap = connection.get("max_length_mm")
        limits[key] = min(float(caller_cap), baseline_cap) if caller_cap is not None else baseline_cap
    return limits


def _count_stats(board, pcbnew, nets, excluded_uuids=None):
    stats = {net: {"track_length_mm": 0.0, "track_count": 0, "via_count": 0}
             for net in nets}
    excluded_uuids = set(excluded_uuids or ())
    for item in board.GetTracks():
        if _uuid(item) in excluded_uuids:
            continue
        net = str(item.GetNetname())
        if net not in stats:
            continue
        row = stats[net]
        if _is_via(item):
            row["via_count"] += 1
        else:
            row["track_count"] += 1
            row["track_length_mm"] += distance(point_mm(pcbnew, item.GetStart()),
                                               point_mm(pcbnew, item.GetEnd()))
    for row in stats.values():
        row["track_length_mm"] = round(row["track_length_mm"], 6)
    return stats


def _unsupported_reason(board):
    try:
        if int(board.GetCopperLayerCount()) != 2:
            return "cleanup supports only two-layer boards"
    except (AttributeError, RuntimeError, TypeError):
        return "could not verify copper layer count"
    try:
        zones = board.Zones()
        if zones.GetCount() if hasattr(zones, "GetCount") else len(zones):
            return "cleanup refuses boards containing copper zones"
    except (AttributeError, RuntimeError, TypeError):
        return "could not verify copper zones"
    for item in board.GetTracks():
        if not _is_via(item) and ("ARC" in type(item).__name__.upper() or
                                  type(item).__name__.upper() not in ("PCB_TRACK", "TRACK")):
            return f"cleanup refuses unsupported routed primitive {type(item).__name__}"
    return None


def _pads_connected(graph, nets):
    return all(graph.pad_components_connected(net) for net in nets)


def _connection_graph_nets(board, pcbnew, nets, connections):
    graph = CopperGraph(board, pcbnew)
    result = set(nets)
    for connection in connections:
        first = graph.pad_nodes.get((str(connection["from_ref"]), str(connection["from_pad"])))
        last = graph.pad_nodes.get((str(connection["to_ref"]), str(connection["to_pad"])))
        if first and last and first[0] == last[0]:
            result.add(first[0])
    return sorted(result)


def _validate_trial(board, pcbnew, nets, graph_nets, connections, limits, padless_baseline,
                    excluded_uuids):
    graph = CopperGraph(board, pcbnew, graph_nets, excluded_uuids)
    if not _pads_connected(graph, nets):
        return False, graph
    if any(graph.padless_component_count(net) > padless_baseline[net] for net in nets):
        return False, graph
    for row in _protected_rows(graph, connections, limits):
        if row["status"] != "connected" or not row["protected_path_preserved"]:
            return False, graph
    return True, graph


def cleanup(board, pcbnew, nets, connections, _run_consolidation=True):
    """Greedily remove redundant selected-net copper while preserving routes.

    ``connections`` contains mappings with ``from_ref``, ``from_pad``,
    ``to_ref``, ``to_pad`` and optional ``max_length_mm`` keys.
    """
    nets = sorted({str(net) for net in nets if str(net)})
    connections = list(connections or ())
    if not nets:
        raise ValueError("at least one selected net is required")
    for connection in connections:
        if not all(key in connection for key in ("from_ref", "from_pad", "to_ref", "to_pad")):
            raise ValueError("protected connections require from_ref/from_pad/to_ref/to_pad")
        maximum = connection.get("max_length_mm")
        if maximum is not None and (not math.isfinite(float(maximum)) or float(maximum) < 0):
            raise ValueError("max_length_mm must be a finite nonnegative number")

    before_stats = _count_stats(board, pcbnew, nets)
    selected_before = _copper_inventory(board, pcbnew, set(nets))
    unselected_before = _unselected_inventory(board, pcbnew, set(nets))
    all_before = _copper_inventory(board, pcbnew)
    original_item_nets = {_uuid(item): str(item.GetNetname()) for item in board.GetTracks()}
    reason = _unsupported_reason(board)
    graph_nets = _connection_graph_nets(board, pcbnew, nets, connections)
    graph = CopperGraph(board, pcbnew, graph_nets)
    baseline_rows = _protected_rows(graph, connections)
    if reason is None and any(not graph.net_pads.get(net) for net in nets):
        reason = "every selected net must have at least one native footprint pad"
    if reason is None and not _pads_connected(graph, nets):
        reason = "selected-net footprint pads are not all natively connected at baseline"
    if reason is None and any(row["status"] != "connected" for row in baseline_rows):
        reason = "a requested protected connection is not natively connected at baseline"
    limits = _limits_from_baseline(connections, baseline_rows)
    for row in baseline_rows:
        key = f"{row['from_ref']}.{row['from_pad']}->{row['to_ref']}.{row['to_pad']}"
        row["limit_mm"] = limits.get(key)
        if row["limit_mm"] is not None and row["baseline_length_mm"] > row["limit_mm"] + GEOMETRY_TOLERANCE_MM:
            reason = reason or "a protected baseline path already exceeds its caller limit"
    padless_baseline = {net: graph.padless_component_count(net) for net in nets}
    locked_before = {_uuid(item) for item in board.GetTracks() if _locked(item)}
    removed = []

    def trial(item):
        token = _uuid(item)
        if _locked(item):
            return False
        try:
            valid, _ = _validate_trial(board, pcbnew, nets, graph_nets, connections,
                                       limits, padless_baseline, set(removed) | {token})
        except Exception:
            valid = False
        if valid:
            removed.append(token)
            return True
        return False

    def trial_batch(items):
        unique = {_uuid(item): item for item in items}
        ordered = [unique[key] for key in sorted(unique)]
        if any(_locked(item) for item in ordered):
            return False
        tokens = {_uuid(item) for item in ordered}
        try:
            valid, _ = _validate_trial(board, pcbnew, nets, graph_nets, connections,
                                       limits, padless_baseline, set(removed) | tokens)
        except Exception:
            valid = False
        if valid:
            removed.extend(_uuid(item) for item in ordered)
            return True
        return False

    if reason is None:
        # First remove only byte-for-byte-equivalent geometry duplicates. Keep
        # the lowest UUID deterministically and never touch locked members.
        duplicates = {}
        for item in board.GetTracks():
            if (_uuid(item) not in removed and str(item.GetNetname()) in nets
                    and not _is_via(item)):
                duplicates.setdefault(_item_signature(item, pcbnew), []).append(item)
        duplicate_removals = []
        for signature in sorted(duplicates, key=repr):
            items = sorted(duplicates[signature], key=_uuid)
            unlocked = [item for item in items if not _locked(item)]
            if len(items) > 1 and unlocked:
                keeper = items[0] if _locked(items[0]) else unlocked[0]
                duplicate_removals.extend(item for item in items
                                          if item is not keeper and not _locked(item))
        if duplicate_removals and not trial_batch(duplicate_removals):
            for item in duplicate_removals:
                trial(item)

        # A redundant via can become removable only after its now-dangling
        # access stubs are removed in the same transaction. Enumerate bounded
        # subsets of native-shape-touching tracks, smallest first.
        for via in sorted((item for item in board.GetTracks()
                           if _uuid(item) not in removed and str(item.GetNetname()) in nets
                           and _is_via(item) and not _locked(item)),
                          key=_uuid):
            contact_tracks = []
            for track in board.GetTracks():
                if (_uuid(track) in removed or _is_via(track) or _locked(track)
                        or str(track.GetNetname()) != str(via.GetNetname())):
                    continue
                if track.GetLayer() not in (int(via.TopLayer()), int(via.BottomLayer())):
                    continue
                if shape_collides(via.GetEffectiveShape(track.GetLayer()),
                                  track.GetEffectiveShape(track.GetLayer())):
                    contact_tracks.append(track)
            contact_tracks.sort(key=_uuid)
            # Avoid combinatorial search around dense junctions; later greedy
            # individual removals still make progress there.
            if len(contact_tracks) > 8:
                continue
            removed_via = False
            for size in range(0, len(contact_tracks) + 1):
                for subset in itertools.combinations(contact_tracks, size):
                    if trial_batch((via, *subset)):
                        removed_via = True
                        break
                if removed_via:
                    break

        # Then try redundant vias before long traces, with stable UUID tie-breaks.
        while True:
            candidates = [item for item in board.GetTracks()
                          if _uuid(item) not in removed and str(item.GetNetname()) in nets
                          and not _locked(item)]
            candidates.sort(key=lambda item: (0 if _is_via(item) else 1,
                                               0.0 if _is_via(item) else -distance(
                                                   point_mm(pcbnew, item.GetStart()),
                                                   point_mm(pcbnew, item.GetEnd())),
                                               _uuid(item)))
            changed = False
            for item in candidates:
                if trial(item):
                    changed = True
                    break
            if not changed:
                break

    excluded = set(removed)
    graph = CopperGraph(board, pcbnew, graph_nets, excluded)
    final_rows = _protected_rows(graph, connections, limits)
    for baseline, final in zip(baseline_rows, final_rows):
        final["baseline_length_mm"] = baseline["baseline_length_mm"]
        final["protected_path_preserved"] = (final["status"] == "connected" and
            final["final_length_mm"] <= float(baseline["limit_mm"]) + GEOMETRY_TOLERANCE_MM)
        final["limit_mm"] = baseline["limit_mm"]
    after_stats = _count_stats(board, pcbnew, nets, excluded)
    selected_after = _copper_inventory(board, pcbnew, set(nets), excluded)
    unselected_after = _unselected_inventory(board, pcbnew, set(nets), excluded)
    all_after = _copper_inventory(board, pcbnew, excluded_uuids=excluded)
    result = {
        "status": "refused" if reason else ("cleaned" if removed else "no_redundant_copper"),
        "reason": reason,
        "nets": nets,
        "before": before_stats,
        "after": after_stats,
        "removed_uuids": removed,
        "protected_connections": final_rows,
        "all_pads_connected": _pads_connected(graph, nets),
        "protected_paths_preserved": all(r["protected_path_preserved"] for r in final_rows),
        "selected_copper_before_sha256": _fingerprint(selected_before),
        "selected_copper_after_sha256": _fingerprint(selected_after),
        "unselected_copper_preserved": unselected_before == unselected_after,
        "output_copper_is_subset": all(all_after[key] <= all_before[key] for key in all_after),
        "locked_copper_preserved": not bool(locked_before.intersection(removed)),
        "padless_components_before": padless_baseline,
        "padless_components_after": {net: graph.padless_component_count(net) for net in nets},
        "protected_path_limits_mm": {
            f"{row['from_ref']}.{row['from_pad']}->{row['to_ref']}.{row['to_pad']}": row["limit_mm"]
            for row in final_rows
        },
    }
    retained_items = []
    if removed:
        removals = set(removed)
        # Re-fetch a single wrapper for each deletion. Keeping a snapshot of
        # SWIG-owned board items while removing several of them can leave stale
        # native pointers in KiCad 10's Python bindings.
        while removals:
            target, token = None, None
            for item in board.GetTracks():
                item_token = _uuid(item)
                if item_token in removals:
                    target, token = item, item_token
                    break
            if target is None:
                break
            board.Remove(target)
            removals.remove(token)
            retained_items.append(target)
    if retained_items:
        # Keep KiCad's SWIG wrappers alive until the worker serializes the
        # board; its response layer strips this private non-JSON field.
        result["_native_removed_items"] = retained_items

    if _run_consolidation and reason is None:
        # The shared-via proposal helper is deliberately separate from the
        # generic removal graph. It receives strict caps derived from the
        # original measured routes, then this same generic pruner removes any
        # access stubs left by an accepted replacement.
        try:
            from ._native_via_consolidation import consolidate
        except ImportError:
            from _native_via_consolidation import consolidate
        strict_limits = {}
        capped_connections = []
        for connection, row in zip(connections, final_rows):
            key = f"{row['from_ref']}.{row['from_pad']}->{row['to_ref']}.{row['to_pad']}"
            cap = row["baseline_length_mm"]
            if connection.get("max_length_mm") is not None:
                cap = min(cap, float(connection["max_length_mm"]))
            strict_limits[key] = cap
            capped_connections.append({**connection, "max_length_mm": cap})
        via_result = consolidate(board, pcbnew, nets, connections, strict_limits)
        retained_items.extend(via_result.get("_native_removed_items", ()))
        if via_result.get("added_uuids") or via_result.get("removed_uuids"):
            post = cleanup(board, pcbnew, nets, capped_connections, _run_consolidation=False)
            retained_items.extend(post.get("_native_removed_items", ()))
            final_track_ids = {_uuid(item) for item in board.GetTracks()}
            added = [uid for uid in via_result.get("added_uuids", ()) if uid in final_track_ids]
            removed_ids = (result["removed_uuids"] + via_result.get("removed_uuids", [])
                           + post["removed_uuids"])
            # Keep the first pass's original path baselines while reporting the
            # final post-save geometry and enforcing the original strict caps.
            for baseline, final in zip(final_rows, post["protected_connections"]):
                final["baseline_length_mm"] = baseline["baseline_length_mm"]
                key = f"{final['from_ref']}.{final['from_pad']}->{final['to_ref']}.{final['to_pad']}"
                final["limit_mm"] = strict_limits[key]
                final["protected_path_preserved"] = (
                    final["status"] == "connected"
                    and final["final_length_mm"] <= final["limit_mm"] + GEOMETRY_TOLERANCE_MM
                )
            result.update({
                "status": "cleaned" if removed_ids or added else result["status"],
                "after": post["after"],
                "removed_uuids": removed_ids,
                "added_uuids": added,
                "protected_connections": post["protected_connections"],
                "all_pads_connected": post["all_pads_connected"],
                "protected_paths_preserved": all(
                    row["protected_path_preserved"] for row in post["protected_connections"]
                ),
                "selected_copper_after_sha256": post["selected_copper_after_sha256"],
                "padless_components_after": post["padless_components_after"],
                "protected_path_limits_mm": {
                    f"{row['from_ref']}.{row['from_pad']}->{row['to_ref']}.{row['to_pad']}": row["limit_mm"]
                    for row in post["protected_connections"]
                },
                "via_consolidation": {key: value for key, value in via_result.items()
                                      if not key.startswith("_")},
            })
            final_item_nets = {_uuid(item): str(item.GetNetname()) for item in board.GetTracks()}
            final_all = _copper_inventory(board, pcbnew)
            unselected_preserved = unselected_before == _unselected_inventory(board, pcbnew, set(nets))
            locked_after = {_uuid(item) for item in board.GetTracks() if _locked(item)}
            added_scope = all(final_item_nets.get(uid) in nets for uid in added)
            removed_scope = all(
                original_item_nets.get(uid) in nets or uid in via_result.get("added_uuids", ())
                for uid in removed_ids
            )
            result["unselected_copper_preserved"] = unselected_preserved
            result["locked_copper_preserved"] = locked_before <= locked_after
            result["output_copper_is_subset"] = all(
                final_all[key] <= all_before[key] for key in final_all
            )
            result["copper_changes_limited_to_selected_nets"] = (
                unselected_preserved and added_scope and removed_scope
            )
    if retained_items:
        result["_native_removed_items"] = retained_items
    return result


def verify(board, pcbnew, connections, limits, nets=None):
    """Rebuild graph evidence after native save/reload using caller limits."""
    nets = sorted({str(net) for net in (nets or ()) if str(net)})
    if not nets:
        nets = sorted({str(connection.get("net", "")) for connection in connections if connection.get("net")})
    if not nets:
        # Resolve route nets from the native pad inventory.
        all_graph = CopperGraph(board, pcbnew)
        nets = sorted({all_graph.pad_nodes[(str(c["from_ref"]), str(c["from_pad"]))][0]
                       for c in connections
                       if (str(c["from_ref"]), str(c["from_pad"])) in all_graph.pad_nodes})
    graph_nets = _connection_graph_nets(board, pcbnew, nets, connections)
    graph = CopperGraph(board, pcbnew, graph_nets)
    rows = _protected_rows(graph, connections, limits)
    return {
        "all_pads_connected": _pads_connected(graph, nets),
        "protected_paths_preserved": all(
            row["status"] == "connected" and row["protected_path_preserved"]
            for row in rows
        ),
        "protected_connections": rows,
    }


def validate_candidate(board, pcbnew, nets, connections, limits):
    """Check a temporary native candidate against selected-net and path guards."""
    nets = sorted({str(net) for net in nets if str(net)})
    graph_nets = _connection_graph_nets(board, pcbnew, nets, connections)
    graph = CopperGraph(board, pcbnew, graph_nets)
    rows = _protected_rows(graph, connections, limits)
    return {
        "all_pads_connected": _pads_connected(graph, nets),
        "protected_paths_preserved": all(
            row["status"] == "connected" and row["protected_path_preserved"]
            for row in rows
        ),
        "protected_connections": rows,
    }
