"""Preserve pre-trim circuit coverage without expanded generated copies."""

import importlib.util
import json
from pathlib import Path

import pytest

from benchmarks.diverse_systems.build_diverse_trials import build_corpus, verify_corpus

ROOT = Path(__file__).resolve().parents[1]


def test_regeneration_matches_frozen_pretrim_inputs_and_runner_loads_every_case(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "generated_suite_runner", ROOT / "benchmarks/general_scale/run_general_scale.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generated = module._materialize_suite(ROOT / "benchmarks/diverse_systems/cases.yaml", tmp_path)
    suite, cases = module._load_suite(generated)
    assert len(cases) == 5
    assert {case["id"] for case in cases} == {
        "relay_768", "ble_sensor_8", "sdr_clock_carrier", "ot_mixed_io_comparison", "dual_dac_72_comparison",
    }
    assert next(case for case in cases if case["id"] == "relay_768")["required_path_count"] == 768
    assert next(case for case in cases if case["id"] == "sdr_clock_carrier")["required_path_count"] == 74
    assert len(suite["cases"]) == 5
    assert not (ROOT / "benchmarks/diverse_systems/contracts").exists()
    with pytest.raises(FileExistsError):
        build_corpus(ROOT, generated.parent)
    contract = generated.parent / "contracts/relay_768.json"
    data = json.loads(contract.read_text())
    data["required_paths"][0]["source"]["pad"] = "wrong"
    contract.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="differs from frozen input"):
        verify_corpus(generated.parent)
    contract.unlink()
    with pytest.raises(ValueError, match="coverage differs"):
        verify_corpus(generated.parent)
