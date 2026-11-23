"""Pure-Python tests for bounded generated-route repair helpers."""

from __future__ import annotations

import json
import sys
import types
from collections import Counter

import pytest

from circuit_weaver import _native_generated_route_repair as repair


class UUID:
    def __init__(self, value):
        self.value = value

    def AsString(self):
        return self.value


class _Vector:
    def __init__(self, x, y):
        self.x, self.y = int(x), int(y)


class _TrackShape:
    def __init__(self, track):
        self.track = track


class _PadShape:
    def __init__(self, pad):
        self.pad = pad

    def Collide(self, other, _distance):
        if isinstance(other, _Vector):
            return (other.x, other.y) == self.pad.position
        return self.pad.block_all or self.pad.block_joint in self.pad.track_joint(other)


class Track:
    def __init__(self, uuid, net, width, *, locked=False, start=(1, 2), end=(3, 4), layer=0):
        self.m_Uuid = UUID(uuid)
        self.net = net
        self.width = width
        self.locked = locked
        self.net_code = 1 if net == "NET_A" else 2
        self.start, self.end, self.layer = tuple(start), tuple(end), layer

    def GetWidth(self):
        return self.width

    def SetWidth(self, width):
        self.width = width

    def GetNetCode(self):
        return self.net_code

    def GetNetname(self):
        return self.net

    def IsLocked(self):
        return self.locked

    def GetLayer(self):
        return self.layer

    def GetStart(self):
        return _Vector(*self.start)

    def GetEnd(self):
        return _Vector(*self.end)

    def SetStart(self, point):
        self.start = (point.x, point.y)

    def SetEnd(self, point):
        self.end = (point.x, point.y)

    def GetEffectiveShape(self):
        return _TrackShape(self)


class PCB_TRACK(Track):
    pass


class PAD:
    def __init__(self, uuid, *, net="NET_B", position=(100, 100), block_joint=(0, 0), block_all=False):
        self.m_Uuid = UUID(uuid)
        self.net, self.net_code = net, 2 if net == "NET_B" else 1
        self.position, self.block_joint = tuple(position), tuple(block_joint)
        self.block_all = block_all

    def GetNetname(self):
        return self.net

    def GetNetCode(self):
        return self.net_code

    def GetPosition(self):
        return _Vector(*self.position)

    def IsOnLayer(self, layer):
        return layer == 0

    def GetEffectiveShape(self, _layer):
        return _PadShape(self)

    @staticmethod
    def track_joint(shape):
        return shape.track.start, shape.track.end


def _width_report(changed):
    return {"status": "candidate_only_requires_fresh_drc", "source_copper_item_count": 0,
            "source_identity_preserved": True, "source_layout_preserved": True,
            "source_geometry_preserved": True, "other_copper_preserved": True,
            "source_copper_exact_subset_preserved": True, "changed_track_count": len(changed),
            "changed_tracks": changed}


class Board:
    def __init__(self, tracks=(), pads=()):
        self.tracks = list(tracks)
        self.pads = list(pads)
        self.settings = types.SimpleNamespace(m_TrackMinWidth=100)

    def GetTracks(self):
        return self.tracks

    def GetDesignSettings(self):
        return self.settings

    def GetFootprints(self):
        return [types.SimpleNamespace(Pads=lambda: self.pads)] if self.pads else []

    def Remove(self, item):
        self.tracks.remove(item)


def install_worker(monkeypatch):
    worker = types.ModuleType("_kicad_worker")
    worker._identity = lambda _board: {"J1": [("1", "NET_A")]}
    worker._fixed_layout_state = lambda _board: "fixed"
    worker._ground_plane_source_geometry_state = lambda _board, _pcb: "geometry"

    def copper(board):
        return Counter(json.dumps({"type": type(item).__name__, "uuid": item.m_Uuid.AsString(),
                                   "net": item.net, "locked": item.locked,
                                   "start": item.start, "end": item.end, "layer": item.layer,
                                   "width": item.width}, sort_keys=True)
                       for item in board.GetTracks())
    worker._copper_state = copper
    monkeypatch.setitem(sys.modules, "_kicad_worker", worker)
    monkeypatch.setattr("circuit_weaver._native_routing_repair._class_rules",
                        lambda _board, _pcb, net: (0.2 if net == "NET_A" else 0.3, 0.6, 0.3, 0.2))


