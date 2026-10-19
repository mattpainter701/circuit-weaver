"""Focused checks for generic suite and evidence requirements."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "benchmarks" / "general_scale" / "run_general_scale.py"
SPEC = importlib.util.spec_from_file_location("general_scale_harness", HARNESS_PATH)
HARNESS = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(HARNESS)
CONNECTIVITY_PATH = ROOT / "benchmarks" / "general_scale" / "connectivity.py"
CONNECTIVITY_SPEC = importlib.util.spec_from_file_location("general_scale_connectivity", CONNECTIVITY_PATH)
CONNECTIVITY = importlib.util.module_from_spec(CONNECTIVITY_SPEC)
assert CONNECTIVITY_SPEC and CONNECTIVITY_SPEC.loader
CONNECTIVITY_SPEC.loader.exec_module(CONNECTIVITY)


def _valid_evidence(tmp_path: Path, count: int = 100, *, spec_hash: str = "spec-hash",
                    board_mm: list[float] | None = None) -> Path:
    board = tmp_path / "final-routed.kicad_pcb"
    board.write_bytes(b"routed-board-fixture")
    board_hash = hashlib.sha256(board.read_bytes()).hexdigest()
    names = sorted(HARNESS.REQUIRED_STEPS)
    components = [
        {"reference": f"R{i + 1}", "required": True, "visible_model_count": 1}
        for i in range(count)
    ]
    evidence = {
        "status": "PASS",
        "steps": [{"name": name, "passed": True} for name in names],
        "previews": {"front": {
            "model_coverage": {"expected": count, "resolved": count, "missing": 0},
            "component_coverage": {
                "expected": count, "represented": count, "missing": 0,
                "components": components,
            },
        }},
        "source_unchanged": True,
        "source_spec_sha256": spec_hash,
        "working_spec_sha256": spec_hash,
        "board_size_effective_mm": board_mm or [150, 110],
        "board_sha256": board_hash,
        "package_source_sha256": "package-hash",
        "runner_sha256": HARNESS.sha256(HARNESS.WORKFLOW),
        "drc": {"status": "ok", "passed": True, "blocker_count": 0,
                "board": str(board), "board_sha256": board_hash},
        "silkscreen": {"unresolved_count": 0, "remaining_conflict_count": 0},
    }
    (tmp_path / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    (tmp_path / "mcp-transcript.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_suite_has_three_distinct_connected_archetypes():
    suite, cases = HARNESS._load_suite(HARNESS.DEFAULT_SUITE)
    assert suite["minimum_physical_parts"] >= 100
    assert len(cases) >= 3
    assert len({case["family"] for case in cases}) >= 3
    assert {case["id"] for case in cases} >= {
        "sensor_control_hub", "field_io_gateway", "precision_control_daq",
    }


def test_samples_suite_is_separate_and_keeps_esp32_drill_override_explicit():
    suite, cases = HARNESS._load_suite(HARNESS.SAMPLE_SUITE)
    assert suite["suite_kind"] == "samples"
    assert suite["minimum_physical_parts"] == 1
    assert len(cases) == 13
    assert {case["id"] for case in cases} == {
        "battery_iot_sensor", "fpga_power_carrier", "high_voltage_isolation",
        "inverter_gate_driver", "iot_sensor_node", "led_power_indicator",
        "motor_controller", "oled_display_module", "rf_frontend",
        "usb_regulated_supply", "usb_uart_bridge", "wearable_bms", "zigbee_humidistat",
    }
    assert {case["id"] for case in cases if case.get("minimum_drill_mm")} == {
        "battery_iot_sensor", "iot_sensor_node",
    }
    general, _ = HARNESS._load_suite(HARNESS.DEFAULT_SUITE)
    assert general["minimum_physical_parts"] >= 100


def test_input_snapshot_preserves_fixture_and_hashes(tmp_path: Path):
    suite, cases = HARNESS._load_suite(HARNESS.DEFAULT_SUITE)
    original = cases[0]["_spec_path"]
    original_hash = HARNESS.sha256(original)
    output = tmp_path / "run"
    output.mkdir()
    snapshot = HARNESS._stage_inputs(HARNESS.DEFAULT_SUITE, suite, cases, output)
    assert HARNESS.sha256(cases[0]["_spec_path"]) == original_hash
    assert cases[0]["_source_spec_path"] == original
    assert snapshot["case_specs"][0]["sha256"] == original_hash
    assert Path(snapshot["suite_path"]).is_file()


def test_runner_leaves_output_directory_creation_to_workflow_validator(tmp_path: Path, monkeypatch):
    suite, cases = HARNESS._load_suite(HARNESS.DEFAULT_SUITE)
    output = tmp_path / "run"
    output.mkdir()
    HARNESS._stage_inputs(HARNESS.DEFAULT_SUITE, suite, cases, output)
    case = cases[0]
    args = argparse.Namespace(
        python=Path("python"), installed=False, expected_wheel=None,
        iterations=10, attempts=1, route_timeout=10, timeout=300,
        minimum_physical_parts=100,
    )

    def fake_workflow(command, **kwargs):
        if "-c" in command:
            contracts = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
            footprints = {}

            def connect(ref, pad, net):
                row = footprints.setdefault(ref, {"ref": ref, "pads": []})
                row["pads"].append({"number": str(pad), "net": net})

            for channel in contracts:
                for ref_key, pad_key, net_key in (
                    ("frontend_ref", "frontend_p_pad", "sensor_p_net"),
                    ("frontend_ref", "frontend_n_pad", "sensor_n_net"),
                    ("frontend_ref", "frontend_ref_pad", "reference_net"),
                    ("frontend_ref", "frontend_out_pad", "frontend_out_net"),
                    ("reference_ref", "reference_output_pad", "reference_net"),
                    ("input_connector_ref", "input_positive_pad", "input_positive_net"),
                    ("input_connector_ref", "input_negative_pad", "input_negative_net"),
                    ("adc_ref", "adc_pad", "adc_net"),
                    ("adc_filter_ref", "adc_filter_input_pad",
                     "post_stage_output_net" if "post_stage_ref" in channel else "frontend_out_net"),
                    ("adc_filter_ref", "adc_filter_output_pad", "adc_net"),
                ):
                    connect(channel[ref_key], channel[pad_key], channel[net_key])
                if "post_stage_ref" in channel:
                    connect(channel["post_stage_ref"], channel["post_stage_input_pad"], channel["frontend_out_net"])
                    connect(channel["post_stage_ref"], channel["post_stage_output_pad"],
                            channel["post_stage_output_net"])
            inventory = {"status": "ok", "footprints": list(footprints.values())}
            proof = CONNECTIVITY.verify_native_connectivity(inventory, contracts)
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"inventory": inventory, "connectivity": proof}), ""
            )
        assert "--width" not in command and "--height" not in command
        run_dir = Path(command[command.index("--output") + 1])
        assert not run_dir.exists()
        run_dir.mkdir(parents=True)
        _valid_evidence(run_dir, count=100, spec_hash=case["_spec_sha256"], board_mm=case["board_mm"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(HARNESS.subprocess, "run", fake_workflow)
    case["board_mm"] = [150, 110]
    result = HARNESS._run_case(case, args, output)
    assert result["status"] == "PASS", result
    assert (output / "runs" / case["id"] / "evidence.json").is_file()
    assert (output / "runs" / case["id"] / "native-pcb-inventory.json").is_file()
    assert (output / "logs" / f"{case['id']}.invocation.json").is_file()


def test_evidence_requires_all_stages_and_real_assembly_inventory(tmp_path: Path):
    run = _valid_evidence(tmp_path)
    result = HARNESS._validate_evidence(run, 100, expected_spec_hash="spec-hash")
    assert result["physical_part_count"] == 100
    assert set(result["passed_stages"]) == HARNESS.REQUIRED_STEPS


def test_package_fingerprint_covers_native_models_and_metadata_only(tmp_path: Path):
    package = tmp_path / "circuit_weaver"
    package.mkdir()
    (package / "module.py").write_text("source", encoding="utf-8")
    (package / "catalog.json").write_text("{}", encoding="utf-8")
    (package / "model.step").write_text("step-v1", encoding="utf-8")
    (package / "provenance.md").write_text("source record", encoding="utf-8")
    (package / "metadata.yaml").write_text("revision: one", encoding="utf-8")
    (package / "circuit-weaver.log").write_text("ignored runtime log", encoding="utf-8")
    before = HARNESS._package_source_sha256(package)
    (package / "model.step").write_text("step-v2", encoding="utf-8")
    after = HARNESS._package_source_sha256(package)
    assert after != before
    (package / "circuit-weaver.log").write_text("changed runtime log", encoding="utf-8")
    assert HARNESS._package_source_sha256(package) == after


def test_evidence_binds_package_payload_and_workflow_runner(tmp_path: Path):
    run = _valid_evidence(tmp_path)
    path = run / "evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["package_source_sha256"] = "selected-package-hash"
    evidence["runner_sha256"] = "selected-runner-hash"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    result = HARNESS._validate_evidence(
        run, 100, expected_spec_hash="spec-hash",
        expected_package_hash="selected-package-hash", expected_runner_hash="selected-runner-hash",
    )
    assert result["physical_part_count"] == 100
    evidence["package_source_sha256"] = "different-package-hash"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(RuntimeError, match="package source hash"):
        HARNESS._validate_evidence(
            run, 100, expected_spec_hash="spec-hash",
            expected_package_hash="selected-package-hash", expected_runner_hash="selected-runner-hash",
        )


@pytest.mark.parametrize("mutation, message", [
    ("stage", "failed or malformed stage"),
    ("count", "physical parts"),
    ("board_hash", "board bytes"),
    ("model", "without a visible"),
])
def test_evidence_rejects_incomplete_or_inconsistent_proof(tmp_path: Path, mutation: str, message: str):
    run = _valid_evidence(tmp_path, 99 if mutation == "count" else 100)
    path = run / "evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "stage":
        evidence["steps"][0]["passed"] = False
    elif mutation == "board_hash":
        evidence["drc"]["board_sha256"] = "wrong"
    elif mutation == "model":
        evidence["previews"]["front"]["component_coverage"]["components"][0]["visible_model_count"] = 0
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(RuntimeError, match=message):
        HARNESS._validate_evidence(run, 100, expected_spec_hash="spec-hash")
