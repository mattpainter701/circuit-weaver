from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from circuit_weaver import autoroute_recovery
from circuit_weaver._native_router_dangling_cleanup import remove_reported_tracks
from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.validator import ValidationIssue


class _Uuid:
    def __init__(self, value: str):
        self.value = value

    def AsString(self):
        return self.value


class PCB_TRACK:
    def __init__(self, uuid="router-track", *, locked=False, layer=0, start=(0, 0), end=(10_000_000, 0)):
        self.m_Uuid = _Uuid(uuid)
        self.locked = locked
        self.layer = layer
        self.start = SimpleNamespace(x=start[0], y=start[1])
        self.end = SimpleNamespace(x=end[0], y=end[1])

    def IsLocked(self):
        return self.locked

    def GetLayer(self):
        return self.layer

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetWidth(self):
        return 200_000

    def GetNetname(self):
        return "NET_A"


class PCB_VIA:
    def __init__(self, uuid="router-via", *, locked=False, position=(5_000_000, 6_000_000),
                 top=0, bottom=31, widths=(600_000, 600_000), drill=300_000):
        self.m_Uuid = _Uuid(uuid)
        self.locked = locked
        self.position = SimpleNamespace(x=position[0], y=position[1])
        self.top, self.bottom = top, bottom
        self.widths, self.drill = widths, drill

    def IsLocked(self):
        return self.locked

    def GetPosition(self):
        return self.position

    def TopLayer(self):
        return self.top

    def BottomLayer(self):
        return self.bottom

    def GetWidth(self, layer):
        return self.widths[0] if layer == self.top else self.widths[1]

    def GetDrillValue(self):
        return self.drill

    def GetViaType(self):
        return 0

    def GetNetname(self):
        return "NET_A"


class _Board:
    def __init__(self, tracks):
        self.tracks = list(tracks)

    def GetTracks(self):
        return list(self.tracks)

    def GetLayerName(self, layer):
        return {0: "F.Cu", 31: "B.Cu"}[layer]

    def Remove(self, item):
        self.tracks.remove(item)

    def GetFootprints(self):
        return []

    def GetDrawings(self):
        return []

    def Zones(self):
        return []


def _warning(uuid="router-track", pos=(10.0, 0.0)):
    return {
        "type": "track_dangling",
        "severity": "warning",
        "items": [{"uuid": uuid, "pos": {"x": pos[0], "y": pos[1]}}],
    }


def _via_warning(uuid="router-via", pos=(5.0, 6.0)):
    return {
        "type": "via_dangling",
        "severity": "warning",
        "items": [{"uuid": uuid, "pos": {"x": pos[0], "y": pos[1]}}],
    }


def test_native_cleanup_removes_exact_reported_track_only():
    keep = PCB_TRACK("keep", start=(0, 1_000_000), end=(10_000_000, 1_000_000))
    target = PCB_TRACK()
    board = _Board([keep, target])

    result = remove_reported_tracks(board, None, [_warning()], enabled_copper_layer_ids={0})

    assert result["status"] == "candidate_only_requires_fresh_drc"
    assert result["removed_count"] == 1
    assert result["removed_tracks"] == [{
        "uuid": "router-track", "net": "NET_A", "layer": "F.Cu",
        "start_iu": [0, 0], "end_iu": [10_000_000, 0], "width_iu": 200_000, "locked": False,
    }]
    assert board.GetTracks() == [keep]


def test_native_cleanup_removes_exact_reported_via_and_returns_type_counts():
    keep = PCB_TRACK("keep")
    target = PCB_VIA()
    board = _Board([keep, target])

    result = remove_reported_tracks(board, None, [_via_warning()], enabled_copper_layer_ids={0, 31})

    assert result["status"] == "candidate_only_requires_fresh_drc"
    assert result["removed_count"] == 0
    assert result["removed_track_count"] == 0
    assert result["removed_via_count"] == result["removed_item_count"] == 1
    assert result["removed_tracks"] == []
    assert result["removed_vias"] == [{
        "uuid": "router-via", "net": "NET_A", "layer": "F.Cu-B.Cu",
        "position_iu": [5_000_000, 6_000_000], "top_layer_id": 0, "bottom_layer_id": 31,
        "top_layer": "F.Cu", "bottom_layer": "B.Cu", "widths_iu": [600_000, 600_000],
        "drill_iu": 300_000, "via_type": 0, "locked": False,
    }]
    assert board.GetTracks() == [keep]


