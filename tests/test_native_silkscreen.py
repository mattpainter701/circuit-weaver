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

_DENSE_FIXTURE_SCRIPT = r'''
import pcbnew, sys
from pathlib import Path

root, output = Path(sys.argv[1]), Path(sys.argv[2])
board = pcbnew.BOARD()
for a, b in (((0, 0), (36, 0)), ((36, 0), (36, 30)),
             ((36, 30), (0, 30)), ((0, 30), (0, 0))):
    edge = pcbnew.PCB_SHAPE()
    edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
    edge.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
    edge.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(pcbnew.FromMM(0.05))
    board.Add(edge)

specs = [
    ("R1", "Resistor_SMD", "R_0402_1005Metric", (15.095, 15.085), 0),
    ("R2", "Resistor_SMD", "R_0402_1005Metric", (12.645, 15.085), 90),
    ("C4", "Capacitor_SMD", "C_0402_1005Metric", (17.595, 7.715), 180),
    ("C5", "Capacitor_SMD", "C_0402_1005Metric", (15.925, 16.685), 0),
    ("C2", "Capacitor_SMD", "C_1206_3216Metric", (18.505, 9.945), 180),
    ("C3", "Capacitor_SMD", "C_0402_1005Metric", (5.715, 15.695), 0),
    ("U1", "Package_TO_SOT_SMD", "SOT-223-3_TabPin2", (10.215, 9.945), 0),
    ("J1", "Connector_USB", "USB_C_Receptacle_GCT_USB4085", (10.3511, 19.82), 0),
    ("C1", "Capacitor_SMD", "C_0805_2012Metric", (8.965, 16.315), 180),
]
for ref, library, name, (x, y), rotation in specs:
    footprint = pcbnew.FootprintLoad(str(root / f"{library}.pretty"), name)
    if footprint is None:
        raise RuntimeError(f"missing test footprint: {library}:{name}")
    board.Add(footprint)
    footprint.SetReference(ref)
    footprint.SetValue(ref)
    footprint.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    footprint.SetOrientationDegrees(rotation)

output.parent.mkdir(parents=True, exist_ok=True)
if not pcbnew.SaveBoard(str(output), board):
    raise RuntimeError("KiCad could not save dense-layout test board")
'''

_ASSOCIATION_CLASS_SCRIPT = r'''
import pcbnew, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from circuit_weaver import _kicad_worker as worker

root = Path(sys.argv[1])
board = pcbnew.BOARD()
for ref, library, name, x in (
    ("U1", "Package_TO_SOT_SMD", "SOT-223-3_TabPin2", 10),
    ("C1", "Capacitor_SMD", "C_0402_1005Metric", 14),
):
    footprint = pcbnew.FootprintLoad(str(root / f"{library}.pretty"), name)
    if footprint is None:
        raise RuntimeError(f"missing test footprint: {library}:{name}")
    board.Add(footprint)
    footprint.SetReference(ref)
    footprint.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(10)))

obstacles = worker._silk_obstacles(board, pcbnew)[pcbnew.F_SilkS]
u1 = board.FindFootprintByReference("U1")
own = worker._courtyard_box(u1, pcbnew)
c1_box = next(box for kind, ref, box in obstacles if kind == "courtyard" and ref == "C1")
candidate = (c1_box[2] + pcbnew.FromMM(0.5), (c1_box[1] + c1_box[3]) // 2)
own_distance = worker._point_box_distance_sq(candidate, own)
c1_distance = worker._point_box_distance_sq(candidate, c1_box)
if not c1_distance < own_distance:
    raise AssertionError("fixture candidate must be closer to C1 than to U1")

# Native geometry places the candidate closer to the support passive, but a
# different reference class must not make U1 look misassociated.
if not worker._candidate_associated(candidate, own, "U1", obstacles):
    raise AssertionError("a nearer C footprint incorrectly rejected the U1 label")

# The same courtyard as a U2 peer must still trigger the association check.
same_class_obstacles = [
    (kind, "U2" if kind == "courtyard" and ref == "C1" else ref, box)
    for kind, ref, box in obstacles
]
if worker._candidate_associated(candidate, own, "U1", same_class_obstacles):
    raise AssertionError("a nearer same-class U footprint did not trigger association")
print("native class association behavior verified")
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


def _make_dense_board(tmp_path: Path, native_tools) -> Path:
    python, root = native_tools
    source = tmp_path / "dense-source.kicad_pcb"
    process = subprocess.run(
        [python, "-c", _DENSE_FIXTURE_SCRIPT, str(root), str(source)],
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


def _add_locked_reference_over_r1_preferred_spot(source: Path, output: Path, native_tools):
    python, root = native_tools
    configure = r'''
