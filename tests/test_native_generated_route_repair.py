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


class Track:
    def __init__(self, uuid, net, width, *, locked=False):
        self.m_Uuid = UUID(uuid)
        self.net = net
        self.width = width
        self.locked = locked
        self.net_code = 1 if net == "NET_A" else 2

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


class PCB_TRACK(Track):
    pass


class Board:
    def __init__(self, tracks=()):
        self.tracks = list(tracks)
        self.settings = types.SimpleNamespace(m_TrackMinWidth=100)

    def GetTracks(self):
        return self.tracks

    def GetDesignSettings(self):
        return self.settings

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
                                   "start": (1, 2), "end": (3, 4), "layer": 0,
                                   "width": item.width}, sort_keys=True)
                       for item in board.GetTracks())
    worker._copper_state = copper
    monkeypatch.setitem(sys.modules, "_kicad_worker", worker)
    monkeypatch.setattr("circuit_weaver._native_routing_repair._class_rules",
                        lambda _board, _pcb, net: (0.2 if net == "NET_A" else 0.3, 0.6, 0.3, 0.2))


class PCBNew:
    @staticmethod
    def FromMM(value):
        return round(value * 1000)


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
