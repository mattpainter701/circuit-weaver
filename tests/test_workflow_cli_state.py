"""Directory entrypoints and native-stage state must agree across surfaces."""

import json
import subprocess
import sys

from circuit_weaver.project_state import (
    ProjectState,
    file_sha256,
    get_project_state_summary,
    save_project_state,
)
from circuit_weaver.workflow_cli import native_stage


def _checkpoint(root, *, status="completed", stage="preview"):
    root.mkdir(exist_ok=True)
    board = root / "routed.kicad_pcb"
    board.write_text("verified board", encoding="utf-8")
    state = ProjectState(name="Demo", status="generated", current_phase="placement_review")
    state.workflow["runner"] = {
        "status": status,
        "current_stage": stage if status == "running" else None,
        "last_completed_stage": stage,
        "updated_at": "2030-01-01T00:00:00+00:00",
        "stages": {
            stage: {
                "status": "completed",
                "inputs": [],
                "outputs": [
                    {"path": str(board), "exists": True, "size": board.stat().st_size, "sha256": file_sha256(board)}
                ],
            }
        },
    }
    save_project_state(root, state)
    return board, state


def test_native_checkpoint_supersedes_old_placement_review(tmp_path):
    _checkpoint(tmp_path)
    summary = get_project_state_summary(tmp_path)
    assert summary["status"] == "workflow_completed"
    assert summary["current_phase"] == "preview_completed"


def test_changed_completed_board_is_not_reported_complete(tmp_path):
    board, _ = _checkpoint(tmp_path)
    board.write_text("modified board", encoding="utf-8")
    summary = get_project_state_summary(tmp_path)
    assert summary["status"] == "workflow_stale"
    assert summary["current_phase"] == "checkpoint_invalidated"


def test_failed_stage_cannot_retain_prior_success_phase(tmp_path):
    _, state = _checkpoint(tmp_path, stage="route")
    state.workflow["runner"].update(status="failed", current_stage="route", last_error={"message": "native DRC"})
    save_project_state(tmp_path, state)
    summary = get_project_state_summary(tmp_path)
    assert summary["status"] == "workflow_failed"
    assert summary["current_phase"] == "route_failed"
    assert summary["last_error"] == "native DRC"


def test_cancellation_request_preserves_current_stage(tmp_path):
    _, state = _checkpoint(tmp_path, status="running", stage="route")
    state.workflow["runner"]["cancel_requested"] = True
    save_project_state(tmp_path, state)
    summary = get_project_state_summary(tmp_path)
    assert summary["status"] == "cancellation_requested"
    assert summary["current_phase"] == "route"


def test_later_generation_supersedes_old_runner_completion(tmp_path):
    _, state = _checkpoint(tmp_path)
    state.workflow["generate"] = {"updated_at": "2031-01-01T00:00:00+00:00"}
    save_project_state(tmp_path, state)
    assert get_project_state_summary(tmp_path)["current_phase"] == "placement_review"


def test_standalone_native_stage_records_output_project_without_mutating_source_folder(tmp_path):
    source_dir = tmp_path / "vendor"
    source_dir.mkdir()
    source = source_dir / "input.kicad_sch"
    source.write_text("source", encoding="utf-8")
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    output = output_dir / "board.kicad_pcb"

    def action():
        output.write_text("valid output", encoding="utf-8")
        return {"status": "ok", "output_path": str(output)}

    assert native_stage("build", source, output, {}, action)["status"] == "ok"
    assert not (source_dir / ".circuit-weaver").exists()
    assert source.read_text() == "source"
    assert get_project_state_summary(output_dir)["current_phase"] == "build_completed"


def test_child_command_leaves_checkpoint_to_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("CIRCUIT_WEAVER_WORKFLOW_CHILD", "1")
    assert (
        native_stage(
            "preview",
            tmp_path / "board",
            tmp_path / "view",
            {},
            lambda: {"status": "error", "message": "native failure"},
        )["status"]
        == "error"
    )
    assert not (tmp_path / ".circuit-weaver").exists()


def test_separate_native_output_does_not_mutate_unrelated_ancestor_manifest(tmp_path):
    parent = tmp_path / "aggregator"
    parent.mkdir()
    parent_state = save_project_state(parent, ProjectState(name="Unrelated existing design"))
    parent_before = parent_state.read_bytes()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "input.kicad_pcb"
    source.write_text("native input")
    output_dir = parent / "review"
    output_dir.mkdir()
    output = output_dir / "board.svg"

    def action():
        output.write_text("<svg/>")
        return {"status": "ok"}

    assert native_stage("preview", source, output, {}, action)["status"] == "ok"
    assert parent_state.read_bytes() == parent_before
    assert (output_dir / ".circuit-weaver/project.json").is_file()


def test_native_success_without_output_is_not_reported_success(tmp_path):
    source = tmp_path / "input.kicad_pcb"
    source.write_text("source", encoding="utf-8")
    value = native_stage("preview", source, tmp_path / "missing.svg", {}, lambda: {"status": "ok"})
    assert value["status"] == "error"
    assert value["workflow"]["error"]["code"] == "stage_output_missing"


def test_native_route_checkpoint_includes_session_and_board(tmp_path):
    source = tmp_path / "input.kicad_pcb"
    source.write_text("source", encoding="utf-8")
    board = tmp_path / "routed.kicad_pcb"
    session = tmp_path / "routed.ses"

    def action():
        board.write_text("routed", encoding="utf-8")
        session.write_text("session", encoding="utf-8")
        return {"status": "ok"}

    assert native_stage(
        "route", source, board, {"routed_board": str(board), "output": str(session)}, action
    )["status"] == "ok"
    session.write_text("externally changed", encoding="utf-8")
    summary = get_project_state_summary(tmp_path)
    assert summary["status"] == "workflow_stale"
    assert summary["current_phase"] == "checkpoint_invalidated"


def test_cli_start_open_and_resume_plan_from_circuit_directory(tmp_path):
    def cli(*args):
        result = subprocess.run(
            [sys.executable, "-m", "circuit_weaver", *args], cwd=tmp_path, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    started = cli("start", "board", "--name", "Example", "--json")
    assert started["ok"]
    opened = cli("open", "board", "--json")
    assert opened["ok"]
    resumed = cli("resume", "board", "--json")
    assert resumed["resumable"]
    assert not (tmp_path / "board" / "output").exists()


def test_bare_terminal_lists_projects_and_menu_without_running_stages(tmp_path):
    from circuit_weaver.project_workflow import start_project

    assert start_project(tmp_path / "my_board", name="MyBoard")["ok"]
    result = subprocess.run(
        [sys.executable, "-m", "circuit_weaver"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "MyBoard" in result.stdout
    assert "Open an existing circuit" in result.stdout
    assert not (tmp_path / "my_board" / "output").exists()


def test_workflow_json_keeps_engine_progress_off_stdout(monkeypatch, capsys):
    from circuit_weaver import dispatcher, workflow_cli

    def noisy_dispatch(args):
        print("engine progress")
        return {"ok": True, "status": "completed"}

    monkeypatch.setattr(workflow_cli, "dispatch", noisy_dispatch)
    monkeypatch.setattr(sys, "argv", ["circuit-weaver", "workflow", "status", ".", "--json"])
    import pytest

    with pytest.raises(SystemExit) as exited:
        dispatcher.main()
    assert exited.value.code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True, "status": "completed"}
    assert "engine progress" in captured.err
