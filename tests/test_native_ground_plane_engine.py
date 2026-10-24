"""Focused unit coverage for native ground-plane preconditions."""
from types import SimpleNamespace

import pytest

from circuit_weaver import _native_ground_plane as ground


class Point:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Edge:
    def __init__(self, a, b):
        self.a, self.b = Point(*a), Point(*b)

    def GetLayer(self):
        return 44

    def GetClass(self):
        return "PCB_SHAPE"

    def GetShape(self):
        return 1

    def GetStart(self):
        return self.a

    def GetEnd(self):
        return self.b


PCBNEW = SimpleNamespace(Edge_Cuts=44, SHAPE_T_SEGMENT=1, ToMM=lambda value: value / 1_000_000)


class Board:
    def __init__(self, edges):
        self.edges = [Edge(a, b) for a, b in edges]

    def GetDrawings(self):
        return self.edges


def rectangle():
    return Board([((1_000_000, 2_000_000), (11_000_000, 2_000_000)),
                  ((11_000_000, 2_000_000), (11_000_000, 8_000_000)),
                  ((11_000_000, 8_000_000), (1_000_000, 8_000_000)),
                  ((1_000_000, 8_000_000), (1_000_000, 2_000_000))])


def test_accepts_only_exact_closed_axis_aligned_rectangle():
    assert ground._outline_bounds(rectangle(), PCBNEW) == (1.0, 2.0, 11.0, 8.0)
    board = rectangle()
    board.edges.pop()
    with pytest.raises(ValueError, match="exactly four"):
        ground._outline_bounds(board, PCBNEW)


def test_rejects_non_rectangular_and_diagonal_edge_cuts():
    board = rectangle()
    board.edges[0] = Edge((1_000_000, 2_000_000), (11_000_000, 8_000_000))
    with pytest.raises(ValueError, match="axis-aligned"):
        ground._outline_bounds(board, PCBNEW)
    board = rectangle()
    board.edges[2] = Edge((11_000_000, 8_000_000), (1_000_000, 9_000_000))
    with pytest.raises(ValueError, match="axis-aligned"):
        ground._outline_bounds(board, PCBNEW)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -0.1, "0.3"])
def test_numeric_options_fail_closed(value):
    with pytest.raises(ValueError, match="clearance_mm"):
        ground._number({"clearance_mm": value}, "clearance_mm", 0.25)


def test_protected_path_requires_existing_trace_only_route():
    graph = SimpleNamespace(route=lambda *args: None)
    with pytest.raises(ValueError, match="no existing trace-only route"):
        ground._trace_route(graph, {"from_ref": "U1", "from_pad": "1", "to_ref": "J1", "to_pad": "1"})


def test_zone_anchor_query_uses_stable_pad_and_track_methods():
    calls = []

    class ConnectedItem:
        def __init__(self, item_class):
            self.item_class = item_class

        def GetClass(self):
            return self.item_class

        def GetNetname(self):
            return "GND"

    class Connectivity:
        def GetConnectedPads(self, zone):
            calls.append("pads")
            return [ConnectedItem("PAD")]

        def GetConnectedTracks(self, zone):
            calls.append("tracks")
            return [ConnectedItem("PCB_VIA"), ConnectedItem("PCB_TRACK")]

    board = SimpleNamespace(GetConnectivity=lambda: Connectivity())
    zone = SimpleNamespace(GetNetname=lambda: "GND", GetLayerName=lambda: "B.Cu")
    anchors = ground._zone_pad_anchors(board, zone)

    assert calls == ["pads", "tracks"]
    assert [item.GetClass() for item in anchors] == ["PAD", "PCB_VIA", "PCB_TRACK"]


def test_enabled_copper_names_are_resolved_from_native_board_stack():
    class LayerStack(list):
        def Seq(self):
            return list(self)

    class Enabled:
        def CuStack(self):
            return LayerStack([0, 2, 4, 31])

    class Settings:
        def GetCopperLayerCount(self):
            return 4

    board = SimpleNamespace(
        GetEnabledLayers=lambda: Enabled(),
        GetLayerName=lambda layer: {0: "F.Cu", 2: "In1.Cu", 4: "In2.Cu", 31: "B.Cu"}[layer],
        GetDesignSettings=lambda: Settings(),
    )
    assert ground._copper_layer_ids(board, SimpleNamespace()) == {
        "F.Cu": 0, "In1.Cu": 2, "In2.Cu": 4, "B.Cu": 31,
    }
