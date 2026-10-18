"""Full native-DRC coverage for the public copper-cleanup API."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from circuit_weaver.kicad_bridge import find_kicad_python
from circuit_weaver.pcb_copper_cleanup import cleanup_copper


@pytest.fixture(scope="module")
def native_kicad():
    python = find_kicad_python()
    cli = shutil.which("kicad-cli") or next(
        (str(path) for version in ("10.0", "9.0", "8.0")
         if (path := Path(f"C:/Program Files/KiCad/{version}/bin/kicad-cli.exe")).is_file()), None,
    )
    if not python or not cli:
        reason = "Native copper cleanup integration requires KiCad pcbnew and kicad-cli"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return python


def _make_board(path: Path, native_python: str, *, shared_via_fixture: bool) -> None:
    if shared_via_fixture:
        body = """
        import pcbnew, sys
        from pathlib import Path
        out = Path(sys.argv[1]); board = pcbnew.BOARD()
        gnd = pcbnew.NETINFO_ITEM(board, "GND"); board.Add(gnd)
        vdd = pcbnew.NETINFO_ITEM(board, "VDD"); board.Add(vdd)
        def point(xy): return pcbnew.VECTOR2I(pcbnew.FromMM(xy[0]), pcbnew.FromMM(xy[1]))
        def add_pad(ref, xy, net):
            fp=pcbnew.FOOTPRINT(board); fp.SetReference(ref); fp.SetValue(""); fp.SetPosition(point(xy))
            pad=pcbnew.PAD(fp); pad.SetNumber("1"); pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT); pad.SetSize(point((0.8,0.8)))
            layers=pcbnew.LSET(); layers.AddLayer(pcbnew.F_Cu); pad.SetLayerSet(layers)
            pad.SetPosition(point(xy)); pad.SetNet(net); fp.Add(pad); board.Add(fp)
            fp.Reference().SetVisible(False); fp.Value().SetVisible(False)
        def track(a,b,net,layer,locked=False):
            t=pcbnew.PCB_TRACK(board); t.SetStart(point(a)); t.SetEnd(point(b))
            t.SetWidth(pcbnew.FromMM(0.2)); t.SetLayer(layer); t.SetNet(net); t.SetLocked(locked)
            board.Add(t); return t
        def via(xy,net):
            v=pcbnew.PCB_VIA(board); v.SetPosition(point(xy)); v.SetWidth(pcbnew.FromMM(0.6))
            v.SetDrill(pcbnew.FromMM(0.3)); v.SetViaType(pcbnew.VIATYPE_THROUGH); v.SetNet(net); board.Add(v)
        for ref,xy in (("J1",(10,12)),("J2",(9,10)),("J3",(14,11))): add_pad(ref,xy,gnd)
        add_pad("J4",(11.7,7),vdd); add_pad("J5",(11.7,15),vdd)
        for xy in ((10.9,12),(9.9,10),(14.9,11)): via(xy,gnd)
        track((10,12),(10.9,12),gnd,pcbnew.F_Cu)
        track((9,10),(9.9,10),gnd,pcbnew.F_Cu)
        track((14,11),(14.9,11),gnd,pcbnew.F_Cu)
        for pts in (((10.9,12),(10.9,6),(14.9,6),(14.9,11)),
                    ((9.9,10),(9.9,16),(14.9,16),(14.9,11))):
            for a,b in zip(pts,pts[1:]): track(a,b,gnd,pcbnew.B_Cu)
        track((11.7,7),(11.7,15),vdd,pcbnew.F_Cu,locked=True)
        bounds=((3,3),(21,3),(21,19),(3,19),(3,3))
        for a,b in zip(bounds,bounds[1:]):
            edge=pcbnew.PCB_SHAPE(board); edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
            edge.SetStart(point(a)); edge.SetEnd(point(b)); edge.SetLayer(pcbnew.Edge_Cuts)
            edge.SetWidth(pcbnew.FromMM(0.05)); board.Add(edge)
        if not pcbnew.SaveBoard(str(out),board): raise RuntimeError("fixture save failed")
        """
    else:
        body = """
        import pcbnew, sys
        from pathlib import Path
        out=Path(sys.argv[1]); board=pcbnew.BOARD()
        gnd=pcbnew.NETINFO_ITEM(board,"GND"); board.Add(gnd)
        vdd=pcbnew.NETINFO_ITEM(board,"VDD"); board.Add(vdd)
        def point(xy): return pcbnew.VECTOR2I(pcbnew.FromMM(xy[0]),pcbnew.FromMM(xy[1]))
        def add_pad(ref,x,net):
            fp=pcbnew.FOOTPRINT(board); fp.SetReference(ref); fp.SetValue(""); fp.SetPosition(point((x,10)))
            pad=pcbnew.PAD(fp); pad.SetNumber("1"); pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT); pad.SetSize(point((1,1)))
            layers=pcbnew.LSET(); layers.AddLayer(pcbnew.F_Cu); pad.SetLayerSet(layers)
            pad.SetPosition(point((x,10))); pad.SetNet(net); fp.Add(pad); board.Add(fp)
            fp.Reference().SetVisible(False); fp.Value().SetVisible(False)
        def track(a,b,net,locked=False):
            item=pcbnew.PCB_TRACK(board); item.SetStart(point(a)); item.SetEnd(point(b))
            item.SetWidth(pcbnew.FromMM(0.3)); item.SetLayer(pcbnew.F_Cu); item.SetNet(net)
            item.SetLocked(locked); board.Add(item); return item
        add_pad("J1",5,gnd); add_pad("J2",25,gnd)
        add_pad("J3",15,vdd); add_pad("J4",20,vdd)
        # Two independent GND paths make one whole route and an exact duplicate removable.
        track((5,10),(5,5),gnd,locked=True)
        track((5,5),(25,5),gnd)
        track((25,5),(25,10),gnd)
        track((5,10),(5,5),gnd)  # duplicate geometry; keep the locked member
        for a,b in (((5,10),(5,15)),((5,15),(25,15)),((25,15),(25,10))): track(a,b,gnd)
        track((15,10),(20,10),vdd,locked=True)
        bounds=((1,1),(29,1),(29,19),(1,19),(1,1))
        for a,b in zip(bounds,bounds[1:]):
            edge=pcbnew.PCB_SHAPE(board); edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
            edge.SetStart(point(a)); edge.SetEnd(point(b)); edge.SetLayer(pcbnew.Edge_Cuts)
            edge.SetWidth(pcbnew.FromMM(0.05)); board.Add(edge)
        if not pcbnew.SaveBoard(str(out),board): raise RuntimeError("fixture save failed")
        """
    process = subprocess.run([native_python, "-c", textwrap.dedent(body), str(path)],
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stdout + process.stderr


@pytest.mark.skip_category("optional-tool")
def test_public_cleanup_prunes_duplicate_tracks_and_preserves_locked_and_unselected_copper(
    tmp_path, native_kicad,
):
    source = tmp_path / "duplicate-cleanup.kicad_pcb"
    output = tmp_path / "duplicate-cleaned.kicad_pcb"
    _make_board(source, native_kicad, shared_via_fixture=False)
    original = source.read_bytes()

    result = cleanup_copper(
        source, output, ["GND"],
        [{"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1"}],
        kicad_python_path=native_kicad,
    )

    assert result["status"] == "ok", result
    assert result["drc"]["passed"] is True
    assert source.read_bytes() == original
    report = result["cleanup"]
    assert report["before"]["GND"]["track_count"] > report["after"]["GND"]["track_count"]
    assert report["output_copper_is_subset"] is True
    assert report["all_pads_connected"] is True
    assert report["protected_paths_preserved"] is True
    assert report["copper_changes_limited_to_selected_nets"] is True
    assert report["unselected_copper_preserved"] is True
    assert report["locked_copper_preserved"] is True
    assert report["original_footprints_and_text_preserved"] is True


@pytest.mark.skip_category("optional-tool")
def test_public_cleanup_consolidates_three_shared_route_vias_to_two_with_full_drc(
    tmp_path, native_kicad,
):
    source = tmp_path / "shared-via-cleanup.kicad_pcb"
    output = tmp_path / "shared-via-cleaned.kicad_pcb"
    _make_board(source, native_kicad, shared_via_fixture=True)
    original = source.read_bytes()
    connections = [
        {"from_ref": "J1", "from_pad": "1", "to_ref": "J3", "to_pad": "1"},
        {"from_ref": "J2", "from_pad": "1", "to_ref": "J3", "to_pad": "1"},
    ]

    result = cleanup_copper(source, output, ["GND"], connections, kicad_python_path=native_kicad)

    assert result["status"] == "ok", result
    assert result["drc"]["passed"] is True
    assert source.read_bytes() == original
    report = result["cleanup"]
    assert report["before"]["GND"]["via_count"] == 3
    assert report["after"]["GND"]["via_count"] == 2
    assert report["output_copper_is_subset"] is False  # The new shared escapes add selected-net copper.
    assert report["all_pads_connected"] is True
    assert report["protected_paths_preserved"] is True
    assert report["copper_changes_limited_to_selected_nets"] is True
    assert report["unselected_copper_preserved"] is True
    assert report["locked_copper_preserved"] is True
    assert report["original_footprints_and_text_preserved"] is True
    rows = report["protected_connections"]
    assert len(rows) == len(connections)
    for row in rows:
        assert row["status"] == "connected"
        assert row["protected_path_preserved"] is True
        assert row["final_length_mm"] < row["baseline_length_mm"]
        assert row["final_length_mm"] <= row["limit_mm"]
    assert report["via_consolidation"]["groups"][0]["before_vias"] == 3
    assert report["via_consolidation"]["groups"][0]["after_vias"] == 2
