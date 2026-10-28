"""The CLI and MCP must call the same hash-bound local import API."""

import argparse
import json

from circuit_weaver import mcp_server, workflow_cli


def test_cli_and_mcp_bundle_plans_agree_and_apply(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "local.kicad_sym"
    source.write_text("(kicad_symbol_lib (version 20231120))")
    manifest = {
        "version": 1,
        "assets": [
            {
                "kind": "symbol_library",
                "source": str(source),
                "nickname": "Local",
                "provenance": "self-authored fixture",
                "license": "CC0-1.0",
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    parser = argparse.ArgumentParser()
    workflow_cli.add_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["library-bundle", "plan", str(path), "--project-root", str(project), "--json"])
    cli = workflow_cli.dispatch(args)
    mcp = json.loads(mcp_server._library_bundle_tool("plan", str(project), json.dumps(manifest)))
    assert cli == mcp
    assert not (project / "sym-lib-table").exists()
    applied = json.loads(mcp_server._library_bundle_tool("apply", str(project), json.dumps(mcp)))
    assert applied["status"] == "applied"
    assert (project / "libraries/symbols/Local.kicad_sym").read_bytes() == source.read_bytes()


def test_mcp_bundle_rejects_relative_paths_malformed_json_and_tamper(tmp_path):
    for operation, root, document in [
        ("plan", ".", "{}"),
        ("plan", str(tmp_path), "[]"),
        ("apply", str(tmp_path), '{"status":"planned"}'),
    ]:
        result = json.loads(mcp_server._library_bundle_tool(operation, root, document))
        assert result["status"] == "error"
        assert result["error"]["code"] == "library_bundle_failed"
        assert mcp_server._as_mcp_tool_result(json.dumps(result)).isError


def test_adoption_adapters_share_api(tmp_path, monkeypatch):
    from circuit_weaver import project_workflow

    calls = []

    def adopt(target, *, name=None):
        calls.append((str(target), name))
        return {"ok": True, "status": "adopted"}

    monkeypatch.setattr(project_workflow, "adopt_project", adopt)
    parser = argparse.ArgumentParser()
    workflow_cli.add_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["adopt", str(tmp_path), "--name", "native", "--json"])
    assert workflow_cli.dispatch(args)["status"] == "adopted"
    assert json.loads(mcp_server._workflow_tool("adopt_project", str(tmp_path), name="native"))["ok"]
    assert calls == [(str(tmp_path), "native"), (str(tmp_path), "native")]


def test_mcp_app_exposes_local_import_and_adoption_tools():
    names = {tool.name for tool in mcp_server._create_app()._tool_manager.list_tools()}
    assert {"adopt_project", "plan_library_bundle", "apply_library_bundle"} <= names
