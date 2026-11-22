"""Adversarial external-behavior checks for project workflow safety."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

from circuit_weaver import project_workflow as workflow
from circuit_weaver.project_state import ProjectState, load_project_state, project_state_path, save_project_state


def _project(root: Path, *, spec: bool = False, **files: str) -> ProjectState:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if spec:
        (root / "design.yaml").write_text("project: adversarial\nblocks: []\n", encoding="utf-8")
    state = ProjectState(name=root.name, kind="native" if not spec else "design")
    save_project_state(root, state)
    return state


def test_open_raw_native_project_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "native"
    root.mkdir()
    board = root / "prototype.kicad_pcb"
    board.write_text("(kicad_pcb (version 20250114))\n", encoding="utf-8")

    result = workflow.open_project(root)

    assert result["ok"] is True
    assert result["status"] == "open"
    assert result["summary"]["kind"] == "native"
    assert str(board.resolve()) in result["summary"]["files"]
    assert not project_state_path(root).exists()


def test_plan_while_runner_lock_is_held_preserves_active_run_and_cancel(tmp_path: Path) -> None:
    root = tmp_path / "project"
    state = _project(root, **{"placed.kicad_pcb": "(kicad_pcb)\n"})
    state.workflow["runner"] = {
        "version": 1,
        "status": "running",
        "current_stage": "route",
        "cancel_requested": True,
        "run_id": "active-run",
        "stages": {"place": {"status": "completed", "outputs": []}},
    }
    save_project_state(root, state)
    before = copy.deepcopy(load_project_state(root).workflow["runner"])

    with workflow._RunLock(root):
        result = workflow.plan_workflow(root, stages=["preview"])

    assert result["ok"] is False
    assert result["error"]["code"] == "workflow_busy"
    assert load_project_state(root).workflow["runner"] == before


def test_fresh_run_output_conflict_preserves_prior_checkpoints(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _project(root, spec=True)
    (root / "output").mkdir()
    (root / "output" / "kept.txt").write_text("prior artifact", encoding="utf-8")
    prior_plan = workflow.plan_workflow(root, stages=["validate"], options={"prior": True})["plan"]
    workflow.record_stage(
        root,
        "validate",
        inputs=[root / "design.yaml"],
        outputs=[],
        options={"prior": True},
        status="completed",
        run_id="prior-run",
    )
    state = load_project_state(root)
    state.workflow["runner"].update(status="completed", run_id="prior-run", plan=prior_plan)
    save_project_state(root, state)
    before = copy.deepcopy(load_project_state(root).workflow["runner"])

    result = workflow.run_workflow(root, stages=["generate"])

    assert result["ok"] is False
    assert result["error"]["code"] == "output_exists"
    assert load_project_state(root).workflow["runner"] == before


def test_zero_exit_non_json_native_output_is_rejected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        workflow,
        "run_captured",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="done", stderr=""),
    )

    try:
        workflow._cli("build", ["build-pcb"], tmp_path, 10)
    except workflow.WorkflowError as exc:
        assert exc.code == "invalid_stage_output"
    else:
        raise AssertionError("non-JSON native output was treated as success")


def test_missing_native_artifact_cannot_complete_build_stage(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "project"
    _project(root, spec=True, **{"output/root.kicad_sch": "(kicad_sch)\n"})
    monkeypatch.setattr(workflow, "_cli", lambda *_args, **_kwargs: {"status": "ok"})

    result = workflow.run_workflow(
        root,
        stages=["build"],
        options={"board_width_mm": 50, "board_height_mm": 40},
    )

    assert result["ok"] is False
    runner = load_project_state(root).workflow["runner"]
    assert runner["status"] == "failed"
    assert runner["stages"]["build"]["status"] == "failed"
    assert runner["stages"]["build"]["error"]["code"] == "stage_output_missing"
    assert not (root / "prototype.kicad_pcb").exists()


def test_input_change_during_stage_fails_checkpoint(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "project"
    _project(root, spec=True)

    def mutate_source(_root: Path, _stage: str, _options: dict, _prior: list[str]) -> tuple[dict, list[str]]:
        (root / "design.yaml").write_text("project: modified-during-stage\nblocks: []\n", encoding="utf-8")
        return {"status": "ok", "valid": True}, []

    monkeypatch.setattr(workflow, "_execute_stage", mutate_source)
    result = workflow.run_workflow(root, stages=["validate"])

    assert result["ok"] is False
    assert result["error"]["code"] == "input_changed"
    checkpoint = load_project_state(root).workflow["runner"]["stages"]["validate"]
    assert checkpoint["status"] == "failed"
    assert checkpoint["error"]["code"] == "input_changed"


def test_preview_after_route_uses_published_routed_board(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "native"
    _project(root, **{"placed.kicad_pcb": "(kicad_pcb (board placed))\n"})
    preview_sources: list[Path] = []

    def fake_cli(stage: str, args: list[str], _cwd: Path, _timeout: int) -> dict:
        if stage == "route":
            source = Path(args[1])
            output = Path(args[args.index("--output") + 1])
            routed_board = Path(args[args.index("--routed-board") + 1])
            assert source == root / "placed.kicad_pcb"
            output.write_text("(session)\n", encoding="utf-8")
            routed_board.write_text("(kicad_pcb (board routed))\n", encoding="utf-8")
            return {"status": "ok"}
        assert stage == "preview"
        preview_sources.append(Path(args[1]))
        output = Path(args[args.index("--output") + 1])
        output.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>\n', encoding="utf-8")
        return {"status": "ok"}

    monkeypatch.setattr(workflow, "_cli", fake_cli)
    result = workflow.run_workflow(root, stages=["route", "preview"])

    assert result["ok"] is True, result
    assert preview_sources == [root / "routed.kicad_pcb"]
    assert (root / "routed.kicad_pcb").is_file()
    assert (root / "preview.svg").is_file()


def test_resume_keeps_custom_saved_route_source_after_output_appears(tmp_path, monkeypatch):
    root = tmp_path / "custom-board-project"
    root.mkdir()
    source = root / "comparison_board.kicad_pcb"
    source.write_text("source", encoding="utf-8")

    calls = []

    def fake_stage(project, stage, _options, _prior):
        calls.append(stage)
        names = ("routed.kicad_pcb", "routed.ses") if stage == "route" else ("preview.svg",)
        outputs = []
        for name in names:
            output = project / name
            output.write_text(name, encoding="utf-8")
            outputs.append(str(output))
        return {"status": "ok"}, outputs

    monkeypatch.setattr(workflow, "_execute_stage", fake_stage)
    result = workflow.run_workflow(root, stages=["route", "preview"])
    assert result["ok"] is True
    route = load_project_state(root).workflow["runner"]["stages"]["route"]
    assert [Path(item["path"]) for item in route["inputs"] if item["path"].endswith(".kicad_pcb")] == [
        source.resolve()
    ]

    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is True
    assert calls == ["route", "preview"]


def test_resume_requires_saved_route_source_even_if_routed_board_exists(tmp_path):
    root = tmp_path / "missing-custom-source"
    root.mkdir()
    source = root / "custom.kicad_pcb"
    source.write_text("source", encoding="utf-8")
    assert workflow.plan_workflow(root, stages=["route"])["ok"] is True
    source.unlink()
    (root / "routed.kicad_pcb").write_text("stale output", encoding="utf-8")

    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is False
    assert resumed["error"]["code"] == "needs_replan"


def test_saved_native_project_and_dru_sidecars_invalidate_route_resume(tmp_path, monkeypatch):
    root = tmp_path / "sidecar-resume"
    root.mkdir()
    board = root / "custom.kicad_pcb"
    board.write_text("board", encoding="utf-8")
    sidecars = [board.with_suffix(suffix) for suffix in (".kicad_pro", ".kicad_dru")]
    for sidecar in sidecars:
        sidecar.write_text("initial", encoding="utf-8")

    def fake_stage(project, stage, _options, _prior):
        names = ("routed.kicad_pcb", "routed.ses") if stage == "route" else ("preview.svg",)
        outputs = []
        for name in names:
            output = project / name
            output.write_text(name, encoding="utf-8")
            outputs.append(str(output))
        return {"status": "ok"}, outputs

    monkeypatch.setattr(workflow, "_execute_stage", fake_stage)
    assert workflow.run_workflow(root, stages=["route", "preview"])["ok"] is True
    route_inputs = load_project_state(root).workflow["runner"]["stages"]["route"]["inputs"]
    input_paths = {Path(item["path"]) for item in route_inputs}
    assert set(sidecars) <= input_paths
    sidecars[1].write_text("changed", encoding="utf-8")

    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is False
    assert resumed["error"]["code"] == "needs_replan"


def test_generated_route_reports_are_not_selected_as_design_specs(tmp_path):
    root = tmp_path / "report-only"
    root.mkdir()
    (root / "ground-plane.json").write_text("{}", encoding="utf-8")
    (root / "route-quality.json").write_text("{}", encoding="utf-8")

    assert workflow._has_spec(root) is False
    try:
        workflow._spec_path(root)
    except workflow.WorkflowError as exc:
        assert exc.code == "spec_missing"
    else:
        raise AssertionError("generated PCB reports were selected as a design spec")
