"""Explicit model requests must not silently succeed without an assembly image."""

import json
import sys

import pytest

from circuit_weaver import dispatcher, pcb_model_preview, pcb_preview


@pytest.fixture(autouse=True)
def renderer_dispatch_only(monkeypatch):
    # These tests mock rendering and deliberately create no native files.
    # Checkpoint and artifact validation are exercised by workflow tests.
    from circuit_weaver import workflow_cli

    monkeypatch.setattr(workflow_cli, "native_stage", lambda stage, source, output, options, action: action())


def test_requested_unavailable_model_render_fails_cli_with_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "circuit-weaver", "preview-pcb", "board.kicad_pcb", "-o", "preview.svg", "--render-3d",
    ])
    monkeypatch.setattr(pcb_preview, "render_board_preview", lambda *_args, **_kwargs: {
        "status": "ok", "prototype": True, "output_path": "preview.svg",
    })
    monkeypatch.setattr(pcb_model_preview, "render_board_model", lambda *_args, **_kwargs: {
        "status": "unavailable", "reason": "native_3d_render_requires_kicad_9_or_newer",
    })

    with pytest.raises(SystemExit) as exit_info:
        dispatcher.main()

    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["model_preview"]["status"] == "unavailable"
    assert "native_3d_render_requires_kicad_9_or_newer" in payload["message"]


def test_default_appearance_keeps_legacy_layer_preview(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sys, "argv", [
        "circuit-weaver", "preview-pcb", "board.kicad_pcb", "-o", "preview.svg",
    ])
    monkeypatch.setattr(pcb_preview, "render_board_preview", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"status": "ok", "appearance": "layers"}
    ))

    with pytest.raises(SystemExit) as exit_info:
        dispatcher.main()

    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["appearance"] == "layers"
    assert calls and calls[0][1]["side"] == "front"


def test_assembly_appearance_propagates_renderer_options(monkeypatch, capsys):
    from circuit_weaver import pcb_assembly_preview

    calls = []
    monkeypatch.setattr(sys, "argv", [
        "circuit-weaver", "preview-pcb", "board.kicad_pcb", "-o", "preview.svg",
        "--appearance", "assembly", "--side", "back", "--timeout", "45",
        "--overwrite", "--kicad-cli-path", "custom-kicad-cli", "--render-3d",
    ])
    monkeypatch.setattr(pcb_assembly_preview, "render_board_assembly", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"status": "ok", "appearance": "assembly"}
    ))
    monkeypatch.setattr(pcb_model_preview, "render_board_model", lambda *_args, **_kwargs: (
        pytest.fail("assembly appearance must render its embedded model only once")
    ))

    with pytest.raises(SystemExit) as exit_info:
        dispatcher.main()

    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["appearance"] == "assembly"
    assert calls == [(("board.kicad_pcb", "preview.svg"), {
        "kicad_cli": "custom-kicad-cli", "side": "back", "timeout_seconds": 45.0, "overwrite": True,
    })]
