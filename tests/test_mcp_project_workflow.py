"""MCP wrappers for the durable project workflow API."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from circuit_weaver import mcp_server


def test_workflow_requires_explicit_absolute_paths() -> None:
    for operation in ("start_project", "open_project", "workflow_status", "cancel_workflow"):
        result = json.loads(mcp_server._workflow_tool(operation, "relative/project"))
        assert result["status"] == "error"
        assert result["error"]["code"] == "invalid_workflow_arguments"


def test_start_project_parses_spec_and_suppresses_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from circuit_weaver import project_workflow

    observed: dict[str, object] = {}

    def start(path: Path, spec: dict, *, name: str | None = None) -> dict:
        print("progress")
        observed.update(path=path, spec=spec, name=name)
        return {"ok": True, "status": "started"}

    monkeypatch.setattr(project_workflow, "start_project", start)
    result = json.loads(
        mcp_server._workflow_tool(
            "start_project",
            str(tmp_path / "project"),
            spec_json='{"project":"demo"}',
            name="demo",
        )
    )
    assert result == {"ok": True, "status": "started"}
    assert observed["spec"] == {"project": "demo"}
    assert observed["name"] == "demo"
    assert capsys.readouterr().out == ""


def test_stage_arguments_are_strict_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = json.loads(
        mcp_server._workflow_tool(
            "plan_workflow",
            str(tmp_path),
            stages_json='{"generate":true}',
        )
    )
    assert result["status"] == "error"
    assert "JSON array" in result["error"]["message"]

    result = json.loads(
        mcp_server._workflow_tool(
            "run_workflow",
            str(tmp_path),
            stages_json='["generate", 1]',
        )
    )
    assert result["status"] == "error"
    assert "only stage names" in result["error"]["message"]

    result = json.loads(
        mcp_server._workflow_tool(
            "start_project",
            str(tmp_path / "nan"),
            spec_json='{"value":NaN}',
        )
    )
    assert result["status"] == "error"
    assert "non-standard JSON constant" in result["error"]["message"]


def test_run_and_resume_call_shared_workflow_functions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from circuit_weaver import project_workflow

    calls: list[tuple[str, dict]] = []

    def run(
        target: Path, *, stages: list[str] | None = None, options: dict | None = None, resume: bool = False
    ) -> dict:
        calls.append(("run", {"target": target, "stages": stages, "options": options, "resume": resume}))
        return {"ok": True, "status": "complete", "completed_stages": stages}

    def resume_workflow(target: Path, *, options: dict | None = None) -> dict:
        calls.append(("resume", {"target": target, "options": options}))
        return {"ok": True, "status": "planned"}

    monkeypatch.setattr(project_workflow, "run_workflow", run)
    monkeypatch.setattr(project_workflow, "resume_workflow", resume_workflow)
    result = json.loads(
        mcp_server._workflow_tool(
            "run_workflow",
            str(tmp_path),
            stages_json='["validate", "generate"]',
            options_json='{"execute":true}',
            resume=True,
        )
    )
    assert result["completed_stages"] == ["validate", "generate"]
    assert calls[0][1]["resume"] is True
    assert calls[0][1]["options"] == {"execute": True}
    assert (
        json.loads(mcp_server._workflow_tool("resume_workflow", str(tmp_path), options_json='{"execute":false}'))[
            "status"
        ]
        == "planned"
    )
    assert calls[1][1]["options"] == {"execute": False}


def test_mcp_app_exposes_workflow_tools_and_keeps_legacy_tools() -> None:
    app = mcp_server._create_app()
    names = {tool.name for tool in app._tool_manager.list_tools()}
    assert {
        "validate_design",
        "generate_artifacts",
        "discover_projects",
        "research_component",
        "manufacturing_readiness",
        "runtime_info",
        "library_access",
        "start_project",
        "open_project",
        "plan_workflow",
        "run_workflow",
        "workflow_status",
        "resume_workflow",
        "cancel_workflow",
    } <= names


def test_mcp_failed_workflow_status_preserves_json_error_envelope() -> None:
    payload = json.dumps(
        {"ok": False, "status": "failed", "error": {"code": "stage_failed", "message": "build failed"}}
    )
    result = mcp_server._as_mcp_tool_result(payload)
    assert result.isError is True
    assert len(result.content) == 1
    assert json.loads(result.content[0].text) == json.loads(payload)


def test_mcp_cancellation_remains_a_valid_workflow_result() -> None:
    payload = json.dumps({"ok": False, "status": "cancelled", "completed": ["validate"]})
    assert mcp_server._as_mcp_tool_result(payload) == payload


def test_live_mcp_error_content_remains_machine_readable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    from circuit_weaver import project_workflow

    expected = {
        "ok": False,
        "status": "failed",
        "error": {"code": "native_drc_failed", "message": "board has physical blockers"},
    }
    monkeypatch.setattr(project_workflow, "resume_workflow", lambda *_args, **_kwargs: expected)

    async def exercise() -> None:
        app = mcp_server._create_app()
        async with create_connected_server_and_client_session(app) as session:
            response = await session.call_tool("resume_workflow", {"target": str(tmp_path)})
            assert response.isError is True
            text_blocks = [block.text for block in response.content if hasattr(block, "text")]
            assert len(text_blocks) == 1
            assert json.loads(text_blocks[0]) == expected

    asyncio.run(exercise())


def test_mcp_run_allows_concurrent_status_and_boundary_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    from circuit_weaver import project_workflow
    from circuit_weaver.project_state import ProjectState, save_project_state

    root = tmp_path / "concurrent-project"
    root.mkdir()
    (root / "design.yaml").write_text("project: concurrent\nblocks: []\n", encoding="utf-8")
    save_project_state(root, ProjectState(name="concurrent", kind="design"))
    stage_started = threading.Event()
    allow_stage_to_finish = threading.Event()
    executed_stages: list[str] = []

    def slow_stage(project_root: Path, stage: str, _options: dict, _prior: list[str]) -> tuple[dict, list[str]]:
        executed_stages.append(stage)
        if stage == "validate":
            stage_started.set()
            if not allow_stage_to_finish.wait(timeout=5):
                raise TimeoutError("test stage was not released")
        return {"status": "ok"}, []

    monkeypatch.setattr(project_workflow, "_execute_stage", slow_stage)
    original_status = project_workflow.workflow_status
    original_cancel = project_workflow.cancel_workflow

    def noisy_status(target: Path) -> dict:
        print("status progress must stay off MCP stdout")
        return original_status(target)

    def noisy_cancel(target: Path) -> dict:
        print("cancel progress must stay off MCP stdout")
        return original_cancel(target)

    monkeypatch.setattr(project_workflow, "workflow_status", noisy_status)
    monkeypatch.setattr(project_workflow, "cancel_workflow", noisy_cancel)

    async def exercise() -> None:
        app = mcp_server._create_app()
        async with create_connected_server_and_client_session(app) as session:
            run_task = asyncio.create_task(
                session.call_tool(
                    "run_workflow",
                    {"target": str(root), "stages_json": '["validate", "generate"]'},
                )
            )
            assert await asyncio.to_thread(stage_started.wait, 3), "workflow did not enter the slow stage"

            status_response = await asyncio.wait_for(
                session.call_tool("workflow_status", {"target": str(root)}),
                timeout=2,
            )
            assert status_response.isError is False
            status = json.loads(status_response.content[0].text)
            assert status["status"] == "running"
            assert status["workflow"]["current_stage"] == "validate"

            cancel_response = await asyncio.wait_for(
                session.call_tool("cancel_workflow", {"target": str(root)}),
                timeout=2,
            )
            assert cancel_response.isError is False
            cancel = json.loads(cancel_response.content[0].text)
            assert cancel["status"] == "cancellation_requested"

            allow_stage_to_finish.set()
            run_response = await asyncio.wait_for(run_task, timeout=5)
            assert run_response.isError is False
            result = json.loads(run_response.content[0].text)
            assert result["status"] == "cancelled"
            assert executed_stages == ["validate"]
            assert capsys.readouterr().out == ""

    try:
        asyncio.run(exercise())
    finally:
        allow_stage_to_finish.set()
