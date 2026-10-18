import hashlib
import json
from pathlib import Path

import pytest

from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.pcb_plane_cleanup import cleanup_plane_traces, load_plane_cleanup_plan

CONNECTION = {"from_ref": "J1", "from_pad": "1", "to_ref": "J2", "to_pad": "1"}
PLAN = {"net": "GND", "protected_connections": [CONNECTION]}
ZONE_CONFIG = {
    "clearance_iu": 250_000, "min_thickness_iu": 200_000, "thermal_gap_iu": 250_000,
    "thermal_spoke_width_iu": 300_000, "pad_connection": 1, "island_removal_mode": 0,
    "min_island_area_iu2": 10_000_000_000_000, "priority": 0, "layer": 0, "net": "GND",
}


@pytest.mark.parametrize("content", [
    "{}",
    json.dumps({**PLAN, "extra": True}),
    json.dumps({"net": " ", "protected_connections": [CONNECTION]}),
    json.dumps({"net": "GND", "protected_connections": []}),
    json.dumps({**PLAN, "protected_connections": [CONNECTION, CONNECTION]}),
    '{"net":"GND","net":"GND","protected_connections":[{"from_ref":"J1","from_pad":"1",'
    '"to_ref":"J2","to_pad":"1"}]}',
])
def test_plane_cleanup_plan_is_strict(tmp_path, content):
    path = tmp_path / "plan.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_plane_cleanup_plan(path)


def _drc(path: Path, *, digest=None, fail=False):
    return DrcResult(status="error" if fail else "ok", board=str(path),
                     board_sha256=digest or hashlib.sha256(path.read_bytes()).hexdigest(),
                     failure_reason="failed" if fail else "")


def _report():
    return {
        "status": "ok", "net": "GND", "removed_count": 1,
        "track_count_before": 4, "track_count_after": 3,
        "track_length_before_mm": 31.0, "track_length_after_mm": 21.0, "length_removed_mm": 10.0,
        "selected_net_track_count_before": 2, "selected_net_track_count_after": 1,
        "selected_net_track_length_before_mm": 20.0, "selected_net_track_length_after_mm": 10.0,
        "removed_tracks": [{"uuid": "trace-1", "net": "GND", "locked": False,
                            "geometry": {"start": [100, 200], "end": [10100, 200], "layer": 0, "width": 300},
                            "centerline_length_mm": 10.0}],
        "zones_before": [{"uuid": "zone-1", "net": "GND", "layer": 0, "layer_name": "F.Cu",
                           "config": dict(ZONE_CONFIG),
                           "outline_sha256": "a" * 64, "filled_polygon_sha256": "b" * 64,
                           "polygon_count": 1, "island_count": 0, "area_mm2": 10.0}],
        "zones_after": [{"uuid": "zone-1", "net": "GND", "layer": 0, "layer_name": "F.Cu",
                          "config": dict(ZONE_CONFIG),
                          "outline_sha256": "a" * 64, "filled_polygon_sha256": "c" * 64,
                          "polygon_count": 1, "island_count": 0, "area_mm2": 10.0}],
        "protected_connections": [{**CONNECTION, "status": "preserved", "protected_path_preserved": True,
                                    "trace_only_length_mm": 10.0, "trace_only_vias": 0,
                                    "frozen_item_uuids": ["protected-trace"]}],
        "existing_filled_planes_verified": True, "only_covered_unlocked_traces_removed": True,
        "no_copper_added": True, "vias_preserved": True, "nonselected_copper_preserved": True,
        "locked_copper_preserved": True, "original_geometry_preserved": True,
        "original_board_geometry_preserved": True,
        "saved_fill_geometry_verified": True, "saved_coverage_verified": True,
        "saved_connectivity_verified": True, "protected_paths_preserved": True,
        "all_same_net_pad_connectivity_preserved": True,
    }


