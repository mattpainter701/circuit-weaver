"""Focused native KiCad tests for plane-backed trace cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from circuit_weaver import _native_plane_cleanup as cleanup
from circuit_weaver._native_copper_graph import CopperGraph, node_key
from circuit_weaver.kicad_bridge import find_kicad_python


def test_copper_graph_routes_report_the_exact_item_uuid_union():
    graph = object.__new__(CopperGraph)
    source, middle, target = node_key(0, 0, "F.Cu"), node_key(1, 0, "F.Cu"), node_key(2, 0, "F.Cu")
    graph.adj = {source: [(middle, 1.0, 0, "GND")],
                 middle: [(source, 1.0, 0, "GND"), (target, 1.0, 0, "GND")],
                 target: [(middle, 1.0, 0, "GND")]}
    graph.edge_items = {
        (source, middle, 1.0, 0, "GND"): {"track-a"},
        (middle, source, 1.0, 0, "GND"): {"track-a"},
        (middle, target, 1.0, 0, "GND"): {"track-b", "contact-peer"},
        (target, middle, 1.0, 0, "GND"): {"track-b", "contact-peer"},
    }
    graph.pad_nodes = {("J1", "1"): ("GND", source), ("J2", "1"): ("GND", target)}

    route = graph.route("J1", "1", "J2", "1")

    assert route["item_uuids"] == ["contact-peer", "track-a", "track-b"]


def test_polygon_boolean_overloads_follow_native_api_features():
    class ModernPolySet:
        def BooleanAdd(self, polygons):
            self.added = polygons

        def BooleanSubtract(self, subject, clip):
            self.subtracted = (subject, clip)

    modern = type("ModernPcbnew", (), {"SHAPE_POLY_SET": ModernPolySet})
    modern_destination = ModernPolySet()
    cleanup._boolean_add(modern_destination, "polygon", modern)
    cleanup._boolean_subtract(modern_destination, "subject", "clip", modern)
    assert modern_destination.added == "polygon"
    assert modern_destination.subtracted == ("subject", "clip")

    class LegacyPolySet:
        PM_FAST = 1
        PM_STRICTLY_SIMPLE = 2

        def BooleanAdd(self, polygons, mode):
            self.added = (polygons, mode)

        def BooleanSubtract(self, subject, clip, mode):
            self.subtracted = (subject, clip, mode)

    legacy = type("LegacyPcbnew", (), {"SHAPE_POLY_SET": LegacyPolySet})
    legacy_destination = LegacyPolySet()
    cleanup._boolean_add(legacy_destination, "polygon", legacy)
    cleanup._boolean_subtract(legacy_destination, "subject", "clip", legacy)
    assert legacy_destination.added == ("polygon", LegacyPolySet.PM_STRICTLY_SIMPLE)
    assert legacy_destination.subtracted == ("subject", "clip", LegacyPolySet.PM_STRICTLY_SIMPLE)


@pytest.mark.skip_category("optional-tool")
def test_native_cleanup_uses_full_filled_polygons_and_verifies_saved_board(tmp_path):
    native_python = find_kicad_python()
    if not native_python:
        reason = "Native plane cleanup integration requires KiCad pcbnew Python"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    source, output = tmp_path / "plane-source.kicad_pcb", tmp_path / "plane-cleaned.kicad_pcb"
    script = tmp_path / "run_plane_cleanup.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys
            sys.path.insert(0, sys.argv[3])
            import pcbnew
            from circuit_weaver import _native_ground_plane as ground
            from circuit_weaver import _native_plane_cleanup as cleanup

            source, output = sys.argv[1:3]
            def p(x, y): return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
            board = pcbnew.BOARD()
            gnd, vdd = pcbnew.NETINFO_ITEM(board, "GND"), pcbnew.NETINFO_ITEM(board, "VDD")
            board.Add(gnd); board.Add(vdd)
            edges = (((2, 2), (22, 2)), ((22, 2), (22, 18)),
                     ((22, 18), (2, 18)), ((2, 18), (2, 2)))
            for a, b in edges:
                edge = pcbnew.PCB_SHAPE(board); edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
                edge.SetStart(p(*a)); edge.SetEnd(p(*b)); edge.SetLayer(pcbnew.Edge_Cuts)
                edge.SetWidth(pcbnew.FromMM(0.05)); board.Add(edge)

            def pad(ref, xy, net):
                fp = pcbnew.FOOTPRINT(board); fp.SetReference(ref); fp.SetValue(""); fp.SetPosition(p(*xy))
                item = pcbnew.PAD(fp); item.SetNumber("1"); item.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                item.SetShape(pcbnew.PAD_SHAPE_RECT); item.SetSize(p(0.8, 0.8))
                layers = pcbnew.LSET(); layers.AddLayer(pcbnew.F_Cu); item.SetLayerSet(layers)
                item.SetPosition(p(*xy)); item.SetNet(net); fp.Add(item); board.Add(fp)
                fp.Reference().SetVisible(False); fp.Value().SetVisible(False)

            def track(a, b, net, *, locked=False, layer=pcbnew.F_Cu):
                item = pcbnew.PCB_TRACK(board); item.SetStart(p(*a)); item.SetEnd(p(*b))
                item.SetWidth(pcbnew.FromMM(0.3)); item.SetLayer(layer); item.SetNet(net)
                item.SetLocked(locked); board.Add(item); return item

            def via(xy, net):
                item = pcbnew.PCB_VIA(board); item.SetPosition(p(*xy)); item.SetWidth(pcbnew.FromMM(0.6))
                item.SetDrill(pcbnew.FromMM(0.3)); item.SetViaType(pcbnew.VIATYPE_THROUGH)
                item.SetNet(net); board.Add(item); return item

            pad("J1", (6, 5), gnd); pad("J2", (18, 5), gnd)
            pad("J3", (6, 13), vdd); pad("J4", (18, 13), vdd)
            protected_track = track((6, 5), (18, 5), gnd)
            non_gnd = track((6, 13), (18, 13), vdd)
            retained_via = via((12, 5), gnd)
            protected_track_id = str(protected_track.m_Uuid.AsString())
            non_gnd_id = str(non_gnd.m_Uuid.AsString())
            retained_via_id = str(retained_via.m_Uuid.AsString())
            if not pcbnew.SaveBoard(source, board): raise RuntimeError("initial source save failed")
            board = pcbnew.LoadBoard(source)

            print("made source geometry", file=sys.stderr, flush=True)
            protected = [{"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1"}]
            ground.apply(board, pcbnew, protected_connections=protected)
            print("ground plane applied", file=sys.stderr, flush=True)
            gnd = board.GetNetsByName()["GND"]
            covered = track((6, 8), (10, 8), gnd)
            locked = track((6, 10), (10, 10), gnd, locked=True)
            if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()): raise RuntimeError("refill with candidates failed")
            if not pcbnew.SaveBoard(source, board): raise RuntimeError("source save failed")

            print("filled source", file=sys.stderr, flush=True)
            board = pcbnew.LoadBoard(source)
            print("loaded source", file=sys.stderr, flush=True)
            limited = pcbnew.LoadBoard(source)
            try:
                cleanup.apply(limited, pcbnew, protected_connections=[
                    {"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1", "max_length_mm": 1.0},
                ])
                max_length_rejected = False
            except ValueError:
                max_length_rejected = True
            if len(list(limited.GetTracks())) != len(list(board.GetTracks())):
                raise RuntimeError("protected path length rejection changed copper")
            report = cleanup.apply(board, pcbnew, protected_connections=protected)
            removed_keepalive = report.pop("_native_removed_items", [])
            print("applied cleanup", file=sys.stderr, flush=True)
            if not pcbnew.SaveBoard(output, board): raise RuntimeError("cleaned save failed")
            saved = pcbnew.LoadBoard(output)
            verification = cleanup.verify(saved, pcbnew, report, protected_connections=protected)
            print("verified saved", file=sys.stderr, flush=True)

            ids = {str(item.m_Uuid.AsString()): item for item in saved.GetTracks()}
            removed_id = str(covered.m_Uuid.AsString())
            retained_ids = {protected_track_id, str(locked.m_Uuid.AsString()), non_gnd_id, retained_via_id}
            tampered = pcbnew.LoadBoard(output)
            list(tampered.Zones())[0].SetMinThickness(pcbnew.FromMM(0.15))
            try:
                cleanup.verify(tampered, pcbnew, report, protected_connections=protected)
                config_tamper_detected = False
            except ValueError:
                config_tamper_detected = True
            stale = pcbnew.LoadBoard(source)
            list(stale.Zones())[0].SetNeedRefill(True)
            try:
                cleanup.apply(stale, pcbnew, protected_connections=protected)
                stale_fill_rejected = False
            except ValueError:
                stale_fill_rejected = True
            outside = pcbnew.SHAPE_POLY_SET(); outer = pcbnew.SHAPE_LINE_CHAIN()
            for xy in ((2, 2), (8, 2), (8, 8), (2, 8)): outer.Append(p(*xy))
            outer.SetClosed(True)
            outside.AddOutline(outer)
            hole = pcbnew.SHAPE_LINE_CHAIN()
            for xy in ((4.5, 4.5), (5.5, 4.5), (5.5, 5.5), (4.5, 5.5)): hole.Append(p(*xy))
            hole.SetClosed(True)
            outside.AddHole(hole, 0)
            crossing = pcbnew.SHAPE_SEGMENT(p(3, 5), p(7, 5), pcbnew.FromMM(0.25))
            contained = pcbnew.SHAPE_SEGMENT(p(2.5, 3), p(3.5, 3), pcbnew.FromMM(0.25))
            hole_rejected = not cleanup._fully_covered(cleanup._shape_polygon(crossing, pcbnew), outside, pcbnew)
            solid_accepted = cleanup._fully_covered(cleanup._shape_polygon(contained, pcbnew), outside, pcbnew)
            removed_keepalive_count = len(removed_keepalive)
            print(json.dumps({"report": report, "verification": verification,
                              "removed_id": removed_id, "removed_absent": removed_id not in ids,
                              "retained_ids_present": retained_ids.issubset(ids),
                              "config_tamper_detected": config_tamper_detected,
                              "stale_fill_rejected": stale_fill_rejected,
                              "max_length_rejected": max_length_rejected,
                              "hole_rejected": hole_rejected, "solid_accepted": solid_accepted,
                              "removed_keepalive_count": removed_keepalive_count}, sort_keys=True))
            """
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [native_python, str(script), str(source), str(output), str(Path(__file__).resolve().parents[1] / "src")],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    report = result["report"]
    assert report["status"] == "ok"
    assert report["removed_count"] >= 1
    assert result["removed_absent"] is True
    assert result["retained_ids_present"] is True
    assert result["config_tamper_detected"] is True
    assert result["stale_fill_rejected"] is True
    assert result["max_length_rejected"] is True
    assert result["hole_rejected"] is True
    assert result["solid_accepted"] is True
    assert report["track_count_before"] - report["track_count_after"] == report["removed_count"]
    assert report["length_removed_mm"] > 0
    assert report["protected_connections"][0]["frozen_item_uuids"]
    assert report["protected_connections"][0]["trace_only_vias"] == 0
    assert result["verification"]["saved_fill_geometry_verified"] is True
    assert result["verification"]["saved_coverage_verified"] is True
    assert result["verification"]["saved_connectivity_verified"] is True

