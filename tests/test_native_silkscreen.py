"""Native regressions for opt-in reference-field cleanup in pcbnew."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from circuit_weaver.kicad_bridge import find_kicad_python, run_kicad

_FIXTURE_SCRIPT = r'''
import pcbnew, sys
from pathlib import Path

root, output = Path(sys.argv[1]), Path(sys.argv[2])
board = pcbnew.BOARD()
for a, b in (((0, 0), (60, 0)), ((60, 0), (60, 40)),
             ((60, 40), (0, 40)), ((0, 40), (0, 0))):
    edge = pcbnew.PCB_SHAPE()
    edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
    edge.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
    edge.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(pcbnew.FromMM(0.05))
    board.Add(edge)

specs = [
    ("J1", "Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical", (10, 20), False),
    ("R1", "Resistor_SMD", "R_0603_1608Metric", (20, 20), False),
    ("R2", "Resistor_SMD", "R_0603_1608Metric", (35, 20), True),
    ("C1", "Capacitor_SMD", "C_0603_1608Metric", (45, 20), True),
]
fields = {}
for ref, library, name, (x, y), back in specs:
    footprint = pcbnew.FootprintLoad(str(root / f"{library}.pretty"), name)
    if footprint is None:
        raise RuntimeError(f"missing test footprint: {library}:{name}")
    board.Add(footprint)
    footprint.SetReference(ref)
    footprint.SetLocked(ref == "J1")
    footprint.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    if back:
        footprint.Flip(footprint.GetPosition(), False)
    footprint.Reference().SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
    fields[ref] = footprint.Reference()

# J1 is an immovable text obstacle; R1 begins at precisely the same location.
fields["J1"].SetTextPos(pcbnew.VECTOR2I(pcbnew.FromMM(14), pcbnew.FromMM(12)))
fields["J1"].SetLocked(True)
fields["R1"].SetTextPos(pcbnew.VECTOR2I(pcbnew.FromMM(14), pcbnew.FromMM(12)))
fields["R1"].SetTextAngleDegrees(90)
# A second same-layer collision on the back tests side-aware obstacle handling.
fields["R2"].SetTextPos(pcbnew.VECTOR2I(pcbnew.FromMM(40), pcbnew.FromMM(12)))
fields["C1"].SetTextPos(pcbnew.VECTOR2I(pcbnew.FromMM(40), pcbnew.FromMM(12)))
fields["C1"].SetTextAngleDegrees(270)

output.parent.mkdir(parents=True, exist_ok=True)
if not pcbnew.SaveBoard(str(output), board):
    raise RuntimeError("KiCad could not save test board")
'''


@pytest.fixture(scope="module")
def native_tools():
    python = find_kicad_python()
    roots = [Path(os.getenv("KICAD_FOOTPRINT_DIR", "/usr/share/kicad/footprints"))]
    roots.extend(Path(f"C:/Program Files/KiCad/{version}/share/kicad/footprints")
                 for version in ("10.0", "9.0", "8.0"))
    root = next((path for path in roots if (path / "Resistor_SMD.pretty").is_dir()), None)
    if not python or root is None:
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail("Reference cleanup tests require KiCad pcbnew and standard footprints")
        pytest.skip("Reference cleanup tests require KiCad pcbnew and standard footprints")
    return python, root


def _make_board(tmp_path: Path, native_tools) -> Path:
    python, root = native_tools
    source = tmp_path / "source.kicad_pcb"
    process = subprocess.run(
        [python, "-c", _FIXTURE_SCRIPT, str(root), str(source)],
        capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    return source


def _field_snapshot(board_path: Path, native_tools, refs: tuple[str, ...]) -> dict:
    python, _root = native_tools
    script = r'''
import json, pcbnew, sys
board = pcbnew.LoadBoard(sys.argv[1])
result = {}
for footprint in board.GetFootprints():
    ref = footprint.GetReference()
    if ref not in sys.argv[2:]:
        continue
    field = footprint.Reference()
    pos = field.GetTextPos()
    result[ref] = {
        "text": field.GetText(), "x": pcbnew.ToMM(pos.x), "y": pcbnew.ToMM(pos.y),
        "angle": field.GetTextAngleDegrees(), "layer": field.GetLayer(),
        "locked": field.IsLocked(), "size": (field.GetTextSize().x, field.GetTextSize().y),
        "uuid": field.m_Uuid.AsString(),
    }
print(json.dumps(result))
'''
    process = subprocess.run([python, "-c", script, str(board_path), *refs],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stdout + process.stderr
    import json

    return json.loads(process.stdout)


@pytest.mark.skip_category("optional-tool")
def test_native_cleanup_resolves_front_and_back_reference_collisions(tmp_path, native_tools):
    source = _make_board(tmp_path, native_tools)
    before = _field_snapshot(source, native_tools, ("J1", "R1", "R2", "C1"))
    inspect = run_kicad("inspect", board=str(source), python_path=native_tools[0])
    placements = {
        item["ref"]: {key: item[key] for key in ("x", "y", "rotation", "layer")}
        for item in inspect["footprints"]
    }
    output = tmp_path / "tidied.kicad_pcb"
    result = run_kicad("place", board=str(source), output=str(output), placements=placements,
                       tidy_references=True, python_path=native_tools[0])

    assert result["status"] == "ok", result
    report = result["silkscreen"]
    assert report["requested"] is True
    assert report["moved_count"] >= 2
    assert report["orientation_normalized_count"] >= 2
    assert report["unresolved_count"] == 0
    assert report["remaining_conflict_count"] == 0
    after = _field_snapshot(output, native_tools, ("J1", "R1", "R2", "C1"))
    assert after["J1"] == before["J1"]
    for ref in ("R1", "R2", "C1"):
        for key in ("text", "layer", "size", "uuid", "locked"):
            assert after[ref][key] == before[ref][key]
        # KiCad 8 canonicalizes some mirrored back fields to 180 degrees on
        # save. Both 0 and 180 are horizontal; 90/270 remain invalid here.
        assert after[ref]["angle"] % 180 == pytest.approx(0)
    assert after["R1"]["x"] != before["R1"]["x"] or after["R1"]["y"] != before["R1"]["y"]
    assert after["C1"]["x"] != before["C1"]["x"] or after["C1"]["y"] != before["C1"]["y"]


@pytest.mark.skip_category("optional-tool")
def test_native_cleanup_reports_locked_infeasible_reference_without_hiding_it(tmp_path, native_tools):
    source = _make_board(tmp_path, native_tools)
# Lock J1's footprint with its field over its own courtyard. No safe move is allowed.
    python, _root = native_tools
    configure = r'''
import pcbnew, sys
board = pcbnew.LoadBoard(sys.argv[1])
field = board.FindFootprintByReference("J1").Reference()
field.SetTextPos(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(20)))
board.FindFootprintByReference("J1").SetLocked(True)
pcbnew.SaveBoard(sys.argv[2], board)
'''
    collided = tmp_path / "locked.kicad_pcb"
    process = subprocess.run([python, "-c", configure, str(source), str(collided)],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stdout + process.stderr
    before = _field_snapshot(collided, native_tools, ("J1",))
    inspect = run_kicad("inspect", board=str(collided), python_path=python)
    placements = {
        item["ref"]: {key: item[key] for key in ("x", "y", "rotation", "layer")}
        for item in inspect["footprints"]
    }
    output = tmp_path / "locked-tidied.kicad_pcb"
    result = run_kicad("place", board=str(collided), output=str(output), placements=placements,
                       tidy_references=True, python_path=python)

    assert result["status"] == "ok", result
    report = result["silkscreen"]
    assert report["unresolved_references"] == ["J1"]
    assert report["unresolved_count"] >= 1
    assert report["remaining_conflict_count"] >= 1
    assert _field_snapshot(output, native_tools, ("J1",)) == before


@pytest.mark.skip_category("optional-tool")
def test_reference_cleanup_is_opt_in(tmp_path, native_tools):
    source = _make_board(tmp_path, native_tools)
    inspect = run_kicad("inspect", board=str(source), python_path=native_tools[0])
    placements = {
        item["ref"]: {key: item[key] for key in ("x", "y", "rotation", "layer")}
        for item in inspect["footprints"]
    }
    before = _field_snapshot(source, native_tools, ("R1", "C1"))
    output = tmp_path / "unchanged-fields.kicad_pcb"
    result = run_kicad("place", board=str(source), output=str(output), placements=placements,
                       python_path=native_tools[0])

    assert result["status"] == "ok", result
    assert "silkscreen" not in result
    assert _field_snapshot(output, native_tools, ("R1", "C1")) == before


@pytest.mark.skip_category("optional-tool")
def test_native_inspect_reports_actual_off_center_courtyard_without_mutation(tmp_path, native_tools):
    python, root = native_tools
    source = tmp_path / "off-center-usb.kicad_pcb"
    create = r'''
import pcbnew, sys
board = pcbnew.BOARD()
fp = pcbnew.FootprintLoad(sys.argv[1] + "/Connector_USB.pretty", "USB_C_Receptacle_GCT_USB4085")
if fp is None:
    raise RuntimeError("USB4085 test footprint not found")
board.Add(fp)
fp.SetReference("J1")
fp.SetValue("USB4085")
fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(18), pcbnew.FromMM(17)))
fp.SetOrientationDegrees(90)
pcbnew.SaveBoard(sys.argv[2], board)
'''
    process = subprocess.run([python, "-c", create, str(root), str(source)],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stdout + process.stderr
    before = _field_snapshot(source, native_tools, ("J1",))

    result = run_kicad("inspect", board=str(source), python_path=python)
    item = next(fp for fp in result["footprints"] if fp["ref"] == "J1")

    # Legacy origin-centered extents remain compatible; optimizers can now use
    # actual size and the local courtyard-center offset instead.
    assert item["width"] > item["courtyard_width"]
    assert item["height"] > item["courtyard_height"]
    assert item["courtyard_width"] < 16.59
    assert item["courtyard_height"] < 18.31
    assert abs(item["courtyard_center_x"]) > 0.1 or abs(item["courtyard_center_y"]) > 0.1
    assert item["rotation"] == pytest.approx(90)
    assert _field_snapshot(source, native_tools, ("J1",)) == before
