"""Workflow contracts for opt-in route recovery and deferred plane filling."""

import json
from pathlib import Path

import pytest

from circuit_weaver import project_workflow as workflow


@pytest.mark.parametrize(
    "options",
    [
        {"route_repair_open_connections": 1},
        {"route_repair_open_connections": "true"},
        {"ground_plane_after_route": 1},
        {"route_recovery_timeout": 0},
        {"route_recovery_timeout": 1801},
        {"route_recovery_timeout": True},
        {"route_recovery_timeout": float("nan")},
        {"route_max_repair_links": 0},
        {"route_max_repair_links": 513},
        {"route_max_repair_links": True},
        {"route_max_repair_links": 1.5},
    ],
)
def test_new_route_recovery_options_reject_invalid_values(options):
    with pytest.raises(workflow.WorkflowError) as exc:
        workflow._normalize_options(options)
    assert exc.value.code == "invalid_options"


@pytest.mark.parametrize("timeout,links", [(1, 1), (1800, 512)])
def test_new_route_recovery_options_accept_documented_bounds(timeout, links):
    normalized = workflow._normalize_options(
        {
            "route_repair_open_connections": True,
            "ground_plane_after_route": False,
            "route_recovery_timeout": timeout,
            "route_max_repair_links": links,
        }
    )
    assert normalized["route_repair_open_connections"] is True
    assert normalized["ground_plane_after_route"] is False
    assert normalized["route_recovery_timeout"] == timeout
    assert normalized["route_max_repair_links"] == links


