"""Explicit model requests must not silently succeed without an assembly image."""

import json
import sys

import pytest

from circuit_weaver import dispatcher, pcb_model_preview, pcb_preview


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
