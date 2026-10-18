"""Real pcbnew integration for deleting a track covered by an existing filled plane."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from circuit_weaver.kicad_bridge import find_kicad_python
from circuit_weaver.pcb_plane_cleanup import cleanup_plane_traces


@pytest.mark.skip_category("optional-tool")
def test_public_plane_cleanup_removes_only_redundant_covered_trace(tmp_path):
    native_python = find_kicad_python()
    if not native_python:
        reason = "Native plane-cleanup integration requires KiCad pcbnew Python"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    source, output = tmp_path / "plane_fixture.kicad_pcb", tmp_path / "plane_cleaned.kicad_pcb"
    script = tmp_path / "make_plane_fixture.py"
    script.write_text(textwrap.dedent("""
        import pcbnew, sys
        sys.path.insert(0, sys.argv[2])
        from circuit_weaver import _native_ground_plane as ground
        out = sys.argv[1]
        def p(x, y): return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
        board = pcbnew.BOARD()
        gnd = pcbnew.NETINFO_ITEM(board, "GND"); vdd = pcbnew.NETINFO_ITEM(board, "VDD")
        board.Add(gnd); board.Add(vdd)
        for a,b in (((2,2),(22,2)),((22,2),(22,18)),((22,18),(2,18)),((2,18),(2,2))):
            edge=pcbnew.PCB_SHAPE(board);edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
            edge.SetStart(p(*a));edge.SetEnd(p(*b));edge.SetLayer(pcbnew.Edge_Cuts)
            edge.SetWidth(pcbnew.FromMM(.05));board.Add(edge)
        def pad(ref, xy, net):
            fp=pcbnew.FOOTPRINT(board);fp.SetReference(ref);fp.SetValue("");fp.SetPosition(p(*xy))
            item=pcbnew.PAD(fp);item.SetNumber("1");item.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            item.SetShape(pcbnew.PAD_SHAPE_RECT);item.SetSize(p(.8,.8))
            layers=pcbnew.LSET();layers.AddLayer(pcbnew.F_Cu);item.SetLayerSet(layers)
            item.SetPosition(p(*xy));item.SetNet(net);fp.Add(item);board.Add(fp)
            fp.Reference().SetVisible(False);fp.Value().SetVisible(False)
        for ref,xy,net in (("J1",(6,7),gnd),("J2",(18,7),gnd),
                           ("J3",(6,13),vdd),("J4",(18,13),vdd)): pad(ref,xy,net)
        for a,b,net in (((6,7),(18,7),gnd),((6,13),(18,13),vdd)):
            trace=pcbnew.PCB_TRACK(board);trace.SetStart(p(*a));trace.SetEnd(p(*b))
            trace.SetWidth(pcbnew.FromMM(.3));trace.SetLayer(pcbnew.F_Cu);trace.SetNet(net);board.Add(trace)
        via=pcbnew.PCB_VIA(board);via.SetPosition(p(12,7));via.SetWidth(pcbnew.FromMM(.6))
        via.SetDrill(pcbnew.FromMM(.3));via.SetViaType(pcbnew.VIATYPE_THROUGH);via.SetNet(gnd);board.Add(via)
        if not pcbnew.SaveBoard(out, board): raise RuntimeError("initial fixture save failed")
        board=pcbnew.LoadBoard(out)
        ground.apply(board, pcbnew, protected_connections=[
            {"from_ref":"J1","from_pad":"1","to_ref":"J2","to_pad":"1"},
        ])
        gnd = board.GetNetsByName()["GND"]
        for a,b in (((8,7),(8,8)),((8,8),(16,8)),((16,8),(16,7))):
            trace=pcbnew.PCB_TRACK(board);trace.SetStart(p(*a));trace.SetEnd(p(*b))
            trace.SetWidth(pcbnew.FromMM(.3));trace.SetLayer(pcbnew.F_Cu);trace.SetNet(gnd);board.Add(trace)
        if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()): raise RuntimeError("refill failed")
        if not pcbnew.SaveBoard(out, board): raise RuntimeError("fixture save failed")
        check=pcbnew.LoadBoard(out)
        if len(list(check.GetTracks())) != 6 or not list(check.Zones())[0].IsFilled():
            raise RuntimeError("fixture did not retain the source trace and filled zone")
    """), encoding="utf-8")
    created = subprocess.run([native_python, str(script), str(source),
                              str(Path(__file__).resolve().parents[1] / "src")], capture_output=True,
                             text=True, timeout=60)
    assert created.returncode == 0, created.stdout + created.stderr

    result = cleanup_plane_traces(
        source, output,
        {"net": "GND", "protected_connections": [
            {"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1"},
        ]},
        kicad_python_path=native_python,
    )

    assert result["status"] == "ok", json.dumps(result, indent=2)
    assert output.is_file()
    assert result["output_kind"] == "plane_cleaned_kicad_board"
    assert result["drc"]["passed"] is True
    report = result["plane_cleanup"]
    assert report["removed_count"] == 3, json.dumps(report, indent=2)
    assert report["only_covered_unlocked_traces_removed"] is True
    assert report["saved_fill_geometry_verified"] is True
    assert report["saved_coverage_verified"] is True
    assert report["saved_connectivity_verified"] is True
    assert report["protected_connections"][0]["status"] == "preserved"
    inspected = subprocess.run(
        [native_python, "-c", "import json,pcbnew,sys; b=pcbnew.LoadBoard(sys.argv[1]); "
         "print(json.dumps({'tracks':len(list(b.GetTracks())), 'zones':len(list(b.Zones())), "
            "'filled':all(z.IsFilled() for z in b.Zones())}))", str(output)],
        capture_output=True, text=True, timeout=30,
    )
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    inventory = json.loads(inspected.stdout.strip().splitlines()[-1])
    assert inventory == {"tracks": 3, "zones": 2, "filled": True}
