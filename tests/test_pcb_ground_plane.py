import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.pcb_ground_plane import add_ground_plane, load_ground_plane_plan

CONNECTION = {"from_ref": "J1", "from_pad": "1", "to_ref": "C1", "to_pad": "2"}
PLAN = {
    "net": "GND", "layers": ["F.Cu", "B.Cu"], "protected_connections": [CONNECTION],
    "edge_clearance_mm": 0.5, "clearance_mm": 0.25, "min_thickness_mm": 0.2,
    "thermal_gap_mm": 0.25, "thermal_spoke_width_mm": 0.3,
}


@pytest.mark.parametrize("content", [
    "{}",
    json.dumps({**PLAN, "layers": []}),
    json.dumps({**PLAN, "layers": ["F.Cu", "F.Cu"]}),
    json.dumps({**PLAN, "extra": True}),
    json.dumps({**PLAN, "clearance_mm": 0}),
    json.dumps({**PLAN, "protected_connections": [CONNECTION, CONNECTION]}),
    '{"net":"GND","net":"GND","layers":["F.Cu","B.Cu"],"protected_connections":[],'
    '"edge_clearance_mm":0.5,"clearance_mm":0.25,"min_thickness_mm":0.2,"thermal_gap_mm":0.25,'
    '"thermal_spoke_width_mm":0.3}',
])
def test_ground_plane_plan_is_strict(tmp_path, content):
    path = tmp_path / "plan.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_ground_plane_plan(path)


def test_ground_plane_plan_accepts_named_inner_copper_layers(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({**PLAN, "layers": ["In1.Cu", "In2.Cu"]}), encoding="utf-8")
    assert load_ground_plane_plan(path)["layers"] == ["In1.Cu", "In2.Cu"]


def test_pre_route_drc_exception_is_limited_to_unconnected_items():
    from circuit_weaver.pcb_ground_plane import _pre_route_drc_allowed

    assert _pre_route_drc_allowed(DrcResult(
        status="ok", board="board.kicad_pcb", raw_report={
            "violations": [], "schematic_parity": [], "unconnected_items": [{"type": "unconnected_items"}],
        },
    ))
    assert not _pre_route_drc_allowed(DrcResult(
        status="ok", board="board.kicad_pcb", raw_report={
            "violations": [{"type": "clearance"}], "schematic_parity": [],
            "unconnected_items": [{"type": "unconnected_items"}],
        },
    ))


def test_pre_route_preserves_library_warnings_but_rejects_misclassified_physical_faults():
    from circuit_weaver.pcb_ground_plane import _pre_route_drc_allowed
    assert _pre_route_drc_allowed(DrcResult(status="ok", board="board.kicad_pcb", raw_report={
        "violations": [{"type": "lib_footprint_issues", "severity": "warning"}],
        "schematic_parity": [], "unconnected_items": [{"type": "unconnected_items"}],
    }))
    assert not _pre_route_drc_allowed(DrcResult(status="ok", board="board.kicad_pcb", raw_report={
        "violations": [], "schematic_parity": [], "unconnected_items": [{"type": "clearance"}],
    }))


def _drc(path: Path, *, fail=False):
    return DrcResult(status="error" if fail else "ok", board=str(path),
                     board_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                     failure_reason="failed" if fail else "")