def test_bend_adjustment_moves_only_whitelisted_shared_endpoint(monkeypatch):
    install_worker(monkeypatch)
    first = PCB_TRACK("a", "NET_A", 200, start=(-100_000, 0), end=(0, 0))
    second = PCB_TRACK("b", "NET_A", 200, start=(0, 0), end=(0, 100_000))
    blocker = PAD("pad", block_joint=(0, 0))
    board = Board([first, second], [blocker])
    widths = _width_report([
        {"uuid": "a", "net": "NET_A", "before_width_iu": 80, "after_width_iu": 200},
        {"uuid": "b", "net": "NET_A", "before_width_iu": 80, "after_width_iu": 200},
    ])
    raw = {"violations": [
        {"type": "clearance", "items": [{"uuid": "a"}, {"uuid": "pad"}]},
        {"type": "clearance", "items": [{"uuid": "b"}, {"uuid": "pad"}]},
    ], "unconnected_items": [], "schematic_parity": []}

    result = repair.adjust_generated_width_bends(board, Board(), PCBNew(), raw, widths)

    assert result["adjusted_bend_count"] == 1
    assert result["adjusted_track_count"] == 2
    bend = result["adjusted_bends"][0]
    assert bend["track_uuids"] == ["a", "b"]
    assert bend["old_position_iu"] == [0, 0]
    assert bend["new_position_iu"] != [0, 0]
    assert bend["blocker_uuids"] == ["pad"]
    assert first.start == (-100_000, 0) and second.end == (0, 100_000)
    assert first.end == second.start == tuple(bend["new_position_iu"])
    assert first.width == second.width == 200


@pytest.mark.parametrize("case", ["third-track", "pad-at-joint", "no-clear-offset"])
def test_bend_adjustment_returns_unchanged_when_topology_or_clearance_is_not_eligible(monkeypatch, case):
    install_worker(monkeypatch)
    first = PCB_TRACK("a", "NET_A", 200, start=(-100_000, 0), end=(0, 0))
    second = PCB_TRACK("b", "NET_A", 200, start=(0, 0), end=(0, 100_000))
    tracks = [first, second]
    if case == "third-track":
        tracks.append(PCB_TRACK("third", "NET_A", 200, start=(0, 0), end=(100_000, 0)))
    blocker = PAD("pad", position=(0, 0) if case == "pad-at-joint" else (100, 100),
                  block_joint=(0, 0), block_all=case == "no-clear-offset")
    board = Board(tracks, [blocker])
    widths = _width_report([
        {"uuid": "a", "net": "NET_A", "before_width_iu": 80, "after_width_iu": 200},
        {"uuid": "b", "net": "NET_A", "before_width_iu": 80, "after_width_iu": 200},
    ])
    raw = {"violations": [
        {"type": "clearance", "items": [{"uuid": "a"}, {"uuid": "pad"}]},
        {"type": "clearance", "items": [{"uuid": "b"}, {"uuid": "pad"}]},
    ], "unconnected_items": [], "schematic_parity": []}
    before = [(track.start, track.end) for track in tracks]

    result = repair.adjust_generated_width_bends(board, Board(), PCBNew(), raw, widths)

    assert result["adjusted_bend_count"] == 0
    assert result["adjusted_bends"] == []
    assert [(track.start, track.end) for track in tracks] == before


class PCBNew:
    @staticmethod
    def FromMM(value):
        return round(value * 1000)

    VECTOR2I = _Vector


def test_width_repair_uses_per_net_class_target_and_keeps_adequate_tracks(monkeypatch):
    install_worker(monkeypatch)
    board = Board([PCB_TRACK("a", "NET_A", 80), PCB_TRACK("b", "NET_B", 150), PCB_TRACK("c", "NET_B", 350)])
    result = repair.repair_generated_widths(board, Board(), PCBNew())
    assert [track.width for track in board.tracks] == [200, 300, 350]
    assert result["status"] == "candidate_only_requires_fresh_drc"
    assert result["changed_tracks"] == [
        {"uuid": "a", "net": "NET_A", "before_width_iu": 80, "after_width_iu": 200},
        {"uuid": "b", "net": "NET_B", "before_width_iu": 150, "after_width_iu": 300},
    ]
    assert result["source_copper_exact_subset_preserved"] is True


def test_width_repair_rejects_source_copper_and_locked_undersized_before_mutating(monkeypatch):
    install_worker(monkeypatch)
    track = PCB_TRACK("a", "NET_A", 80)
    with pytest.raises(ValueError, match="source board"):
        repair.repair_generated_widths(Board([track]), Board([PCB_TRACK("source", "NET_A", 200)]), PCBNew())
    with pytest.raises(ValueError, match="locked"):
        repair.repair_generated_widths(Board([track, PCB_TRACK("locked", "NET_A", 150, locked=True)]),
                                       Board(), PCBNew())
    assert track.width == 80


