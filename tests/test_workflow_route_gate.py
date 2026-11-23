from __future__ import annotations

import json
from pathlib import Path

import pytest

from circuit_weaver import board_review
from circuit_weaver import project_workflow as workflow


def _contracts(limit):
    return {"routes": {"clock": {"net": "CLK", "source": "AUTHORED", "max_length_mm": limit,
                                 "from": {"ref": "U1", "pad": "1"}, "to": {"ref": "J1", "pad": "1"}}}}


def _native(*args, **kwargs):
    return {"status": "ok", "rectangular_outline": True,
            "bounds": {"x": 0, "y": 0, "width": 20, "height": 20},
            "footprints": [{"ref": ref, "x": x, "y": 5, "rotation": 0, "width": 2, "height": 2,
                            "pads": [{"number": "1", "net": "CLK", "x": x, "y": 5, "copper_layers": [0]}]}
                           for ref, x in [("U1", 5), ("J1", 10)]],
            "tracks": [{"net": "CLK", "kind": "segment", "start_mm": [5, 5], "end_mm": [10, 5], "layer_id": 0}],
            "vias": [], "track_count": 1, "zone_count": 0}


def _fake_cli(stage, args, cwd, timeout):
    Path(args[args.index("--output") + 1]).write_text("(session)")
    Path(args[args.index("--routed-board") + 1]).write_text("(kicad_pcb)")
    return {"status": "ok"}


@pytest.mark.parametrize("limit,expected", [(6, "verified"), (1, "failed"), (True, "unverified")])
def test_authored_route_gate_runs_before_publication(tmp_path, monkeypatch, limit, expected):
    (tmp_path / "placed.kicad_pcb").write_text("(kicad_pcb)")
    monkeypatch.setattr(workflow, "_cli", _fake_cli)
    monkeypatch.setattr(board_review, "run_kicad", _native)
    if expected != "verified":
        with pytest.raises(workflow.WorkflowError, match="Authored route checks") as caught:
            workflow._execute_stage(tmp_path, "route", {"route_contracts": _contracts(limit)}, [])
        assert caught.value.code == "route_contract_failed"
        assert not (tmp_path / "routed.kicad_pcb").exists()
        assert not (tmp_path / "routed.ses").exists()
        report = next((tmp_path / ".circuit-weaver/attempts").glob("route-*/route-quality.json"))
    else:
        result, outputs = workflow._execute_stage(tmp_path, "route", {"route_contracts": _contracts(limit)}, [])
        assert result["route_quality"]["status"] == "verified"
        assert str(tmp_path / "route-quality.json") in outputs
        assert (tmp_path / "routed.kicad_pcb").exists() and (tmp_path / "routed.ses").exists()
        report = tmp_path / "route-quality.json"
    saved = json.loads(report.read_text())
    assert saved["status"] == expected
    assert len(saved["board_sha256"]) == 64 and len(saved["contract_sha256"]) == 64


@pytest.mark.parametrize("contracts", [
    {}, {"routes": []}, {"routes": {"x": {}}, "impedance_ohms": 50},
    {"routes": {1: {}, "1": {}}},
])
def test_empty_or_unrecognized_gate_options_fail_before_running(tmp_path, contracts):
    (tmp_path / "placed.kicad_pcb").write_text("(kicad_pcb)")
    result = workflow.plan_workflow(tmp_path, stages=["route"], options={"route_contracts": contracts})
    assert not result["ok"]
    assert result["error"]["code"] == "invalid_options"


def test_reviewed_candidate_mutation_during_publish_rolls_back(tmp_path, monkeypatch):
    (tmp_path / "placed.kicad_pcb").write_text("(kicad_pcb)")
    monkeypatch.setattr(workflow, "_cli", _fake_cli)
    monkeypatch.setattr(board_review, "run_kicad", _native)
    original_link = workflow.os.link

    def mutate_after_link(source, target):
        original_link(source, target)
        if Path(source).name == "routed.kicad_pcb":
            Path(source).write_text("different unreviewed native candidate")

    monkeypatch.setattr(workflow.os, "link", mutate_after_link)
    with pytest.raises(workflow.WorkflowError) as caught:
        workflow._execute_stage(tmp_path, "route", {"route_contracts": _contracts(6)}, [])
    assert caught.value.code == "stage_output_changed"
    assert not (tmp_path / "routed.kicad_pcb").exists()
    assert not (tmp_path / "routed.ses").exists()
    assert not (tmp_path / "route-quality.json").exists()
    assert next((tmp_path / ".circuit-weaver/attempts").glob("route-*/routed.kicad_pcb")).exists()
