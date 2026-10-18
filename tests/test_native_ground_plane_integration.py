"""Real KiCad integration test for native plane fill and saved-board verification."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from circuit_weaver.kicad_bridge import find_kicad_python
from circuit_weaver.pcb_ground_plane import add_ground_plane


@pytest.mark.skip_category("optional-tool")
def test_native_ground_planes_fill_and_preserve_routes_after_reload(tmp_path):
    native_python = find_kicad_python()
    if not native_python:
        reason = "Native ground-plane integration requires KiCad pcbnew Python"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    source = tmp_path / "ground_fixture.kicad_pcb"
    output = tmp_path / "ground_filled.kicad_pcb"
    script = tmp_path / "fill_ground_fixture.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys
            sys.path.insert(0, sys.argv[3])
            import pcbnew
            from circuit_weaver import _native_ground_plane as ground

            source, output = sys.argv[1:3]
            print("fixture: imports", file=sys.stderr, flush=True)
            def point(x, y):
                return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))

            board = pcbnew.BOARD()
            gnd, vdd = pcbnew.NETINFO_ITEM(board, "GND"), pcbnew.NETINFO_ITEM(board, "VDD")
            board.Add(gnd); board.Add(vdd)
            for a, b in (((2, 2), (22, 2)), ((22, 2), (22, 18)),
                         ((22, 18), (2, 18)), ((2, 18), (2, 2))):
                edge = pcbnew.PCB_SHAPE(board); edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
                edge.SetStart(point(*a)); edge.SetEnd(point(*b)); edge.SetLayer(pcbnew.Edge_Cuts)
                edge.SetWidth(pcbnew.FromMM(0.05)); board.Add(edge)

            def add_pad(ref, xy, net):
                fp = pcbnew.FOOTPRINT(board); fp.SetReference(ref); fp.SetValue("")
                fp.SetPosition(point(*xy))
                pad = pcbnew.PAD(fp); pad.SetNumber("1"); pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetShape(pcbnew.PAD_SHAPE_RECT); pad.SetSize(point(0.8, 0.8))
                layers = pcbnew.LSET(); layers.AddLayer(pcbnew.F_Cu); pad.SetLayerSet(layers)
                pad.SetPosition(point(*xy)); pad.SetNet(net); fp.Add(pad); board.Add(fp)
                fp.Reference().SetVisible(False); fp.Value().SetVisible(False)

            def add_track(a, b, net, layer=pcbnew.F_Cu):
                track = pcbnew.PCB_TRACK(board); track.SetStart(point(*a)); track.SetEnd(point(*b))
                track.SetWidth(pcbnew.FromMM(0.3)); track.SetLayer(layer); track.SetNet(net); board.Add(track)

            def copper_inventory(board):
                rows = []
                for item in board.GetTracks():
                    if item.GetClass() == "PCB_VIA":
                        position = item.GetPosition()
                        try:
                            top_width = item.GetWidth(item.TopLayer())
                            bottom_width = item.GetWidth(item.BottomLayer())
                        except TypeError:
                            top_width = bottom_width = item.GetWidth()
                        geometry = (round(pcbnew.ToMM(position.x), 6), round(pcbnew.ToMM(position.y), 6),
                                    int(item.TopLayer()), int(item.BottomLayer()),
                                    int(top_width), int(bottom_width),
                                    int(item.GetDrill()))
                    else:
                        start, end = item.GetStart(), item.GetEnd()
                        geometry = (round(pcbnew.ToMM(start.x), 6), round(pcbnew.ToMM(start.y), 6),
                                    round(pcbnew.ToMM(end.x), 6), round(pcbnew.ToMM(end.y), 6),
                                    int(item.GetLayer()), int(item.GetWidth()))
                    rows.append((item.GetClass(), item.GetNetname(), geometry))
                return sorted(rows)

            for ref, xy in (("J1", (6, 5)), ("J2", (18, 5)),
                            ("J3", (6, 13)), ("J4", (18, 13))):
                add_pad(ref, xy, gnd if ref in ("J1", "J2") else vdd)
            add_track((6, 5), (18, 5), gnd)
            add_track((6, 13), (18, 13), vdd)
            via = pcbnew.PCB_VIA(board); via.SetPosition(point(12, 5))
            via.SetWidth(pcbnew.FromMM(0.6)); via.SetDrill(pcbnew.FromMM(0.3))
            via.SetViaType(pcbnew.VIATYPE_THROUGH); via.SetNet(gnd); board.Add(via)
            if not pcbnew.SaveBoard(source, board):
                raise RuntimeError("could not save native ground-plane fixture")
            print("fixture: source saved", file=sys.stderr, flush=True)

            board = pcbnew.LoadBoard(source)
            print("fixture: source loaded", file=sys.stderr, flush=True)
            before = copper_inventory(board)
            protected = [
                {"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1"},
                {"from_ref": "J3", "from_pad": "1", "to_ref": "J4", "to_pad": "1"},
            ]
            print("fixture: applying", file=sys.stderr, flush=True)
            result = ground.apply(board, pcbnew, protected_connections=protected)
            print("fixture: applied", file=sys.stderr, flush=True)
            if not pcbnew.SaveBoard(output, board):
                raise RuntimeError("could not save filled ground-plane fixture")
            print("fixture: output saved", file=sys.stderr, flush=True)
            saved = pcbnew.LoadBoard(output)
            print("fixture: output loaded", file=sys.stderr, flush=True)
            after = copper_inventory(saved)
            if before != after:
                raise RuntimeError("native plane fill changed source tracks or vias")
            saved_report = ground.verify(saved, pcbnew, result, protected_connections=protected)
            print("fixture: verified", file=sys.stderr, flush=True)
            tampered = pcbnew.LoadBoard(output)
            list(tampered.Zones())[0].SetMinThickness(pcbnew.FromMM(0.15))
            try:
                ground.verify(tampered, pcbnew, result, protected_connections=protected)
                tamper_detected = False
            except ValueError:
                tamper_detected = True
            print(json.dumps({"result": result, "saved_report": saved_report,
                              "tamper_detected": tamper_detected}, sort_keys=True))
            """
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [native_python, str(script), str(source), str(output), str(Path(__file__).resolve().parents[1] / "src")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    result, saved = report["result"], report["saved_report"]
    assert output.is_file()
    assert result["status"] == "ok"
    assert result["added_zones"] == result["filled_zones"] == 2
    assert result["filled_area_mm2"] > 0
    assert result["island_count"] == 0
    assert result["tracks_preserved"] is True
    assert len(result["protected_connections"]) == 2
    assert all(row["status"] == "preserved" for row in result["protected_connections"])
    assert saved["saved_fill_geometry_verified"] is True
    assert saved["saved_connectivity_verified"] is True
    assert saved["tracks_preserved"] is True
    assert report["tamper_detected"] is True


@pytest.mark.skip_category("optional-tool")
def test_public_ground_plane_api_passes_full_drc(tmp_path):
    native_python = find_kicad_python()
    if not native_python:
        reason = "Public ground-plane integration requires KiCad pcbnew Python"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    source, output = tmp_path / "public_fixture.kicad_pcb", tmp_path / "public_filled.kicad_pcb"
    script = tmp_path / "make_public_fixture.py"
    script.write_text(
        textwrap.dedent(
            """
            import pcbnew, sys
            out = sys.argv[1]
            def p(x, y): return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
            board = pcbnew.BOARD()
            gnd, vdd = pcbnew.NETINFO_ITEM(board, "GND"), pcbnew.NETINFO_ITEM(board, "VDD")
            board.Add(gnd); board.Add(vdd)
            for a,b in (((2,2),(22,2)),((22,2),(22,18)),((22,18),(2,18)),((2,18),(2,2))):
                edge=pcbnew.PCB_SHAPE(board);edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
                edge.SetStart(p(*a));edge.SetEnd(p(*b));edge.SetLayer(pcbnew.Edge_Cuts)
                edge.SetWidth(pcbnew.FromMM(.05));board.Add(edge)
            def pad(ref,xy,net):
                fp=pcbnew.FOOTPRINT(board);fp.SetReference(ref);fp.SetValue("");fp.SetPosition(p(*xy))
                item=pcbnew.PAD(fp);item.SetNumber("1");item.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                item.SetShape(pcbnew.PAD_SHAPE_RECT);item.SetSize(p(.8,.8))
                layers=pcbnew.LSET();layers.AddLayer(pcbnew.F_Cu);item.SetLayerSet(layers)
                item.SetPosition(p(*xy));item.SetNet(net);fp.Add(item);board.Add(fp)
                fp.Reference().SetVisible(False);fp.Value().SetVisible(False)
            def track(a,b,net):
                item=pcbnew.PCB_TRACK(board);item.SetStart(p(*a));item.SetEnd(p(*b))
                item.SetWidth(pcbnew.FromMM(.3));item.SetLayer(pcbnew.F_Cu);item.SetNet(net);board.Add(item)
            for ref,xy,net in (("J1",(6,5),gnd),("J2",(18,5),gnd),
                               ("J3",(6,13),vdd),("J4",(18,13),vdd)):
                pad(ref,xy,net)
            track((6,5),(18,5),gnd);track((6,13),(18,13),vdd)
            via=pcbnew.PCB_VIA(board);via.SetPosition(p(12,5));via.SetWidth(pcbnew.FromMM(.6))
            via.SetDrill(pcbnew.FromMM(.3));via.SetViaType(pcbnew.VIATYPE_THROUGH);via.SetNet(gnd);board.Add(via)
            if not pcbnew.SaveBoard(out,board): raise RuntimeError("fixture save failed")
            """
        ),
        encoding="utf-8",
    )
    created = subprocess.run([native_python, str(script), str(source)], capture_output=True, text=True, timeout=60)
    assert created.returncode == 0, created.stdout + created.stderr
    plan = {
        "net": "GND",
        "layers": ["F.Cu", "B.Cu"],
        "protected_connections": [
            {"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1"},
            {"from_ref": "J3", "from_pad": "1", "to_ref": "J4", "to_pad": "1"},
        ],
        "edge_clearance_mm": 0.5,
        "clearance_mm": 0.25,
        "min_thickness_mm": 0.2,
        "thermal_gap_mm": 0.2,
        "thermal_spoke_width_mm": 0.2,
    }
    result = add_ground_plane(source, output, plan, kicad_python_path=native_python)
    assert result["status"] == "ok", json.dumps(result, indent=2)
    assert output.is_file()
    assert result["drc"]["passed"] is True
    assert result["ground_plane"]["filled_zones"] == 2
    assert result["ground_plane"]["filled_area_mm2"] > 0
    assert result["ground_plane"]["tracks_preserved"] is True
    assert len(result["ground_plane"]["protected_connections"]) == 2
    assert all(row["status"] == "preserved" for row in result["ground_plane"]["protected_connections"])
