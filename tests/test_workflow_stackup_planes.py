import copy
import json
from pathlib import Path

import pytest

from circuit_weaver import project_workflow as workflow

PLAN = {"net": "GND", "layers": ["In1.Cu", "In2.Cu"], "protected_connections": [],
        "edge_clearance_mm": 0.5, "clearance_mm": 0.25, "min_thickness_mm": 0.2,
        "thermal_gap_mm": 0.25, "thermal_spoke_width_mm": 0.3}


def _refill():
    return {"status": "verified", "saved_fill_geometry_verified": True,
            "zone_definitions_preserved": True, "copper_layers_preserved": True, "zone_count": 2,
            "unconnected_pads_by_net": {"GND": []},
            "connected_copper": [{"id": name, "connected_copper_count": 3} for name in PLAN["layers"]],
            "zones": [{"id": name, "net": "GND", "layer_name": name, "area_mm2": 100.0,
                       "island_count": 0, "polygon_sha256": "a" * 64} for name in PLAN["layers"]]}


def _native(*args, **kwargs):
    return {"status": "ok", "copper_layer_inventory_complete": True, "copper_layer_count": 4,
            "copper_layer_names": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]}


@pytest.mark.parametrize("count", [True, False, 3, 6, "4", 4.0, None])
def test_workflow_rejects_unsupported_or_ambiguous_copper_count(count):
    with pytest.raises(workflow.WorkflowError) as error:
        workflow._normalize_options({"copper_layers": count})
    assert error.value.code == "invalid_options"


def test_stackup_and_plane_changes_invalidate_plan_hash(tmp_path):
    (tmp_path / "placed.kicad_pcb").write_text("board")
    two = workflow._plan_data(tmp_path, ["route"], {"copper_layers": 2})
    four = workflow._plan_data(tmp_path, ["route"], {"copper_layers": 4})
    plane = workflow._plan_data(tmp_path, ["route"], {"copper_layers": 4, "ground_plane": PLAN})
    assert len({two["hash"], four["hash"], plane["hash"]}) == 3


def test_requested_four_layers_cannot_silently_route_a_two_layer_board(tmp_path, monkeypatch):
    (tmp_path / "placed.kicad_pcb").write_text("board")
    monkeypatch.setattr("circuit_weaver.kicad_bridge.run_kicad", lambda *a, **k: {
        **_native(), "copper_layer_count": 2, "copper_layer_names": ["F.Cu", "B.Cu"],
    })
    monkeypatch.setattr(workflow, "_cli", lambda *a: pytest.fail("router must not start"))
    with pytest.raises(workflow.WorkflowError) as error:
        workflow._execute_stage(tmp_path, "route", {"copper_layers": 4}, [])
    assert error.value.code == "stackup_mismatch"


def _routing_fakes(tmp_path, monkeypatch, report):
    original = tmp_path / "placed.kicad_pcb"
    original.write_bytes(b"original board")
    monkeypatch.setattr("circuit_weaver.kicad_bridge.run_kicad", _native)
    observed = {}

    def planes(source, target, plan, **options):
        assert source == original and plan == PLAN and options["pre_route"] is True
        target.write_bytes(b"private filled board")
        observed["plane_board"] = target
        return {"status": "ok", "output_path": str(target), "routing_complete": False,
                "requires_route": True, "fabrication_ready": False}

    def cli(stage, command, root, timeout):
        assert command[1] == str(observed["plane_board"])
        Path(command[command.index("--output") + 1]).write_text("session")
        Path(command[command.index("--routed-board") + 1]).write_text("routed board")
        return {"status": "ok", "zone_refill": report}

    monkeypatch.setattr("circuit_weaver.pcb_ground_plane.add_ground_plane", planes)
    monkeypatch.setattr(workflow, "_cli", cli)
    return original, observed


def test_route_prepares_planes_privately_then_publishes_verified_evidence(tmp_path, monkeypatch):
    original, observed = _routing_fakes(tmp_path, monkeypatch, _refill())
    result, outputs = workflow._execute_stage(tmp_path, "route", {"copper_layers": 4, "ground_plane": PLAN}, [])
    assert original.read_bytes() == b"original board"
    assert observed["plane_board"].is_file()
    assert (tmp_path / "routed.kicad_pcb").is_file()
    assert str(tmp_path / "ground-plane.json") in outputs
    evidence = json.loads((tmp_path / "ground-plane.json").read_text())
    assert evidence["after_route"]["saved_fill_geometry_verified"]
    assert evidence["routing_complete"] is True
    assert evidence["electrical_or_manufacturing_approval"] is False
    assert result["copper_layers"]["count"] == 4


@pytest.mark.parametrize("mutation", ["missing", "stale", "island", "wrong_net", "count", "area", "opens", "anchor"])
def test_route_refuses_unverified_planes_before_publication(tmp_path, monkeypatch, mutation):
    report = copy.deepcopy(_refill())
    if mutation == "missing":
        report = None
    elif mutation == "stale":
        report["saved_fill_geometry_verified"] = False
    elif mutation == "island":
        report["zones"][0]["island_count"] = 1
    elif mutation == "wrong_net":
        report["zones"][0]["net"] = "VCC"
    elif mutation == "count":
        report["zone_count"] = 1
    elif mutation == "opens":
        report["unconnected_pads_by_net"]["GND"] = ["C1.2"]
    elif mutation == "anchor":
        report["connected_copper"][0]["connected_copper_count"] = 0
    else:
        report["zones"][0]["area_mm2"] = float("nan")
    original, _ = _routing_fakes(tmp_path, monkeypatch, report)
    with pytest.raises(workflow.WorkflowError) as error:
        workflow._execute_stage(tmp_path, "route", {"copper_layers": 4, "ground_plane": PLAN}, [])
    assert error.value.code == "plane_verification_failed"
    assert original.read_bytes() == b"original board"
    assert not (tmp_path / "routed.kicad_pcb").exists()
    assert not (tmp_path / "routed.ses").exists()
    assert list((tmp_path / ".circuit-weaver/attempts").glob("route-*/ground-plane-after.json"))


def test_build_forwards_explicit_layer_count(tmp_path, monkeypatch):
    folder = tmp_path / "output"
    folder.mkdir()
    (folder / "design.kicad_sch").write_text("schematic")
    seen = []

    def cli(stage, command, root, timeout):
        seen.extend(command)
        path = Path(command[command.index("--output") + 1])
        path.write_text("board")
        return {"status": "ok", "output_path": str(path)}

    monkeypatch.setattr(workflow, "_cli", cli)
    workflow._execute_stage(tmp_path, "build", {"board_width_mm": 20, "board_height_mm": 20,
                                               "copper_layers": 4}, [])
    assert seen[seen.index("--copper-layers") + 1] == "4"