def _fake_worker(candidate=b"plane cleaned board", report=None, source_to_mutate=None):
    def worker(operation, **kwargs):
        assert operation == "cleanup_plane_traces"
        assert kwargs["plan"] == PLAN
        Path(kwargs["output"]).write_bytes(candidate)
        if source_to_mutate:
            Path(source_to_mutate).write_bytes(b"changed source")
        return {"status": "ok", "plane_cleanup": report or _report(),
                "silkscreen": {"unresolved_count": 0, "remaining_conflict_count": 0}}
    return worker


def test_plane_cleanup_api_stages_and_publishes_with_hash_bound_drc(tmp_path, monkeypatch):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source board")
    source.with_suffix(".kicad_pro").write_bytes(b"project")
    source.with_suffix(".kicad_dru").write_bytes(b"rules")
    destination = tmp_path / "cleaned.kicad_pcb"
    candidate = b"plane cleaned board"
    monkeypatch.setattr("circuit_weaver.pcb_plane_cleanup.run_kicad", _fake_worker(candidate))
    monkeypatch.setattr("circuit_weaver.pcb_plane_cleanup.run_drc", lambda path, **_: _drc(path))

    result = cleanup_plane_traces(source, destination, PLAN)

    assert result["status"] == "ok"
    assert result["output_kind"] == "plane_cleaned_kicad_board"
    assert result["fabrication_ready"] is False
    assert destination.read_bytes() == candidate
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"project"
    assert destination.with_suffix(".kicad_dru").read_bytes() == b"rules"
    assert result["drc"]["board_sha256"] == hashlib.sha256(candidate).hexdigest()


@pytest.mark.parametrize("failure", ["input_drc", "output_drc", "stale_drc", "bad_report",
                                      "bad_zone_config", "source_changed"])
def test_plane_cleanup_rejects_bad_evidence_without_replacing_output(tmp_path, monkeypatch, failure):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")
    destination = tmp_path / "out.kicad_pcb"
    destination.write_bytes(b"previous output")
    calls = 0
    report = _report()
    if failure == "bad_report":
        report["saved_coverage_verified"] = False
    elif failure == "bad_zone_config":
        report["zones_before"][0]["config"] = {}
        report["zones_after"][0]["config"] = {}
    worker = _fake_worker(report=report, source_to_mutate=source if failure == "source_changed" else None)
    monkeypatch.setattr("circuit_weaver.pcb_plane_cleanup.run_kicad", worker)

    def drc(path, **_):
        nonlocal calls
        calls += 1
        if failure == "input_drc":
            return _drc(path, fail=True)
        if failure == "output_drc" and calls == 2:
            return _drc(path, fail=True)
        if failure == "stale_drc" and calls == 2:
            return _drc(path, digest="0" * 64)
        return _drc(path)

    monkeypatch.setattr("circuit_weaver.pcb_plane_cleanup.run_drc", drc)

    result = cleanup_plane_traces(source, destination, PLAN, overwrite=True)

    assert result["status"] == "error"
    assert destination.read_bytes() == b"previous output"
    assert not list(tmp_path.glob(".cw-plane-cleanup-*"))


def test_plane_cleanup_rejects_in_place_and_bad_output(tmp_path):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")
    assert cleanup_plane_traces(source, source, PLAN)["status"] == "error"
    assert cleanup_plane_traces(source, tmp_path / "out.ses", PLAN)["status"] == "error"


def test_plane_cleanup_report_rejects_protected_connection_over_maximum():
    from circuit_weaver.pcb_plane_cleanup import _valid_plane_cleanup_report

    bounded_plan = {"net": "GND", "protected_connections": [{**CONNECTION, "max_length_mm": 9.99}]}

    assert not _valid_plane_cleanup_report(_report(), bounded_plan)


def test_plane_cleanup_cli_reports_invalid_plan_as_json_error(tmp_path, monkeypatch, capsys):
    from circuit_weaver import dispatcher

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({**PLAN, "extra": True}), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["circuit-weaver", "plane-cleanup-pcb",
                                     str(tmp_path / "board.kicad_pcb"), "-o",
                                     str(tmp_path / "out.kicad_pcb"), "--plan", str(plan)])

    with pytest.raises(SystemExit) as exc:
        dispatcher.main()

    assert exc.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "error"
    assert "Plane-cleanup plan must contain only" in output["message"]