def test_width_repair_bound_counts_only_tracks_that_need_changes(monkeypatch):
    install_worker(monkeypatch)
    tracks = [PCB_TRACK(f"ok-{index}", "NET_A", 200) for index in range(4097)]
    tracks.append(PCB_TRACK("needs-width", "NET_A", 80))
    result = repair.repair_generated_widths(Board(tracks), Board(), PCBNew())
    assert result["changed_track_count"] == 1
    assert tracks[-1].width == 200


def test_width_repair_rejects_unknown_net_and_malformed_bounds_before_mutation(monkeypatch):
    install_worker(monkeypatch)
    track = PCB_TRACK("a", "", 80)
    with pytest.raises(ValueError, match="known positive-code"):
        repair.repair_generated_widths(Board([track]), Board(), PCBNew())
    with pytest.raises(ValueError, match="max_tracks"):
        repair.repair_generated_widths(Board(), Board(), PCBNew(), max_tracks=True)
    assert track.width == 80


def test_prune_removes_only_whitelisted_tracks_implicated_in_clearances(monkeypatch):
    install_worker(monkeypatch)
    a, b, c = PCB_TRACK("a", "NET_A", 200), PCB_TRACK("b", "NET_B", 300), PCB_TRACK("c", "NET_A", 250)
    board = Board([a, b, c])
    widths = {"changed_tracks": [
        {"uuid": "a", "net": "NET_A", "before_width_iu": 80, "after_width_iu": 200},
        {"uuid": "b", "net": "NET_B", "before_width_iu": 150, "after_width_iu": 300},
    ], "status": "candidate_only_requires_fresh_drc", "source_identity_preserved": True,
        "source_layout_preserved": True, "source_geometry_preserved": True, "other_copper_preserved": True,
        "source_copper_exact_subset_preserved": True, "source_copper_item_count": 0}
    raw = {"violations": [{"type": "clearance", "items": [{"uuid": "a"}, {"uuid": "c"}]},
                           {"type": "clearance", "items": [{"uuid": "b"}, {"uuid": "c"}]}],
           "unconnected_items": [{"items": [{"uuid": "open-a"}, {"uuid": "open-b"}]}],
           "schematic_parity": []}
    result = repair.prune_generated_width_clearances(board, Board(), PCBNew(), raw, widths)
    assert board.tracks == [c]
    assert {row["uuid"] for row in result["removed_tracks"]} == {"a", "b"}
    assert result["other_copper_preserved"] is True
    assert result["_native_removed_items"] == [a, b]


@pytest.mark.parametrize("raw", [
    {"violations": [{"type": "clearance", "items": [{"uuid": "outside"}]}]},
    {"violations": [{"type": "unconnected", "items": [{"uuid": "a"}]}]},
    {"violations": [], "unconnected_items": [], "schematic_parity": [],
     "native_diagnostics_may_be_truncated": True},
    {"violations": [], "unconnected_items": [], "schematic_parity": [],
     "native_diagnostics": {"native_diagnostics_may_be_truncated": True}},
])
def test_prune_fails_closed_for_ineligible_or_untrusted_reports(monkeypatch, raw):
    install_worker(monkeypatch)
    board = Board([PCB_TRACK("a", "NET_A", 200)])
    widths = {"changed_tracks": [{"uuid": "a", "net": "NET_A", "before_width_iu": 80, "after_width_iu": 200}],
              "status": "candidate_only_requires_fresh_drc", "source_identity_preserved": True,
              "source_layout_preserved": True, "source_geometry_preserved": True, "other_copper_preserved": True,
              "source_copper_exact_subset_preserved": True, "source_copper_item_count": 0}
    with pytest.raises(ValueError):
        repair.prune_generated_width_clearances(board, Board(), PCBNew(), raw, widths)
    assert [track.m_Uuid.AsString() for track in board.tracks] == ["a"]


def test_prune_checks_bounds_and_current_width_before_mutation(monkeypatch):
    install_worker(monkeypatch)
    track = PCB_TRACK("a", "NET_A", 201)
    board = Board([track])
    widths = {"changed_tracks": [{"uuid": "a", "net": "NET_A", "before_width_iu": 80, "after_width_iu": 200}],
              "status": "candidate_only_requires_fresh_drc", "source_identity_preserved": True,
              "source_layout_preserved": True, "source_geometry_preserved": True, "other_copper_preserved": True,
              "source_copper_exact_subset_preserved": True, "source_copper_item_count": 0}
    raw = {"violations": [{"type": "clearance", "items": [{"uuid": "a"}]}],
           "unconnected_items": [], "schematic_parity": []}
    with pytest.raises(ValueError, match="widened"):
        repair.prune_generated_width_clearances(board, Board(), PCBNew(), raw, widths)
    assert board.tracks == [track]
    with pytest.raises(ValueError, match="max_items"):
        repair.prune_generated_width_clearances(board, Board(), PCBNew(), raw, widths, max_items=65)