def _report():
    return {
        "status": "ok", "net": "GND", "layers": ["F.Cu", "B.Cu"], "added_zones": 2,
        "filled_zones": 2, "filled_area_mm2": 100.0, "island_count": 0,
        "zone_stats": [
            {"layer_name": "F.Cu", "layer_id": 0, "area_mm2": 50.0, "polygon_count": 1, "island_count": 0,
             "polygon_sha256": "a" * 64, "outline_sha256": "c" * 64, "net": "GND",
             "clearance_mm": 0.25, "min_thickness_mm": 0.2, "thermal_gap_mm": 0.25,
             "thermal_spoke_width_mm": 0.3, "pad_connection": 1, "island_removal_mode": 0},
            {"layer_name": "B.Cu", "layer_id": 2, "area_mm2": 50.0, "polygon_count": 1, "island_count": 0,
             "polygon_sha256": "b" * 64, "outline_sha256": "c" * 64, "net": "GND",
             "clearance_mm": 0.25, "min_thickness_mm": 0.2, "thermal_gap_mm": 0.25,
             "thermal_spoke_width_mm": 0.3, "pad_connection": 1, "island_removal_mode": 0},
        ],
        "zone_connected_copper_counts": [3, 3],
        "track_count_before": 4, "track_count_after": 4, "tracks_preserved": True,
        "routing_complete": True, "requires_route": False,
        "original_footprints_and_text_preserved": True, "original_board_geometry_preserved": True,
        "saved_fill_geometry_verified": True,
        "saved_connectivity_verified": True,
        "protected_connections": [{**CONNECTION, "status": "preserved", "trace_only_length_mm": 4.0,
                                    "trace_only_vias": 0}],
    }


def test_plane_api_stages_verifies_and_publishes_atomically(tmp_path, monkeypatch):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source board")
    source.with_suffix(".kicad_pro").write_bytes(b"project")
    source.with_suffix(".kicad_dru").write_bytes(b"rules")
    destination = tmp_path / "planed.kicad_pcb"
    original_files = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    candidate = b"filled board"

    def worker(operation, **kwargs):
        assert operation == "add_ground_plane"
        assert kwargs["plan"] == PLAN
        Path(kwargs["output"]).write_bytes(candidate)
        return {"status": "ok", "ground_plane": _report(),
                "silkscreen": {"unresolved_count": 0, "remaining_conflict_count": 0}}

    monkeypatch.setattr("circuit_weaver.pcb_ground_plane.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_ground_plane.run_drc", lambda path, **_: _drc(path))

    result = add_ground_plane(source, destination, PLAN)

    assert result["status"] == "ok"
    assert result["output_kind"] == "ground_plane_kicad_board"
    assert result["fabrication_ready"] is False
    assert destination.read_bytes() == candidate
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"project"
    assert destination.with_suffix(".kicad_dru").read_bytes() == b"rules"
    assert result["ground_plane"] == _report()
    assert result["source_sha256"] == hashlib.sha256(original_files["input.kicad_pcb"]).hexdigest()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name in original_files} == original_files


@pytest.mark.parametrize("failure", ["input_drc", "output_drc", "stale_output_drc", "bad_report",
                                      "missing_layout_flag", "false_layout_flag", "silkscreen", "nan_area",
                                      "bool_area", "missing_track_counts", "bad_connected_counts",
                                      "bad_protected_metric", "missing_geometry_flag", "bad_zone_fingerprint",
                                      "bad_zone_config", "outline_mismatch"])
def test_plane_api_rejects_unverified_candidate_without_replacing_output(tmp_path, monkeypatch, failure):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")
    destination = tmp_path / "out.kicad_pcb"
    destination.write_bytes(b"previous output")
    destination.with_suffix(".kicad_pro").write_bytes(b"previous project")

    def worker(operation, **kwargs):
        Path(kwargs["output"]).write_bytes(b"candidate")
        report = _report()
        if failure == "bad_report":
            report["saved_connectivity_verified"] = False
        elif failure == "missing_layout_flag":
            report.pop("original_footprints_and_text_preserved")
        elif failure == "false_layout_flag":
            report["original_footprints_and_text_preserved"] = False
        elif failure == "nan_area":
            report["filled_area_mm2"] = float("nan")
        elif failure == "bool_area":
            report["filled_area_mm2"] = True
        elif failure == "missing_track_counts":
            report.pop("track_count_before")
            report.pop("track_count_after")
        elif failure == "bad_connected_counts":
            report["zone_connected_copper_counts"] = [3]
        elif failure == "bad_protected_metric":
            report["protected_connections"][0]["trace_only_length_mm"] = float("nan")
        elif failure == "missing_geometry_flag":
            report.pop("original_board_geometry_preserved")
        elif failure == "bad_zone_fingerprint":
            report["zone_stats"][0]["polygon_sha256"] = "z" * 64
        elif failure == "bad_zone_config":
            report["zone_stats"][0]["clearance_mm"] = 0.5
        elif failure == "outline_mismatch":
            report["zone_stats"][0]["outline_sha256"] = "d" * 64
        return {"status": "ok", "ground_plane": report,
                "silkscreen": {"unresolved_count": int(failure == "silkscreen"), "remaining_conflict_count": 0}}

    drc_calls = 0

    def drc(path, **kwargs):
        nonlocal drc_calls
        drc_calls += 1
        result = _drc(path, fail=failure == "input_drc" or (failure == "output_drc" and drc_calls == 2))
        if failure == "stale_output_drc" and drc_calls == 2:
            return DrcResult(status="ok", board=str(path), board_sha256="0" * 64)
        return result

    monkeypatch.setattr("circuit_weaver.pcb_ground_plane.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_ground_plane.run_drc", drc)

    result = add_ground_plane(source, destination, PLAN, overwrite=True)

    assert result["status"] == "error"
    assert destination.read_bytes() == b"previous output"
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"previous project"
    assert not list(tmp_path.glob(".cw-ground-plane-*"))


