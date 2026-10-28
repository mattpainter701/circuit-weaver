"""Pure unit coverage for fail-closed native routing repair boundaries."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from circuit_weaver import _native_routing_repair as repair


class _TruncatingNativeUnits:
    @staticmethod
    def FromMM(value):
        return int(value * 1_000_000)

    @staticmethod
    def ToMM(value):
        return value * 1e-6

    @staticmethod
    def VECTOR2I(x, y):
        return x, y


@pytest.mark.parametrize("native_point", [
    (31_550_000, 73_123_456),
    (-31_550_000, -13_456_789),
    (1_234_567, -2_345_678),
    (1, -1),
])
def test_native_query_and_output_points_preserve_exact_source_iu(native_point):
    millimeters = tuple(_TruncatingNativeUnits.ToMM(value) for value in native_point)

    assert repair._native_point_vector(_TruncatingNativeUnits, millimeters) == native_point


def test_native_point_rounding_avoids_reproduced_one_iu_truncation():
    coordinate = _TruncatingNativeUnits.ToMM(31_550_000)
    assert _TruncatingNativeUnits.FromMM(coordinate) == 31_549_999
    assert _TruncatingNativeUnits.FromMM(-coordinate) == -31_549_999

    assert repair._native_point_vector(_TruncatingNativeUnits, (coordinate, -coordinate)) == (
        31_550_000, -31_550_000,
    )


class _Pad:
    def __init__(self, name="SIG", code=1, class_name="Default", uuid="pad-1"):
        self.name = name
        self.code = code
        self.class_name = class_name
        self.m_Uuid = SimpleNamespace(AsString=lambda: uuid)

    def GetNetname(self):
        return self.name

    def GetNetCode(self):
        return self.code

    def GetNetClassName(self):
        return self.class_name


class _Board:
    def __init__(self, path, pads=()):
        self.path = str(path)
        self.pads = list(pads)

    def GetFileName(self):
        return self.path

    def GetFootprints(self):
        return [SimpleNamespace(Pads=lambda pads=self.pads: pads)]

    def GetDesignSettings(self):
        return SimpleNamespace(
            m_TrackMinWidth=200_000,
            m_ViasMinSize=500_000,
            m_MinThroughDrill=300_000,
            m_MinClearance=100_000,
        )


class _Pcbnew:
    @staticmethod
    def ToMM(value):
        return value / 1_000_000


def _write_project(tmp_path, classes):
    board_path = tmp_path / "fixture.kicad_pcb"
    project_path = tmp_path / "fixture.kicad_pro"
    project_path.write_text(json.dumps({"net_settings": {"classes": classes}}), encoding="utf-8")
    return board_path


def _class_row(**changes):
    row = {
        "name": "Default",
        "track_width": 0.15,
        "via_diameter": 0.4,
        "via_drill": 0.2,
        "clearance": 0.08,
    }
    row.update(changes)
    return row


def test_class_rules_uses_native_class_and_maximum_board_minima(tmp_path):
    path = _write_project(tmp_path, [_class_row()])
    board = _Board(path, [_Pad()])

    assert repair._class_rules(board, _Pcbnew, "SIG") == (0.2, 0.5, 0.3, 0.1)


@pytest.mark.parametrize(
    "pads,classes,match",
    [
        ([_Pad(class_name="Missing")], [_class_row()], "unique project rule"),
        ([_Pad(class_name="Default"), _Pad(class_name="Other", uuid="pad-2")],
         [_class_row()], "inconsistent"),
        ([_Pad(class_name="Default+Other")], [_class_row()], "unique project rule"),
        ([_Pad()], [], "unique project rule"),
    ],
)
def test_class_rules_fails_closed_for_unknown_or_composite_classes(tmp_path, pads, classes, match):
    path = _write_project(tmp_path, classes)

    with pytest.raises(ValueError, match=match):
        repair._class_rules(_Board(path, pads), _Pcbnew, "SIG")


@pytest.mark.parametrize(
    "field,value",
    [
        ("track_width", True),
        ("via_diameter", 0),
        ("via_drill", "0.2"),
        ("clearance", float("nan")),
    ],
)
def test_class_rules_rejects_invalid_project_values(tmp_path, field, value):
    path = _write_project(tmp_path, [_class_row(**{field: value})])

    with pytest.raises(ValueError, match=field):
        repair._class_rules(_Board(path, [_Pad()]), _Pcbnew, "SIG")


class _RouteBoard:
    def __init__(self, items, zones=()):
        self.items = list(items)
        self.zones = list(zones)

    def Zones(self):
        return self.zones

    def GetTracks(self):
        return []

    def GetFootprints(self):
        return [SimpleNamespace(Pads=lambda: self.items)]


def _drc_pair(first, second, *, nets=None):
    entries = [
        {"uuid": first.m_Uuid.AsString(), "pos": {"x": 0, "y": 0}},
        {"uuid": second.m_Uuid.AsString(), "pos": {"x": 1, "y": 0}},
    ]
    if nets is not None:
        for entry, net in zip(entries, nets):
            entry["net"] = net
    return {"unconnected_items": [{"items": entries}]}


def _install_worker_stub(monkeypatch):
    monkeypatch.setitem(sys.modules, "_kicad_worker", SimpleNamespace())


def test_repair_rejects_zones_before_routing(monkeypatch):
    _install_worker_stub(monkeypatch)
    board = _RouteBoard([], zones=[object()])

    with pytest.raises(ValueError, match="without zones"):
        repair.repair_one(board, _Pcbnew, {})


@pytest.mark.parametrize("entries,match", [
    ([{"uuid": "absent-a"}, {"uuid": "absent-b"}], "UUID is absent"),
    ([{"uuid": "pad-1", "net": "WRONG"}, {"uuid": "pad-2", "net": "SIG"}], "net does not match"),
])
def test_repair_rejects_missing_uuid_or_report_net_mismatch(monkeypatch, entries, match):
    _install_worker_stub(monkeypatch)
    first, second = _Pad(uuid="pad-1"), _Pad(uuid="pad-2")
    board = _RouteBoard([first, second])

    entries = [{**entry, "pos": {"x": index, "y": 0}} for index, entry in enumerate(entries)]
    with pytest.raises(ValueError, match=match):
        repair.repair_one(board, _Pcbnew, {"unconnected_items": [{"items": entries}]})


def test_repair_rejects_actual_cross_net_pair(monkeypatch):
    _install_worker_stub(monkeypatch)
    first, second = _Pad("SIG", 1, uuid="pad-1"), _Pad("OTHER", 2, uuid="pad-2")
    board = _RouteBoard([first, second])

    with pytest.raises(ValueError, match="same positive native net"):
        repair.repair_one(board, _Pcbnew, _drc_pair(first, second))


def test_missing_report_net_is_accepted_after_native_uuid_and_net_validation(monkeypatch):
    _install_worker_stub(monkeypatch)
    first, second = _Pad(uuid="pad-1"), _Pad(uuid="pad-2")
    reached_rules = RuntimeError("pair validation passed")
    monkeypatch.setattr(repair, "_class_rules", lambda *_args: (_ for _ in ()).throw(reached_rules))

    with pytest.raises(RuntimeError, match="pair validation passed"):
        repair.repair_one(_RouteBoard([first, second]), _Pcbnew, _drc_pair(first, second))


def test_astar_accepts_a_clear_direct_goal_segment_from_any_goal_layer():
    checked = []

    def clear(start, goal, layer):
        checked.append((start, goal, layer))
        return True

    path, expanded = repair._astar_search(
        (0.0, 0.0), (8.0, 0.0), 2, {1}, {(0, 0, 1)},
        (0.0, 0.0, 8.0, 8.0), 0.25, 20, clear, lambda _point: False,
    )

    assert path == [(0, 0, 1)]
    assert expanded == 1
    assert checked == [((0.0, 0.0), (8.0, 0.0), 1)]


def test_astar_does_not_charge_stale_heap_entries_as_expansions(monkeypatch):
    push = repair.heapq.heappush

    def push_with_stale_entry(queue, entry):
        push(queue, entry)
        estimate, distance, sequence, node = entry
        push(queue, (estimate + 100.0, distance + 100.0, sequence + 1_000_000, node))

    monkeypatch.setattr(repair.heapq, "heappush", push_with_stale_entry)
    path, expanded = repair._astar_search(
        (0.0, 0.0), (10.0, 0.0), 1, set(), {(0, 0, 0)},
        (0.0, 0.0, 1.0, 0.0), 1.0, 10, lambda *_args: True, lambda _point: False,
    )

    assert path is None
    assert expanded == 2


def test_finer_grid_fallback_shares_one_expansion_budget(monkeypatch):
    calls = []

    def exhausted(_start, _goal, _layer_count, _target_layers, _origins, _bounds,
                  step, budget, _clear, _change):
        calls.append((step, budget))
        return None, budget

    monkeypatch.setattr(repair, "_astar_search", exhausted)
    path, step, expanded = repair._search_with_fallback(
        (0, 0), (1, 0), 1, {0}, {(0, 0, 0)}, (0, 0, 1, 1), 0.25, 10,
        lambda *_args: True, lambda _point: False,
    )

    assert path is None and step is None
    assert calls == [(0.25, 5), (0.125, 5)]
    assert expanded == 10


def test_fallback_finds_narrow_clearance_checked_corridor_unreachable_on_coarse_grid():
    start, goal = (0.0, 0.0), (1.0, 0.0)
    checked_segments = []

    def clear(a, b, _layer):
        checked_segments.append((a, b))
        crosses_blocker = min(a[0], b[0]) < 0.625 and max(a[0], b[0]) > 0.375
        stays_below_corridor = min(a[1], b[1]) < 0.1
        return not (crosses_blocker and stays_below_corridor)

    coarse_path, _ = repair._astar_search(
        start, goal, 1, {0}, {(0, 0, 0)}, (0.0, 0.0, 1.0, 0.125),
        0.25, 100, clear, lambda _point: False,
    )
    assert coarse_path is None

    path, step, expanded = repair._search_with_fallback(
        start, goal, 1, {0}, {(0, 0, 0)}, (0.0, 0.0, 1.0, 0.125),
        0.25, 200, clear, lambda _point: False,
    )

    assert path is not None
    assert step == 0.125
    assert expanded <= 200
    coordinates = [(start[0] + node[0] * step, start[1] + node[1] * step) for node in path]
    assert any(y == 0.125 for _, y in coordinates)
    assert all(clear(a, b, 0) for a, b in zip(coordinates, coordinates[1:]))
    assert clear(coordinates[-1], goal, 0)
    assert all(segment in checked_segments for segment in zip(coordinates, coordinates[1:]))


def test_collinear_collapse_preserves_turns_and_layer_transitions():
    points = [
        ((0.0, 0.0), 0), ((0.25, 0.0), 0), ((0.5, 0.0), 0),
        ((0.5, 0.25), 0), ((0.5, 0.25), 1), ((0.5, 0.5), 1),
    ]

    assert repair._collapse_collinear_points(points) == [
        ((0.0, 0.0), 0), ((0.5, 0.0), 0), ((0.5, 0.25), 0),
        ((0.5, 0.25), 1), ((0.5, 0.5), 1),
    ]
