from __future__ import annotations

import argparse
import json

import pytest

from circuit_weaver import board_review, mcp_server, workflow_cli


def _inventory():
    return {"status": "ok", "rectangular_outline": True,
            "bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
            "footprints": [{"ref": "U1", "x": 5, "y": 5, "rotation": 0,
                            "courtyard_width": 10, "courtyard_height": 10}],
            "tracks": [], "vias": [], "track_count": 0, "zone_count": 0}


def test_cli_python_mcp_share_read_only_board_review(tmp_path, monkeypatch):
    source = tmp_path / "board.kicad_pcb"
    source.write_text("native board fixture")
    settings = source.with_suffix(".kicad_pro")
    settings.write_text("settings")
    calls = []

    def inspect(operation, **options):
        calls.append((operation, options))
        return _inventory()

    monkeypatch.setattr(board_review, "run_kicad", inspect)
    parser = argparse.ArgumentParser()
    workflow_cli.add_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["review-pcb", str(source), "--json"])
    cli = workflow_cli.dispatch(args)
    python = board_review.review_pcb(source)
    mcp = json.loads(mcp_server._board_review_tool(str(source)))
    assert cli == python == mcp
    assert python["status"] == "verified"
    assert python["routing"]["status"] == "not_requested"
    assert python["electrical_or_manufacturing_approval"] is False
    assert source.read_text() == "native board fixture" and settings.read_text() == "settings"
    assert not (tmp_path / ".circuit-weaver").exists()
    assert all(not options["include_route_geometry"] for _, options in calls)


def test_empty_routing_request_cannot_be_a_verified_contract(tmp_path, monkeypatch):
    source = tmp_path / "board.kicad_pcb"
    source.write_text("native board fixture")
    monkeypatch.setattr(board_review, "run_kicad", lambda *a, **kw: _inventory())
    result = board_review.review_pcb(source, {"routing": {}})
    assert result["status"] == "unverified"
    assert result["routing"]["status"] == "unverified"


def test_native_errors_and_changed_source_never_verify(tmp_path, monkeypatch):
    source = tmp_path / "board.kicad_pcb"
    source.write_text("before")
    monkeypatch.setattr(board_review, "run_kicad", lambda *a, **kw: {"status": "error", "message": "missing tool"})
    assert board_review.review_pcb(source)["status"] == "unverified"

    def changed(*args, **kwargs):
        source.write_text("after")
        return _inventory()

    monkeypatch.setattr(board_review, "run_kicad", changed)
    with pytest.raises(ValueError, match="changed"):
        board_review.review_pcb(source)


def test_new_settings_file_during_inspection_invalidates_context(tmp_path, monkeypatch):
    source = tmp_path / "board.kicad_pcb"
    source.write_text("before")

    def changed(*args, **kwargs):
        source.with_suffix(".kicad_pro").write_text("new context")
        return _inventory()

    monkeypatch.setattr(board_review, "run_kicad", changed)
    with pytest.raises(ValueError, match="settings changed"):
        board_review.review_pcb(source)


@pytest.mark.parametrize("document", [
    '{"placement":{},"placement":{}}', '{"placement":{"cell_size_mm":NaN}}', "[]",
])
def test_review_json_rejects_ambiguous_or_nonfinite_input(document):
    with pytest.raises(ValueError):
        board_review.parse_review_plan(document)


def test_mcp_rejects_relative_board_path_and_registers_review():
    result = json.loads(mcp_server._board_review_tool("relative.kicad_pcb"))
    assert result["status"] == "error"
    assert mcp_server._as_mcp_tool_result(json.dumps(result)).isError
    names = {tool.name for tool in mcp_server._create_app()._tool_manager.list_tools()}
    assert "review_board" in names
