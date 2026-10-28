from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path

from circuit_weaver import mcp_server, workflow_cli


def test_library_access_cli_dispatch_uses_resolved_project_root(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def audit(query, *, project_root):
        captured["query"] = query
        captured["project_root"] = project_root
        return {"query": query, "read_only": True}

    from circuit_weaver import library_access

    monkeypatch.setattr(library_access, "audit_library_access", audit)
    result = workflow_cli.dispatch(Namespace(command="library-access", query="ADF4351", project_root=str(tmp_path)))

    assert captured == {"query": "ADF4351", "project_root": tmp_path.resolve()}
    assert result == {"query": "ADF4351", "read_only": True}


def test_live_mcp_library_access_is_read_only_and_requires_absolute_project_path(tmp_path: Path, monkeypatch) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    from circuit_weaver import library_access

    called: list[Path | None] = []

    def audit(query, *, project_root):
        called.append(project_root)
        return {
            "query": query,
            "read_only": True,
            "curated_support": {"available": False},
            "spice_model_binding": {"available": False},
        }

    monkeypatch.setattr(library_access, "audit_library_access", audit)

    async def exercise() -> None:
        app = mcp_server._create_app()
        async with create_connected_server_and_client_session(app) as session:
            invalid = await session.call_tool("library_access", {"query": "ADF4351", "project_root": "."})
            assert invalid.isError is True
            assert json.loads(invalid.content[0].text)["error"]["code"] == "library_access_failed"
            assert called == []

            response = await session.call_tool(
                "library_access", {"query": "ADF4351", "project_root": str(tmp_path.resolve())}
            )
            assert response.isError is False
            payload = json.loads(response.content[0].text)
            assert payload["read_only"] is True
            assert payload["curated_support"]["available"] is False
            assert payload["spice_model_binding"]["available"] is False
            assert called == [tmp_path.resolve()]

    asyncio.run(exercise())
