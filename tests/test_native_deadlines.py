"""Keep native subprocess launches within a shared absolute deadline."""

from __future__ import annotations

import json
import math
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from circuit_weaver import drc_runner, kicad_bridge


def _result(stdout="", *, returncode=0, stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_find_python_caps_probe_timeout_to_remaining_deadline(monkeypatch):
    bridge = kicad_bridge
    monkeypatch.setenv("CIRCUIT_WEAVER_KICAD_PYTHON", "")
    monkeypatch.setenv("KICAD_PYTHON", "")
    monkeypatch.setattr(bridge.shutil, "which", lambda candidate: candidate)
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 100.0)
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        return _result("CW_PCBNEW_READY")

    monkeypatch.setattr(bridge.subprocess, "run", run)

    assert bridge.find_kicad_python("fake-kicad-python", deadline=104.0) == "fake-kicad-python"
    assert calls[0]["timeout"] == 4.0


def test_expired_deadline_prevents_python_discovery_probe(monkeypatch):
    bridge = kicad_bridge
    monkeypatch.setenv("CIRCUIT_WEAVER_KICAD_PYTHON", "")
    monkeypatch.setenv("KICAD_PYTHON", "")
    monkeypatch.setattr(bridge.shutil, "which", lambda candidate: candidate)
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 10.0)
    launch = Mock()
    monkeypatch.setattr(bridge.subprocess, "run", launch)

    assert bridge.find_kicad_python("fake-kicad-python", deadline=9.0) is None
    launch.assert_not_called()


def test_run_kicad_caps_native_timeout_and_preserves_legacy_call(monkeypatch, tmp_path):
    bridge = kicad_bridge
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"board-input")
    source.with_suffix(".kicad_pro").write_bytes(b"project-input")
    monkeypatch.setattr(
        bridge, "find_kicad_python", lambda explicit=None, *, deadline=None: "fake-python"
    )
    monkeypatch.setattr(bridge, "inspect_project_footprint_context", lambda path: {"stable": True})
    monkeypatch.setattr(bridge, "stage_project_footprint_context", lambda source, target: None)
    clock = iter([100.0, 102.0])
    monkeypatch.setattr(bridge.time, "monotonic", lambda: next(clock))
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return _result('CW_KICAD_RESULT={"status":"ok"}')

    monkeypatch.setattr(bridge.subprocess, "run", run)
    response = bridge.run_kicad("inspect", python_path="fake-python", timeout=120, deadline=105.0, board=source)

    assert response == {"status": "ok"}
    assert calls[0]["timeout"] == 3.0
    assert source.read_bytes() == b"board-input"
    assert source.with_suffix(".kicad_pro").read_bytes() == b"project-input"


def test_run_kicad_expiry_during_staging_never_launches_worker(monkeypatch, tmp_path):
    bridge = kicad_bridge
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"board-input")
    monkeypatch.setattr(
        bridge, "find_kicad_python", lambda explicit=None, *, deadline=None: "fake-python"
    )
    monkeypatch.setattr(bridge, "inspect_project_footprint_context", lambda path: {})
    monkeypatch.setattr(bridge, "stage_project_footprint_context", lambda source, target: None)
    clock = iter([100.0, 106.0])
    monkeypatch.setattr(bridge.time, "monotonic", lambda: next(clock))
    launch = Mock()
    monkeypatch.setattr(bridge.subprocess, "run", launch)

    response = bridge.run_kicad("repair_open", timeout=120, deadline=105.0, board=source)

    assert response["status"] == "error"
    assert "deadline expired before native launch" in response["message"]
    launch.assert_not_called()


def test_run_kicad_legacy_call_keeps_original_probe_and_worker_timeouts(monkeypatch, tmp_path):
    bridge = kicad_bridge
    source = tmp_path / "legacy.kicad_pcb"
    source.write_bytes(b"legacy-board")
    finder = Mock(return_value="fake-python")
    monkeypatch.setattr(bridge, "find_kicad_python", finder)
    monkeypatch.setattr(bridge, "inspect_project_footprint_context", lambda path: {})
    monkeypatch.setattr(bridge, "stage_project_footprint_context", lambda source, target: None)
    calls = []
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda command, **kwargs: calls.append(kwargs) or _result('CW_KICAD_RESULT={"status":"ok"}'),
    )

    assert bridge.run_kicad("inspect", timeout=37, board=source) == {"status": "ok"}
    finder.assert_called_once_with(None)
    assert calls[0]["timeout"] == 37


