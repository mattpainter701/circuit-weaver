"""Every shipped sample must reconcile its physical inventory with native KiCad."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from circuit_weaver.kicad_bridge import find_kicad_python, run_kicad
from circuit_weaver.project_state import ensure_project_state, save_project_state

REPO = Path(__file__).resolve().parents[1]
SAMPLES = sorted(p for p in (REPO / "samples").glob("*/*.yaml") if p.name != "canonical_spec.yaml")


def _cli(*args):
    result = subprocess.run(
        [sys.executable, "-m", "circuit_weaver", *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.skip_category("optional-tool")
@pytest.mark.parametrize("spec", SAMPLES, ids=lambda p: p.parent.name)
def test_sample_physical_inventory_round_trips(tmp_path, spec):
    native = find_kicad_python()
    if not native:
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail("Native sample corpus requires KiCad pcbnew and standard libraries")
        pytest.skip("Native sample corpus requires KiCad pcbnew and standard libraries")
    assert len(SAMPLES) == 13, "Update the corpus contract when adding or retiring a sample"
    original = spec.read_bytes()
    save_project_state(tmp_path, ensure_project_state(tmp_path))
    try:
        generated = _cli("generate", spec, "--output", tmp_path / "generated", "--require-kicad", "--no-svg")
        assert generated["valid"] is True
        manifest = json.loads(Path(generated["assembly_manifest"]).read_text(encoding="utf-8"))
        items = manifest["items"]
        expected = {item["reference"]: item for item in items}
        assert len(expected) == len(items) > 0
        assert all(item["footprint"] for item in items)
        board = tmp_path / "sample.kicad_pcb"
        # This regression checks physical inventory on a declared review
        # canvas; full workflow benchmarks separately exercise design outlines.
        build_args = ["build-pcb", generated["root_schematic"], "--output", board,
                      "--width", "80", "--height", "60", "--clearance", "0.127"]
        if spec.parent.name in {"iot_sensor_node", "battery_iot_sensor"}:
            # The native ESP32 module land pattern includes 0.2 mm thermal
            # plated holes. This is an explicit prototype process assumption.
            build_args.extend(["--minimum-drill", "0.2"])
        built = _cli(*build_args)
        assert built["counts"]["components"] == built["counts"]["footprints"] == len(items)
        assert built["validation"]["pin_mapping"] == "exact_number_match"
        assert built["validation"]["pcbnew_round_trip"] == "passed"
        assert built["validation"]["physical_drc"]["non_connect_blockers"] == 0
        assert built["validation"]["board_sha256"] == hashlib.sha256(board.read_bytes()).hexdigest()
        inventory = run_kicad("inspect", board=str(board), python_path=native)
        assert {item["ref"] for item in inventory["footprints"]} == set(expected)
        for footprint in inventory["footprints"]:
            item = expected[footprint["ref"]]
            if item.get("source_kind") == "strap":
                assert {pad["net"] for pad in footprint["pads"]} == {item["net1"], item["net2"]}
        assert spec.read_bytes() == original
    finally:
        evidence = os.getenv("CW_NATIVE_PCB_EVIDENCE")
        if evidence:
            shutil.copytree(tmp_path, Path(evidence) / "sample-footprints" / spec.parent.name, dirs_exist_ok=True)