def test_plane_api_rejects_in_place_and_bad_output(tmp_path):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")
    assert add_ground_plane(source, source, PLAN)["status"] == "error"
    assert add_ground_plane(source, tmp_path / "out.ses", PLAN)["status"] == "error"


def test_plane_cli_reports_invalid_plan_as_json_error(tmp_path, monkeypatch, capsys):
    from circuit_weaver import dispatcher

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({**PLAN, "extra": True}), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["circuit-weaver", "plane-pcb", str(tmp_path / "board.kicad_pcb"),
                                     "-o", str(tmp_path / "out.kicad_pcb"), "--plan", str(plan)])

    with pytest.raises(SystemExit) as exc:
        dispatcher.main()

    assert exc.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "error"
    assert "Ground-plane plan must contain only" in output["message"]


def test_worker_rejects_source_outline_geometry_mutation_before_save(monkeypatch):
    from circuit_weaver import _kicad_worker as worker

    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class Uuid:
        def AsString(self):
            return "outline-segment"

    class PCB_SHAPE:
        m_Uuid = Uuid()

        def __init__(self):
            self.start, self.end = Point(1, 2), Point(3, 4)

        def GetShape(self):
            return 0

        def GetShapeStr(self):
            return "Line"

        def GetLayer(self):
            return 44

        def GetStart(self):
            return self.start

        def GetEnd(self):
            return self.end

        def GetWidth(self):
            return 50_000

        def GetFillMode(self):
            return 0

        def IsLocked(self):
            return False

        def GetNetname(self):
            return ""

    shape = PCB_SHAPE()

    class Board:
        def GetFootprints(self):
            return []

        def GetDrawings(self):
            return [shape]

        def GetTracks(self):
            return []

    board = Board()
    pcbnew = ModuleType("pcbnew")
    pcbnew.SHAPE_T_SEGMENT = 0
    pcbnew.LoadBoard = lambda _path: board
    native = ModuleType("_native_ground_plane")

    def mutate_outline(*_args, **_kwargs):
        shape.end.x += 1
        return {"status": "ok"}

    native.apply = mutate_outline
    monkeypatch.setitem(sys.modules, "pcbnew", pcbnew)
    monkeypatch.setitem(sys.modules, "_native_ground_plane", native)
    monkeypatch.setattr(worker, "_identity", lambda _board: {})
    monkeypatch.setattr(worker, "_fixed_layout_state", lambda _board: {})
    monkeypatch.setattr(worker, "_copper_state", lambda _board: {})

    with pytest.raises(ValueError, match="changed source footprint, pad, model, drawing or outline geometry"):
        worker.execute({"board": "source.kicad_pcb", "operation": "add_ground_plane", "output": "unused.kicad_pcb",
                        "plan": {"net": "GND", "layers": ["F.Cu", "B.Cu"], "protected_connections": [],
                                 "edge_clearance_mm": 0.5, "clearance_mm": 0.25, "min_thickness_mm": 0.2,
                                 "thermal_gap_mm": 0.25, "thermal_spoke_width_mm": 0.3}})
