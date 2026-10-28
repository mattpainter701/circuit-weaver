"""Pure dispatcher tests for generated-route candidate operations."""

from __future__ import annotations

import hashlib
import json
import sys
import types
import weakref
from collections import Counter

import pytest

from circuit_weaver import _kicad_worker as worker


class _Uuid:
    def __init__(self, value):
        self.value = value

    def AsString(self):
        return self.value


class PCB_TRACK:
    def __init__(self, uuid, net="NET_A", width=80, locked=False):
        self.m_Uuid = _Uuid(uuid)
        self.net = net
        self.width = width
        self.locked = locked
        self.start = (1, 2)
        self.end = (3, 4)
        self.layer = 0

    def GetWidth(self):
        return self.width

    def GetNetname(self):
        return self.net

    def IsLocked(self):
        return self.locked


class PCB_VIA(PCB_TRACK):
    pass


class PCB_ARC(PCB_TRACK):
    pass


class _Board:
    def __init__(self, tracks=(), zones=(), *, copper_layers=2):
        self.tracks = list(tracks)
        self.zones = list(zones)
        self.copper_layers = copper_layers

    def GetTracks(self):
        return self.tracks

    def Zones(self):
        return self.zones

    def GetFootprints(self):
        return []

    def Remove(self, item):
        self.tracks.remove(item)

    def Add(self, item):
        self.tracks.append(item)


def _copper_state(board):
    return Counter(json.dumps({
        "type": type(item).__name__, "uuid": item.m_Uuid.AsString(), "net": item.net,
        "locked": item.locked, "start": item.start, "end": item.end,
        "layer": item.layer, "width": item.width,
    }, sort_keys=True) for item in board.GetTracks())


def _setup(tmp_path, monkeypatch, *, tracks=None, source_tracks=(), zones=(), copper_layers=2):
    tracks = tracks if tracks is not None else [PCB_TRACK("track-1")]
    candidate = _Board(tracks, zones, copper_layers=copper_layers)
    source = _Board(source_tracks, copper_layers=copper_layers)
    output_board = {"board": None}
    output_path = tmp_path / "output.kicad_pcb"
    source_path = tmp_path / "source.kicad_pcb"
    current_path = tmp_path / "current.kicad_pcb"
    project = b'{"net_settings": {}}'
    source_path.write_bytes(b"source-board")
    current_path.write_bytes(b"candidate-board")
    source_path.with_suffix(".kicad_pro").write_bytes(project)
    current_path.with_suffix(".kicad_pro").write_bytes(project)
    output_path.with_suffix(".kicad_pro").write_bytes(project)

    pcbnew = types.ModuleType("pcbnew")
    pcbnew.F_Cu = 0
    pcbnew.B_Cu = 31
    pcbnew.Version = lambda: "fake-kicad"

    def load_board(path):
        resolved = str(path)
        if resolved == str(current_path.resolve()):
            return candidate
        if resolved == str(source_path.resolve()):
            return source
        if resolved == str(output_path.resolve()):
            saved = output_board["board"]
            if saved is None:
                return None
            return _Board([_clone_track(item) for item in saved.GetTracks()],
                          saved.Zones(), copper_layers=saved.copper_layers)
        raise AssertionError(f"unexpected LoadBoard path {resolved}")

    def save_board(path, board):
        output_path.write_bytes(b"saved-board")
        output_path.with_suffix(".kicad_pro").write_bytes(b"normalized-by-native-save")
        output_board["board"] = board
        return True

    pcbnew.LoadBoard = load_board
    pcbnew.SaveBoard = save_board
    monkeypatch.setitem(sys.modules, "pcbnew", pcbnew)
    monkeypatch.setattr(worker, "_identity", lambda _board: {})
    monkeypatch.setattr(worker, "_fixed_layout_state", lambda _board: "layout")
    monkeypatch.setattr(worker, "_ground_plane_source_geometry_state", lambda _board, _pcb: "geometry")
    monkeypatch.setattr(worker, "_native_stack_state", lambda _board, _pcb: "stack")
    monkeypatch.setattr(worker, "_zone_definition_state", lambda _board, _pcb: "zone-definitions")
    monkeypatch.setattr(worker, "_zone_fill_state", lambda _board, _pcb: "zone-fills")
    monkeypatch.setattr(worker, "_native_copper_layer_stack",
                        lambda board, _pcb: ((0, 31), board.copper_layers == 2))
    monkeypatch.setattr(worker, "_copper_state", _copper_state)
    monkeypatch.setattr(worker, "_inspect", lambda _board, _pcb, **_kwargs: {})
    helper_module = types.ModuleType("_native_generated_route_repair")
    def unexpected_operation(*_args, **_kwargs):
        raise AssertionError("unexpected generated-route helper operation")
    helper_module.repair_generated_widths = unexpected_operation
    helper_module.prune_generated_width_clearances = unexpected_operation
    monkeypatch.setitem(sys.modules, "_native_generated_route_repair", helper_module)

    request = {
        "operation": "repair_generated_widths",
        "board": str(current_path), "source_board": str(source_path), "output": str(output_path),
        "expected_board_sha256": hashlib.sha256(current_path.read_bytes()).hexdigest(),
        "expected_source_board_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "expected_source_project_sha256": hashlib.sha256(project).hexdigest(),
    }
    return request, candidate, source, output_path, helper_module, project


