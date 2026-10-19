"""Focused checks for generic suite and evidence requirements."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import venv
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
        "status": "PASS — prototype workflow validated; manufacturing export correctly blocked",
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
    assert result["workflow_status"].startswith("PASS — prototype workflow validated")
    assert (output / "runs" / case["id"] / "evidence.json").is_file()
    assert (output / "runs" / case["id"] / "native-pcb-inventory.json").is_file()
    assert (output / "logs" / f"{case['id']}.invocation.json").is_file()


def test_run_case_preserves_venv_interpreter_and_its_site_packages(tmp_path: Path, monkeypatch):
    venv_root = tmp_path / "isolated-venv"
    venv.EnvBuilder(
        with_pip=False, system_site_packages=False, symlinks=(os.name != "nt"),
    ).create(venv_root)
    python = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if os.name != "nt":
        assert python.is_symlink()
    site_packages = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (Path(site_packages) / "venv_only_marker.py").write_text("VALUE = 'loaded'\n", encoding="utf-8")
    if os.name != "nt":
        resolved_probe = subprocess.run(
            [str(python.resolve()), "-c", (
                "import importlib.util,sys; "
                "print(sys.prefix); print(importlib.util.find_spec('venv_only_marker') is not None)"
            )],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        assert Path(resolved_probe[0]) != venv_root
        assert resolved_probe[1] == "False"

    workflow = tmp_path / "workflow_probe.py"
    workflow.write_text(
        "import json, pathlib, sys\n"
        "try:\n"
        " import venv_only_marker\n"
        " marker_loaded = venv_only_marker.VALUE == 'loaded'\n"
        "except ImportError:\n"
        " marker_loaded = False\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.mkdir(parents=True)\n"
        "(out / 'interpreter.json').write_text(json.dumps({'prefix': sys.prefix, 'marker_loaded': marker_loaded}))\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(HARNESS, "WORKFLOW", workflow)
    spec_path = tmp_path / "fixture.yaml"
    spec_path.write_text("name: fixture\n", encoding="utf-8")
    case = {
        "id": "venv_probe", "family": "test", "_spec_path": spec_path,
        "_spec_sha256": HARNESS.sha256(spec_path), "_source_spec_path": spec_path,
        "board_mm": [10, 10],
    }
    output = tmp_path / "run"
    args = argparse.Namespace(
        python=python, installed=False, expected_wheel=None,
        iterations=1, attempts=1, route_timeout=1, timeout=60,
        minimum_physical_parts=1,
    )

    result = HARNESS._run_case(case, args, output)
    captured = json.loads((output / "runs" / case["id"] / "interpreter.json").read_text(encoding="utf-8"))
    invocation = json.loads((output / "logs" / f"{case['id']}.invocation.json").read_text(encoding="utf-8"))

    assert result["status"] == "FAILED"  # The probe deliberately stops before evidence validation.
    assert os.path.normcase(os.path.abspath(captured["prefix"])) == os.path.normcase(str(venv_root.absolute()))
    assert captured["marker_loaded"] is True
    assert invocation["python"] == str(python.absolute())


def test_source_probe_checks_mcp_in_the_selected_interpreter(tmp_path: Path, monkeypatch):
    executable = tmp_path / "selected-venv" / "bin" / "python"
    calls = []
    package_init = HARNESS.REPO / "src" / "circuit_weaver" / "__init__.py"
    generator = HARNESS.REPO / "src" / "circuit_weaver" / "generator.py"

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, f"{package_init}\n{generator}\n", "",
        )

    monkeypatch.setattr(HARNESS.subprocess, "run", fake_run)
    HARNESS._source_probe(executable, installed=False, wheel=None, timeout=30)

    assert calls[0][0] == str(executable.absolute())
    assert "from mcp import ClientSession, StdioServerParameters" in calls[0][2]
    assert "from mcp.client.stdio import stdio_client" in calls[0][2]


def test_native_connectivity_probe_preserves_selected_executable(tmp_path: Path, monkeypatch):
    executable = tmp_path / "selected-venv" / "bin" / "python"
    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"native board")
    output = tmp_path / "run"
    logs = tmp_path / "logs"
    output.mkdir()
    logs.mkdir()
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0,
            json.dumps({"inventory": {"status": "ok", "footprints": []},
                        "connectivity": {"status": "pass", "verified_channels": 0}}), "",
        )

    monkeypatch.setattr(HARNESS.subprocess, "run", fake_run)
    HARNESS._inspect_and_verify_connectivity(
        {"id": "native_probe", "required_channels": []},
        argparse.Namespace(python=executable, installed=False, timeout=60),
        output, logs, board,
    )

    assert commands[0][0] == str(executable.absolute())


def test_main_aggregates_canonical_pass_with_verbose_workflow_status(tmp_path: Path, monkeypatch, capsys):
    suite_path = HARNESS.DEFAULT_SUITE
    case = {"id": "fixture", "family": "test"}
    source = {
        "status": "", "revision": "r1", "workflow_sha256": "workflow-hash",
        "benchmark_tree_sha256": "benchmark-hash", "package_tree_sha256": "package-hash",
    }
    monkeypatch.setattr(HARNESS, "source_state", lambda: dict(source))
    monkeypatch.setattr(HARNESS, "_load_suite", lambda _path: ({"minimum_physical_parts": 1}, [case]))
    monkeypatch.setattr(HARNESS, "_stage_inputs", lambda *_args: {"suite_path": "staged-suite.yaml"})
    monkeypatch.setattr(HARNESS, "_source_probe", lambda *_args: {"package_source_sha256": "package-hash"})
    monkeypatch.setattr(HARNESS, "_run_case", lambda *_args: {
        "case_id": "fixture", "status": "PASS",
        "workflow_status": "PASS — prototype workflow validated; manufacturing export correctly blocked",
    })

    output = tmp_path / "aggregate"
    result = HARNESS.main(["--suite", str(suite_path), "--output", str(output)])
    manifest = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))

    assert result == 0
    assert manifest["status"] == "PASS"
    assert manifest["results"][0]["status"] == "PASS"
    assert manifest["results"][0]["workflow_status"].startswith("PASS —")
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_evidence_requires_all_stages_and_real_assembly_inventory(tmp_path: Path):
    run = _valid_evidence(tmp_path)
    result = HARNESS._validate_evidence(run, 100, expected_spec_hash="spec-hash")
    assert result["workflow_status"].startswith("PASS — prototype workflow validated")
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
