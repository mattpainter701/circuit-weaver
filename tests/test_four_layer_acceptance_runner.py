from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "four_layer_acceptance" / "run_acceptance.py"
SPEC = importlib.util.spec_from_file_location("four_layer_acceptance_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runner)


def test_completed_route_with_open_contract_cannot_pass():
    checkpoint = {
        "status": "completed",
        "result": {"route_quality": {"status": "failed", "routes": {"CLK": {"status": "failed", "reason": "open"}}}},
    }
    assert runner._phase_status(checkpoint, "route", {"board": "a" * 64}) == "unverified"


def test_completed_route_without_authored_route_evidence_cannot_pass():
    checkpoint = {"status": "completed", "result": {"status": "ok"}}
    assert runner._phase_status(checkpoint, "route", {"board": "a" * 64}) == "unverified"


def test_route_evidence_with_reported_open_cannot_pass():
    checkpoint = {
        "status": "completed",
        "result": {"route_quality": {"status": "verified", "routes": {"CLK": {"status": "verified", "open_count": 1}}}},
    }
    assert runner._phase_status(checkpoint, "route", {"board": "a" * 64}) == "unverified"


def test_completed_route_with_verified_contract_and_artifacts_passes():
    checkpoint = {
        "status": "completed",
        "result": {"route_quality": {"status": "verified", "routes": {"CLK": {"status": "verified"}}}},
    }
    assert runner._phase_status(checkpoint, "route", {"board": "a" * 64}) == "passed"


def _golden_native_proof():
    return {
        "provenance": {
            "source": "small-four-layer-flow04/evidence.json",
            "source_sha256": "0b33d88aa1699a51af3289cab707df663cc6c6587e86a35b6ee40760ecea8c67",
        },
        "routed_board_sha256": "43505c528cd13bfd6ac96919852f204dbb427a72880b170bfab301ff2ac7d42a",
        "route_result": {
            "routing_complete": True,
            "drc": {
                "status": "ok",
                "passed": True,
                "blocker_count": 0,
                "findings": [],
                "board_sha256": "43505c528cd13bfd6ac96919852f204dbb427a72880b170bfab301ff2ac7d42a",
            },
            "verification": {"kicad_drc": "passed", "connection_completeness": "verified"},
            "route_quality": {"status": "verified", "routes": {"I2C_SCL": {"status": "verified"}}},
        },
        "stackup": {"count": 4, "ordered_names": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]},
        "ground_plan": {"net": "GND", "layers": ["In1.Cu", "In2.Cu"]},
        "plane": {
            "status": "verified",
            "saved_fill_geometry_verified": True,
            "zone_definitions_preserved": True,
            "copper_layers_preserved": True,
            "zone_count": 2,
            "unconnected_pads_by_net": {"GND": []},
            "connected_copper": [
                {"id": "zone1", "connected_copper_count": 3},
                {"id": "zone2", "connected_copper_count": 3},
            ],
            "zones": [
                {
                    "id": "zone1",
                    "net": "GND",
                    "layer_name": "In1.Cu",
                    "area_mm2": 1650.15,
                    "island_count": 0,
                    "polygon_sha256": "9" * 64,
                },
                {
                    "id": "zone2",
                    "net": "GND",
                    "layer_name": "In2.Cu",
                    "area_mm2": 1650.15,
                    "island_count": 0,
                    "polygon_sha256": "9" * 64,
                },
            ],
        },
        "preview": {
            "component_coverage": {"expected": 4, "represented": 4, "missing": 0},
            "model_coverage": {"expected": 4, "resolved": 4, "missing": 0},
        },
    }