def test_drc_version_probe_caps_timeout_to_remaining_deadline(monkeypatch):
    drc = drc_runner
    monkeypatch.setattr(drc.time, "monotonic", lambda: 20.0)
    calls = []
    monkeypatch.setattr(
        drc.subprocess,
        "run",
        lambda command, **kwargs: calls.append(kwargs) or _result("KiCad 10.0.4"),
    )

    assert drc._kicad_cli_major(Path("fake-cli"), 120, deadline=24.5) == 10
    assert calls[0]["timeout"] == 4.5


def test_drc_expired_before_version_probe_does_not_launch(monkeypatch, tmp_path):
    drc = drc_runner
    board = tmp_path / "input.kicad_pcb"
    board.write_bytes(b"board-input")
    monkeypatch.setattr(drc, "_kicad_cli_path", lambda: Path("fake-cli"))
    monkeypatch.setattr(drc.time, "monotonic", lambda: 30.0)
    version = Mock()
    monkeypatch.setattr(drc, "_kicad_cli_major", version)

    result = drc.run_drc(board, evidence_ledger=types.SimpleNamespace(), deadline=29.0)

    assert result.status == "failed"
    assert "expired before KiCad version probe" in result.failure_reason
    version.assert_not_called()


def _prepare_drc(monkeypatch, drc, board):
    monkeypatch.setattr(drc, "_kicad_cli_path", lambda: Path("fake-cli"))
    monkeypatch.setattr(drc, "_kicad_cli_major", lambda *args, **kwargs: 10)
    monkeypatch.setattr(drc, "inspect_project_footprint_context", lambda path: {})
    monkeypatch.setattr(drc, "stage_project_footprint_context", lambda source, target: None)
    monkeypatch.setattr(
        drc,
        "stage_installed_footprint_context",
        lambda *args, **kwargs: {"status": "unavailable"},
    )
    monkeypatch.setattr(drc, "_native_error_limits_reached", lambda raw: {})
    monkeypatch.setattr(drc, "_raw_findings", lambda raw: [])
    monkeypatch.setattr(drc, "_parse_drc_json", lambda *args, **kwargs: ())
    return board


def test_drc_expiry_during_staging_never_launches_native_process(monkeypatch, tmp_path):
    drc = drc_runner
    board = _prepare_drc(monkeypatch, drc, tmp_path / "input.kicad_pcb")
    board.write_bytes(b"board-input")
    clock = iter([100.0, 101.0, 106.0])
    monkeypatch.setattr(drc.time, "monotonic", lambda: next(clock))
    launch = Mock()
    monkeypatch.setattr(drc.subprocess, "run", launch)

    result = drc.run_drc(board, evidence_ledger=types.SimpleNamespace(), deadline=105.0)

    assert result.status == "failed"
    assert "during staging" in result.failure_reason
    launch.assert_not_called()


def test_drc_caps_native_timeout_and_legacy_timeout_is_unchanged(monkeypatch, tmp_path):
    drc = drc_runner
    board = _prepare_drc(monkeypatch, drc, tmp_path / "input.kicad_pcb")
    board.write_bytes(b"board-input")
    clock = iter([100.0, 101.0, 102.0])
    monkeypatch.setattr(drc.time, "monotonic", lambda: next(clock))
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps({"kicad_version": "10.0.4", "violations": []}), encoding="utf-8")
        return _result()

    monkeypatch.setattr(drc.subprocess, "run", run)
    result = drc.run_drc(board, evidence_ledger=drc.EvidenceLedger(), timeout=120, deadline=105.0)
    assert result.status == "ok"
    assert calls[0]["timeout"] == 3.0

    # Omitted deadline retains the original call shape and timeout value.
    legacy = _prepare_drc(monkeypatch, drc, board)
    calls.clear()
    monotonic = Mock(side_effect=AssertionError("legacy path must not consult monotonic time"))
    monkeypatch.setattr(drc.time, "monotonic", monotonic)
    result = drc.run_drc(legacy, evidence_ledger=drc.EvidenceLedger(), timeout=37)
    assert result.status == "ok"
    assert calls[0]["timeout"] == 37
    monotonic.assert_not_called()


@pytest.mark.parametrize("bad_deadline", [True, math.inf, math.nan, "123.0"])
def test_invalid_deadline_fails_closed_without_native_launch(monkeypatch, tmp_path, bad_deadline):
    bridge = kicad_bridge
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"board-input")
    finder = Mock()
    monkeypatch.setattr(bridge, "find_kicad_python", finder)

    response = bridge.run_kicad("inspect", board=source, deadline=bad_deadline)

    assert response["status"] == "error"
    finder.assert_not_called()
