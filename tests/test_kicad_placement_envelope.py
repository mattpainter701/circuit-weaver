"""Placement must enclose native geometry even with malformed courtyards."""

from types import SimpleNamespace

import pytest

from circuit_weaver._kicad_worker import _placement_envelope


def _box(extents):
    left, top, right, bottom = extents
    return SimpleNamespace(GetX=lambda: left, GetY=lambda: top,
                           GetRight=lambda: right, GetBottom=lambda: bottom)


@pytest.mark.parametrize("body,courtyard,expected", [
    ((-155, -255, 155, 255), (-127, -229, 127, 229), (-155, -255, 155, 255)),
    ((-100, -50, 100, 50), (-150, -100, 150, 100), (-150, -100, 150, 100)),
    ((-50, -200, 350, 100), (-100, -100, 200, 150), (-100, -200, 350, 150)),
    ((101, 201, 301, 401), (110, 210, 290, 390), (101, 201, 301, 401)),
    ((-401, -301, -201, -101), (-390, -290, -210, -110), (-401, -301, -201, -101)),
    ((-10, -20, 30, 40), None, (-10, -20, 30, 40)),
])
def test_envelope_never_shrinks_native_pads_body_or_authored_courtyard(body, courtyard, expected):
    calls = []

    def native_box(include_text, include_invisible_text):
        calls.append((include_text, include_invisible_text))
        return _box(body)

    def courtyard_box():
        assert courtyard is not None
        return _box(courtyard)

    footprint = SimpleNamespace(GetBoundingBox=native_box)
    polygon = SimpleNamespace(IsEmpty=lambda: courtyard is None, BBox=courtyard_box)
    assert _placement_envelope(footprint, polygon) == expected
    # Labels are positioned separately; distant or hidden fields cannot inflate
    # the physical placement envelope.
    assert calls == [(False, False)]


def test_unavailable_native_body_geometry_does_not_fall_back_to_a_small_courtyard():
    def unavailable(*_args):
        raise RuntimeError("native geometry unavailable")

    footprint = SimpleNamespace(GetBoundingBox=unavailable)
    polygon = SimpleNamespace(IsEmpty=lambda: False, BBox=lambda: _box((-1, -1, 1, 1)))
    with pytest.raises(RuntimeError, match="native geometry unavailable"):
        _placement_envelope(footprint, polygon)