def test_native_cleanup_supports_via_width_accessor_without_layer_argument():
    no_layer_width_type = type("PCB_VIA", (PCB_VIA,), {"GetWidth": lambda self: self.widths[0]})
    via = no_layer_width_type()
    board = _Board([via])

    result = remove_reported_tracks(board, None, [_via_warning()], enabled_copper_layer_ids={0, 31})

    assert result["removed_vias"][0]["widths_iu"] == [600_000, 600_000]
    assert board.GetTracks() == []


def test_native_cleanup_supports_mixed_tracks_and_vias_under_shared_cap():
    board = _Board([PCB_TRACK(), PCB_VIA()])
    result = remove_reported_tracks(
        board, None, [_warning(), _via_warning()], max_tracks=2, enabled_copper_layer_ids={0, 31},
    )
    assert result["removed_count"] == result["removed_track_count"] == 1
    assert result["removed_via_count"] == 1
    assert result["removed_item_count"] == 2
    assert board.GetTracks() == []


def test_native_cleanup_uses_native_copper_inventory_not_display_layer_name():
    track = PCB_TRACK(layer=0)
    board = _Board([track])
    board.GetLayerName = lambda _layer: "Top Signal"
    result = remove_reported_tracks(board, None, [_warning()], enabled_copper_layer_ids={0})
    assert result["removed_tracks"][0]["layer"] == "Top Signal"
    assert not board.GetTracks()


def test_native_cleanup_rejects_layer_outside_native_copper_inventory():
    track = PCB_TRACK(layer=99)
    board = _Board([track])
    board.GetLayerName = lambda _layer: "Auxiliary"
    with pytest.raises(ValueError, match="not on an enabled copper layer"):
        remove_reported_tracks(board, None, [_warning()], enabled_copper_layer_ids={0})
    assert board.GetTracks() == [track]


@pytest.mark.parametrize(
    "warning,track,error",
    [
        ({"type": "clearance", "severity": "warning", "items": []}, PCB_TRACK(), "Only fresh"),
        (_warning(pos=(12.0, 0.0)), PCB_TRACK(), "endpoint does not match"),
        (_warning(), PCB_TRACK(locked=True), "locked"),
        (_warning("missing"), PCB_TRACK(), "exactly one native"),
    ],
)
def test_native_cleanup_rejects_ambiguous_or_unsafe_track_selection(warning, track, error):
    board = _Board([track])

    with pytest.raises(ValueError, match=error):
        remove_reported_tracks(board, None, [warning], enabled_copper_layer_ids={0})

    assert board.GetTracks() == [track]


def test_native_cleanup_rejects_duplicate_ids_and_out_of_bound_requests():
    board = _Board([PCB_TRACK()])
    with pytest.raises(ValueError, match="duplicated"):
        remove_reported_tracks(board, None, [_warning(), _warning()], enabled_copper_layer_ids={0})
    with pytest.raises(ValueError, match="from 1 through 64"):
        remove_reported_tracks(board, None, [_warning()], max_tracks=65, enabled_copper_layer_ids={0})
    assert len(board.GetTracks()) == 1


@pytest.mark.parametrize(
    "warning,via,error",
    [
        (_via_warning(pos=(9, 6)), PCB_VIA(), "position does not match"),
        (_via_warning(), PCB_VIA(locked=True), "locked router-generated"),
        (_via_warning(), PCB_VIA(bottom=99), "enabled copper layers"),
        (_via_warning(), PCB_VIA(drill=600_000), "annular width or drill"),
        (_via_warning(), PCB_TRACK("router-via"), "ordinary PCB_VIA"),
        (_warning(), PCB_VIA("router-track"), "ordinary PCB_TRACK"),
    ],
)
def test_native_cleanup_rejects_unsafe_via_selection(warning, via, error):
    board = _Board([via])
    with pytest.raises(ValueError, match=error):
        remove_reported_tracks(board, None, [warning], enabled_copper_layer_ids={0, 31})
    assert board.GetTracks() == [via]


