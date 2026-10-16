"""The demo must reject plausible routing results for unverified board bytes."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "workflow_demo", Path(__file__).resolve().parents[1] / "scripts" / "validate_app_workflow.py"
)
_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_demo)


def test_demo_rejects_stale_final_board_evidence(tmp_path):
    board = tmp_path / "routed.kicad_pcb"
    board.write_text("verified board")
    payload = {"routing_complete": True, "output_kind": "routed_kicad_board",
               "drc": {"status": "ok", "passed": True, "blocker_count": 0,
                       "board": str(board), "board_sha256": _demo.digest(board)}}
    _demo.verify_routing(payload, board)
    board.write_text("changed after verification")
    with pytest.raises(RuntimeError, match="final board bytes"):
        _demo.verify_routing(payload, board)


def test_demo_rejects_partial_routing_even_with_clean_drc(tmp_path):
    with pytest.raises(RuntimeError, match="incomplete"):
        _demo.verify_routing({"routing_complete": False, "drc": {"passed": True}}, tmp_path / "board.kicad_pcb")