import pcbnew, sys
from pathlib import Path
board = pcbnew.LoadBoard(sys.argv[1])
r1 = board.FindFootprintByReference("R1")
field = r1.Reference()
field.SetTextAngleDegrees(0)
field_box = field.GetBoundingBox()
courtyard_layer = pcbnew.B_CrtYd if r1.GetLayer() == pcbnew.B_Cu else pcbnew.F_CrtYd
courtyard = r1.GetCourtyard(courtyard_layer).BBox()
margin = pcbnew.FromMM(0.2)
# Mirror the native cleanup's preferred centered-above candidate using the
# actual KiCad courtyard and text geometry, then put a locked label there.
center_x = (courtyard.GetX() + courtyard.GetRight()) // 2
center_y = courtyard.GetY() - field_box.GetHeight() // 2 - 2 * margin
blocker = pcbnew.FootprintLoad(str(Path(sys.argv[3]) / "Connector_PinHeader_2.54mm.pretty"),
                               "PinHeader_1x02_P2.54mm_Vertical")
if blocker is None:
    raise RuntimeError("missing test footprint for locked reference blocker")
board.Add(blocker)
blocker.SetReference("J2")
blocker.SetValue("locked blocker")
blocker.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(55), pcbnew.FromMM(30)))
blocker.SetLocked(True)
blocker.Reference().SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
blocker.Reference().SetTextThickness(pcbnew.FromMM(0.15))
blocker.Reference().SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
blocker.Reference().SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
blocker.Reference().SetTextAngleDegrees(0)
blocker.Reference().SetTextPos(pcbnew.VECTOR2I(center_x, center_y))
if not pcbnew.SaveBoard(sys.argv[2], board):
    raise RuntimeError("KiCad could not save locked-blocker test board")
