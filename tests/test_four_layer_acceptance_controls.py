"""Behavioral checks for four-layer preflight negative controls."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

from benchmarks.four_layer_acceptance.asset_checks import check_footprint_assets
from benchmarks.four_layer_acceptance.controls import _component_rows, run_negative_controls
from circuit_weaver import footprint_lib

RUNNER_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "four_layer_acceptance" / "run_acceptance.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("four_layer_controls_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(RUNNER_SPEC)
assert RUNNER_SPEC and RUNNER_SPEC.loader
RUNNER_SPEC.loader.exec_module(runner)


def _iot_case() -> dict:
    return next(case for case in runner.load_cases() if case["case_id"] == "iot_environment_node")


def test_sdr_nested_parameters_are_one_physical_component():
    case = next(case for case in runner.load_cases() if case["case_id"] == "sdr_clock_carrier_control")
    original = copy.deepcopy(case)
    controls = run_negative_controls(case, runner._preflight)
    endpoint_control = next(row for row in controls if row["control_id"] == "swapped_endpoint_net")
    assert endpoint_control["status"] == "detected"
    assert endpoint_control["expectations_unchanged"] is True
    assert case == original


def _case(case_id: str) -> dict:
    return next(case for case in runner.load_cases() if case["case_id"] == case_id)


def _use_no_host_footprint_roots(monkeypatch) -> None:
    monkeypatch.setattr(footprint_lib, "_global_library_table_entries", lambda *_args: {})
    monkeypatch.setattr(
        footprint_lib.KiCadFootprintLibrary,
        "_default_roots",
        staticmethod(lambda _root: []),
    )
    monkeypatch.setattr(
        runner,
        "_footprint_asset_checks",
        lambda items, project_root: check_footprint_assets(items, roots=[], project_root=project_root),
    )


def test_real_preflight_detects_connectivity_and_stackup_mutations(monkeypatch) -> None:
    _use_no_host_footprint_roots(monkeypatch)
    case = _iot_case()
    original = copy.deepcopy(case)

    results = run_negative_controls(case, runner._preflight)
    by_id = {result["control_id"]: result for result in results}

    assert by_id["swapped_endpoint_net"]["status"] == "detected"
    assert by_id["swapped_endpoint_net"]["mutation_applied"] is True
    assert by_id["swapped_endpoint_net"]["baseline_preflight"]["status"] == "passed"
    assert by_id["swapped_endpoint_net"]["mutated_preflight"]["checks"] == [
        {
            "check": "required_endpoint",
            "target": "U3.4",
            "expected_net": "I2C_SCL",
            "actual_net": "I2C_SCL_WRONG",
            "status": "failed",
        }
    ]
    assert by_id["wrong_stackup"]["status"] == "detected"
    assert by_id["wrong_stackup"]["mutated_preflight"]["checks"]
    assert by_id["swapped_endpoint_net"]["expectations_unchanged"] is True
    assert by_id["wrong_stackup"]["expectations_unchanged"] is True
    assert case == original


def test_unavailable_library_context_keeps_library_control_unverified(monkeypatch) -> None:
    _use_no_host_footprint_roots(monkeypatch)
    case = _iot_case()
    results = run_negative_controls(case, runner._preflight)
    by_id = {result["control_id"]: result for result in results}

    library_control = by_id["missing_library_part"]
    assert library_control["mutation_applied"] is True
    assert library_control["status"] == "unverified"

    row = by_id["weak_electrical_limits"]
    assert row["mutation_applied"] is True
    assert row["status"] in {"unverified", "undetected"}
    assert row["status"] != "detected"
    assert row["baseline_snapshot"]["spec_sha256"] != row["mutated_snapshot"]["spec_sha256"] or (
        row["baseline_snapshot"]["case_sha256"] != row["mutated_snapshot"]["case_sha256"]
    )
    assert row["expectations_unchanged"] is True


def test_real_preflight_detects_missing_footprint_against_complete_temp_library(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(footprint_lib, "_global_library_table_entries", lambda *_args: {})
    monkeypatch.setattr(
        footprint_lib.KiCadFootprintLibrary,
        "_default_roots",
        staticmethod(lambda _root: []),
    )
    monkeypatch.setattr(
        runner,
        "_footprint_asset_checks",
        lambda items, project_root: check_footprint_assets(items, roots=[tmp_path], project_root=project_root),
    )
    case = _iot_case()

    discovery = runner._preflight(case)
    names = {
        row.get("normalized_footprint")
        for row in discovery["footprint_asset_resolution"]["checks"]
        if isinstance(row.get("normalized_footprint"), str) and ":" in row["normalized_footprint"]
    }
    assert names
    for qualified in names:
        nickname, name = qualified.split(":", 1)
        footprint = tmp_path / f"{nickname}.pretty" / f"{name}.kicad_mod"
        footprint.parent.mkdir(parents=True, exist_ok=True)
        footprint.write_text(f'(footprint "{name}")\n', encoding="utf-8")

    positive = runner._preflight(case)
    assert positive["status"] == "passed"
    results = run_negative_controls(case, runner._preflight)
    library_control = next(row for row in results if row["control_id"] == "missing_library_part")

    assert library_control["baseline_preflight"]["status"] == "passed"
    assert library_control["status"] == "detected"
    assert any(
        row.get("check") == "footprint_asset_resolution"
        and row.get("status") == "failed"
        and row.get("exists") is False
        for row in library_control["mutated_preflight"]["checks"]
    )


def test_ground_anchorless_qualified_footprint_expectation_matches_manifest(monkeypatch) -> None:
    _use_no_host_footprint_roots(monkeypatch)
    case = _case("iot_environment_node_ground_anchorless")

    result = runner._preflight(case)

    assert result["status"] == "passed"
    footprint_expectation = next(
        row
        for row in result["checks"]
        if row.get("check") == "compiled_part_declaration" and row.get("target") == "Resistor_SMD:R_0402_1005Metric"
    )
    assert footprint_expectation == {
        "check": "compiled_part_declaration",
        "target": "Resistor_SMD:R_0402_1005Metric",
        "status": "passed",
        "matched_by": "manifest_footprint",
    }


def test_relay_connectivity_control_mutates_template_backed_pin_override(monkeypatch) -> None:
    _use_no_host_footprint_roots(monkeypatch)
    case = _case("relay_output_8")
    original = copy.deepcopy(case)

    results = run_negative_controls(case, runner._preflight)
    control = next(row for row in results if row["control_id"] == "swapped_endpoint_net")

    assert control["mutation_applied"] is True
    assert control["status"] == "detected"
    assert control["expectations_unchanged"] is True
    assert any(
        row.get("check") == "required_endpoint" and row.get("target") == "U200.1" and row.get("status") == "failed"
        for row in control["mutated_preflight"]["checks"]
    )
    assert case == original


def test_component_lookup_ignores_duplicate_reference_metadata() -> None:
    ic_row = {"ref": "U90", "ic": "AD9528", "signal_nets": {"1": "EXT_REF_P"}}
    spec = {"digital": [ic_row], "placement": [{"ref": "U90", "x": 10, "y": 20}]}

    rows = _component_rows(spec, "U90")

    assert rows == [ic_row]