def _clone_track(item):
    return type(item)(item.m_Uuid.AsString(), item.net, item.width, item.locked)


def _width_report(board, _source, _pcbnew, *, max_tracks=4096):
    changed = []
    for item in board.GetTracks():
        if type(item).__name__ == "PCB_TRACK" and item.width < 200:
            before = item.width
            item.width = 200
            changed.append({"uuid": item.m_Uuid.AsString(), "net": item.net,
                            "before_width_iu": before, "after_width_iu": item.width})
    return {"status": "candidate_only_requires_fresh_drc", "source_copper_item_count": 0,
            "changed_track_count": len(changed), "changed_tracks": changed,
            "source_identity_preserved": True, "source_layout_preserved": True,
            "source_geometry_preserved": True, "source_copper_exact_subset_preserved": True,
            "other_copper_preserved": True}


def test_dispatch_width_save_reload_and_allows_native_output_project_rewrite(tmp_path, monkeypatch):
    request, candidate, source, output_path, helper, project = _setup(tmp_path, monkeypatch)
    helper.repair_generated_widths = _width_report

    result = worker.execute(request)

    assert result["status"] == "ok"
    report = result["generated_route_repair"]
    assert report["changed_tracks"] == [{"uuid": "track-1", "net": "NET_A",
                                         "before_width_iu": 80, "after_width_iu": 200}]
    assert report["saved_copper_verified"] is True
    assert report["board_sha256"] == hashlib.sha256(output_path.read_bytes()).hexdigest()
    assert candidate.GetTracks()[0].width == 200
    assert source.GetTracks() == []
    assert output_path.with_suffix(".kicad_pro").read_bytes() != project


@pytest.mark.parametrize("hash_key", [
    "expected_board_sha256", "expected_source_board_sha256", "expected_source_project_sha256",
])
def test_dispatch_hash_mismatch_fails_before_repair_mutation(tmp_path, monkeypatch, hash_key):
    request, candidate, _, _, helper, _ = _setup(tmp_path, monkeypatch)
    called = []
    helper.repair_generated_widths = lambda *args, **kwargs: called.append(True)
    request[hash_key] = "0" * 64
    with pytest.raises(ValueError, match="hash does not match"):
        worker.execute(request)
    assert not called
    assert candidate.GetTracks()[0].width == 80


@pytest.mark.parametrize("case", ["source-copper", "zones", "four-layer", "unknown-item"])
def test_dispatch_rejects_out_of_scope_board_before_repair(tmp_path, monkeypatch, case):
    tracks, source_tracks, zones, layers = [PCB_TRACK("track-1")], (), (), 2
    if case == "source-copper":
        source_tracks = [PCB_TRACK("authored")]
    elif case == "zones":
        zones = [object()]
    elif case == "four-layer":
        layers = 4
    else:
        tracks = [PCB_ARC("arc")]
    request, candidate, _, _, helper, _ = _setup(
        tmp_path, monkeypatch, tracks=tracks, source_tracks=source_tracks, zones=zones, copper_layers=layers,
    )
    called = []
    helper.repair_generated_widths = lambda *args, **kwargs: called.append(True)
    with pytest.raises(ValueError):
        worker.execute(request)
    assert not called
    assert candidate.GetTracks() == tracks