'''
    process = subprocess.run([python, "-c", configure, str(source), str(output), str(root)],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stdout + process.stderr


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
        # The cleanup uses a shared above-component default, keeping each
        # designator close to and visually associated with its footprint.
        assert after[ref]["y"] < 20
    assert after["R1"]["x"] != before["R1"]["x"] or after["R1"]["y"] != before["R1"]["y"]
    assert after["C1"]["x"] != before["C1"]["x"] or after["C1"]["y"] != before["C1"]["y"]


@pytest.mark.skip_category("optional-tool")
def test_locked_reference_blocks_preferred_spot_and_cleanup_is_idempotent(tmp_path, native_tools):
    source = _make_board(tmp_path, native_tools)
    blocked = tmp_path / "locked-preferred-spot.kicad_pcb"
    _add_locked_reference_over_r1_preferred_spot(source, blocked, native_tools)
    before = _field_snapshot(blocked, native_tools, ("J2", "R1"))
    inspect = run_kicad("inspect", board=str(blocked), python_path=native_tools[0])
    placements = {
        item["ref"]: {key: item[key] for key in ("x", "y", "rotation", "layer")}
        for item in inspect["footprints"]
    }

    first_output = tmp_path / "first-tidy.kicad_pcb"
    first = run_kicad("place", board=str(blocked), output=str(first_output), placements=placements,
                      tidy_references=True, python_path=native_tools[0])
    assert first["status"] == "ok", first
    first_report = first["silkscreen"]
    assert first_report["conflict_counts"]["courtyard"] == 0
    assert first_report["conflict_counts"]["pad_mask"] == 0
    assert first_report["conflict_counts"]["silkscreen_graphics"] == 0
    assert first_report["conflict_counts"]["reference_reference"] == 0
    assert first_report["conflict_counts"]["edge"] == 0
    # J2 is deliberately parked remotely to obstruct R1's preferred slot, so
    # the new association check correctly reports that test-only locked label.
    assert first_report["conflict_counts"]["association"] == 1
    after_first = _field_snapshot(first_output, native_tools, ("J2", "R1"))
    assert after_first["J2"] == before["J2"]
    assert (after_first["R1"]["x"] - 20) ** 2 + (after_first["R1"]["y"] - 20) ** 2 < 25
    assert (after_first["R1"]["x"] - before["J2"]["x"]) ** 2 + (
        after_first["R1"]["y"] - before["J2"]["y"]
    ) ** 2 > 1

    second_inspect = run_kicad("inspect", board=str(first_output), python_path=native_tools[0])
    second_placements = {
        item["ref"]: {key: item[key] for key in ("x", "y", "rotation", "layer")}
        for item in second_inspect["footprints"]
    }
    second_output = tmp_path / "second-tidy.kicad_pcb"
    second = run_kicad("place", board=str(first_output), output=str(second_output),
                       placements=second_placements, tidy_references=True,
                       python_path=native_tools[0])
    assert second["status"] == "ok", second
    assert second["silkscreen"]["moved_count"] == 0
    assert second["silkscreen"]["orientation_normalized_count"] == 0
    assert second["silkscreen"]["conflict_counts"]["association"] == 1
    assert second["silkscreen"]["remaining_conflict_count"] == 1
    assert _field_snapshot(second_output, native_tools, ("J2", "R1")) == after_first


@pytest.mark.skip_category("optional-tool")
def test_dense_native_labels_stay_near_their_own_footprints_and_are_idempotent(tmp_path, native_tools):
    source = _make_dense_board(tmp_path, native_tools)
    inspect = run_kicad("inspect", board=str(source), python_path=native_tools[0])
    placements = {
        item["ref"]: {key: item[key] for key in ("x", "y", "rotation", "layer")}
        for item in inspect["footprints"]
    }
    output = tmp_path / "dense-tidy.kicad_pcb"
    first = run_kicad("place", board=str(source), output=str(output), placements=placements,
                      tidy_references=True, python_path=native_tools[0])
    assert first["status"] == "ok", first
    report = first["silkscreen"]
    assert report["remaining_conflict_count"] == 0
    assert report["conflict_counts"]["association"] == 0

    refs = ("R1", "R2", "C4", "C5", "C2", "C3", "U1", "J1", "C1")
    after_first = _field_snapshot(output, native_tools, refs)
    for ref, position in (("R1", (15.095, 15.085)), ("R2", (12.645, 15.085))):
        assert (after_first[ref]["x"] - position[0]) ** 2 + (
            after_first[ref]["y"] - position[1]
        ) ** 2 < 9

    second_inspect = run_kicad("inspect", board=str(output), python_path=native_tools[0])
    second_placements = {
        item["ref"]: {key: item[key] for key in ("x", "y", "rotation", "layer")}
        for item in second_inspect["footprints"]
    }
    second_output = tmp_path / "dense-tidy-again.kicad_pcb"
    second = run_kicad("place", board=str(output), output=str(second_output),
                       placements=second_placements, tidy_references=True,
                       python_path=native_tools[0])
    assert second["status"] == "ok", second
    assert second["silkscreen"]["moved_count"] == 0
    assert second["silkscreen"]["conflict_counts"]["association"] == 0
    assert _field_snapshot(second_output, native_tools, refs) == after_first


@pytest.mark.skip_category("optional-tool")
def test_association_uses_reference_class_for_nearer_courtyards(native_tools):
    python, root = native_tools
    source_root = Path(__file__).resolve().parents[1] / "src"
    process = subprocess.run([python, "-c", _ASSOCIATION_CLASS_SCRIPT, str(root), str(source_root)],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stdout + process.stderr
    assert "native class association behavior verified" in process.stdout


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