def _worker_board(tracks):
    board = _Board(tracks)
    board.GetCopperLayerCount = lambda: 2
    board.GetEnabledLayers = lambda: SimpleNamespace(CuStack=lambda: [0, 31])
    board.GetLayerName = lambda layer: {0: "F.Cu", 31: "B.Cu"}[layer]
    return board


def _run_cleanup_worker(monkeypatch, tmp_path, warning_uuid="router-track", *,
                         warning_rows=None, source_objects=None, generated_objects=None):
    from circuit_weaver import _kicad_worker as worker

    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"immutable source")
    board_path = tmp_path / "partial.kicad_pcb"
    board_path.write_bytes(b"partial input")
    output = tmp_path / "cleaned.kicad_pcb"
    source_track = PCB_TRACK("source-track", start=(0, 1_000_000), end=(10_000_000, 1_000_000))
    if source_objects is None:
        source_objects = [source_track]
    if generated_objects is None:
        generated_objects = [PCB_TRACK("router-track")]
    source_board = _worker_board(source_objects)
    partial_board = _worker_board([*(copy.deepcopy(item) for item in source_objects), *generated_objects])
    saved_boards = {str(source.resolve()): source_board, str(board_path.resolve()): partial_board}

    class _Pcbnew:
        @staticmethod
        def LoadBoard(path):
            return copy.deepcopy(saved_boards[str(Path(path).resolve())])

        @staticmethod
        def SaveBoard(path, board):
            Path(path).write_bytes(b"saved native board")
            saved_boards[str(Path(path).resolve())] = copy.deepcopy(board)
            return True

        @staticmethod
        def Version():
            return "mock-kicad"

    monkeypatch.setitem(sys.modules, "pcbnew", _Pcbnew)
    monkeypatch.setattr(worker, "_identity", lambda _board: {"J1": [("1", "NET_A")]})
    monkeypatch.setattr(worker, "_fixed_layout_state", lambda _board: "fixed-layout")
    monkeypatch.setattr(worker, "_ground_plane_source_geometry_state", lambda _board, _pcb: "source-geometry")
    monkeypatch.setattr(
        worker, "_native_stack_state",
        lambda _board, _pcb: {"count": 2, "layers": [{"id": 0}, {"id": 31}]},
    )
    monkeypatch.setattr(worker, "_zone_definition_state", lambda _board, _pcb: [])
    monkeypatch.setattr(worker, "_zone_fill_state", lambda _board, _pcb: {"rows": [], "fillable_zone_count": 0})
    monkeypatch.setattr(worker, "_inspect", lambda _board, _pcb: {})
    request = {
        "operation": "cleanup_router_dangling",
        "board": str(board_path),
        "output": str(output),
        "source_board": str(source),
        "expected_source_board_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "expected_board_sha256": hashlib.sha256(board_path.read_bytes()).hexdigest(),
        "warning_rows": warning_rows if warning_rows is not None else [_warning(warning_uuid)],
        "max_tracks": 64,
    }
    return worker, request, source, output, saved_boards


def test_native_worker_cleanup_saves_only_selected_generated_track(monkeypatch, tmp_path):
    worker, request, source, output, saved = _run_cleanup_worker(monkeypatch, tmp_path)

    result = worker.execute(request)

    assert result["status"] == "ok"
    proof = result["router_dangling_cleanup"]
    assert proof["source_copper_exact_subset_preserved"] is True
    assert proof["saved_removed_tracks_verified"] is True
    assert proof["saved_removed_vias_verified"] is True
    assert proof["saved_removed_copper_items_verified"] is True
    assert proof["saved_source_copper_exact_subset_preserved"] is True
    assert [track.m_Uuid.AsString() for track in saved[str(output.resolve())].GetTracks()] == ["source-track"]
    assert source.read_bytes() == b"immutable source"
    assert output.is_file()


