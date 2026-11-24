from __future__ import annotations

from types import SimpleNamespace

import pytest

from circuit_weaver import _native_routing_repair as repair


class _FakePcbnew:
    F_SilkS = 31
    B_SilkS = 32
    F_Cu = 0
    B_Cu = 1
    F_Mask = 40
    B_Mask = 41

    @staticmethod
    def FromMM(value):
        return int(round(value * 1_000_000))

    @staticmethod
    def ToMM(value):
        return value / 1_000_000

    @staticmethod
    def VECTOR2I(x, y):
        return (x, y)


class _FakeSilkShape:
    def __init__(self, collision):
        self.collision = collision
        self.calls = []

    def Collide(self, via_shape, clearance_iu):
        self.calls.append((via_shape, clearance_iu))
        return self.collision


class _ViaQuery:
    def __init__(self, tented_layers=()):
        self.position = None
        self.layer_calls = []
        self.tented_layers = set(tented_layers)

    def SetPosition(self, point):
        self.position = point

    def GetEffectiveShape(self, copper_layer):
        shape = SimpleNamespace(layer=copper_layer)
        self.layer_calls.append(shape)
        return shape

    def IsTented(self, mask_layer):
        return mask_layer in self.tented_layers


def test_native_silkscreen_obstacles_keep_exact_shape_and_layer_mapping():
    front_shape, back_shape = _FakeSilkShape(False), _FakeSilkShape(False)

    class _Item:
        def __init__(self, layer, shape, visible=True):
            self.layer = layer
            self.shape = shape
            self.visible = visible

        def IsVisible(self):
            return self.visible

        def GetLayer(self):
            return self.layer

        def GetEffectiveShape(self):
            return self.shape

    class _Footprint:
        def Reference(self):
            return _Item(_FakePcbnew.F_SilkS, front_shape)

        def Value(self):
            return _Item(_FakePcbnew.B_SilkS, _FakeSilkShape(False), visible=False)

        def GraphicalItems(self):
            return [_Item(_FakePcbnew.B_SilkS, back_shape), _Item(99, _FakeSilkShape(False))]

    board = SimpleNamespace(
        GetDesignSettings=lambda: SimpleNamespace(m_SilkClearance=200_000),
        GetFootprints=lambda: [_Footprint()],
    )
    obstacles, clearance = repair._native_silkscreen_obstacles(
        board, _FakePcbnew, lambda _item: (0.0, 0.0, 1.0, 1.0)
    )

    assert clearance == 200_000
    assert [(layer, shape) for layer, _box, shape in obstacles] == [
        (_FakePcbnew.F_Cu, front_shape), (_FakePcbnew.B_Cu, back_shape)
    ]


def test_via_obstacle_index_keeps_boundary_and_large_envelopes():
    nearby = repair._via_obstacle_lookup([
        ((-2.1, -0.1, 0, 2), "crosses zero"),
        ((4, 0, 4.5, 1), "separate"),
        ((-200, -200, 200, 200), "large silk"),
    ], 2)
    for point in [(-2.1, -0.1), (0, 2)]:
        assert set(nearby(point)) == {"crosses zero", "large silk"}
    assert set(nearby((4, 0))) == {"separate", "large silk"}


def test_silkscreen_bbox_is_only_broad_phase_and_exact_shape_decides():
    silk = _FakeSilkShape(False)
    via = _ViaQuery()
    obstacles = [(_FakePcbnew.F_Cu, (0.0, 0.0, 10.0, 10.0), silk)]

    # Point lies inside a diagonal drawing's large bbox but native exact geometry
    # says the via does not collide with the stroke.
    assert not repair._collides_with_silkscreen(
        (5.0, 5.0), via, obstacles, 0.6, 200_000, _FakePcbnew
    )
    assert silk.calls == [(SimpleNamespace(layer=_FakePcbnew.F_Cu), 200_000)]
    assert via.position == (5_000_000, 5_000_000)


