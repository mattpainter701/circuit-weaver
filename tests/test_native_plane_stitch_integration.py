"""Optional real-KiCad test for a starved thermal local plane island."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from circuit_weaver.drc_runner import run_drc
from circuit_weaver.evidence import EvidenceLedger
from circuit_weaver.kicad_bridge import find_kicad_python


@pytest.mark.skip_category("optional-tool")
def test_native_stitch_via_resolves_filled_local_thermal_island(tmp_path):
    native_python = find_kicad_python()
    if not native_python:
        reason = "Native plane-stitch integration requires KiCad pcbnew Python"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    source = tmp_path / "stitch_fixture.kicad_pcb"
    candidate = tmp_path / "stitch_candidate.kicad_pcb"
    project = source.with_suffix(".kicad_pro")
    violation_path = tmp_path / "thermal-violations.json"
    recipe = tmp_path / "make_stitch_fixture.py"
    recipe.write_text(
        textwrap.dedent(
            r'''
            import json, os, sys
            from pathlib import Path
            if os.name == "nt":
                import wx
                _wx_app = wx.GetApp() or wx.App(False)
            import pcbnew
            source = Path(sys.argv[1])

            def point(x, y):
                return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))

            board = pcbnew.BOARD()
            board.SetCopperLayerCount(4)
            gnd = pcbnew.NETINFO_ITEM(board, "GND")
            vcc = pcbnew.NETINFO_ITEM(board, "VCC")
            board.Add(gnd); board.Add(vcc)
            outline = ((2, 2), (28, 2), (28, 28), (2, 28))
            for start, end in zip(outline, outline[1:] + outline[:1]):
                edge = pcbnew.PCB_SHAPE(board)
                edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
                edge.SetStart(point(*start)); edge.SetEnd(point(*end))
                edge.SetLayer(pcbnew.Edge_Cuts); edge.SetWidth(pcbnew.FromMM(0.05))
                board.Add(edge)

            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference("J1"); fp.SetValue(""); fp.SetPosition(point(15, 15))
            fp.Reference().SetVisible(False); fp.Value().SetVisible(False)
            pad = pcbnew.PAD(fp); pad.SetNumber("1")
            pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH); pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetSize(point(1.0, 1.0)); pad.SetDrillSize(point(0.4, 0.4))
            copper = pcbnew.LSET()
            for layer in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu):
                copper.AddLayer(layer)
            pad.SetLayerSet(copper); pad.SetPosition(point(15, 15)); pad.SetNet(gnd)
            fp.Add(pad); board.Add(fp)

            def add_gnd_anchor(reference, xy):
                anchor_fp = pcbnew.FOOTPRINT(board)
                anchor_fp.SetReference(reference); anchor_fp.SetValue("")
                anchor_fp.Reference().SetVisible(False); anchor_fp.Value().SetVisible(False)
                anchor_fp.SetPosition(point(*xy))
                anchor = pcbnew.PAD(anchor_fp); anchor.SetNumber("1")
                anchor.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
                anchor.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
                anchor.SetSize(point(1.0, 1.0)); anchor.SetDrillSize(point(0.4, 0.4))
                anchor.SetLayerSet(copper); anchor.SetPosition(point(*xy)); anchor.SetNet(gnd)
                anchor_fp.Add(anchor); board.Add(anchor_fp)

            # Keep a dominant, externally connected In1 plane island, and
            # give the target pad one existing F.Cu spoke. The foreign-net
            # In1 fence blocks two other thermal spokes, leaving a realistic
            # one-spoke starved thermal that a via into the plane can repair.
            add_gnd_anchor("J2", (5, 5))
            add_gnd_anchor("J3", (20, 15))
            manual_spoke = pcbnew.PCB_TRACK(board)
            manual_spoke.SetStart(point(15, 15)); manual_spoke.SetEnd(point(20, 15))
            manual_spoke.SetWidth(pcbnew.FromMM(0.25)); manual_spoke.SetLayer(pcbnew.F_Cu)
            manual_spoke.SetNet(gnd); board.Add(manual_spoke)

            # A closed foreign-net track loop leaves a narrow GND corridor
            # beside the PTH pad. Its single legal thermal direction is the
            # deliberately starved source polygon targeted by the DRC fixture.
            wall = ((14.2, 13.5), (17.5, 13.5), (17.5, 16.5), (14.2, 16.5))
            for start, end in zip(wall, wall[1:] + wall[:1]):
                track = pcbnew.PCB_TRACK(board)
                track.SetStart(point(*start)); track.SetEnd(point(*end))
                track.SetWidth(pcbnew.FromMM(0.2)); track.SetLayer(pcbnew.In1_Cu)
                track.SetNet(vcc); board.Add(track)

            def zone(layer):
                item = pcbnew.ZONE(board)
                item.SetLayer(layer); item.SetNet(gnd)
                item.SetLocalClearance(pcbnew.FromMM(0.2))
                item.SetMinThickness(pcbnew.FromMM(0.2))
                item.SetThermalReliefGap(pcbnew.FromMM(0.25))
                item.SetThermalReliefSpokeWidth(pcbnew.FromMM(0.3))
                item.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
                item.SetMinIslandArea(0)
                item.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
                contour = item.Outline(); contour.NewOutline()
                for xy in ((3, 3), (27, 3), (27, 27), (3, 27)):
                    contour.Append(point(*xy))
                board.Add(item)
                return item

            source_zone = zone(pcbnew.In1_Cu)
            zone(pcbnew.In2_Cu)
            board.BuildConnectivity()
            if not pcbnew.SaveBoard(str(source), board):
                raise RuntimeError("could not save initial stitch fixture")
            board = pcbnew.LoadBoard(str(source))
            board.BuildConnectivity()
            if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
                raise RuntimeError("could not fill initial stitch fixture")
            board.BuildConnectivity()
            if not pcbnew.SaveBoard(str(source), board):
                raise RuntimeError("could not save filled stitch fixture")
            '''
        ),
        encoding="utf-8",
    )
    project.write_text(json.dumps({
        "net_settings": {"classes": [{
            "name": "Default", "clearance": 0.2, "track_width": 0.25,
            "via_diameter": 0.6, "via_drill": 0.3,
        }]},
    }), encoding="utf-8")
    import circuit_weaver
    package = Path(circuit_weaver.__file__).resolve().parent
    created = subprocess.run(
        [native_python, str(recipe), str(source)],
        capture_output=True, text=True, timeout=120,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    source_snapshot = source.read_bytes()

    baseline = run_drc(source, evidence_ledger=EvidenceLedger(), timeout=180, project_context=tmp_path)
    assert baseline.status == "ok", baseline.failure_reason
    raw = baseline.raw_report
    assert isinstance(raw, dict), "native baseline DRC did not return its raw report"
    thermal = [row for row in raw.get("violations", []) if isinstance(row, dict)
               and row.get("type") == "starved_thermal" and row.get("severity") == "error"]
    assert thermal, "fixture must produce a native starved-thermal UUID pair"
    blockers = [row for row in raw.get("violations", []) if isinstance(row, dict)
                and row.get("severity") == "error"]
    assert len(blockers) == len(thermal), "fixture baseline must contain only starved-thermal blockers"
    assert baseline.blocker_count == len(thermal), "fixture baseline must have no unconnected or other blockers"
    violation_path.write_text(json.dumps(thermal, indent=2) + "\n", encoding="utf-8")

    apply_script = tmp_path / "apply_native_stitches.py"
    apply_script.write_text(
        textwrap.dedent(
            r'''
            import json, os, sys
            from pathlib import Path
            if os.name == "nt":
                import wx
                _wx_app = wx.GetApp() or wx.App(False)
            import pcbnew
            sys.path.insert(0, sys.argv[3])
            import _native_plane_stitch as stitch
            import _kicad_worker as worker
            source, candidate, package, report_path, violation_path = map(Path, sys.argv[1:6])
            source_project = source.with_suffix(".kicad_pro")
            candidate_project = candidate.with_suffix(".kicad_pro")
            candidate_project.write_bytes(source_project.read_bytes())
            source_rules = source.with_suffix(".kicad_dru")
            candidate_rules = candidate.with_suffix(".kicad_dru")
            if source_rules.exists():
                candidate_rules.write_bytes(source_rules.read_bytes())
            board = pcbnew.LoadBoard(str(source))
            identity = worker._identity(board)
            layout = worker._fixed_layout_state(board)
            geometry = worker._ground_plane_source_geometry_state(board, pcbnew)
            zones = worker._zone_definition_state(board, pcbnew)
            copper = worker._copper_state(board)
            violations = json.loads(violation_path.read_text(encoding="utf-8"))
            result = stitch.repair(board, pcbnew, violations, max_vias=4,
                                   search_radius_mm=6.0, grid_step_mm=0.25)
            if result.get("status") != "candidate_only_requires_full_drc":
                raise RuntimeError("stitch helper did not return candidate-only status")
            if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
                raise RuntimeError("could not refill candidate plane zones")
            board.BuildConnectivity()
            if not pcbnew.SaveBoard(str(candidate), board):
                raise RuntimeError("could not save candidate stitch board")
            candidate_project.write_bytes(source_project.read_bytes())
            if source_rules.exists():
                candidate_rules.write_bytes(source_rules.read_bytes())
            reloaded = pcbnew.LoadBoard(str(candidate))
            reloaded.BuildConnectivity()
            if not pcbnew.ZONE_FILLER(reloaded).Fill(reloaded.Zones()):
                raise RuntimeError("could not refill reloaded candidate planes")
            reloaded.BuildConnectivity()
            if not pcbnew.SaveBoard(str(candidate), reloaded):
                raise RuntimeError("could not save reloaded candidate planes")
            if (worker._identity(reloaded) != identity
                    or worker._fixed_layout_state(reloaded) != layout
                    or worker._ground_plane_source_geometry_state(reloaded, pcbnew) != geometry
                    or worker._zone_definition_state(reloaded, pcbnew) != zones):
                raise RuntimeError("saved candidate changed source labels, placement, geometry, or zone rules")
            added = worker._copper_state(reloaded) - copper
            removed = copper - worker._copper_state(reloaded)
            added_rows = [json.loads(row) for row, count in added.items() for _ in range(count)]
            if (removed or len(added_rows) != result["added_vias"]
                    or any(row.get("type") != "PCB_VIA" or row.get("net") != "GND"
                           or row.get("locked") for row in added_rows)):
                raise RuntimeError("saved candidate changed existing copper or added non-GND tracks")
            report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": result["status"], "added_vias": result["added_vias"],
                              "locations": result["locations"]}), flush=True)
            '''
        ),
        encoding="utf-8",
    )
    applied = subprocess.run(
        [native_python, str(apply_script), str(source), str(candidate), str(package),
         str(tmp_path / "stitch-result.json"), str(violation_path)],
        capture_output=True, text=True, timeout=180,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert source.read_bytes() == source_snapshot, "native stitch operation modified the frozen source board"
    candidate.with_suffix(".kicad_pro").write_bytes(project.read_bytes())
    assert candidate.with_suffix(".kicad_pro").read_bytes() == project.read_bytes()
    if source.with_suffix(".kicad_dru").exists():
        candidate.with_suffix(".kicad_dru").write_bytes(source.with_suffix(".kicad_dru").read_bytes())
        assert candidate.with_suffix(".kicad_dru").read_bytes() == source.with_suffix(".kicad_dru").read_bytes()
    stitch_result = json.loads((tmp_path / "stitch-result.json").read_text(encoding="utf-8"))
    assert 1 <= stitch_result["added_vias"] <= 4
    assert stitch_result["status"] == "candidate_only_requires_full_drc"
    assert stitch_result["requires_zone_refill_save_reload_and_fresh_full_native_drc"] is True

    final = run_drc(candidate, evidence_ledger=EvidenceLedger(), timeout=180, project_context=tmp_path)
    assert final.status == "ok", final.failure_reason
    assert final.passed, json.dumps(final.to_dict(), indent=2)
    assert final.blocker_count == 0
    assert source.read_bytes() != candidate.read_bytes(), "candidate should contain new same-net stitch copper"
