"""Exact native fingerprints for board arcs and circles."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from circuit_weaver._kicad_worker import _ground_plane_graphic_state


@dataclass(frozen=True)
class _Point:
    x: int
    y: int


class _Uuid:
    def __init__(self, value: str):
        self.value = value

    def AsString(self) -> str:
        return self.value


class _Pcbnew:
    SHAPE_T_SEGMENT = 0
    SHAPE_T_ARC = 1
    SHAPE_T_CIRCLE = 2


class PCB_SHAPE:
    def __init__(self, shape: int, *, start=(100, 200), middle=(300, 400), end=(500, 600),
                 uuid="native-drawing", layer=31, width=125_000, fill=0, locked=True, net="GND"):
        self.shape = shape
        self.start = _Point(*start)
        self.middle = _Point(*middle)
        self.end = _Point(*end)
        self.m_Uuid = _Uuid(uuid)
        self.layer = layer
        self.width = width
        self.fill = fill
        self.locked = locked
        self.net = net

    def GetShape(self):
        return self.shape

    def GetShapeStr(self):
        return {1: "Arc", 2: "Circle"}.get(self.shape, "Bezier")

    def GetStart(self):
        return self.start

    def GetArcMid(self):
        return self.middle

    def GetEnd(self):
        return self.end

    def GetLayer(self):
        return self.layer

    def GetWidth(self):
        return self.width

    def GetFillMode(self):
        return self.fill

    def IsLocked(self):
        return self.locked

    def GetNetname(self):
        return self.net


def test_arc_fingerprint_includes_start_mid_end_and_common_properties():
    arc = PCB_SHAPE(_Pcbnew.SHAPE_T_ARC)

    state = _ground_plane_graphic_state(arc, _Pcbnew)

    assert state == (
        "PCB_SHAPE", "native-drawing", "Arc", 31,
        ((100, 200), (300, 400), (500, 600)), 125_000, 0, True, "GND",
    )
    assert state == _ground_plane_graphic_state(PCB_SHAPE(_Pcbnew.SHAPE_T_ARC), _Pcbnew)


@pytest.mark.parametrize(
    "geometry",
    [
        {"start": (101, 200)},
        {"middle": (300, 401)},
        {"end": (500, 601)},
    ],
)
def test_arc_control_point_change_changes_fingerprint(geometry):
    original = _ground_plane_graphic_state(PCB_SHAPE(_Pcbnew.SHAPE_T_ARC), _Pcbnew)
    changed = _ground_plane_graphic_state(PCB_SHAPE(_Pcbnew.SHAPE_T_ARC, **geometry), _Pcbnew)

    assert changed != original


def test_circle_fingerprint_uses_center_and_radius_point_and_common_properties():
    circle = PCB_SHAPE(_Pcbnew.SHAPE_T_CIRCLE, start=(1_000, 2_000), end=(4_000, 6_000))

    state = _ground_plane_graphic_state(circle, _Pcbnew)

    assert state == (
        "PCB_SHAPE", "native-drawing", "Circle", 31,
        ((1_000, 2_000), (4_000, 6_000)), 125_000, 0, True, "GND",
    )
    assert state == _ground_plane_graphic_state(
        PCB_SHAPE(_Pcbnew.SHAPE_T_CIRCLE, start=(1_000, 2_000), end=(4_000, 6_000)), _Pcbnew,
    )


@pytest.mark.parametrize(
    "geometry",
    [
        {"start": (1_001, 2_000)},
        {"end": (4_000, 6_001)},
    ],
)
def test_circle_center_or_radius_point_change_changes_fingerprint(geometry):
    original = _ground_plane_graphic_state(
        PCB_SHAPE(_Pcbnew.SHAPE_T_CIRCLE, start=(1_000, 2_000), end=(4_000, 6_000)), _Pcbnew,
    )
    changed_geometry = {"start": (1_000, 2_000), "end": (4_000, 6_000)}
    changed_geometry.update(geometry)
    changed = _ground_plane_graphic_state(PCB_SHAPE(_Pcbnew.SHAPE_T_CIRCLE, **changed_geometry), _Pcbnew)

    assert changed != original


@pytest.mark.parametrize(
    "property_name,value",
    [
        ("uuid", "changed-uuid"),
        ("layer", 0),
        ("width", 126_000),
        ("fill", 1),
        ("locked", False),
        ("net", "SIGNAL"),
    ],
)
def test_arc_common_drawing_property_change_changes_fingerprint(property_name, value):
    original = _ground_plane_graphic_state(PCB_SHAPE(_Pcbnew.SHAPE_T_ARC), _Pcbnew)
    changed = _ground_plane_graphic_state(
        PCB_SHAPE(_Pcbnew.SHAPE_T_ARC, **{property_name: value}), _Pcbnew,
    )

    assert changed != original


def test_unknown_native_shape_fails_closed():
    unsupported = PCB_SHAPE(99)

    with pytest.raises(ValueError, match="does not support existing Bezier drawings"):
        _ground_plane_graphic_state(unsupported, _Pcbnew)
