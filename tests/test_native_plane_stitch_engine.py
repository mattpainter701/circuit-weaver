"""Fail-closed boundary tests for native thermal-plane stitching."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from circuit_weaver import _native_plane_stitch as stitch


class _Uuid:
    def __init__(self, value: str):
        self.value = value

    def AsString(self):
        return self.value


class _FilledPolygons:
    def OutlineCount(self):
        return 1

    def Outline(self, _index):
        return SimpleNamespace(Area=lambda: 10_000_000_000_000)

    def HoleCount(self, _index):
        return 0


class _Pad:
    def __init__(self, *, uuid="pad-1", net="GND", net_code=1, on_layer=True):
        self.m_Uuid = _Uuid(uuid)
        self.net = net
        self.net_code = net_code
        self.on_layer = on_layer

    def GetNetCode(self):
        return self.net_code

    def GetNetname(self):
        return self.net

    def IsOnLayer(self, _layer):
        return self.on_layer


class _Zone:
    def __init__(self, *, uuid="zone-1", net="GND", net_code=1, layer=4,
                 rule_area=False, filled=True):
        self.m_Uuid = _Uuid(uuid)
        self.net = net
        self.net_code = net_code
        self.layer = layer
        self.rule_area = rule_area
        self.filled = filled

    def GetNetCode(self):
        return self.net_code

    def GetNetname(self):
        return self.net

    def GetLayer(self):
        return self.layer

    def GetIsRuleArea(self):
        return self.rule_area

    def IsFilled(self):
        return self.filled

    def HasFilledPolysForLayer(self, _layer):
        return self.filled

    def GetFilledPolysList(self, _layer):
        return _FilledPolygons()


class _Board:
    def __init__(self, pads=(), zones=()):
        self.pads = list(pads)
        self.zones = list(zones)
        self.added = []

    def GetFootprints(self):
        return [SimpleNamespace(Pads=lambda: self.pads)]

    def Zones(self):
        return self.zones

    def GetLayerName(self, layer):
        return {0: "F.Cu", 2: "B.Cu", 4: "In1.Cu", 5: "F.SilkS", 6: "In2.Cu"}[int(layer)]

    def Add(self, item):
        self.added.append(item)


def _row(pad="pad-1", zone="zone-1", **changes):
    row = {
        "type": "starved_thermal",
        "severity": "error",
        "items": [{"uuid": pad}, {"uuid": zone}],
    }
    row.update(changes)
    return row


def _small_native_case(*, pad=None, zone=None):
    pad = pad or _Pad()
    zone = zone or _Zone()
    return _Board([pad], [zone]), pad, zone


@pytest.mark.parametrize(
    "violations,match",
    [
        (None, "violations must be a list"),
        ([], "contains no resolvable"),
        ([None], "only native thermal error rows"),
        ([{"type": "clearance", "severity": "error", "items": []}], "only native thermal error rows"),
        ([{"type": "starved_thermal", "severity": "warning", "items": []}], "only native thermal error rows"),
        ([_row(items=[{"uuid": "pad-1"}])], "exactly one pad and one zone"),
        ([_row(pad="stale-pad")], "do not map to one native pad and zone"),
    ],
)
def test_rejects_malformed_stale_or_nonthermal_native_report_without_mutation(violations, match):
    board, _, _ = _small_native_case()

    with pytest.raises(ValueError, match=match):
        stitch.repair(board, object(), violations)

    assert board.added == []


@pytest.mark.parametrize(
    "pad,zone,match",
    [
        (_Pad(net="VCC", net_code=2), _Zone(), "same-positive-net pad and copper zone"),
        (_Pad(net="GND", net_code=2), _Zone(net_code=1), "same-positive-net pad and copper zone"),
        (_Pad(on_layer=False), _Zone(), "same-positive-net pad and copper zone"),
        (_Pad(), _Zone(layer=5), "same-positive-net pad and copper zone"),
        (_Pad(), _Zone(rule_area=True), "rule-area zone"),
        (_Pad(), _Zone(filled=False), "without current native fill"),
    ],
)
def test_rejects_report_uuid_that_no_longer_matches_native_net_or_zone(pad, zone, match):
    board, _, _ = _small_native_case(pad=pad, zone=zone)

    with pytest.raises(ValueError, match=match):
        stitch.repair(board, object(), [_row()])

    assert board.added == []


def test_rejects_duplicate_native_pad_zone_pair():
    board, _, _ = _small_native_case()

    with pytest.raises(ValueError, match="pairs must be unique"):
        stitch.repair(board, object(), [_row(), _row()])

    assert board.added == []


def test_rejects_more_than_bounded_native_thermal_rows_before_board_access():
    board, _, _ = _small_native_case()
    board.GetFootprints = lambda: pytest.fail("oversized report must be rejected before board traversal")

    with pytest.raises(ValueError, match="bounded native target count"):
        stitch.repair(board, object(), [_row() for _ in range(17)])

    assert board.added == []


@pytest.mark.parametrize("duplicate_kind", ["pad", "zone"])
def test_rejects_duplicate_native_item_uuids(duplicate_kind):
    first_pad, second_pad = _Pad(uuid="pad-1"), _Pad(uuid="pad-1")
    first_zone, second_zone = _Zone(uuid="zone-1"), _Zone(uuid="zone-1")
    board = _Board([first_pad, second_pad] if duplicate_kind == "pad" else [first_pad],
                   [first_zone, second_zone] if duplicate_kind == "zone" else [first_zone])

    with pytest.raises(ValueError, match="UUIDs are not unique"):
        stitch.repair(board, object(), [_row()])

    assert board.added == []


def test_rejects_non_gnd_target_net_before_planning_candidate():
    board, _, _ = _small_native_case(pad=_Pad(net="VCC"), zone=_Zone(net="VCC"))

    with pytest.raises(ValueError, match="restricted to the native GND"):
        stitch.repair(board, object(), [_row()])

    assert board.added == []


def test_rejects_unsupported_copper_stack_before_candidate_mutation(monkeypatch):
    board, _, _ = _small_native_case()
    board.GetEnabledLayers = lambda: SimpleNamespace(CuStack=lambda: [0, 2])
    board.GetDesignSettings = lambda: SimpleNamespace(GetCopperLayerCount=lambda: 2)
    monkeypatch.setattr(stitch, "_class_rules", lambda *_args: (0.25, 0.6, 0.3, 0.2))
    pcbnew = SimpleNamespace(ToMM=lambda value: value / 1_000_000)

    with pytest.raises(ValueError, match="only a verified four-layer"):
        stitch.repair(board, pcbnew, [_row()])

    assert board.added == []


def test_rejects_board_outline_too_small_for_rule_sized_via(monkeypatch):
    board, _, _ = _small_native_case()
    stack = [0, 4, 6, 2]
    board.GetEnabledLayers = lambda: SimpleNamespace(CuStack=lambda: stack)
    board.GetDesignSettings = lambda: SimpleNamespace(
        GetCopperLayerCount=lambda: 4,
        m_MinClearance=200_000,
        m_HoleToHoleMin=250_000,
        m_HoleClearance=200_000,
        m_SilkClearance=200_000,
        m_CopperEdgeClearance=200_000,
    )
    monkeypatch.setattr(stitch, "_class_rules", lambda *_args: (0.25, 0.6, 0.3, 0.2))
    monkeypatch.setattr(stitch, "_outline_bounds", lambda *_args: (0.0, 0.0, 0.5, 0.5))
    monkeypatch.setattr(stitch, "_validate_stack", lambda *_args: (stack, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]))
    pcbnew = SimpleNamespace(ToMM=lambda value: value / 1_000_000)

    with pytest.raises(ValueError, match="no legal stitching-via placement area"):
        stitch.repair(board, pcbnew, [_row()])

    assert board.added == []


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"max_vias": 0}, "max_vias must be an integer"),
        ({"max_vias": 17}, "max_vias must be an integer"),
        ({"search_radius_mm": float("nan")}, "search_radius_mm must be finite"),
        ({"grid_step_mm": 0.01}, "grid_step_mm must be finite"),
    ],
)
def test_rejects_unbounded_via_search_options(kwargs, match):
    board, _, _ = _small_native_case()

    with pytest.raises(ValueError, match=match):
        stitch.repair(board, object(), [_row()], **kwargs)

    assert board.added == []
