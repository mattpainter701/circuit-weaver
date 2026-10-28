"""Pure unit coverage for fail-closed native routing repair boundaries."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from circuit_weaver import _native_routing_repair as repair


class _KiCad8Via:
    def __init__(self, width):
        self.width = width
        self.calls = []

    def GetWidth(self):
        self.calls.append(())
        return self.width


class _KiCad10Via:
    def __init__(self, width):
        self.width = width
        self.calls = []

    def GetWidth(self, layer):
        self.calls.append(layer)
        return self.width


def test_via_width_reader_supports_kicad8_noarg_binding():
    via = _KiCad8Via(600_000)

    assert repair._via_width_on_layer(via, 4) == 600_000
    assert via.calls == [()]


def test_via_width_reader_preserves_kicad10_layer_specific_binding():
    via = _KiCad10Via(600_000)

    assert repair._via_width_on_layer(via, 4) == 600_000
    assert via.calls == [4]


@pytest.mark.parametrize("width", [0, -1, True, float("nan"), float("inf"), 1.5])
def test_via_width_reader_rejects_invalid_native_width_without_coercion(width):
    via = _KiCad10Via(width)

    with pytest.raises(ValueError, match="valid positive integer"):
        repair._via_width_on_layer(via, 4)


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


def test_npth_keepouts_use_exact_native_drill_shape_and_board_hole_clearance():
    # Pad copper extents deliberately differ from the drill. Circle and slot
    # drills both arrive as KiCad's effective hole shape, without approximation.
    circle_hole, slot_hole = _HoleShape(), _HoleShape()
    circle = _DrilledPad(_PcbnewWithNPTH.PAD_ATTRIB_NPTH, circle_hole,
                         drill=(600_000, 600_000), copper_size=(4_000_000, 4_000_000))
    slot = _DrilledPad(_PcbnewWithNPTH.PAD_ATTRIB_NPTH, slot_hole,
                       drill=(1_200_000, 400_000), copper_size=(100_000, 100_000))
    plated = _DrilledPad(0, _HoleShape(collision=True))

    holes, clearance, hole_to_hole = repair._native_npth_hole_obstacles(
        _HoleBoard([circle, slot, plated]), _PcbnewWithNPTH,
    )
    candidate = object()

    assert holes == [circle_hole, slot_hole]
    assert clearance == 250_000
    assert hole_to_hole == 450_000
    assert repair._collides_with_npth_hole(candidate, holes, clearance) is False
    assert circle_hole.calls == [(candidate, 250_000)]
    assert slot_hole.calls == [(candidate, 250_000)]
    assert plated.GetEffectiveHoleShape().calls == []


def test_npth_collision_rejects_candidate_using_native_clearance_units():
    hole = _HoleShape(collision=True)
    candidate = object()

    assert repair._collides_with_npth_hole(candidate, [hole], 375_000) is True
    assert hole.calls == [(candidate, 375_000)]


def test_candidate_via_drill_uses_separate_hole_to_hole_clearance():
    npth_hole = _HoleShape(collision=True)
    candidate_via_drill = object()  # via.GetEffectiveHoleShape() native geometry

    assert repair._collides_with_npth_hole(candidate_via_drill, [npth_hole], 450_000) is True
    assert npth_hole.calls == [(candidate_via_drill, 450_000)]


def test_npth_spatial_bounds_expand_exact_drill_shape_by_hole_clearance():
    hole = _HoleShape()

    assert repair._native_hole_bbox_mm(hole, 250_000, _Pcbnew) == (0.75, 1.75, 4.25, 6.25)
    assert hole.bbox_clearance == 250_000


@pytest.mark.parametrize("clearance,hole_to_hole", [
    (None, 100_000), (True, 100_000), (-1, 100_000), (1.5, 100_000),
    (float("nan"), 100_000), (100_000, None), (100_000, True),
    (100_000, -1), (100_000, 1.5), (100_000, float("nan")),
])
def test_npth_keepouts_fail_closed_for_missing_or_invalid_hole_rules(clearance, hole_to_hole):
    hole = _HoleShape()
    board = _HoleBoard([_DrilledPad(_PcbnewWithNPTH.PAD_ATTRIB_NPTH, hole)], clearance, hole_to_hole)

    with pytest.raises(ValueError, match="hole-clearance rule is unavailable or invalid"):
        repair._native_npth_hole_obstacles(board, _PcbnewWithNPTH)


def test_npth_keepouts_fail_closed_when_native_drill_shape_is_unavailable():
    class _NoHoleGeometryPad(_DrilledPad):
        GetEffectiveHoleShape = None

    board = _HoleBoard([
        _NoHoleGeometryPad(_PcbnewWithNPTH.PAD_ATTRIB_NPTH, _HoleShape()),
    ])

    with pytest.raises(ValueError, match="native NPTH drill geometry is unavailable"):
        repair._native_npth_hole_obstacles(board, _PcbnewWithNPTH)


@pytest.mark.parametrize("drill", [
    (0, 0), (0, 400_000), (-1, 400_000), (True, 400_000), (1.5, 400_000),
])
def test_npth_keepouts_fail_closed_for_malformed_native_drill_dimensions(drill):
    board = _HoleBoard([
        _DrilledPad(_PcbnewWithNPTH.PAD_ATTRIB_NPTH, _HoleShape(), drill=drill),
    ])

    with pytest.raises(ValueError, match="native NPTH drill geometry is unavailable"):
        repair._native_npth_hole_obstacles(board, _PcbnewWithNPTH)


def test_repair_one_routes_candidate_via_checks_with_native_hole_rules(monkeypatch, tmp_path):
    class _Vector:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class _Box:
        def __init__(self, x, y, width, height):
            self.x, self.y, self.width, self.height = x, y, width, height

        def GetX(self): return self.x
        def GetY(self): return self.y
        def GetWidth(self): return self.width
        def GetHeight(self): return self.height

    class _Shape:
        def Collide(self, _candidate, clearance_iu):
            return clearance_iu >= 450_000

    class _Hole(_Shape):
        def BBox(self, clearance_iu):
            assert clearance_iu == 450_000
            return _Box(9_550_000, 9_550_000, 900_000, 900_000)

    class PAD:
        def __init__(self, uuid, position, *, npth=False, layer_ids=(1, 2), hole=None):
            self.m_Uuid = SimpleNamespace(AsString=lambda: uuid)
            self.position = _Vector(*position)
            self.npth = npth
            self.layer_ids = set(layer_ids)
            self.hole = hole

        def GetNetCode(self): return 0 if self.npth else 1
        def GetNetname(self): return "" if self.npth else "SIG"
        def GetPosition(self): return self.position
        def GetAttribute(self): return 3 if self.npth else 0
        def GetDrillSize(self): return _Vector(600_000, 400_000) if self.npth else _Vector(0, 0)
        def GetEffectiveHoleShape(self): return self.hole
        def GetBoundingBox(self): return _Box(self.position.x - 100_000, self.position.y - 100_000,
                                              200_000, 200_000)
        def GetEffectiveShape(self, *_args): return _Shape()
        def IsOnLayer(self, layer_id): return layer_id in self.layer_ids

    class _Field:
        def IsVisible(self): return False
        def GetLayer(self): return 0

    class _Footprint:
        def __init__(self, pads): self.pads = pads
        def Pads(self): return self.pads
        def Reference(self): return _Field()
        def Value(self): return _Field()
        def GraphicalItems(self): return []

    class _Board:
        def __init__(self, pads): self.footprint = _Footprint(pads)
        def Zones(self): return []
        def GetTracks(self): return []
        def GetFootprints(self): return [self.footprint]
        def GetDrawings(self): return []
        def GetDesignSettings(self):
            return SimpleNamespace(m_HoleClearance=100_000, m_HoleToHoleMin=450_000)
        def BuildConnectivity(self): pass

    class _Track:
        def __init__(self, _board): self.shape = _Shape()
        def SetStart(self, _value): pass
        def SetEnd(self, _value): pass
        def SetWidth(self, _value): pass
        def SetLayer(self, _value): pass
        def SetNetCode(self, _value): pass
        def GetEffectiveShape(self): return self.shape

    class _Via:
        def __init__(self, _board): self.drill_shape = _Shape()
        def SetViaType(self, _value): pass
        def SetLayerPair(self, *_values): pass
        def SetWidth(self, _value): pass
        def SetDrill(self, value):
            assert isinstance(value, int) and value == 300_000
        def SetNetCode(self, _value): pass
        def SetPosition(self, _value): pass
        def GetWidth(self, _layer): return 600_000
        def GetEffectiveShape(self, _layer): return _Shape()
        def GetEffectiveHoleShape(self): return self.drill_shape

    class _Native(_PcbnewWithNPTH):
        F_Cu, B_Cu, Edge_Cuts = 1, 2, 44
        VIATYPE_THROUGH = 0
        VECTOR2I = staticmethod(lambda x, y: (x, y))
        PCB_TRACK = _Track
        PCB_VIA = _Via

    first = PAD("first", (9_000_000, 10_000_000), layer_ids=(1,))
    last = PAD("last", (11_000_000, 10_000_000), layer_ids=(2,))
    npth = PAD("npth", (10_000_000, 10_000_000), npth=True, hole=_Hole())
    board = _Board([first, last, npth])
    worker = SimpleNamespace(
        _native_copper_layer_stack=lambda *_args: ([1, 2], True),
        _rectangular_outline_bounds_iu=lambda *_args: (0, 0, 100_000_000, 100_000_000),
    )
    rules = SimpleNamespace(native_edge_clearance=lambda *_args: {"value_iu": 0})
    monkeypatch.setitem(sys.modules, "_kicad_worker", worker)
    monkeypatch.setitem(sys.modules, "_native_board_rules", rules)
    monkeypatch.setattr(repair, "_class_rules", lambda *_args: (0.2, 0.6, 0.3, 0.1))

    def search(start, goal, _layers, _targets, _origins, _bounds, _step, _budget, _clear, can_change):
        # Candidate via copper clears m_HoleClearance; its drill must still
        # reject against the same NPTH using the larger hole-to-hole rule.
        assert can_change((10.0, 10.0)) is False
        return None, None, 1, start, goal, False

    monkeypatch.setattr(repair, "_search_with_fallback", search)
    report = {"unconnected_items": [{"items": [
        {"uuid": "first", "pos": {"x": 9, "y": 10}},
        {"uuid": "last", "pos": {"x": 11, "y": 10}},
    ]}]}

    with pytest.raises(RuntimeError, match="no collision-free repair"):
        repair.repair_one(board, _Native, report)


@pytest.mark.parametrize("width_iu,edge_iu,expected", [
    (200_000, 500_000, 600_000),
    (600_000, 500_000, 800_000),
    (200_001, 500_000, 600_001),
    (0, 0, 0),
])
def test_edge_search_margin_adds_edge_clearance_to_copper_radius(width_iu, edge_iu, expected):
    assert repair._edge_margin_iu(width_iu, edge_iu) == expected


@pytest.mark.parametrize("width_iu,edge_iu", [(True, 1), (-1, 1), (1, -1), (1.5, 1)])
def test_edge_search_margin_rejects_invalid_native_constraints(width_iu, edge_iu):
    with pytest.raises(ValueError, match="native IU integer"):
        repair._edge_margin_iu(width_iu, edge_iu)


def test_segment_edge_check_uses_track_and_via_specific_margin():
    outline = (0, 0, 10_000_000, 10_000_000)
    track_margin = repair._edge_margin_iu(200_000, 500_000)
    via_margin = repair._edge_margin_iu(600_000, 500_000)

    assert repair._segment_inside_edge_margin((600_000, 600_000), (9_400_000, 600_000), outline, track_margin)
    assert not repair._segment_inside_edge_margin((599_999, 600_000), (9_400_000, 600_000), outline, track_margin)
    assert repair._segment_inside_edge_margin((800_000, 800_000), (9_200_000, 800_000), outline, via_margin)
    assert not repair._segment_inside_edge_margin((799_999, 800_000), (9_200_000, 800_000), outline, via_margin)


def test_track_search_can_use_wire_only_strip_outside_via_margin():
    outline = (0, 0, 10_000_000, 10_000_000)
    track_margin = repair._edge_margin_iu(200_000, 500_000)
    via_margin = repair._edge_margin_iu(600_000, 500_000)

    assert track_margin == 600_000
    assert via_margin == 800_000
    assert repair._segment_inside_edge_margin((600_000, 1_000_000), (2_000_000, 1_000_000), outline, track_margin)
    assert not repair._segment_inside_edge_margin((600_000, 1_000_000), (2_000_000, 1_000_000), outline, via_margin)


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
            m_CopperEdgeClearance=500_000,
        )


class _Pcbnew:
    @staticmethod
    def ToMM(value):
        return value / 1_000_000

    @staticmethod
    def FromMM(value):
        return int(round(value * 1_000_000))


class _HoleShape:
    def __init__(self, collision=False):
        self.collision = collision
        self.calls = []

    def Collide(self, candidate, clearance_iu):
        self.calls.append((candidate, clearance_iu))
        return self.collision

    def BBox(self, clearance_iu):
        self.bbox_clearance = clearance_iu
        return SimpleNamespace(GetX=lambda: 750_000, GetY=lambda: 1_750_000,
                               GetWidth=lambda: 3_500_000, GetHeight=lambda: 4_500_000)


class _DrilledPad:
    def __init__(self, attribute, hole_shape, *, drill=(600_000, 400_000), copper_size=(1, 1)):
        self.attribute = attribute
        self.hole_shape = hole_shape
        self.drill = SimpleNamespace(x=drill[0], y=drill[1])
        self.copper_size = copper_size

    def GetAttribute(self):
        return self.attribute

    def GetDrillSize(self):
        return self.drill

    def GetEffectiveHoleShape(self):
        return self.hole_shape


class _HoleBoard:
    def __init__(self, pads, clearance=250_000, hole_to_hole=450_000):
        self.pads = list(pads)
        self.clearance = clearance
        self.hole_to_hole = hole_to_hole

    def GetFootprints(self):
        return [SimpleNamespace(Pads=lambda: self.pads)]

    def GetDesignSettings(self):
        return SimpleNamespace(m_HoleClearance=self.clearance, m_HoleToHoleMin=self.hole_to_hole)


class _PcbnewWithNPTH(_Pcbnew):
    PAD_ATTRIB_NPTH = 3


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


class _RulesRouteBoard(_RouteBoard):
    def __init__(self, path, items):
        super().__init__(items)
        self.path = str(path)

    def GetFileName(self):
        return self.path

    def GetDesignSettings(self):
        return SimpleNamespace(
            m_TrackMinWidth=200_000,
            m_ViasMinSize=500_000,
            m_MinThroughDrill=300_000,
            m_MinClearance=100_000,
            m_CopperEdgeClearance=500_000,
        )


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


def test_native_repair_rejects_unsupported_custom_edge_rule_before_geometry(monkeypatch, tmp_path):
    _install_worker_stub(monkeypatch)
    first, second = _Pad(uuid="pad-1"), _Pad(uuid="pad-2")
    path = _write_project(tmp_path, [_class_row()])
    path.with_suffix(".kicad_dru").write_text(
        '(rule "edge" (constraint edge_clearance (min 0.8mm)))\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="custom KiCad edge_clearance rules"):
        repair.repair_one(_RulesRouteBoard(path, [first, second]), _Pcbnew,
                          _drc_pair(first, second))


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
    path, step, expanded, *_ = repair._search_with_fallback(
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

    path, step, expanded, *_ = repair._search_with_fallback(
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


def test_long_span_coarse_pass_is_quarter_capped_and_finer_passes_share_actual_remainder(monkeypatch):
    calls = []

    def bounded_search(_start, _goal, _layer_count, _target_layers, _origins, _bounds,
                       step, budget, _clear, _change):
        calls.append((step, budget))
        used = {0.5: 20, 0.25: 10, 0.125: budget}[step]
        return None, used

    monkeypatch.setattr(repair, "_astar_search", bounded_search)
    path, step, expanded, *_ = repair._search_with_fallback(
        (0.0, 0.0), (100.0, 0.0), 2, {1}, {(0, 0, 1)},
        (0.0, 0.0, 100.0, 1.0), 0.25, 100, lambda *_args: True, lambda _point: False,
    )

    assert path is None and step is None
    assert calls == [(0.5, 25), (0.5, 25), (0.25, 30), (0.125, 50)]
    assert expanded == 100


def test_long_span_coarse_pass_can_return_candidate_within_quarter_budget(monkeypatch):
    calls = []

    def coarse_success(_start, _goal, _layer_count, _target_layers, _origins, _bounds,
                       step, budget, _clear, _change):
        calls.append((step, budget))
        return [(0, 0, 1)], 12

    monkeypatch.setattr(repair, "_astar_search", coarse_success)
    path, step, expanded, *_ = repair._search_with_fallback(
        (0.0, 0.0), (100.0, 0.0), 2, {1}, {(0, 0, 1)},
        (0.0, 0.0, 100.0, 1.0), 0.25, 100, lambda *_args: True, lambda _point: False,
    )

    assert path == [(0, 0, 1)]
    assert step == 0.5
    assert expanded == 12
    assert calls == [(0.5, 25)]


def _segment_hits_rect(a, b, rect):
    xmin, ymin, xmax, ymax = rect
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - xmin), (dx, xmax - a[0]),
                  (-dy, a[1] - ymin), (dy, ymax - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def _long_route_maze():
    height, xmax = 100.0, 115.8
    bounds = (0.0, 0.0, xmax, height)
    rectangles = []
    x, index = 3.0, 0
    while x < xmax - 1.0:
        center = height / 2 + (-3.5 if index % 2 == 0 else 3.5)
        lo, hi = center - 2.0, center + 2.0
        rectangles.extend(((x - 0.1, -0.1, x + 0.6, lo + 0.1),
                           (x - 0.1, hi - 0.1, x + 0.6, height + 0.1)))
        x += 3.0
        index += 1
    start, goal = (0.5, 50.0), (115.3, 50.0)

    def clear(a, b, _layer):
        return (bounds[0] <= a[0] <= bounds[2] and bounds[1] <= a[1] <= bounds[3]
                and bounds[0] <= b[0] <= bounds[2] and bounds[1] <= b[1] <= bounds[3]
                and not any(_segment_hits_rect(a, b, rect) for rect in rectangles))

    return start, goal, bounds, clear


def test_long_route_maze_uses_coarse_pass_under_total_budget():
    import math

    start, goal, bounds, clear = _long_route_maze()
    fine_path, fine_step, fine_used, *_ = repair._search_with_fallback(
        start, goal, 1, {0}, {(0, 0, 0)}, bounds, 0.25, 150_000, clear, lambda _point: False,
    )
    assert fine_path is not None
    assert fine_step == 0.5
    assert 0 < fine_used < 37_500
    coordinates = [(start[0] + node[0] * fine_step, start[1] + node[1] * fine_step)
                   for node in fine_path]
    assert all(clear(a, b, 0) for a, b in zip(coordinates, coordinates[1:]))
    assert clear(coordinates[-1], goal, 0)
    length = sum(math.dist(a, b) for a, b in zip(coordinates, coordinates[1:])) + math.dist(coordinates[-1], goal)
    assert length > math.dist(start, goal) + 50.0


def test_long_span_coarse_trap_falls_back_to_requested_fine_grid():
    start, goal = (0.5, 0.0), (65.5, 0.0)
    bounds = (0.0, 0.0, 66.0, 0.25)
    blocker = (30.0, -0.1, 35.0, 0.2)

    def clear(a, b, _layer):
        return (bounds[0] <= a[0] <= bounds[2] and bounds[1] <= a[1] <= bounds[3]
                and bounds[0] <= b[0] <= bounds[2] and bounds[1] <= b[1] <= bounds[3]
                and not _segment_hits_rect(a, b, blocker))

    path, step, expanded, *_ = repair._search_with_fallback(
        start, goal, 1, {0}, {(0, 0, 0)}, bounds, 0.25, 1000, clear, lambda _point: False,
    )

    assert path is not None
    assert step == 0.25
    assert 0 < expanded <= 1000
    points = [(start[0] + node[0] * step, start[1] + node[1] * step) for node in path]
    assert all(clear(a, b, 0) for a, b in zip(points, points[1:]))
    assert clear(points[-1], goal, 0)


def test_reverse_coarse_swaps_layers_and_preserves_off_grid_physical_endpoints(monkeypatch):
    calls = []
    start, goal = (-0.137, 1.019), (46.241, 3.087)

    def search(a, b, layers, targets, origins, bounds, step, budget, clear, change):
        calls.append((a, b, targets, origins, step, budget))
        return (None, 20) if len(calls) == 1 else ([(0, 0, 1), (-1, 0, 1), (-1, 0, 0)], 12)

    monkeypatch.setattr(repair, "_astar_search", search)
    path, step, used, origin, target, reverse = repair._search_with_fallback(
        start, goal, 2, {1}, {(0, 0, 0)}, (-1, 0, 50, 5), .25, 100,
        lambda *_: True, lambda _: True,
    )
    assert reverse and used == 32 and origin == goal and target == start
    assert calls[1] == (goal, start, {0}, {(0, 0, 1)}, .5, 25)
    points = repair._physical_search_points(path, step, origin, target, reverse, [0, 2])
    assert points[0] == (start, 0) and points[-1] == (goal, 2)
    assert ((goal[0] - .5, goal[1]), 0) in points
    assert ((goal[0] - .5, goal[1]), 2) in points


def test_medium_span_threshold_and_shared_cap_do_not_claim_impossible_route(monkeypatch):
    calls = []

    def exhausted(a, b, layers, targets, origins, bounds, step, budget, clear, change):
        calls.append((step, budget, a))
        return None, budget

    monkeypatch.setattr(repair, "_astar_search", exhausted)
    result = repair._search_with_fallback(
        (0, 0), (46, 0), 1, {0}, {(0, 0, 0)}, (0, 0, 46, 2), .25, 150000,
        lambda *_: False, lambda _: False,
    )
    assert result[0] is None and result[2] == 150000
    assert calls == [(.5, 37500, (0, 0)), (.5, 37500, (46, 0)),
                     (.25, 37500, (0, 0)), (.125, 37500, (0, 0))]


def test_medium_coarse_real_detour_and_impossible_pocket():
    start, goal = (0.125, .125), (46.125, .125)
    wall = (20, -1, 21, 2)
    def clear(a, b, _layer):
        return not _segment_hits_rect(a, b, wall)
    path, step, used, origin, target, reverse = repair._search_with_fallback(
        start, goal, 1, {0}, {(0, 0, 0)}, (0, 0, 47, 4), .25, 150000,
        clear, lambda _: False,
    )
    assert path is not None and step == .5 and used <= 37500 and not reverse
    points = repair._physical_search_points(path, step, origin, target, reverse, [0])
    assert all(clear(a, b, 0) for (a, _), (b, _) in zip(points, points[1:]))
    trapped = repair._search_with_fallback(
        start, goal, 1, {0}, {(0, 0, 0)}, (0, 0, 47, 4), .25, 100,
        lambda a, b, _: a == b, lambda _: False,
    )
    assert trapped[0] is None and trapped[2] <= 100