def test_native_worker_cleanup_saves_only_selected_generated_via(monkeypatch, tmp_path):
    worker, request, source, output, saved = _run_cleanup_worker(
        monkeypatch, tmp_path, warning_rows=[_via_warning()], generated_objects=[PCB_VIA()],
    )

    result = worker.execute(request)

    assert result["status"] == "ok"
    proof = result["router_dangling_cleanup"]
    assert proof["removed_count"] == 0
    assert proof["removed_track_count"] == 0
    assert proof["removed_via_count"] == proof["removed_item_count"] == 1
    assert proof["source_copper_exact_subset_preserved"] is True
    assert proof["saved_removed_tracks_verified"] is True
    assert proof["saved_removed_vias_verified"] is True
    assert proof["saved_removed_copper_items_verified"] is True
    assert proof["saved_source_copper_exact_subset_preserved"] is True
    assert [type(item).__name__ for item in saved[str(output.resolve())].GetTracks()] == ["PCB_TRACK"]
    assert source.read_bytes() == b"immutable source"
    assert output.is_file()


def test_native_worker_refuses_warning_that_selects_original_source_copper(monkeypatch, tmp_path):
    worker, request, source, output, _saved = _run_cleanup_worker(
        monkeypatch, tmp_path, warning_uuid="source-track",
    )
    request["warning_rows"] = [_warning("source-track", pos=(10.0, 1.0))]

    with pytest.raises(ValueError, match="present in the original source"):
        worker.execute(request)

    assert source.read_bytes() == b"immutable source"
    assert not output.exists()


def test_native_worker_refuses_warning_that_selects_original_source_via(monkeypatch, tmp_path):
    source_via = PCB_VIA("source-via")
    worker, request, source, output, _saved = _run_cleanup_worker(
        monkeypatch, tmp_path, warning_rows=[_via_warning("source-via")],
        source_objects=[PCB_TRACK("source-track"), source_via], generated_objects=[],
    )

    with pytest.raises(ValueError, match="present in the original source"):
        worker.execute(request)

    assert source.read_bytes() == b"immutable source"
    assert not output.exists()


def _open_finding(index):
    return ValidationIssue(
        "CW-DRC-002", message=f"open {index}", severity="blocker",
        detection_confidence="verified", rule_id="CW-DRC-002",
    )


def _dangling_finding():
    return ValidationIssue(
        "CW-DRC-841", level="warning", message="Track has unconnected end",
        severity="major", detection_confidence="verified", rule_id="CW-DRC-841",
    )


def _drc(board: Path, warning_ids=(), *, open_count=2, extra_violation=None):
    violations = [_warning(uuid, pos=(10.0 + index, 0.0)) for index, uuid in enumerate(warning_ids)]
    if extra_violation is not None:
        violations.append(extra_violation)
    raw = {
        "$schema": "https://schemas.kicad.org/drc.v1.json",
        "source": board.name,
        "violations": violations,
        "unconnected_items": [{"items": [{"uuid": f"open-{i}"}]} for i in range(open_count)],
        "schematic_parity": [],
    }
    findings = [_open_finding(i) for i in range(open_count)]
    findings.extend(_dangling_finding() for _ in warning_ids)
    if extra_violation is not None:
        findings.append(ValidationIssue("CW-DRC-499", level="warning", message="other", rule_id="CW-DRC-499"))
    digest = hashlib.sha256(board.read_bytes()).hexdigest()
    return DrcResult("ok", str(board), board_sha256=digest, findings=tuple(findings), raw_report=raw)


def _cleanup_report(removed_tracks, **updates):
    removed_tracks = list(removed_tracks)
    report = {
        "status": "candidate_only_requires_fresh_drc",
        "removed_count": len(removed_tracks),
        "removed_track_count": len(removed_tracks),
        "removed_via_count": 0,
        "removed_item_count": len(removed_tracks),
        "removed_tracks": removed_tracks,
        "removed_vias": [],
        "source_copper_exact_subset_preserved": True,
        "saved_removed_tracks_verified": True,
        "saved_removed_vias_verified": True,
        "saved_removed_copper_items_verified": True,
    }
    report.update(updates)
    return report