def test_route_recovery_flags_forward_and_parent_budget_covers_both_phases(tmp_path, monkeypatch):
    root = tmp_path / "route-recovery"
    root.mkdir()
    (root / "placed.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    observed = {}

    def fake_cli(stage, args, cwd, timeout):
        assert stage == "route"
        observed["args"] = args
        observed["timeout"] = timeout
        Path(args[args.index("--output") + 1]).write_text("(session)", encoding="utf-8")
        Path(args[args.index("--routed-board") + 1]).write_text("(kicad_pcb)", encoding="utf-8")
        return {"status": "ok"}

    monkeypatch.setattr(workflow, "_cli", fake_cli)
    result, outputs = workflow._execute_stage(
        root,
        "route",
        {
            "route_timeout": 1800,
            "route_repair_open_connections": True,
            "route_recovery_timeout": 1800,
            "route_max_repair_links": 512,
            "route_max_passes": 250,
            "route_attempts": 3,
            "route_optimization_threads": 2,
        },
        [],
    )

    args = observed["args"]
    assert "--repair-open-connections" in args
    for flag, expected in (
        ("--recovery-timeout", "1800"),
        ("--max-repair-links", "512"),
        ("--max-passes", "250"),
        ("--attempts", "3"),
        ("--optimization-threads", "2"),
    ):
        assert args[args.index(flag) + 1] == expected
    assert float(args[args.index("--timeout") + 1]) == 1800
    assert observed["timeout"] == 4200
    assert result["status"] == "ok"
    assert {Path(path).name for path in outputs} == {"routed.kicad_pcb", "routed.ses"}


def _verified_plane_refill():
    return {
        "status": "verified",
        "saved_fill_geometry_verified": True,
        "zone_definitions_preserved": True,
        "copper_layers_preserved": True,
        "zone_count": 1,
        "zones": [
            {
                "id": "zone-1",
                "net": "GND",
                "layer_name": "In1.Cu",
                "area_mm2": 12.5,
                "island_count": 0,
                "polygon_sha256": "a" * 64,
            }
        ],
        "unconnected_pads_by_net": {"GND": []},
        "connected_copper": [{"id": "zone-1", "connected_copper_count": 2}],
    }


def test_deferred_ground_planes_are_added_after_route_then_refilled_and_imported_to_primary_board(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "deferred-plane"
    root.mkdir()
    (root / "placed.kicad_pcb").write_text("placed board", encoding="utf-8")
    events = []
    session = ""

    def fake_cli(stage, args, cwd, timeout):
        events.append("route")
        nonlocal session
        session = args[args.index("--output") + 1]
        board = args[args.index("--routed-board") + 1]
        Path(session).write_text("complete session", encoding="utf-8")
        Path(board).write_text("routed board", encoding="utf-8")
        return {"status": "ok"}

    def add_plane(board, output, plan, **kwargs):
        events.append(("add_plane", Path(board).read_text(encoding="utf-8"), kwargs))
        Path(output).write_text("plane-filled candidate", encoding="utf-8")
        return {"status": "ok", "output_path": str(output)}

    def import_session(board, ses, output, **kwargs):
        events.append(("import", Path(board).read_text(encoding="utf-8"), Path(ses).read_text(encoding="utf-8")))
        Path(output).write_text("accepted plane-routed board", encoding="utf-8")
        return {"status": "ok", "zone_refill": _verified_plane_refill(), "drc": {"board_sha256": "b" * 64}}

    monkeypatch.setattr(workflow, "_cli", fake_cli)
    monkeypatch.setattr("circuit_weaver.pcb_ground_plane.add_ground_plane", add_plane)
    monkeypatch.setattr("circuit_weaver.pcb_layout.import_routing_session", import_session)

    result, outputs = workflow._execute_stage(
        root,
        "route",
        {
            "ground_plane_after_route": True,
            "ground_plane": {"net": "GND", "layers": ["In1.Cu"]},
        },
        [],
    )

    assert events[0] == "route"
    assert events[1][0] == "add_plane"
    assert events[1][1] == "placed board"
    assert events[1][2].get("pre_route") is True
    assert events[2] == ("import", "plane-filled candidate", "complete session")
    primary = root / "routed.kicad_pcb"
    assert primary.read_text(encoding="utf-8") == "accepted plane-routed board"
    assert Path(result["artifact"]["path"]) == primary
    assert {Path(path).name for path in outputs} == {"routed.kicad_pcb", "routed.ses", "ground-plane.json"}
    plane_record = json.loads((root / "ground-plane.json").read_text(encoding="utf-8"))
    assert plane_record["preparation_phase"] == "after_route"
    assert plane_record["after_route"]["status"] == "verified"


def test_deferred_plane_refill_must_pass_the_same_strict_plane_gate(tmp_path, monkeypatch):
    root = tmp_path / "bad-deferred-plane"
    root.mkdir()
    (root / "placed.kicad_pcb").write_text("placed board", encoding="utf-8")

    def fake_cli(stage, args, cwd, timeout):
        Path(args[args.index("--output") + 1]).write_text("complete session", encoding="utf-8")
        Path(args[args.index("--routed-board") + 1]).write_text("routed board", encoding="utf-8")
        return {"status": "ok"}

    def add_plane(_board, output, _plan, **_kwargs):
        Path(output).write_text("unverified plane board", encoding="utf-8")
        return {"status": "ok"}

    def import_session(_board, _ses, output, **_kwargs):
        Path(output).write_text("should stay private", encoding="utf-8")
        invalid = _verified_plane_refill()
        invalid["zones"][0]["island_count"] = 1
        return {"status": "ok", "zone_refill": invalid, "drc": {"board_sha256": "c" * 64}}

    monkeypatch.setattr(workflow, "_cli", fake_cli)
    monkeypatch.setattr("circuit_weaver.pcb_ground_plane.add_ground_plane", add_plane)
    monkeypatch.setattr("circuit_weaver.pcb_layout.import_routing_session", import_session)

    with pytest.raises(workflow.WorkflowError, match="Routed planes contain unverified fill"):
        workflow._execute_stage(
            root,
            "route",
            {
                "ground_plane_after_route": True,
                "ground_plane": {"net": "GND", "layers": ["In1.Cu"]},
            },
            [],
        )
    assert not (root / "routed.kicad_pcb").exists()
    assert not (root / "routed.ses").exists()
