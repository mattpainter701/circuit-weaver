"""Unit coverage for native return-router validation and geometry math."""

from types import SimpleNamespace

import pytest

from circuit_weaver import _native_return_paths as paths


class Pad:
    def __init__(self, number, net=1, front=True):
        self.number, self.net, self.front = number, net, front

    def GetNumber(self):
        return self.number

    def GetNetCode(self):
        return self.net

    def GetNetname(self):
        return "GND" if self.net == 1 else "VDD"

    def GetLayerSet(self):
        return SimpleNamespace(Contains=lambda layer: (layer == 0 and self.front) or (layer == 31 and not self.front))


class Footprint:
    def __init__(self, reference, pads):
        self.reference, self.pads = reference, pads

    def GetReference(self):
        return self.reference

    def Pads(self):
        return self.pads


class Box:
    def GetLeft(self):
        return 0

    def GetTop(self):
        return 0

    def GetRight(self):
        return 10_000_000

    def GetBottom(self):
        return 10_000_000

    def GetWidth(self):
        return 10_000_000

    def GetHeight(self):
        return 10_000_000


class Board:
    def __init__(self, footprints, copper_layers=2, zones=()):
        self.footprints = footprints
        self.copper_layers = copper_layers
        self.zones = zones

    def GetFootprints(self):
        return self.footprints

    def GetDesignSettings(self):
        return SimpleNamespace(GetCopperLayerCount=lambda: self.copper_layers)

    def Zones(self):
        return self.zones

    def GetBoardEdgesBoundingBox(self):
        return Box()


PCBNEW = SimpleNamespace(
    F_Cu=0,
    B_Cu=31,
    FromMM=lambda value: round(value * 1_000_000),
    ToMM=lambda value: value / 1_000_000,
)


def fixture(*, target_net=1, target_front=True, copper_layers=2, zones=()):
    return Board(
        [Footprint("C1", [Pad("1")]), Footprint("U1", [Pad("1", target_net, target_front)])],
        copper_layers,
        zones,
    )


def connection():
    return {"from_ref": "C1", "from_pad": "1", "to_ref": "U1", "to_pad": "1"}


def test_rejects_mismatched_or_non_copper_endpoints_before_native_geometry():
    with pytest.raises(ValueError, match="same nonempty net"):
        paths.refine(fixture(target_net=2), PCBNEW, [connection()])
    with pytest.raises(ValueError, match="front-copper-accessible"):
        paths.refine(fixture(target_front=False), PCBNEW, [connection()])


def test_rejects_duplicate_physical_pad_mapping():
    board = fixture()
    board.footprints[0].pads.append(Pad("1"))
    with pytest.raises(ValueError, match="duplicate physical pad"):
        paths.refine(board, PCBNEW, [connection()])


def test_rejects_unsupported_layer_count_and_copper_zones():
    with pytest.raises(ValueError, match="two-layer"):
        paths.refine(fixture(copper_layers=4), PCBNEW, [connection()])
    with pytest.raises(ValueError, match="without copper zones"):
        paths.refine(fixture(zones=[object()]), PCBNEW, [connection()])


def test_native_collision_check_fails_closed_without_effective_shapes():
    with pytest.raises(ValueError, match="Native copper collision verification is unavailable"):
        paths._copper_collision(object(), object(), PCBNEW.F_Cu, 0.127, PCBNEW)


def test_segment_distance_handles_disjoint_collinear_and_box_corner_cases():
    assert paths._segments_distance((0, 0), (1, 0), (2, 0), (3, 0)) == 1
    assert paths._segments_distance((0, 0), (2, 2), (0, 2), (2, 0)) == 0
    assert paths._segment_box_distance((0, 0), (2, 2), (0, 3, 2, 4)) > 0
