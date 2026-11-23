"""Read-only liveness reporting for persisted workflow runs."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from circuit_weaver import project_workflow as workflow
from circuit_weaver.project_state import load_project_state, project_state_path, save_project_state


def _running_project(tmp_path: Path) -> Path:
    created = workflow.start_project(tmp_path / "board", {"project": "Board", "digital": []})
    assert created["ok"] is True
    root = Path(created["project_dir"])
    state = load_project_state(root)
    assert state is not None
    state.workflow["runner"] = {
        "version": 1,
        "status": "running",
        "run_id": "run-123",
        "current_stage": "route",
        "stages": {"route": {"status": "running", "checkpoint": "keep-me"}},
        "cancel_requested": False,
    }
    save_project_state(root, state)
    return root


def test_open_project_reports_active_lock_and_preserves_durable_runner(tmp_path: Path) -> None:
    root = _running_project(tmp_path)

    with workflow._RunLock(root) as held_lock:
        manifest_before = project_state_path(root).read_bytes()
        # Read through the owning handle: Windows byte-range locks can prevent
        # another handle from reading the locked byte.
        held_lock.handle.seek(0)
        lock_before = held_lock.handle.read()
        status = workflow.workflow_status(root)
        assert status["ok"] is True
        assert status["status"] == "running"
        assert status["workflow_activity"] == {"state": "active", "basis": "advisory_lock"}
        assert status["workflow"]["status"] == "running"
        assert status["workflow"]["stages"]["route"]["checkpoint"] == "keep-me"
        assert project_state_path(root).read_bytes() == manifest_before
        held_lock.handle.seek(0)
        assert held_lock.handle.read() == lock_before


@pytest.mark.parametrize("metadata", ["not-json", '{"pid": 999999999, "host": "another-host"}'])
def test_open_project_reports_interrupted_without_trusting_lock_metadata(tmp_path: Path, metadata: str) -> None:
    root = _running_project(tmp_path)
    lock_path = root / ".circuit-weaver" / "workflow.lock"
    lock_path.write_text(metadata, encoding="utf-8")
    manifest_before = project_state_path(root).read_bytes()
    lock_before = lock_path.read_bytes()

    status = workflow.open_project(root)

    assert status["ok"] is True
    assert status["status"] == "interrupted"
    assert status["workflow_activity"] == {"state": "interrupted", "basis": "advisory_lock"}
    assert status["workflow"]["status"] == "interrupted"
    assert status["workflow"]["persisted_status"] == "running"
    assert status["workflow"]["stages"]["route"]["checkpoint"] == "keep-me"
    assert project_state_path(root).read_bytes() == manifest_before
    assert lock_path.read_bytes() == lock_before


def test_open_project_reports_interrupted_when_lock_file_is_missing(tmp_path: Path) -> None:
    root = _running_project(tmp_path)

    status = workflow.workflow_status(root)

    assert status["status"] == "interrupted"
    assert status["workflow_activity"]["state"] == "interrupted"
    assert not (root / ".circuit-weaver" / "workflow.lock").exists()


def test_unprobeable_lock_is_unknown_and_does_not_mark_run_interrupted(tmp_path: Path, monkeypatch) -> None:
    root = _running_project(tmp_path)
    real_open = Path.open

    def denied_open(path: Path, *args, **kwargs):
        if path.name == "workflow.lock":
            raise PermissionError("lock access denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)

    status = workflow.open_project(root)

    assert status["ok"] is True
    assert status["status"] == "running"
    assert status["workflow_activity"]["state"] == "unknown"
    assert status["workflow_activity"]["diagnostic"].startswith("PermissionError:")
    assert status["workflow"]["status"] == "running"
    assert status["workflow"]["persisted_status"] == "running"


def test_nonrunning_project_status_keeps_existing_status_contract(tmp_path: Path) -> None:
    root = _running_project(tmp_path)
    state = load_project_state(root)
    assert state is not None
    state.workflow["runner"]["status"] = "completed"
    save_project_state(root, state)

    status = workflow.open_project(root)

    assert status["status"] == "completed"
    assert status["workflow"]["status"] == "completed"
    assert status["workflow_activity"] == {"state": "idle", "basis": "persisted_status"}


def test_interrupted_status_view_preserves_resume_path(tmp_path: Path, monkeypatch) -> None:
    created = workflow.start_project(tmp_path / "resumable", {"project": "Resumable", "digital": []})
    assert created["ok"] is True
    root = Path(created["project_dir"])
    plan = workflow.plan_workflow(root, stages=["validate"])
    assert plan["ok"] is True
    state = load_project_state(root)
    assert state is not None
    state.workflow["runner"].update({"status": "running", "run_id": "resume-me"})
    save_project_state(root, state)
    monkeypatch.setattr(workflow, "_execute_stage", lambda *_args: ({"status": "ok"}, []))

    status = workflow.workflow_status(root)
    resumed = workflow.resume_workflow(root)

    assert status["status"] == "interrupted"
    assert status["workflow"]["persisted_status"] == "running"
    assert resumed["ok"] is True
    assert resumed["status"] == "completed"


def test_process_death_releases_lock_and_reports_interrupted(tmp_path: Path) -> None:
    root = _running_project(tmp_path)
    manifest_before = project_state_path(root).read_bytes()
    import_root = Path(workflow.__file__).parent.parent
    child_code = (
        "import pathlib, sys, time\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from circuit_weaver import project_workflow as workflow\n"
        "lock = workflow._RunLock(pathlib.Path(sys.argv[1]))\n"
        "lock.__enter__()\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(root), str(import_root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        active_status = None
        while time.monotonic() < deadline:
            if child.poll() is not None:
                _stdout, stderr = child.communicate()
                raise AssertionError(f"lock holder exited before acquiring lock: {stderr}")
            candidate = workflow.workflow_status(root)
            if candidate.get("workflow_activity", {}).get("state") == "active":
                active_status = candidate
                break
            time.sleep(0.025)
        assert active_status is not None, "child process never acquired the workflow lock"
        assert active_status["status"] == "running"
        child.kill()
        child.communicate(timeout=5)

        status_after_death = workflow.workflow_status(root)

        assert status_after_death["ok"] is True
        assert status_after_death["status"] == "interrupted"
        assert status_after_death["workflow_activity"]["state"] == "interrupted"
        assert status_after_death["workflow"]["persisted_status"] == "running"
        assert project_state_path(root).read_bytes() == manifest_before
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