def test_host_cleanup_iterates_dead_stub_chain_without_regressing_open_count(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"source board")
    partial = tmp_path / "partial.kicad_pcb"
    partial.write_bytes(b"SES imported partial")
    snapshots = {source: hashlib.sha256(source.read_bytes()).hexdigest()}
    results = iter([
        lambda board: _drc(board, ("stub-1",)),
        lambda board: _drc(board, ("stub-2",)),
        lambda board: _drc(board),
    ])
    calls = []

    def run_drc(board, **kwargs):
        return next(results)(Path(board))

    def run_kicad(operation, **kwargs):
        calls.append((operation, kwargs["warning_rows"][0]["items"][0]["uuid"]))
        Path(kwargs["output"]).write_bytes(f"cleaned-{len(calls)}".encode())
        return {
            "status": "ok",
            "router_dangling_cleanup": _cleanup_report(
                [{"uuid": kwargs["warning_rows"][0]["items"][0]["uuid"]}]
            ),
        }

    monkeypatch.setattr("circuit_weaver.drc_runner.run_drc", run_drc)
    monkeypatch.setattr("circuit_weaver.kicad_bridge.run_kicad", run_kicad)
    monkeypatch.setattr("circuit_weaver.pcb_layout._prepare_settings", lambda *_args: {})
    monkeypatch.setattr("circuit_weaver.pcb_layout._restore_settings", lambda *_args: None)
    monkeypatch.setattr("circuit_weaver.pcb_layout._check_snapshots", lambda *_args: None)

    board, final_drc, summary = autoroute_recovery._clean_router_dangling_tracks(
        source,
        partial,
        work=tmp_path / "work",
        python="native-python",
        deadline=autoroute_recovery.time.monotonic() + 60,
        source_snapshots=snapshots,
    )

    assert board.name == "partial-cleaned-02.kicad_pcb"
    assert final_drc.blocker_count == 2
    assert [uuid for _, uuid in calls] == ["stub-1", "stub-2"]
    assert summary["status"] == "cleaned"
    assert summary["removed_track_count"] == 2
    assert all(Path(step["before_drc"]).is_file() and Path(step["after_drc"]).is_file() for step in summary["steps"])
    assert json.loads(Path(summary["final_drc"]).read_text())["raw_report"]["violations"] == []


def test_host_cleanup_retains_raw_drc_and_rejects_other_native_violations(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"source")
    partial = tmp_path / "partial.kicad_pcb"
    partial.write_bytes(b"partial")
    monkeypatch.setattr(
        "circuit_weaver.drc_runner.run_drc",
        lambda board, **_kwargs: _drc(
            Path(board),
            ("stub-1",),
            extra_violation={"type": "clearance", "severity": "warning"},
        ),
    )
    monkeypatch.setattr(
        "circuit_weaver.kicad_bridge.run_kicad",
        lambda *_args, **_kwargs: pytest.fail("worker must not run"),
    )

    with pytest.raises(ValueError, match="findings beyond"):
        autoroute_recovery._clean_router_dangling_tracks(
            source,
            partial,
            work=tmp_path / "work",
            python=None,
            deadline=autoroute_recovery.time.monotonic() + 60,
            source_snapshots={source: hashlib.sha256(source.read_bytes()).hexdigest()},
        )

    evidence = tmp_path / "work" / "native-dangling-cleanup" / "step-00-before-drc.json"
    saved = json.loads(evidence.read_text())
    assert saved["raw_report"]["violations"][0]["type"] == "track_dangling"
    assert saved["raw_report"]["violations"][1]["type"] == "clearance"


def _install_host_mocks(monkeypatch, run_drc, run_kicad):
    monkeypatch.setattr("circuit_weaver.drc_runner.run_drc", run_drc)
    monkeypatch.setattr("circuit_weaver.kicad_bridge.run_kicad", run_kicad)
    monkeypatch.setattr("circuit_weaver.pcb_layout._prepare_settings", lambda *_args: {})
    monkeypatch.setattr("circuit_weaver.pcb_layout._restore_settings", lambda *_args: None)


