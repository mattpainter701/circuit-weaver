"""Regression tests for pair sampling and the native replay evidence gate."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks/general_scale/replay_routing_repair.py"
spec = importlib.util.spec_from_file_location("repair_replay", SCRIPT)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


@pytest.mark.parametrize(
    ("count", "limit", "expected"),
    [(0, None, []), (8, 0, []), (8, 1, [0]), (8, 3, [0, 3, 7]), (2, 5, [0, 1]), (3, None, [0, 1, 2])],
)
def test_samples_cover_distance_range(count, limit, expected):
    assert list(replay.sample_indices(count, limit)) == expected


@pytest.mark.parametrize("defect", ["stale", "incomplete", "missing_inventory"])
def test_probes_require_complete_native_report_on_current_board(monkeypatch, tmp_path, defect):
    board = tmp_path / "input.kicad_pcb"
    board.write_bytes(b"frozen input")
    raw = {"violations": [], "unconnected_items": [], "schematic_parity": []}
    result = SimpleNamespace(
        status="ok", board_sha256=replay.file_sha256(board), raw_report=raw,
        diagnostics_complete=True, failure_reason="test DRC",
    )
    if defect == "stale":
        board.write_bytes(b"changed")
    elif defect == "incomplete":
        result.diagnostics_complete = False
    else:
        del raw["violations"]
    monkeypatch.setattr(replay, "run_drc", lambda *args, **kwargs: result)
    with pytest.raises(RuntimeError, match="Incomplete native DRC"):
        replay.checked_drc(board)

