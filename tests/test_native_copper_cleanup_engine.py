"""Focused geometry and native-board tests for removal-only copper cleanup."""
from __future__ import annotations

import pytest

from circuit_weaver._native_copper_cleanup import _limits_from_baseline
from circuit_weaver._native_copper_graph import CopperGraph, node_key, segment_intersections


def test_geometry_splits_crossings_t_junctions_and_overlaps():
    assert segment_intersections((0, 0), (4, 0), (2, -1), (2, 1)) == [0.5]
    assert segment_intersections((0, 0), (4, 0), (2, 0), (6, 0)) == [0.5, 1.0]


def test_protected_baseline_cap_does_not_preadd_geometry_tolerance():
    connection = {"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1"}
    row = {"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1",
           "status": "connected", "baseline_length_mm": 12.5}
    limits = _limits_from_baseline([connection], [row])
    assert limits["J1.1->J2.1"] == 12.5


@pytest.mark.skip_category("optional-tool")
def test_native_cleanup_refuses_selected_net_without_native_pads():
    pcbnew = _native_pcbnew()
    from circuit_weaver._native_copper_cleanup import cleanup

    board = pcbnew.BOARD()
    target = _add_net(pcbnew, board, "TARGET")
    _add_fp(pcbnew, board, "J1", [("1", 1.0, target), ("2", 5.0, target)])
    _add_track(pcbnew, board, target, (0.0, 0.0), (6.0, 0.0))
    before = {_uuid(item) for item in board.GetTracks()}

    report = cleanup(board, pcbnew, ["MISSING"], [
        {"from_ref": "J1", "from_pad": "1", "to_ref": "J1", "to_pad": "2"},
    ])

    assert report["status"] == "refused"
    assert report["reason"] == "every selected net must have at least one native footprint pad"
    assert {_uuid(item) for item in board.GetTracks()} == before


def test_shortest_path_equal_cost_pad_and_coordinate_nodes_has_stable_tiebreak():
    graph = object.__new__(CopperGraph)
    from collections import defaultdict
    graph.adj = defaultdict(list)
    pad = ("PAD", "J1", "1", "N")
    left = node_key(0, 0, "F.Cu")
    right = node_key(0, 1, "F.Cu")
    destination = node_key(2, 0, "F.Cu")
    graph._add_edge(pad, left, 1.0, "N")
    graph._add_edge(pad, right, 1.0, "N")
    graph._add_edge(left, destination, 1.0, "N")
    graph._add_edge(right, destination, 1.0, "N")
    route = graph.shortest("N", pad, [destination])
    assert route["length_mm"] == pytest.approx(2.0)
    assert route["path"][0] == pad


def _native_pcbnew():
    return pytest.importorskip("pcbnew", reason="requires the KiCad native Python runtime")


def _point(pcbnew, x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


def _add_net(pcbnew, board, name):
    net = pcbnew.NETINFO_ITEM(board, name)
    board.Add(net)
    return net


def _add_fp(pcbnew, board, reference, pad_specs):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(reference)
    board.Add(fp)
    layer_set = pcbnew.LSET()
    layer_set.AddLayer(pcbnew.F_Cu)
    for number, x, net in pad_specs:
        pad = pcbnew.PAD(fp)
        pad.SetNumber(str(number))
        pad.SetSize(_point(pcbnew, 1.0, 1.0))
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetPosition(_point(pcbnew, x, 0))
        pad.SetLayerSet(layer_set)
        pad.SetNet(net)
        fp.Add(pad)
    return fp


def _add_track(pcbnew, board, net, start, end, *, locked=False):
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(_point(pcbnew, *start))
    track.SetEnd(_point(pcbnew, *end))
    track.SetWidth(pcbnew.FromMM(0.2))
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(net)
    if locked:
        track.SetLocked(True)
    board.Add(track)
    return track


@pytest.mark.skip_category("optional-tool")
def test_native_cleanup_removes_duplicate_and_preserves_unselected_locked_copper():
    pcbnew = _native_pcbnew()
    from circuit_weaver._native_copper_cleanup import cleanup

    board = pcbnew.BOARD()
    target = _add_net(pcbnew, board, "TARGET")
    other = _add_net(pcbnew, board, "OTHER")
    _add_fp(pcbnew, board, "J1", [("1", 1.0, target), ("2", 5.0, target)])
    _add_fp(pcbnew, board, "J2", [("1", 1.0, other), ("2", 5.0, other)])
    first = _add_track(pcbnew, board, target, (0.0, 0.0), (6.0, 0.0), locked=True)
    duplicate = _add_track(pcbnew, board, target, (0.0, 0.0), (6.0, 0.0))
    untouched = _add_track(pcbnew, board, other, (0.0, 0.0), (6.0, 0.0))

    report = cleanup(board, pcbnew, ["TARGET"], [
        {"from_ref": "J1", "from_pad": "1", "to_ref": "J1", "to_pad": "2"},
        {"from_ref": "J2", "from_pad": "1", "to_ref": "J2", "to_pad": "2"},
    ])

    remaining = {_uuid(item) for item in board.GetTracks()}
    assert _uuid(duplicate) in report["removed_uuids"]
    assert _uuid(first) in remaining and _uuid(untouched) in remaining
    assert report["all_pads_connected"] is True
    assert report["protected_paths_preserved"] is True
    assert all(row["status"] == "connected" for row in report["protected_connections"])
    assert report["unselected_copper_preserved"] is True
    assert report["output_copper_is_subset"] is True
    assert report["locked_copper_preserved"] is True


def _uuid(item):
    try:
        return str(item.m_Uuid.AsString())
    except AttributeError:
        return str(item.GetUuid().AsString())