def test_host_cleanup_rejects_increased_open_count_after_removal(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"source")
    partial = tmp_path / "partial.kicad_pcb"
    partial.write_bytes(b"partial")
    queue = iter([lambda p: _drc(p, ("stub-1",), open_count=2), lambda p: _drc(p, open_count=3)])

    def run_kicad(_operation, **kwargs):
        Path(kwargs["output"]).write_bytes(b"cleaned")
        return {"status": "ok", "router_dangling_cleanup": _cleanup_report([{"uuid": "stub-1"}])}

    _install_host_mocks(monkeypatch, lambda p, **_k: next(queue)(Path(p)), run_kicad)
    monkeypatch.setattr("circuit_weaver.pcb_layout._check_snapshots", lambda *_args: None)

    with pytest.raises(ValueError, match="no-regression"):
        autoroute_recovery._clean_router_dangling_tracks(
            source, partial, work=tmp_path / "work", python=None,
            deadline=autoroute_recovery.time.monotonic() + 60,
            source_snapshots={source: hashlib.sha256(source.read_bytes()).hexdigest()},
        )

    evidence = tmp_path / "work" / "native-dangling-cleanup" / "step-01-after-drc.json"
    assert json.loads(evidence.read_text())["drc"]["blocker_count"] == 3


def test_host_cleanup_stops_at_bounded_track_cap(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"source")
    partial = tmp_path / "partial.kicad_pcb"
    partial.write_bytes(b"partial")
    warnings = tuple(f"stub-{index}" for index in range(65))
    called = False

    def run_kicad(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "error"}

    _install_host_mocks(monkeypatch, lambda p, **_k: _drc(Path(p), warnings), run_kicad)
    monkeypatch.setattr("circuit_weaver.pcb_layout._check_snapshots", lambda *_args: None)
    with pytest.raises(ValueError, match="exceeded its 64-item bound"):
        autoroute_recovery._clean_router_dangling_tracks(
            source, partial, work=tmp_path / "work", python=None,
            deadline=autoroute_recovery.time.monotonic() + 60,
            source_snapshots={source: hashlib.sha256(source.read_bytes()).hexdigest()},
        )
    assert not called
    assert (tmp_path / "work" / "native-dangling-cleanup" / "step-00-before-drc.json").is_file()


def test_host_cleanup_rechecks_source_snapshot_after_worker_mutation(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"source")
    partial = tmp_path / "partial.kicad_pcb"
    partial.write_bytes(b"partial")
    snapshots = {source: hashlib.sha256(source.read_bytes()).hexdigest()}

    def run_kicad(_operation, **kwargs):
        source.write_bytes(b"changed source")
        Path(kwargs["output"]).write_bytes(b"candidate")
        return {"status": "ok", "router_dangling_cleanup": {}}

    _install_host_mocks(monkeypatch, lambda p, **_k: _drc(Path(p), ("stub-1",)), run_kicad)
    with pytest.raises(ValueError, match="changed during processing"):
        autoroute_recovery._clean_router_dangling_tracks(
            source, partial, work=tmp_path / "work", python=None,
            deadline=autoroute_recovery.time.monotonic() + 60,
            source_snapshots=snapshots,
        )
    assert (tmp_path / "work" / "native-dangling-cleanup" / "step-00-before-drc.json").is_file()


def test_host_cleanup_rejects_worker_that_does_not_claim_native_preservation(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"source")
    partial = tmp_path / "partial.kicad_pcb"
    partial.write_bytes(b"partial")

    def run_kicad(_operation, **kwargs):
        Path(kwargs["output"]).write_bytes(b"candidate")
        return {
            "status": "ok",
            "router_dangling_cleanup": _cleanup_report(
                [{"uuid": "stub-1"}], source_copper_exact_subset_preserved=False,
            ),
        }

    _install_host_mocks(monkeypatch, lambda p, **_k: _drc(Path(p), ("stub-1",)), run_kicad)
    monkeypatch.setattr("circuit_weaver.pcb_layout._check_snapshots", lambda *_args: None)
    with pytest.raises(ValueError, match="did not prove exact"):
        autoroute_recovery._clean_router_dangling_tracks(
            source, partial, work=tmp_path / "work", python=None,
            deadline=autoroute_recovery.time.monotonic() + 60,
            source_snapshots={source: hashlib.sha256(source.read_bytes()).hexdigest()},
        )
