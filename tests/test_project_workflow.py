"""Focused contracts for durable project workflow orchestration."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from circuit_weaver import project_workflow as workflow
from circuit_weaver.project_state import file_sha256, load_project_state, project_state_path


def _project(tmp_path: Path) -> Path:
    result = workflow.start_project(tmp_path / "board", {"project": "Board", "digital": []})
    assert result["ok"] is True
    return Path(result["project_dir"])


def _fake_stage(root: Path, stage: str, options: dict, prior: list[str]):
    if stage == "generate":
        output = root / "output" / "design.kicad_sch"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("generated", encoding="utf-8")
        return {"status": "ok"}, [str(output)]
    return {"status": "ok"}, [str(root / "design.yaml")]


def test_start_project_refuses_existing_files_and_keeps_cwd_paths_portable(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep.txt").write_text("preserve", encoding="utf-8")
    result = workflow.start_project("existing", {"project": "Changed"})
    assert result["ok"] is False
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "preserve"

    created = workflow.start_project("relative-board", {"project": "Relative"})
    assert created["ok"] is True
    assert Path(created["project_dir"]).is_absolute()
    assert (
        json.loads((Path(created["project_dir"]) / ".circuit-weaver" / "project.json").read_text())["name"]
        == "Relative"
    )


def test_open_native_project_advertises_read_only_adoption(tmp_path: Path):
    root = tmp_path / "native"
    root.mkdir()
    schematic = root / "controller.kicad_sch"
    schematic.write_text("(kicad_sch)", encoding="utf-8")

    opened = workflow.open_project(root)

    assert opened["ok"] is True
    assert opened["summary"]["read_only"] is True
    assert opened["next_actions"] == ["adopt_project"]
    assert not project_state_path(root).exists()


def test_adopt_project_records_hashes_without_rewriting_sources_and_is_idempotent(tmp_path: Path):
    root = tmp_path / "legacy-project"
    root.mkdir()
    spec = root / "design.yaml"
    board = root / "prototype.kicad_pcb"
    spec.write_text("project: Legacy\ndigital: []\n", encoding="utf-8")
    board.write_text("(kicad_pcb)", encoding="utf-8")
    original = {path: path.read_bytes() for path in (spec, board)}

    adopted = workflow.adopt_project(root, name="Adopted Legacy")

    assert adopted["ok"] is True
    assert adopted["adopted"] is True
    state = load_project_state(root)
    assert state is not None
    assert state.name == "Adopted Legacy"
    assert state.kind == "design"
    assert state.current_phase == "adopted"
    assert {item["path"] for item in state.sources} == {"design.yaml", "prototype.kicad_pcb"}
    assert {item["path"]: item["sha256"] for item in state.sources} == {
        "design.yaml": file_sha256(spec),
        "prototype.kicad_pcb": file_sha256(board),
    }
    assert {path: path.read_bytes() for path in original} == original
    before = state.to_dict()

    repeated = workflow.adopt_project(root, name="Must Not Rename")

    assert repeated["ok"] is True
    assert repeated["adopted"] is False
    assert load_project_state(root).to_dict() == before


def test_adopt_project_rejects_unsupported_ambiguous_and_nested_state_targets(tmp_path: Path):
    unsupported = tmp_path / "empty"
    unsupported.mkdir()
    assert workflow.adopt_project(unsupported)["error"]["code"] == "unsupported_target"

    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    (ambiguous / "first.yaml").write_text("project: First", encoding="utf-8")
    (ambiguous / "second.yaml").write_text("project: Second", encoding="utf-8")
    assert workflow.adopt_project(ambiguous)["error"]["code"] == "ambiguous_spec"

    outer = _project(tmp_path)
    nested = outer / "legacy-child"
    nested.mkdir()
    child_board = nested / "board.kicad_pcb"
    child_board.write_text("(kicad_pcb)", encoding="utf-8")
    child_adoption = workflow.adopt_project(nested)
    assert child_adoption["ok"] is True
    assert project_state_path(nested).is_file()

    parent = tmp_path / "contains-stateful-child"
    parent.mkdir()
    child = parent / "managed"
    assert workflow.start_project(child, {"project": "Managed"})["ok"]
    (parent / "board.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    result = workflow.adopt_project(parent)
    assert result["ok"] is False
    assert result["error"]["code"] == "ambiguous_project"


def test_adopt_project_prefers_explicit_child_boundary_over_unclaimed_parent_marker(tmp_path: Path):
    root = tmp_path / "native-root"
    root.mkdir()
    (root / "board.kicad_pro").write_text("{}", encoding="utf-8")
    nested = root / "hardware"
    nested.mkdir()
    (nested / "controller.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")

    result = workflow.adopt_project(nested)

    assert result["ok"] is True
    assert result["project_dir"] == str(nested.resolve())
    assert project_state_path(nested).is_file()


def test_adopt_project_ignores_unrelated_ancestor_manifest_for_direct_board_project(tmp_path: Path):
    from circuit_weaver.project_state import ProjectState, save_project_state

    aggregator = tmp_path / "automation_validation_results"
    aggregator.mkdir()
    aggregator_state = ProjectState(name="Unrelated Aggregator", kind="native")
    save_project_state(aggregator, aggregator_state)
    (aggregator / "routed-noopt.kicad_pro").write_text("{}", encoding="utf-8")
    project = aggregator / "python-project"
    project.mkdir()
    (project / "prototype.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")

    opened = workflow.open_project(project)
    assert opened["ok"] is True
    assert opened["project_dir"] == str(project.resolve())
    assert opened["summary"]["read_only"] is True
    assert not project_state_path(project).exists()

    adopted = workflow.adopt_project(project)

    assert adopted["ok"] is True
    assert adopted["project_dir"] == str(project.resolve())
    assert project_state_path(project).is_file()
    assert load_project_state(aggregator).to_dict() == aggregator_state.to_dict()


def test_adopt_project_rejects_child_recorded_by_parent_state(tmp_path: Path):
    from circuit_weaver.project_state import ProjectState, save_project_state

    parent = tmp_path / "recorded-owner"
    child = parent / "board"
    child.mkdir(parents=True)
    board = child / "controller.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    parent.mkdir(exist_ok=True)
    owner = ProjectState(
        name="Recorded Owner",
        kind="native",
        sources=[{"path": str(board.resolve()), "sha256": file_sha256(board)}],
    )
    save_project_state(parent, owner)

    result = workflow.adopt_project(child)

    assert result["ok"] is False
    assert result["error"]["code"] == "state_conflict"
    assert result["error"]["choices"] == [str(parent.resolve())]
    assert not project_state_path(child).exists()


def test_adopt_project_serializes_with_workflow_lock_and_rechecks_state(tmp_path: Path, monkeypatch):
    root = tmp_path / "native"
    root.mkdir()
    (root / "board.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")

    with workflow._RunLock(root):
        busy = workflow.adopt_project(root)
    assert busy["ok"] is False
    assert busy["error"]["code"] == "workflow_busy"
    assert not project_state_path(root).exists()

    class RacingLock:
        def __init__(self, locked_root):
            assert locked_root == root

        def __enter__(self):
            from circuit_weaver.project_state import save_project_state

            save_project_state(root, workflow.ProjectState(name="Created While Waiting", kind="native"))
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(workflow, "_RunLock", RacingLock)
    raced = workflow.adopt_project(root, name="Must Not Replace")

    assert raced["ok"] is True
    assert raced["adopted"] is False
    assert load_project_state(root).name == "Created While Waiting"


def test_resume_rejects_changed_source_inputs(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(workflow, "_execute_stage", lambda r, s, o, p: _fake_stage(r, s, o, p))
    result = workflow.run_workflow(root, stages=["validate"])
    assert result["ok"] is True
    spec = root / "design.yaml"
    spec.write_text(spec.read_text(encoding="utf-8") + "\ncompany: Changed\n", encoding="utf-8")
    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is False
    assert resumed["error"]["code"] == "needs_replan"


def test_later_kicad_sidecars_do_not_invalidate_completed_validation(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(workflow, "_execute_stage", lambda r, s, o, p: ({"status": "ok"}, []))
    assert workflow.run_workflow(root, stages=["validate"])["ok"] is True
    output = root / "output"
    output.mkdir()
    (output / "generated.kicad_pro").write_text("settings", encoding="utf-8")
    (output / "generated.kicad_dru").write_text("rules", encoding="utf-8")
    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is True


def test_append_only_generation_logs_are_not_later_stage_inputs(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    seen_inputs: dict[str, list[Path]] = {}

    def fake_stage(project, stage, options, prior):
        if stage == "generate":
            output = project / "output" / "design.kicad_sch"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("generated", encoding="utf-8")
            design_log = project / "output" / "design.log"
            design_log.write_text("generation log", encoding="utf-8")
            cw_log = project / "output" / "circuit-weaver.log"
            cw_log.write_text("runner log", encoding="utf-8")
            return {"status": "ok"}, [str(output), str(design_log), str(cw_log)]
        seen_inputs[stage] = [Path(path) for path in workflow._stage_inputs(project, stage, prior)]
        board = project / "prototype.kicad_pcb"
        board.write_text("built board", encoding="utf-8")
        return {"status": "ok"}, [str(board)]

    monkeypatch.setattr(workflow, "_execute_stage", fake_stage)
    first = workflow.run_workflow(root, stages=["generate", "build"])
    assert first["ok"] is True
    build_inputs = seen_inputs["build"]
    assert all(path.name not in {"design.log", "circuit-weaver.log"} for path in build_inputs)

    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is True


def test_generate_checkpoint_tracks_project_tables_symbols_and_footprints(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    tracked = [
        root / "fp-lib-table",
        root / "sym-lib-table",
        root / "Custom.pretty" / "part.kicad_mod",
        root / "Custom.kicad_sym",
    ]
    tracked[0].write_text("footprint table", encoding="utf-8")
    tracked[1].write_text("symbol table", encoding="utf-8")
    tracked[2].parent.mkdir()
    tracked[2].write_text("footprint", encoding="utf-8")
    tracked[3].write_text("symbol", encoding="utf-8")
    expected = {path.resolve() for path in tracked}
    assert expected.issubset({Path(path).resolve() for path in workflow._stage_inputs(root, "generate", [])})

    generated_cad = []

    def fake_generate(project, stage, options, prior):
        output_dir = project / "output"
        (output_dir / "Generated.pretty").mkdir(parents=True, exist_ok=True)
        generated = [
            output_dir / "generated.kicad_sch",
            output_dir / "sym-lib-table",
            output_dir / "fp-lib-table",
            output_dir / "Generated.kicad_sym",
            output_dir / "Generated.pretty" / "part.kicad_mod",
        ]
        for path in generated:
            path.write_text("generated", encoding="utf-8")
        generated_cad.extend(generated[1:])
        return {"status": "ok"}, [str(path) for path in generated]

    monkeypatch.setattr(workflow, "_execute_stage", fake_generate)
    assert workflow.run_workflow(root, stages=["generate"])["ok"] is True
    generate_inputs = {Path(path).resolve() for path in workflow._stage_inputs(root, "generate", [])}
    assert not ({path.resolve() for path in generated_cad} & generate_inputs)
    assert workflow.resume_workflow(root)["ok"] is True
    tracked[0].write_text("updated footprint table", encoding="utf-8")
    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is False
    assert resumed["error"]["code"] == "needs_replan"


def test_preview_checkpoint_tracks_contained_models_and_ignores_receipts(tmp_path: Path, monkeypatch):
    root = tmp_path / "native-preview"
    root.mkdir()
    board = root / "prototype.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    model = root / "libraries" / "models" / "package.step"
    model.parent.mkdir(parents=True)
    model.write_text("step model", encoding="utf-8")
    receipt_model = root / "libraries" / ".circuit-weaver-receipts" / "ignored.wrl"
    receipt_model.parent.mkdir(parents=True)
    receipt_model.write_text("receipt", encoding="utf-8")

    stage_inputs = {Path(path).resolve() for path in workflow._stage_inputs(root, "preview", [])}
    assert model.resolve() in stage_inputs
    assert receipt_model.resolve() not in stage_inputs

    def fake_preview(project, stage, options, prior):
        output = project / "preview.svg"
        output.write_text("preview", encoding="utf-8")
        return {"status": "ok"}, [str(output)]

    monkeypatch.setattr(workflow, "_execute_stage", fake_preview)
    assert workflow.run_workflow(root, stages=["preview"])["ok"] is True
    model.write_text("changed step model", encoding="utf-8")
    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is False
    assert resumed["error"]["code"] == "needs_replan"


def test_native_only_build_can_be_planned_without_a_yaml_spec(tmp_path: Path):
    root = tmp_path / "native-board"
    root.mkdir()
    schematic = root / "controller.kicad_sch"
    schematic.write_text("(kicad_sch (version 20250114))", encoding="utf-8")
    result = workflow.plan_workflow(
        root,
        stages=["build"],
        options={"board_width_mm": 70, "board_height_mm": 45},
    )
    assert result["ok"] is True
    assert [item["path"] for item in result["plan"]["inputs"]] == [str(schematic.resolve())]


def test_build_passes_explicit_clearance_and_minimum_drill(tmp_path: Path, monkeypatch):
    root = tmp_path / "native-board"
    root.mkdir()
    (root / "controller.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    captured: list[str] = []
    wrapper_timeouts: list[int] = []

    def fake_cli(stage, args, cwd, timeout):
        wrapper_timeouts.append(timeout)
        captured.extend(args)
        output = Path(args[args.index("--output") + 1])
        output.write_text("(kicad_pcb)", encoding="utf-8")
        return {"status": "ok"}

    monkeypatch.setattr(workflow, "_cli", fake_cli)
    workflow._execute_stage(
        root,
        "build",
        {
            "board_width_mm": 50,
            "board_height_mm": 35,
            "clearance_mm": 0.127,
            "minimum_drill_mm": 0.25,
        },
        [],
    )

    assert captured[captured.index("--clearance") + 1] == "0.127"
    assert captured[captured.index("--minimum-drill") + 1] == "0.25"
    assert wrapper_timeouts == [900]


def test_build_rule_overrides_must_be_finite_and_positive(tmp_path: Path):
    root = _project(tmp_path)
    for options in (
        {"clearance_mm": 0},
        {"minimum_drill_mm": -0.1},
        {"clearance_mm": float("inf")},
        {"minimum_drill_mm": True},
    ):
        result = workflow.plan_workflow(root, stages=["validate"], options=options)
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_options"


def test_route_controls_are_validated_and_forwarded(tmp_path: Path, monkeypatch):
    root = tmp_path / "route-controls"
    root.mkdir()
    (root / "placed.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    for options in (
        {"route_max_passes": -1},
        {"route_max_passes": 10000},
        {"route_max_passes": True},
        {"route_optimization_threads": -1},
        {"route_optimization_threads": 1025},
        {"route_optimization_threads": False},
        {"route_attempts": 0},
        {"route_attempts": 33},
        {"route_effort": "unbounded"},
    ):
        result = workflow.plan_workflow(root, stages=["route"], options=options)
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_options"

    captured: list[str] = []

    def fake_cli(stage, args, cwd, timeout):
        captured.extend(args)
        Path(args[args.index("--output") + 1]).write_text("(session)", encoding="utf-8")
        Path(args[args.index("--routed-board") + 1]).write_text("(kicad_pcb)", encoding="utf-8")
        return {"status": "ok"}

    monkeypatch.setattr(workflow, "_cli", fake_cli)
    result = workflow._execute_stage(
        root,
        "route",
        {
            "route_max_passes": 250,
            "route_optimization_threads": 2,
            "route_attempts": 3,
            "route_effort": "high",
            "route_timeout": 17,
        },
        [],
    )

    for flag, value in (
        ("--max-passes", "250"),
        ("--optimization-threads", "2"),
        ("--attempts", "3"),
        ("--effort", "high"),
    ):
        assert captured[captured.index(flag) + 1] == value
    assert result[0]["status"] == "ok"


@pytest.mark.parametrize("requested", [17, 1800, 3600])
def test_route_parent_reserves_native_import_time_after_clamped_router_deadline(tmp_path: Path, monkeypatch, requested):
    root = tmp_path / "route-budget"
    root.mkdir()
    (root / "placed.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    observed = {}

    def fake_cli(stage, args, cwd, timeout):
        observed["router_seconds"] = float(args[args.index("--timeout") + 1])
        observed["parent_seconds"] = timeout
        # Model a router finishing at its deadline: import/DRC must still have
        # bounded time before the workflow can publish its native outputs.
        assert timeout > observed["router_seconds"]
        Path(args[args.index("--output") + 1]).write_text("(session)", encoding="utf-8")
        Path(args[args.index("--routed-board") + 1]).write_text("(kicad_pcb)", encoding="utf-8")
        return {"status": "ok"}

    monkeypatch.setattr(workflow, "_cli", fake_cli)
    result, outputs = workflow._execute_stage(root, "route", {"route_timeout": requested}, [])

    assert result["status"] == "ok"
    assert observed["router_seconds"] == min(requested, 1800)
    assert 0 < observed["parent_seconds"] - observed["router_seconds"] <= 600
    assert observed["parent_seconds"] <= 2400
    assert len(outputs) == 2


def test_resume_rejects_changed_checkpoint_artifact(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(workflow, "_execute_stage", lambda r, s, o, p: _fake_stage(r, s, o, p))
    assert workflow.run_workflow(root, stages=["generate"])["ok"] is True
    output = root / "output" / "design.kicad_sch"
    output.write_text("externally changed", encoding="utf-8")
    result = workflow.resume_workflow(root)
    assert result["ok"] is False
    assert result["error"]["code"] == "needs_replan"


def test_interrupted_generation_resumes_from_owned_partial_output(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    attempts = 0

    def crash_after_write(project, stage, options, prior):
        nonlocal attempts
        attempts += 1
        output = project / "output" / "partial.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("partial", encoding="utf-8")
        if attempts == 1:
            raise RuntimeError("simulated interruption")
        final = project / "output" / "complete.txt"
        final.write_text("complete", encoding="utf-8")
        return {"status": "ok"}, [str(output), str(final)]

    monkeypatch.setattr(workflow, "_execute_stage", crash_after_write)
    failed = workflow.run_workflow(root, stages=["generate"])
    assert failed["ok"] is False
    state = load_project_state(root)
    assert state.workflow["runner"]["status"] == "failed"
    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is True
    assert (root / "output" / "complete.txt").is_file()


def test_cancel_is_cooperative_at_stage_boundary(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def slow_stage(project, stage, options, prior):
        entered.set()
        release.wait(timeout=3)
        output = project / "output" / "done.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("done", encoding="utf-8")
        return {"status": "ok"}, [str(output)]

    monkeypatch.setattr(workflow, "_execute_stage", slow_stage)
    results: list[dict] = []
    thread = threading.Thread(
        target=lambda: results.append(workflow.run_workflow(root, stages=["generate"], options={}))
    )
    thread.start()
    assert entered.wait(timeout=2)
    assert workflow.cancel_workflow(root)["status"] == "cancellation_requested"
    release.set()
    thread.join(timeout=4)
    assert results and results[0]["status"] == "cancelled"
    assert load_project_state(root).workflow["runner"]["stages"]["generate"]["status"] == "completed"


def test_workflow_lock_rejects_a_second_owner(tmp_path: Path):
    root = _project(tmp_path)
    with workflow.workflow_lock(root):
        result = workflow.run_stage(root, "preview", lambda: "unused", inputs=[], outputs=[])
    assert result["ok"] is False
    assert result["error"]["code"] == "workflow_busy"


def test_native_cli_child_uses_owned_capture(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"status":"ok"}', stderr="")

    monkeypatch.setattr(workflow, "run_captured", fake_run)
    result = workflow._cli("build", ["build"], tmp_path, 30)

    assert result == {"status": "ok"}
    assert calls[0][1]["env"][workflow._CHILD_ENV] == "1"
    assert calls[0][1]["timeout"] == 30


def test_failed_workflow_checkpoint_preserves_structured_native_diagnostics(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    native_result = {"status": "error", "best_candidate": {"message": "physical clearance failed", "drc": ["gap"]}}

    def failed_stage(*args, **kwargs):
        raise workflow.WorkflowError("stage_failed", "placement rejected", stage_result=native_result)

    monkeypatch.setattr(workflow, "_execute_stage", failed_stage)
    result = workflow.run_workflow(root, stages=["validate"])
    assert result["ok"] is False
    checkpoint = load_project_state(root).workflow["runner"]["stages"]["validate"]
    assert checkpoint["status"] == "failed"
    assert checkpoint["result"] == native_result
    assert result["error"] == {"code": "stage_failed", "message": "placement rejected"}


def test_direct_native_failure_helper_retains_diagnostics_in_durable_checkpoint(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    native_result = {
        "status": "error", "message": "Imported routing failed KiCad DRC; board was not published",
        "drc": {"blocker_count": 2, "findings": [{"rule_id": "CW-DRC-002", "message": "Missing connection"}]},
        "diagnostics": ["native-detail"] * 5000,
    }

    def failed_stage(*_args, **_kwargs):
        workflow._require_ok(native_result, "ground_plane")

    monkeypatch.setattr(workflow, "_execute_stage", failed_stage)
    result = workflow.run_workflow(root, stages=["validate"])

    assert result["ok"] is False
    assert result["error"] == {"code": "ground_plane_failed", "message": native_result["message"]}
    assert len(json.dumps(result["error"])) < 200
    checkpoint = load_project_state(root).workflow["runner"]["stages"]["validate"]
    assert checkpoint["status"] == "failed"
    assert checkpoint["result"] == native_result
    assert not (root / "routed.kicad_pcb").exists()
    assert not (root / "routed.ses").exists()


def test_deferred_plane_import_failure_retains_full_native_payload_without_publication(tmp_path: Path, monkeypatch):
    root = tmp_path / "deferred-plane-failure"
    root.mkdir()
    (root / "placed.kicad_pcb").write_text("frozen placement", encoding="utf-8")
    native_result = {
        "status": "error", "message": "Imported routing failed KiCad DRC; board was not published",
        "drc": {"blocker_count": 1, "board_sha256": "a" * 64,
                "findings": [{"rule_id": "CW-DRC-002", "message": "Missing plane connection"}]},
    }

    def route(_stage, args, _cwd, _timeout):
        Path(args[args.index("--output") + 1]).write_text("complete session", encoding="utf-8")
        Path(args[args.index("--routed-board") + 1]).write_text("private routed board", encoding="utf-8")
        return {"status": "ok"}

    def add_plane(_board, output, _plan, **_kwargs):
        Path(output).write_text("private plane input", encoding="utf-8")
        return {"status": "ok"}

    monkeypatch.setattr(workflow, "_cli", route)
    monkeypatch.setattr("circuit_weaver.pcb_ground_plane.add_ground_plane", add_plane)
    monkeypatch.setattr("circuit_weaver.pcb_layout.import_routing_session", lambda *_args, **_kwargs: native_result)

    with pytest.raises(workflow.WorkflowError) as failure:
        workflow._execute_stage(root, "route", {
            "ground_plane_after_route": True,
            "ground_plane": {"net": "GND", "layers": ["In1.Cu"]},
        }, [])

    assert failure.value.code == "ground_plane_failed"
    assert failure.value.stage_result is native_result
    assert str(failure.value) == native_result["message"]
    assert not (root / "routed.kicad_pcb").exists()
    assert not (root / "routed.ses").exists()


def test_missing_native_output_helper_keeps_success_payload_for_failure_evidence(tmp_path: Path):
    native_result = {"status": "ok", "native_report": {"pad_identity_preserved": True}}

    with pytest.raises(workflow.WorkflowError) as failure:
        workflow._require_outputs("build", native_result, [tmp_path / "missing.kicad_pcb"])

    assert failure.value.code == "stage_output_missing"
    assert failure.value.stage_result is native_result
    assert not (tmp_path / "missing.kicad_pcb").exists()


def test_nonzero_native_cli_keeps_structured_error_ahead_of_large_inventory(tmp_path: Path, monkeypatch):
    inventory = ["model-entry"] * 5000
    payload = json.dumps(
        {
            "status": "error",
            "error": {"code": "preview_failed", "message": "model preview failed before inventory export"},
            "models": inventory,
        }
    )
    monkeypatch.setattr(
        workflow,
        "run_captured",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=payload, stderr=""),
    )

    try:
        workflow._cli("preview", ["preview-pcb"], tmp_path, 30)
    except workflow.WorkflowError as exc:
        assert exc.code == "preview_failed"
        assert "model preview failed before inventory export" in str(exc)
        assert len(str(exc)) < 200
        assert exc.stage_result["models"] == inventory
    else:
        raise AssertionError("nonzero native exit was treated as success")


def test_run_stage_uses_exact_root_and_ignores_nested_project(tmp_path: Path):
    parent = tmp_path / "native-output"
    parent.mkdir()
    source = parent / "input.kicad_sch"
    source.write_text("(kicad_sch)", encoding="utf-8")
    child = parent / "nested"
    assert workflow.start_project(child, {"project": "Nested"})["ok"]
    child_before = load_project_state(child).to_dict()
    output = parent / "output.kicad_pcb"

    result = workflow.run_stage(
        parent,
        "build",
        lambda: (output.write_text("(kicad_pcb)", encoding="utf-8") or {"status": "ok"}),
        inputs=[source],
        outputs=[output],
    )

    assert result["ok"] is True
    assert load_project_state(parent) is not None
    assert load_project_state(child).to_dict() == child_before


def test_run_stage_missing_input_fails_before_creating_manifest(tmp_path: Path):
    root = tmp_path / "exact-root"
    invoked = False

    def action():
        nonlocal invoked
        invoked = True
        return {"status": "ok"}

    result = workflow.run_stage(root, "build", action, inputs=["absent.kicad_sch"], outputs=[])

    assert result["ok"] is False
    assert result["error"]["code"] == "input_missing"
    assert invoked is False
    assert not (root / ".circuit-weaver" / "project.json").exists()


def test_run_stage_missing_output_records_failure_and_result(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "input.kicad_sch"
    source.write_text("(kicad_sch)", encoding="utf-8")

    result = workflow.run_stage(
        root,
        "build",
        lambda: {"status": "ok", "detail": "native action returned without artifact"},
        inputs=[source],
        outputs=["expected.kicad_pcb"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "stage_output_missing"
    assert result["result"]["detail"] == "native action returned without artifact"
    assert load_project_state(root).workflow["runner"]["stages"]["build"]["status"] == "failed"


def test_run_stage_resolves_relative_outputs_against_exact_root(tmp_path: Path):
    root = tmp_path / "different-from-cwd"
    root.mkdir()

    def action():
        (root / "board.kicad_pcb").write_text("board", encoding="utf-8")
        return {"status": "ok"}

    result = workflow.run_stage(root, "build", action, inputs=[], outputs=["board.kicad_pcb"])

    assert result["ok"] is True


def test_atomic_publish_second_link_failure_preserves_all_staged_files(tmp_path: Path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    board = attempt / "prototype.kicad_pcb"
    sidecar = attempt / "prototype.kicad_pro"
    board.write_bytes(b"board")
    sidecar.write_bytes(b"settings")
    real_link = workflow.os.link
    calls = 0

    def fail_second_link(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated destination race")
        return real_link(source, target)

    monkeypatch.setattr(workflow.os, "link", fail_second_link)
    try:
        workflow._publish_attempt(
            attempt,
            tmp_path,
            board,
            tmp_path / board.name,
            {},
        )
    except workflow.WorkflowError as exc:
        assert exc.code == "publish_failed"
    else:
        raise AssertionError("expected no-clobber publication to fail")
    assert board.read_bytes() == b"board"
    assert sidecar.read_bytes() == b"settings"
    assert not (tmp_path / board.name).exists()
    assert not (tmp_path / sidecar.name).exists()


def test_successful_publish_preserves_unstaged_native_evidence(tmp_path: Path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    board = attempt / "routed.kicad_pcb"
    evidence = attempt / "routing.dsn"
    board.write_bytes(b"board")
    evidence.write_bytes(b"router evidence")
    result = {"status": "ok", "dsn": str(evidence)}

    published = workflow._publish_attempt(attempt, tmp_path, board, tmp_path / board.name, result)

    assert published == [str(tmp_path / board.name)]
    assert (tmp_path / board.name).read_bytes() == b"board"
    assert evidence.read_bytes() == b"router evidence"
    assert result["dsn"] == str(evidence)
