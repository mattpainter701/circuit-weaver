"""End-to-end regressions using real KiCad and Freerouting, without tool mocks."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from circuit_weaver.autoroute import _find_freerouting_command
from circuit_weaver.kicad_bridge import find_kicad_python, run_kicad


@pytest.fixture(scope="module")
def native_tools():
    python = find_kicad_python()
    roots = [Path(os.getenv("KICAD_FOOTPRINT_DIR", "/usr/share/kicad/footprints"))]
    roots += [Path(f"C:/Program Files/KiCad/{v}/share/kicad/footprints") for v in ("10.0", "9.0", "8.0")]
    root = next((p for p in roots if (p / "Resistor_SMD.pretty").is_dir()), None)
    if not python or not root or not _find_freerouting_command():
        reason = "Real PCB workflow requires KiCad pcbnew, standard footprints, and Freerouting"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return python, root


def _cli(*args):
    process = subprocess.run([sys.executable, "-m", "circuit_weaver", *map(str, args)],
                             capture_output=True, text=True, timeout=180)
    assert process.returncode == 0, process.stdout + process.stderr
    return json.loads(process.stdout)


@pytest.mark.skip_category("optional-tool")
@pytest.mark.parametrize("case", ["led", "divider", "rc", "back", "soic"])
def test_real_placement_route_import_and_drc(tmp_path, native_tools, case):
    python, root = native_tools
    source = tmp_path / f"{case}.kicad_pcb"
    fixture = Path(__file__).parent / "fixtures" / "make_basic_pcbs.py"
    fixture_command = [python, str(fixture), str(root), str(source), case]
    fixture_process = subprocess.run(fixture_command, check=False, capture_output=True, text=True)
    if fixture_process.returncode:
        evidence = os.getenv("CW_NATIVE_PCB_EVIDENCE")
        if evidence:
            import shutil

            destination = Path(evidence) / case
            shutil.copytree(tmp_path, destination, dirs_exist_ok=True)
            (destination / "fixture-failure.json").write_text(json.dumps({
                "command": fixture_command,
                "returncode": fixture_process.returncode,
                "stdout": fixture_process.stdout,
                "stderr": fixture_process.stderr,
            }, indent=2))
        pytest.fail(
            f"Native KiCad fixture failed ({fixture_process.returncode}).\n"
            f"stdout:\n{fixture_process.stdout}\nstderr:\n{fixture_process.stderr}"
        )
    original = source.read_bytes()
    settings_before = {suffix: source.with_suffix(suffix).read_bytes()
                       for suffix in (".kicad_pro", ".kicad_dru") if source.with_suffix(suffix).exists()}
    before = run_kicad("inspect", board=str(source), python_path=python)
    placed, routed = tmp_path / "placed.kicad_pcb", tmp_path / "routed.kicad_pcb"
    placement = _cli("place-pcb", source, "--output", placed, "--iterations", 1500,
                     "--kicad-python-path", python)
    assert placement["placement"]["quality"]["review_required"] is False
    route = _cli("autoroute", placed, "--routed-board", routed, "--max-passes", 10,
                 "--timeout", 90, "--attempts", 3, "--kicad-python-path", python)
    assert route["output_kind"] == "routed_kicad_board"
    assert route["artifact"]["path"] == route["output_path"]
    assert route["verification"]["requires_kicad_drc"] is False
    assert route["routing_complete"] is True
    assert route["drc"]["passed"] is True
    assert route["drc"]["blocker_count"] == 0
    assert route["drc"]["board"] == str(routed.resolve())
    assert route["drc"]["board_sha256"] == hashlib.sha256(routed.read_bytes()).hexdigest()
    assert route["search"]["completed_attempts"] == 3
    edge = route["router"]["native_edge_clearance"]
    assert edge["value_iu"] == before["native_edge_clearance"]["value_iu"]
    assert edge["source"] == "BOARD_DESIGN_SETTINGS.m_CopperEdgeClearance"
    assert edge["transport"] == "--router.copper_to_edge_clearance_um"
    if case == "rc":
        assert before["native_edge_clearance"]["value_mm"] == 0.8
        assert edge["argv"] == "--router.copper_to_edge_clearance_um=800"
    assert source.read_bytes() == original
    for suffix in (".kicad_pro", ".kicad_dru"):
        if suffix in settings_before:
            assert source.with_suffix(suffix).read_bytes() == settings_before[suffix]
            assert routed.with_suffix(suffix).read_bytes() == settings_before[suffix]
    after = run_kicad("inspect", board=str(routed), python_path=python)
    by_ref = {f["ref"]: f for f in after["footprints"]}
    assert after["track_count"] > 0
    assert after["rectangular_outline"] is True
    for footprint in before["footprints"]:
        actual = by_ref[footprint["ref"]]
        assert sorted((p["number"], p["net"]) for p in actual["pads"]) == sorted(
            (p["number"], p["net"]) for p in footprint["pads"])
        assert actual["layer"] == footprint["layer"]
        if footprint["locked"]:
            assert all(actual[key] == footprint[key] for key in ("x", "y", "rotation", "locked"))
    evidence = os.getenv("CW_NATIVE_PCB_EVIDENCE")
    if evidence:
        import shutil

        destination = Path(evidence) / case
        shutil.copytree(tmp_path, destination, dirs_exist_ok=True)
        (destination / "result.json").write_text(json.dumps({"placement": placement, "routing": route}, indent=2))


@pytest.mark.skip_category("optional-tool")
def test_rc_fixture_saves_missing_project_sidecar_with_native_settings(tmp_path, native_tools):
    python, root = native_tools
    fixture = Path(__file__).parent / "fixtures" / "make_basic_pcbs.py"
    output = tmp_path / "rc.kicad_pcb"
    wrapper = """
import pcbnew, runpy, sys
from pathlib import Path

save_board = pcbnew.SaveBoard
def save_without_project(path, board):
    result = save_board(path, board)
    if result:
        Path(path).with_suffix(".kicad_pro").unlink(missing_ok=True)
    return result
pcbnew.SaveBoard = save_without_project
fixture, footprints, output, case = sys.argv[1:5]
sys.argv = [fixture, footprints, output, case]
runpy.run_path(fixture, run_name="__main__")
"""
    process = subprocess.run(
        [python, "-c", wrapper, str(fixture), str(root), str(output), "rc"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stdout + process.stderr

    project_path = output.with_suffix(".kicad_pro")
    assert project_path.is_file()
    project = json.loads(project_path.read_text())
    assert {"board", "net_settings", "meta"}.issubset(project)
    assert project["board"]["design_settings"]["rules"]["min_track_width"] == 0.3
    assert project["board"]["design_settings"]["rules"]["min_clearance"] == 0.25
    assert project["board"]["design_settings"]["rules"]["min_copper_edge_clearance"] == 0.8
    default_class = next(item for item in project["net_settings"]["classes"] if item["name"] == "Default")
    assert default_class["track_width"] == 0.35
    assert default_class["clearance"] == 0.25
    assert default_class["via_diameter"] == 0.7
    assert default_class["via_drill"] == 0.3
