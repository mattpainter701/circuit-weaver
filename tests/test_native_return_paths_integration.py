"""Real KiCad integration coverage for return routing and original-copper retention."""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from circuit_weaver.kicad_bridge import find_kicad_python
from circuit_weaver.pcb_return_paths import refine_return_paths


@pytest.fixture(scope="module")
def native_kicad():
    python = find_kicad_python()
    roots = [Path(os.getenv("KICAD_FOOTPRINT_DIR", "/usr/share/kicad/footprints"))]
    roots += [Path(f"C:/Program Files/KiCad/{version}/share/kicad/footprints") for version in ("10.0", "9.0", "8.0")]
    footprint_root = next((root for root in roots if (root / "Connector_PinHeader_2.54mm.pretty").is_dir()), None)
    if not python or not footprint_root:
        reason = "Native return routing test requires KiCad pcbnew and standard footprints"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return python, footprint_root


@pytest.mark.skip_category("optional-tool")
def test_native_return_shortcut_uses_back_copper_and_preserves_source_copper(tmp_path, native_kicad):
    python, footprint_root = native_kicad
    source = tmp_path / "return_fixture.kicad_pcb"
    fixture_script = tmp_path / "make_return_fixture.py"
    fixture_script.write_text(
        textwrap.dedent("""
        import pcbnew, sys
        from pathlib import Path

        footprint_root, output = Path(sys.argv[1]), Path(sys.argv[2])
        board = pcbnew.BOARD()
        for a, b in (((4, 1), (20, 1)), ((20, 1), (20, 11)),
                     ((20, 11), (4, 11)), ((4, 11), (4, 1))):
            edge = pcbnew.PCB_SHAPE()
            edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
            edge.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
            edge.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
            edge.SetLayer(pcbnew.Edge_Cuts)
            edge.SetWidth(pcbnew.FromMM(0.05))
            board.Add(edge)

        gnd, vdd = pcbnew.NETINFO_ITEM(board, "GND"), pcbnew.NETINFO_ITEM(board, "VDD")
        board.Add(gnd); board.Add(vdd)
        for ref, xy, net in (("J1", (9, 6), gnd), ("J2", (15, 6), gnd),
                             ("J3", (12, 4.5), vdd), ("J4", (12, 7.5), vdd)):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(ref); fp.SetValue("")
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(xy[0]), pcbnew.FromMM(xy[1])))
            pad = pcbnew.PAD(fp)
            pad.SetNumber("1"); pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.8), pcbnew.FromMM(0.8)))
            pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(xy[0]), pcbnew.FromMM(xy[1])))
            copper = pcbnew.LSET(); copper.AddLayer(pcbnew.F_Cu); pad.SetLayerSet(copper)
            pad.SetNet(net); fp.Add(pad); board.Add(fp)
            fp.Reference().SetVisible(False); fp.Value().SetVisible(False)

        def segment(start, end, net, width):
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]), pcbnew.FromMM(start[1])))
            track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]), pcbnew.FromMM(end[1])))
            track.SetWidth(pcbnew.FromMM(width)); track.SetLayer(pcbnew.F_Cu)
            track.SetNetCode(net.GetNetCode()); board.Add(track)

        # The existing GND route goes around the VDD barrier on the front.
        segment((9, 6), (9, 3.5), gnd, 0.3)
        segment((9, 3.5), (15, 3.5), gnd, 0.3)
        segment((15, 3.5), (15, 6), gnd, 0.3)
        # A foreign-net trace blocks the short front-layer connection.
        segment((12, 4.5), (12, 7.5), vdd, 0.3)
        output.parent.mkdir(parents=True, exist_ok=True)
        if not pcbnew.SaveBoard(str(output), board):
            raise RuntimeError("Could not save native return-path fixture")
        project = output.with_suffix(".kicad_pro")
        if not project.exists() and not pcbnew.GetSettingsManager().SaveProject(str(project), board.GetProject()):
            raise RuntimeError("Could not save fixture project settings")
    """),
        encoding="utf-8",
    )
    created = subprocess.run(
        [python, str(fixture_script), str(footprint_root), str(source)], capture_output=True, text=True, timeout=60
    )
    assert created.returncode == 0, created.stdout + created.stderr

    output = tmp_path / "return_refined.kicad_pcb"
    result = refine_return_paths(
        source,
        output,
        [
            {
                "from_ref": "J1",
                "from_pad": "1",
                "to_ref": "J2",
                "to_pad": "1",
                "max_length_mm": 8.0,
            }
        ],
    )

    assert result["status"] == "ok", result
    assert result["drc"]["passed"] is True
    assert output.is_file()
    refinement = result["refinement"]
    assert refinement["original_copper_preserved"] is True
    assert refinement["original_footprints_and_text_preserved"] is True
    assert refinement["added_vias"] == 2
    assert refinement["added_tracks"] >= 3
    assert refinement["connections"][0]["layer"] == "B.Cu"
    assert refinement["connections"][0]["planar_length_mm"] <= 8.0

    reused_output = tmp_path / "return_reused.kicad_pcb"
    reused = refine_return_paths(
        output,
        reused_output,
        [{"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1"}],
    )
    assert reused["status"] == "ok", reused
    assert reused["drc"]["passed"] is True
    assert reused_output.is_file()
    assert reused["refinement"]["added_vias"] == 0
    assert reused["refinement"]["original_copper_preserved"] is True
    assert reused["refinement"]["original_footprints_and_text_preserved"] is True