def test_golden_native_four_layer_proof_can_pass_cad_but_not_product_qualification():
    proof = _golden_native_proof()
    drc = runner._native_drc_evidence(proof["route_result"], proof["routed_board_sha256"])
    continuity, pads = runner._ground_plane_evidence(proof["plane"], proof["ground_plan"])
    gates = {
        "native_drc": drc["status"],
        "route_connectivity_contracts": "passed",
        "four_layer_stackup": "passed" if runner._four_layer_stackup_complete(proof["stackup"]) else "unverified",
        "ground_plane_continuity": "passed" if continuity else "unverified",
        "ground_pad_connections": "passed" if pads else "unverified",
        "modeled_assembly": "passed" if runner._coverage_complete(proof["preview"]) else "unverified",
        "electrical_function": "unverified",
        "timing": "unverified",
        "impedance": "unverified",
        "thermal": "unverified",
    }
    assert proof["provenance"]["source_sha256"] == "0b33d88aa1699a51af3289cab707df663cc6c6587e86a35b6ee40760ecea8c67"
    assert (
        runner._gate_group_status(
            gates,
            (
                "native_drc",
                "route_connectivity_contracts",
                "four_layer_stackup",
                "ground_plane_continuity",
                "ground_pad_connections",
                "modeled_assembly",
            ),
        )
        == "passed"
    )
    cad_keys = (
        "native_drc",
        "route_connectivity_contracts",
        "four_layer_stackup",
        "ground_plane_continuity",
        "ground_pad_connections",
        "modeled_assembly",
    )
    assert runner._cad_acceptance_status({"phases": [{"status": "passed"}], "gates": gates}, cad_keys) == "passed"
    assert runner._gate_group_status(gates, ("electrical_function", "timing", "impedance", "thermal")) == "unverified"
    assert runner._cad_acceptance_status({"phases": [{"status": "failed"}], "gates": gates}, cad_keys) == "failed"


def test_golden_cad_gates_fail_closed_on_open_stackup_zone_pad_or_model_gap():
    proof = _golden_native_proof()
    open_route = copy.deepcopy(proof["route_result"]["route_quality"])
    open_route["routes"]["I2C_SCL"]["open_count"] = 1
    assert not runner._route_quality_complete(open_route)
    assert not runner._four_layer_stackup_complete({"count": 2, "ordered_names": ["F.Cu", "B.Cu"]})
    isolated_plane = copy.deepcopy(proof["plane"])
    isolated_plane["zones"][0]["island_count"] = 1
    assert runner._ground_plane_evidence(isolated_plane, proof["ground_plan"])[0] is False
    open_pad = copy.deepcopy(proof["plane"])
    open_pad["unconnected_pads_by_net"]["GND"] = ["R2.2"]
    assert runner._ground_plane_evidence(open_pad, proof["ground_plan"])[1] is False
    missing_model = copy.deepcopy(proof["preview"])
    missing_model["model_coverage"]["missing"] = 1
    assert not runner._coverage_complete(missing_model)


def test_runner_refuses_nonempty_output_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "load_cases", lambda: [{"case_id": "alpha", "spec": {}}])
    output = tmp_path / "existing"
    output.mkdir()
    (output / "old.json").write_text("{}")
    args = argparse.Namespace(case=None, through="preflight", output=output)
    with pytest.raises(FileExistsError, match="refusing to reuse"):
        runner.run(args)


def test_reports_keep_case_evidence_separate_and_scope_preflight_as_unverified(tmp_path, monkeypatch):
    cases = [
        {"case_id": "alpha", "title": "Alpha", "family": "analog", "spec": {"id": "a"}, "controls": []},
        {"case_id": "beta", "title": "Beta", "family": "digital", "spec": {"id": "b"}, "controls": []},
    ]
    monkeypatch.setattr(runner, "load_cases", lambda: cases)
    monkeypatch.setattr(runner, "_git", lambda *args: "test-revision")

    def preflight(case):
        count = 11 if case["case_id"] == "alpha" else 22
        return {
            "status": "passed",
            "checks": [],
            "metrics": {
                "component_count": count,
                "pad_count": count * 2,
                "net_count": count - 1,
                "board_area_mm2": None,
            },
            "connectivity": {"compiled_net_count": count - 1},
            "assembly": {"manifest_item_count": count, "footprint_missing_count": 0, "footprint_missing_refs": []},
        }

    monkeypatch.setattr(runner, "_preflight", preflight)
    args = argparse.Namespace(case=None, through="preflight", output=tmp_path / "results")
    assert runner.run(args) == 0
    report = json.loads((tmp_path / "results" / "acceptance-report.json").read_text())
    by_id = {case["case_id"]: case for case in report["cases"]}
    assert set(by_id) == {"alpha", "beta"}
    assert by_id["alpha"]["metrics"]["component_count"] == 11
    assert by_id["beta"]["metrics"]["component_count"] == 22
    assert by_id["alpha"]["acceptance_status"] == "unverified"
    assert by_id["beta"]["acceptance_status"] == "unverified"
    assert by_id["alpha"]["gates"]["native_drc"] == "unverified"
    assert report["summary"]["unverified"] == 2
