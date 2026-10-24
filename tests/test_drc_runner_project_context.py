"""DRC context and exact validation-byte regressions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from circuit_weaver.drc_runner import run_drc
from circuit_weaver.evidence import EvidenceLedger


def test_drc_uses_same_basename_isolated_board_with_declared_local_library(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    board = project / "board.kicad_pcb"
    library = project / "libraries" / "Local.pretty"
    library.mkdir(parents=True)
    board.write_bytes(b"exact board bytes")
    board.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
    footprint = library / "A.kicad_mod"
    footprint.write_bytes(b"footprint bytes")
    table = project / "fp-lib-table"
    table.write_text(
        '(fp_lib_table (lib (name "Local") (type "KiCad") '
        '(uri "${KIPRJMOD}/libraries/Local.pretty")))\n',
        encoding="utf-8",
    )
    original_hashes = {
        path: path.read_bytes()
        for path in (board, board.with_suffix(".kicad_pro"), footprint, table)
    }
    fake_cli = tmp_path / "kicad-cli"
    monkeypatch.setattr("circuit_weaver.drc_runner._kicad_cli_path", lambda: fake_cli)
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        staged_board = Path(command[-1])
        staged_root = staged_board.parent
        observed["board_path"] = staged_board
        observed["stdin"] = kwargs.get("stdin")
        assert staged_board.name == board.name
        assert staged_board.read_bytes() == board.read_bytes()
        assert (staged_root / "fp-lib-table").read_bytes() == table.read_bytes()
        assert (staged_root / "libraries" / "Local.pretty" / "A.kicad_mod").read_bytes() == b"footprint bytes"
        report_path = Path(command[command.index("--output") + 1])
        report_path.write_text(json.dumps({
            "kicad_version": "test",
            "violations": [],
            "unconnected_items": [],
            "schematic_parity": [],
        }), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_drc(board, evidence_ledger=EvidenceLedger(), project_context=board)

    assert result.status == "ok"
    assert result.board == str(board)
    assert result.board_sha256
    assert observed["stdin"] is subprocess.DEVNULL
    assert Path(observed["board_path"]) != board
    assert {path: path.read_bytes() for path in original_hashes} == original_hashes


@pytest.mark.parametrize("mutation", ["board", "settings", "new_table", "new_asset"])
def test_drc_rejects_native_validation_or_source_context_drift(tmp_path, monkeypatch, mutation):
    project = tmp_path / "project"
    project.mkdir()
    board = project / "board.kicad_pcb"
    board.write_bytes(b"exact board bytes")
    settings = board.with_suffix(".kicad_pro")
    settings.write_bytes(b"authored rules")
    if mutation == "new_asset":
        library = project / "Local.pretty"
        library.mkdir()
        (library / "A.kicad_mod").write_bytes(b"footprint")
        (project / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/Local.pretty")))'
        )
    monkeypatch.setattr("circuit_weaver.drc_runner._kicad_cli_path", lambda: Path("kicad-cli"))

    def fake_run(command, **kwargs):
        staged = Path(command[-1])
        if mutation == "board":
            staged.write_bytes(b"changed validation geometry")
        elif mutation == "settings":
            staged.with_suffix(".kicad_pro").write_bytes(b"changed validation rules")
        elif mutation == "new_table":
            (project / "fp-lib-table").write_text("(fp_lib_table)")
        else:
            (library / "B.kicad_mod").write_bytes(b"new footprint")
        Path(command[command.index("--output") + 1]).write_text(json.dumps({
            "kicad_version": "test", "violations": [], "unconnected_items": [], "schematic_parity": [],
        }))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_drc(board, evidence_ledger=EvidenceLedger(), project_context=project)
    assert result.status == "failed"
    assert "mutated" in result.failure_reason or "changed" in result.failure_reason
    assert board.read_bytes() == b"exact board bytes"
    assert settings.read_bytes() == b"authored rules"