def test_silkscreen_exact_collision_blocks_candidate_via():
    silk = _FakeSilkShape(True)
    via = _ViaQuery()
    obstacles = [(_FakePcbnew.B_Cu, (0.0, 0.0, 2.0, 2.0), silk)]

    assert repair._collides_with_silkscreen(
        (1.0, 1.0), via, obstacles, 0.6, 150_000, _FakePcbnew
    )
    assert silk.calls[0][1] == 150_000
    assert via.layer_calls[0].layer == _FakePcbnew.B_Cu


def test_tented_via_side_does_not_false_block_on_silkscreen():
    silk = _FakeSilkShape(True)
    via = _ViaQuery(tented_layers={_FakePcbnew.F_Mask})
    obstacles = [(_FakePcbnew.F_Cu, (0.0, 0.0, 2.0, 2.0), silk)]

    assert not repair._collides_with_silkscreen(
        (1.0, 1.0), via, obstacles, 0.6, 150_000, _FakePcbnew
    )
    assert silk.calls == []
    assert via.layer_calls == []


def test_exposed_via_side_still_checks_silkscreen_when_opposite_side_is_tented():
    silk = _FakeSilkShape(True)
    via = _ViaQuery(tented_layers={_FakePcbnew.F_Mask})
    obstacles = [(_FakePcbnew.B_Cu, (0.0, 0.0, 2.0, 2.0), silk)]

    assert repair._collides_with_silkscreen(
        (1.0, 1.0), via, obstacles, 0.6, 150_000, _FakePcbnew
    )
    assert silk.calls[0][1] == 150_000
    assert via.layer_calls[0].layer == _FakePcbnew.B_Cu


def test_silkscreen_collision_fails_closed_when_via_tenting_is_unavailable():
    class _NoTentingQuery:
        def SetPosition(self, _point):
            pass

    with pytest.raises(ValueError, match="via tenting intent is unavailable"):
        repair._collides_with_silkscreen(
            (1.0, 1.0), _NoTentingQuery(),
            [(_FakePcbnew.F_Cu, (0.0, 0.0, 2.0, 2.0), _FakeSilkShape(True))],
            0.6, 150_000, _FakePcbnew,
        )


def test_silkscreen_broad_phase_skips_distant_shapes():
    silk = _FakeSilkShape(True)
    via = _ViaQuery()
    obstacles = [(_FakePcbnew.F_Cu, (10.0, 10.0, 12.0, 12.0), silk)]

    assert not repair._collides_with_silkscreen(
        (0.0, 0.0), via, obstacles, 0.6, 200_000, _FakePcbnew
    )
    assert silk.calls == []
    assert via.layer_calls == []


@pytest.mark.parametrize("bad_clearance", [None, True, -1, 10.5, float("nan")])
def test_silkscreen_obstacles_fail_closed_for_invalid_native_clearance(bad_clearance):
    board = SimpleNamespace(
        GetDesignSettings=lambda: SimpleNamespace(m_SilkClearance=bad_clearance),
        GetFootprints=lambda: [],
    )

    with pytest.raises(ValueError, match="silkscreen clearance is unavailable or invalid"):
        repair._native_silkscreen_obstacles(board, _FakePcbnew, lambda _item: (0, 0, 0, 0))


def test_silkscreen_obstacles_fail_closed_when_effective_shape_is_missing():
    class _Item:
        def IsVisible(self):
            return True

        def GetLayer(self):
            return _FakePcbnew.F_SilkS

        def GetEffectiveShape(self):
            return None

    class _Footprint:
        def Reference(self):
            return _Item()

        def Value(self):
            return SimpleNamespace(IsVisible=lambda: False)

        def GraphicalItems(self):
            return []

    board = SimpleNamespace(
        GetDesignSettings=lambda: SimpleNamespace(m_SilkClearance=200_000),
        GetFootprints=lambda: [_Footprint()],
    )

    with pytest.raises(ValueError, match="effective shape is unavailable"):
        repair._native_silkscreen_obstacles(board, _FakePcbnew, lambda _item: (0, 0, 1, 1))
