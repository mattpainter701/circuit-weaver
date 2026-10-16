"""The demo must reject plausible routing results for unverified board bytes."""

import csv
import importlib.util
import json
import subprocess
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


def test_demo_rejects_preview_for_stale_board_or_changed_svg(tmp_path):
    board = tmp_path / "routed.kicad_pcb"
    board.write_text("verified board")
    svg = tmp_path / "board-preview.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    payload = {"status": "ok", "source_sha256": _demo.digest(board), "svg_sha256": _demo.digest(svg),
               "side": "front", "prototype": True, "outline_count": 1, "layers": ["F.Cu", "F.Fab"]}
    _demo.verify_preview(payload, board, svg, "front")
    board.write_text("changed board")
    with pytest.raises(RuntimeError, match="different board bytes"):
        _demo.verify_preview(payload, board, svg, "front")
    payload["source_sha256"] = _demo.digest(board)
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><path/></svg>')
    with pytest.raises(RuntimeError, match="SVG bytes"):
        _demo.verify_preview(payload, board, svg, "front")


def test_demo_retains_captured_subprocess_error_detail():
    error = subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"],
                                         stderr="fatal: detected dubious ownership in repository")

    assert "fatal: detected dubious ownership" in _demo.exception_message(error)


def _assembly_delivery(tmp_path, designators="U1,R1"):
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    items = [
        {"reference": "U1", "dnp": False, "exclude_from_bom": False},
        {"reference": "R1", "dnp": False, "exclude_from_bom": False},
        {"reference": "C1", "dnp": True, "exclude_from_bom": False},
        {"reference": "TP1", "dnp": False, "exclude_from_bom": True},
    ]
    (delivery / "assembly_manifest.json").write_text(
        json.dumps({"item_count": len(items), "items": items}), encoding="utf-8"
    )
    (delivery / "delivery_manifest.json").write_text(
        json.dumps({
            "status": "blocked", "fabrication_ready": False, "assembly_item_count": len(items),
            "artifacts": [
                {"kind": "bom", "path": "bom_jlcpcb.csv", "status": "ready"},
                {"kind": "cpl", "path": "cpl_jlcpcb.csv", "status": "blocked"},
            ],
        }), encoding="utf-8"
    )
    with (delivery / "bom_jlcpcb.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part#"])
        writer.writerow(["device", designators, "SOT-23", ""])
    return delivery, {"status": "blocked", "fabrication_ready": False,
                      "assembly_item_count": len(items), "bom_rows": 1}


def test_demo_verifies_blocked_bom_matches_active_assembly_inventory(tmp_path):
    delivery_dir, payload = _assembly_delivery(tmp_path)

    assert _demo.verify_blocked_assembly_delivery(payload, delivery_dir) == {
        "assembly_items": 4, "bom_rows": 1, "bom_designators": 2,
    }


@pytest.mark.parametrize("designators", ["U1", "U1,R1,R1", "U1,R1,EXTRA"])
def test_demo_rejects_bom_missing_duplicate_or_unexpected_designators(tmp_path, designators):
    delivery_dir, payload = _assembly_delivery(tmp_path, designators)

    with pytest.raises(RuntimeError, match="designator"):
        _demo.verify_blocked_assembly_delivery(payload, delivery_dir)
