"""Native placement must use cut centerlines, not line artwork thickness."""
from types import SimpleNamespace

import pytest

from circuit_weaver._kicad_worker import _inspect


class Edge:
    def __init__(self, start, end, shape=0):
        self.start, self.end, self.shape = start, end, shape

    def GetStart(self):
        return SimpleNamespace(x=self.start[0], y=self.start[1])

    def GetEnd(self):
        return SimpleNamespace(x=self.end[0], y=self.end[1])

    def GetShape(self):
        return self.shape

    def GetLayer(self):
        return 25


def inspect_edges(edges, stroke_mm=0.254):
    # Deliberately supply the same stroke-inflated box returned by KiCad for
    # the A64 vendor board; changing line width must not resize the PCB.
    half = stroke_mm / 2
    box = SimpleNamespace(GetX=lambda: 100 - half, GetY=lambda: 37.5 - half,
                          GetWidth=lambda: 90 + stroke_mm, GetHeight=lambda: 62.5 + stroke_mm)
    board = SimpleNamespace(GetFootprints=lambda: [], GetDrawings=lambda: edges,
                            GetBoardEdgesBoundingBox=lambda: box, GetTracks=lambda: [], Zones=lambda: [])
    native = SimpleNamespace(SHAPE_T_RECT=1, SHAPE_T_SEGMENT=0, Edge_Cuts=25, ToMM=lambda value: value)
    return _inspect(board, native)


@pytest.mark.parametrize("stroke", [0.0, 0.05, 0.254, 1.0])
@pytest.mark.parametrize("reverse", [False, True])
def test_native_rectangle_bounds_ignore_stroke_and_corner_direction(stroke, reverse):
    corners = [(100, 37.5), (190, 100)]
    if reverse:
        corners.reverse()
    result = inspect_edges([Edge(*corners, shape=1)], stroke)
    assert result["rectangular_outline"] is True
    assert result["bounds"] == {"x": 100, "y": 37.5, "width": 90, "height": 62.5}


def test_native_four_segment_outline_uses_exact_offset_boundary():
    edges = [Edge((190, 100), (100, 100)), Edge((100, 37.5), (190, 37.5)),
             Edge((190, 100), (190, 37.5)), Edge((100, 100), (100, 37.5))]
    result = inspect_edges(edges)
    assert result["rectangular_outline"] is True
    assert result["bounds"] == {"x": 100, "y": 37.5, "width": 90, "height": 62.5}


@pytest.mark.parametrize("edges", [
    [], [Edge((100, 37.5), (100, 100), shape=1)],
    [Edge((0, 0), (1, 0)), Edge((1, 0), (1, 1)), Edge((1, 1), (0, 1))],
    [Edge((0, 0), (1, 0)), Edge((1, 0), (0, 0)), Edge((0, 1), (1, 1)), Edge((1, 1), (0, 1))],
    [Edge((0, 0), (1, 1)), Edge((1, 1), (0, 1)), Edge((0, 1), (1, 0)), Edge((1, 0), (0, 0))],
    [Edge((0, 0), (1, 1), shape=2)],
])
def test_open_duplicate_diagonal_and_unsupported_edges_do_not_allow_placement(edges):
    assert inspect_edges(edges)["rectangular_outline"] is False
