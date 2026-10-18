"""Bounded shared-via proposals for protected same-net pad pairs.

Proposals replace at least three existing transitions with two shared through
vias. Native geometry screens new copper; graph checks gate every commit.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict

try:
    from . import _native_return_paths as routing
    from ._native_copper_graph import CopperGraph, point_mm
except ImportError:  # The KiCad worker executes outside the application package.
    import _native_return_paths as routing
    from _native_copper_graph import CopperGraph, point_mm


def _uuid(item):
    return item.m_Uuid.AsString()


def _key(connection):
    return f"{connection['from_ref']}.{connection['from_pad']}->" f"{connection['to_ref']}.{connection['to_pad']}"


def _grid(center, radius, step):
    count = round(radius / step)
    return [
        (round(center[0] + x * step, 6), round(center[1] + y * step, 6))
        for x in range(-count, count + 1)
        for y in range(-count, count + 1)
    ]


def _escapes(board, pcbnew, endpoint, points, net, ignored, bounds):
    result = {}
    for landing in sorted(set(points)):
        if not routing._via_clear(landing, net, (), board, pcbnew, 0.127, 0.6, bounds, ignored_uuids=ignored):
            continue
        for path in routing._candidate_paths(endpoint, landing):
            if routing._valid_path(path, net, (), board, pcbnew, 0.127, 0.2, bounds):
                result[landing] = (routing._path_length(path), path)
                break
    return result


def _rank_shared(source_escapes, target_escapes, limits, maximum=8):
    common = set.intersection(*(set(paths) for paths in source_escapes))
    rows = []
    for source in common:
        for target, (tail_length, tail_path) in target_escapes.items():
            dx, dy = abs(source[0] - target[0]), abs(source[1] - target[1])
            bridge_length = max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)
            lengths = [paths[source][0] + bridge_length + tail_length for paths in source_escapes]
            score = max(length - limit for length, limit in zip(lengths, limits))
            if score <= 0.5:
                rows.append((score, sum(lengths), source, target, lengths, tail_path))
    return sorted(rows, key=lambda row: row[:4])[:maximum]


def _accepted(board, pcbnew, nets, connections, limits, padless):
    graph = CopperGraph(board, pcbnew)
    if any(
        not graph.pad_components_connected(net) or graph.padless_component_count(net) > padless[net] for net in nets
    ):
        return False
    for connection in connections:
        route = graph.route(connection["from_ref"], connection["from_pad"], connection["to_ref"], connection["to_pad"])
        if route is None or route["length_mm"] > limits[_key(connection)] + 1e-4:
            return False
    return True


def consolidate(board, pcbnew, nets, connections, limits):
    """Try shared transitions for pairs of protected routes with a common pad."""
    pads = routing._pad_index(board)
    groups = defaultdict(dict)
    for connection in connections:
        first = (connection["from_ref"], str(connection["from_pad"]))
        last = (connection["to_ref"], str(connection["to_pad"]))
        if first not in pads or last not in pads:
            continue
        net = pads[first].GetNetname()
        if net not in nets or pads[last].GetNetname() != net:
            continue
        for hub, source in ((last, first), (first, last)):
            groups[(net, hub)][source] = connection
    result = {"added_uuids": [], "removed_uuids": [], "_native_removed_items": [], "groups": []}
    for (net, hub), sources in sorted(groups.items()):
        # Limit proposal cost on large arrays of power pads.
        for pair in list(itertools.combinations(sorted(sources), 2))[:8]:
            graph = CopperGraph(board, pcbnew)
            via_positions = set()
            for source in pair:
                route = graph.route(*source, *hub)
                if route is None:
                    break
                for a, b in zip(route["path"], route["path"][1:]):
                    if len(a) == len(b) == 3 and isinstance(a[0], (int, float)) and a[:2] == b[:2] and a[2] != b[2]:
                        via_positions.add(a[:2])
            else:
                old_vias = [
                    item
                    for item in board.GetTracks()
                    if item.GetClass() == "PCB_VIA"
                    and item.GetNetname() == net
                    and tuple(round(v, 6) for v in point_mm(pcbnew, item.GetPosition())) in via_positions
                ]
                if len(old_vias) < 3 or any(item.IsLocked() for item in old_vias):
                    continue
                bounds = routing._bounds(board, pcbnew)
                ignored = {_uuid(item) for item in old_vias}
                a, b = [point_mm(pcbnew, pads[source].GetPosition()) for source in pair]
                target = point_mm(pcbnew, pads[hub].GetPosition())
                numeric_net = pads[hub].GetNetCode()
                pair_limits = [limits[_key(sources[source])] for source in pair]
                source_center = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                source_radius = max(abs(a[0] - b[0]), abs(a[1] - b[1])) / 2 + 0.75
                # Two levels keep the search bounded while allowing fine-pitch
                # pad and mask clearances to choose a legal via landing.
                source_points = _grid(source_center, min(2.5, source_radius), 0.1)
                target_points = _grid(target, 1.5, 0.1)
                escapes = [
                    _escapes(board, pcbnew, endpoint, source_points, numeric_net, ignored, bounds)
                    for endpoint in (a, b)
                ]
                tails = _escapes(board, pcbnew, target, target_points, numeric_net, ignored, bounds)
                coarse = _rank_shared(escapes, tails, pair_limits, maximum=4)
                candidates = []
                for coarse_row in coarse:
                    fine_sources = _grid(coarse_row[2], 0.08, 0.01)
                    fine_targets = _grid(coarse_row[3], 0.08, 0.01)
                    fine_escapes = [
                        _escapes(board, pcbnew, endpoint, fine_sources, numeric_net, ignored, bounds)
                        for endpoint in (a, b)
                    ]
                    fine_tails = _escapes(board, pcbnew, target, fine_targets, numeric_net, ignored, bounds)
                    for candidate in _rank_shared(fine_escapes, fine_tails, pair_limits, maximum=8):
                        if candidate[0] <= 1e-4:
                            candidates.append((candidate, fine_escapes))
                padless = {name: graph.padless_component_count(name) for name in nets}
                committed = False
                for proposal, fine_escapes in sorted(candidates, key=lambda row: row[0][:4]):
                    _, _, source, landing, lengths, tail_path = proposal
                    if math.dist(source, landing) < 0.6 + 0.127:
                        continue
                    for bridge in routing._candidate_paths(source, landing):
                        if not routing._valid_path(
                            bridge, numeric_net, (), board, pcbnew, 0.127, 0.2, bounds, pcbnew.B_Cu
                        ):
                            continue
                        actual_lengths = [
                            paths[source][0] + routing._path_length(bridge) + routing._path_length(tail_path)
                            for paths in fine_escapes
                        ]
                        if any(length > cap + 1e-4 for length, cap in zip(actual_lengths, pair_limits)):
                            continue
                        before = {_uuid(item) for item in board.GetTracks()}
                        for item in old_vias:
                            board.Remove(item)
                        for paths in fine_escapes:
                            routing._add_path(board, pcbnew, paths[source][1], numeric_net, 0.2)
                        routing._add_path(board, pcbnew, list(reversed(tail_path)), numeric_net, 0.2)
                        routing._add_path(board, pcbnew, bridge, numeric_net, 0.2, pcbnew.B_Cu)
                        for point in (source, landing):
                            routing._add_via(board, pcbnew, point, numeric_net, 0.6, 0.3)
                        added = [item for item in board.GetTracks() if _uuid(item) not in before]
                        if _accepted(board, pcbnew, nets, connections, limits, padless):
                            result["added_uuids"].extend(_uuid(item) for item in added)
                            result["removed_uuids"].extend(ignored)
                            result["_native_removed_items"].extend(old_vias)
                            result["groups"].append(
                                {
                                    "net": net,
                                    "hub": list(hub),
                                    "sources": [list(item) for item in pair],
                                    "before_vias": len(old_vias),
                                    "after_vias": 2,
                                    "source_via_mm": list(source),
                                    "target_via_mm": list(landing),
                                    "new_path_lengths_mm": actual_lengths,
                                }
                            )
                            committed = True
                        else:
                            for item in added:
                                board.Remove(item)
                            # Keep wrappers alive through the worker save.
                            result["_native_removed_items"].extend(added)
                            for item in old_vias:
                                board.Add(item)
                        if committed:
                            break
                    if committed:
                        break
    return result
