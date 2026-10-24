from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from circuit_weaver import kicad_bridge


def test_native_probe_never_inherits_rpc_protocol_input(monkeypatch):
    monkeypatch.setattr(kicad_bridge.shutil, "which", lambda value: str(value))

    def probe(command, **options):
        # Inheriting an open RPC input stream can block the native import.
        if options.get("stdin") != subprocess.DEVNULL:
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return SimpleNamespace(returncode=0, stdout="CW_PCBNEW_READY\n", stderr="")

    monkeypatch.setattr(kicad_bridge.subprocess, "run", probe)
    assert kicad_bridge.find_kicad_python("native-python") == "native-python"


def test_native_worker_receives_json_and_only_copies_can_change(tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("board source")
    settings = board.with_suffix(".kicad_pro")
    settings.write_text("source settings")
    monkeypatch.setattr(kicad_bridge, "find_kicad_python", lambda path: "native-python")

    def worker(command, **options):
        request = json.loads(options["input"])
        assert request["operation"] == "inspect" and request["include_route_geometry"] is True
        copied = Path(request["board"])
        assert copied != board and copied.read_text() == "board source"
        assert copied.with_suffix(".kicad_pro").read_text() == "source settings"
        copied.write_text("native side effect")
        copied.with_suffix(".kicad_pro").write_text("native settings side effect")
        return SimpleNamespace(returncode=0, stdout='diagnostic\nCW_KICAD_RESULT={"status":"ok"}\n', stderr="")

    monkeypatch.setattr(kicad_bridge.subprocess, "run", worker)
    assert kicad_bridge.run_kicad("inspect", board=board, include_route_geometry=True)["status"] == "ok"
    assert board.read_text() == "board source" and settings.read_text() == "source settings"