def test_dispatch_rejects_mutated_source_input_after_native_helper(tmp_path, monkeypatch):
    request, candidate, _, output_path, helper, _ = _setup(tmp_path, monkeypatch)
    source_path = tmp_path / "source.kicad_pcb"

    def mutate_source(board, _source, _pcbnew, **_kwargs):
        source_path.write_bytes(b"changed during operation")
        return _width_report(board, None, None)

    helper.repair_generated_widths = mutate_source
    with pytest.raises(ValueError, match="hash does not match"):
        worker.execute(request)
    assert candidate.GetTracks()[0].width == 200
    assert not output_path.exists()


def test_dispatch_prune_keeps_removed_swig_references_through_save_reload_and_inspection(tmp_path, monkeypatch):
    request, candidate, _, output_path, helper, _ = _setup(
        tmp_path, monkeypatch, tracks=[PCB_TRACK("remove-me"), PCB_TRACK("keep-me", width=250)],
    )
    request["operation"] = "prune_generated_width_clearances"
    request["drc_report"] = {"violations": [], "unconnected_items": [], "schematic_parity": []}
    request["width_repair_report"] = {"changed_tracks": []}
    removed_ref = {}
    original_save = sys.modules["pcbnew"].SaveBoard
    original_load = sys.modules["pcbnew"].LoadBoard

    def prune(board, *_args, **_kwargs):
        item = board.GetTracks()[0]
        removed_ref["weak"] = weakref.ref(item)
        board.Remove(item)
        return {"status": "candidate_only_requires_fresh_drc", "source_copper_item_count": 0,
                "removed_track_count": 1, "removed_tracks": [{"uuid": "remove-me", "net": "NET_A",
                                                                  "width_iu": 200}],
                "_native_removed_items": [item]}

    helper.prune_generated_width_clearances = prune

    def save_with_lifetime(path, board):
        assert removed_ref["weak"]() is not None
        return original_save(path, board)

    def load_with_lifetime(path):
        if str(path) == str(output_path.resolve()):
            assert removed_ref["weak"]() is not None
        return original_load(path)

    monkeypatch.setattr(sys.modules["pcbnew"], "SaveBoard", save_with_lifetime)
    monkeypatch.setattr(sys.modules["pcbnew"], "LoadBoard", load_with_lifetime)
    monkeypatch.setattr(worker, "_inspect", lambda *_args, **_kwargs: {
        "removed_reference_alive_during_inspect": removed_ref["weak"]() is not None,
    })

    result = worker.execute(request)

    assert result["removed_reference_alive_during_inspect"] is True
    assert result["generated_route_repair"]["removed_tracks"][0]["uuid"] == "remove-me"
    assert "_native_removed_items" not in result["generated_route_repair"]
    assert candidate.GetTracks()[0].m_Uuid.AsString() == "keep-me"
    assert output_path.exists()


@pytest.mark.parametrize("track", [PCB_VIA("via"), PCB_TRACK("locked", locked=True)])
def test_dispatch_prune_rejects_removed_vias_or_locked_tracks_before_save(tmp_path, monkeypatch, track):
    request, _, _, output_path, helper, _ = _setup(tmp_path, monkeypatch, tracks=[track])
    request["operation"] = "prune_generated_width_clearances"
    request["drc_report"] = {"violations": [], "unconnected_items": [], "schematic_parity": []}
    request["width_repair_report"] = {"changed_tracks": []}

    def prune(board, *_args, **_kwargs):
        item = board.GetTracks()[0]
        board.Remove(item)
        return {"status": "candidate_only_requires_fresh_drc", "source_copper_item_count": 0,
                "removed_track_count": 1, "removed_tracks": [{"uuid": item.m_Uuid.AsString(),
                                                                  "net": item.net, "width_iu": item.width}],
                "_native_removed_items": [item]}

    helper.prune_generated_width_clearances = prune
    with pytest.raises(ValueError, match="unsupported or locked"):
        worker.execute(request)
    assert not output_path.exists()
