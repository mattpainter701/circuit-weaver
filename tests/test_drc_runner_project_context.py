"""DRC context and exact validation-byte regressions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from circuit_weaver.drc_runner import run_drc
from circuit_weaver.evidence import EvidenceLedger


def _fake_install(tmp_path: Path, major: int = 9, nickname: str = "Stock") -> tuple[Path, Path, Path]:
    install = tmp_path / "KiCad" / f"{major}.0"
    cli = install / "bin" / "kicad-cli"
    cli.parent.mkdir(parents=True)
    root = install / "share" / "kicad" / "footprints"
    (root / "Stock.pretty").mkdir(parents=True)
    table = install / "share" / "kicad" / "template" / "fp-lib-table"
    table.parent.mkdir(parents=True)
    table.write_text(
        f'(fp_lib_table (lib (name "{nickname}") (type "KiCad") (uri "${{KICAD{major}_FOOTPRINT_DIR}}/Stock.pretty")))',
        encoding="utf-8",
    )
    return cli, table, root


def test_drc_uses_same_basename_isolated_board_with_declared_local_library(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    board = project / "board.kicad_pcb"
    library = project / "libraries" / "Local.pretty"
    library.mkdir(parents=True)
    board.write_bytes(b'(kicad_pcb (footprint "Local:A"))')
    board.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
    footprint = library / "A.kicad_mod"
    footprint.write_bytes(b"footprint bytes")
    table = project / "fp-lib-table"
    table.write_text(
        '(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/libraries/Local.pretty")))\n',
        encoding="utf-8",
    )
    original_hashes = {path: path.read_bytes() for path in (board, board.with_suffix(".kicad_pro"), footprint, table)}
    fake_cli, _stock_table, stock_root = _fake_install(tmp_path, nickname="Local")
    monkeypatch.setattr("circuit_weaver.drc_runner._kicad_cli_path", lambda: fake_cli)
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        if command[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="9.0.2\n", stderr="")
        staged_board = Path(command[-1])
        staged_root = staged_board.parent
        observed["board_path"] = staged_board
        observed["stdin"] = kwargs.get("stdin")
        assert staged_board.name == board.name
        assert staged_board.read_bytes() == board.read_bytes()
        assert (staged_root / "fp-lib-table").read_bytes() == table.read_bytes()
        assert (staged_root / "libraries" / "Local.pretty" / "A.kicad_mod").read_bytes() == b"footprint bytes"
        config_root = Path(kwargs["env"]["KICAD_CONFIG_HOME"])
        staged_global_table = config_root / "9.0" / "fp-lib-table"
        staged_global_bytes = staged_global_table.read_bytes()
        assert b'"Local"' not in staged_global_bytes
        assert Path(kwargs["env"]["KICAD9_FOOTPRINT_DIR"]) != stock_root
        report_path = Path(command[command.index("--output") + 1])
        report_path.write_text(
            json.dumps(
                {
                    "kicad_version": "test",
                    "violations": [],
                    "unconnected_items": [],
                    "schematic_parity": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_drc(board, evidence_ledger=EvidenceLedger(), project_context=board)

    assert result.status == "ok"
    assert result.board == str(board)
    assert result.board_sha256
    assert result.library_context["status"] == "staged"
    assert result.library_context["sha256"]
    assert result.library_context["generated_table_sha256"]
    assert result.library_context["stock_root"] == str(stock_root)
    assert result.library_context["effective_root"] != result.library_context["stock_root"]
    assert result.library_context["asset_identity"]["status"] == "verified"
    assert result.library_context["asset_identity"]["file_count"] == 0
    assert observed["stdin"] is subprocess.DEVNULL
    assert Path(observed["board_path"]) != board
    assert {path: path.read_bytes() for path in original_hashes} == original_hashes


def test_drc_keeps_invalid_stock_context_visible_and_does_not_borrow_user_config(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    board = project / "board.kicad_pcb"
    board.write_bytes(b"exact board bytes")
    fake_cli, _stock_table, _stock_root = _fake_install(tmp_path, major=9)
    monkeypatch.setattr("circuit_weaver.drc_runner._kicad_cli_path", lambda: fake_cli)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setenv("KICAD10_FOOTPRINT_DIR", str(tmp_path / "unverified-user-footprints"))
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        if command[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="10.0.6\n", stderr="")
        observed["config"] = kwargs["env"]["KICAD_CONFIG_HOME"]
        assert "KICAD10_FOOTPRINT_DIR" not in kwargs["env"]
        report = Path(command[command.index("--output") + 1])
        report.write_text(
            json.dumps(
                {
                    "kicad_version": "10.0.6",
                    "violations": [
                        {
                            "type": "lib_footprint_issues",
                            "severity": "warning",
                            "description": "The current configuration does not include the footprint library 'Stock'",
                        }
                    ],
                    "unconnected_items": [],
                    "schematic_parity": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_drc(board, evidence_ledger=EvidenceLedger(), project_context=project)

    assert result.status == "ok"
    assert result.library_context["status"] == "invalid"
    assert result.findings[0].code == "CW-DRC-012"
    assert Path(str(observed["config"])) != tmp_path / "user-config"


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
    fake_cli, _stock_table, _stock_root = _fake_install(tmp_path)
    monkeypatch.setattr("circuit_weaver.drc_runner._kicad_cli_path", lambda: fake_cli)

    def fake_run(command, **kwargs):
        if command[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="9.0.2\n", stderr="")
        staged = Path(command[-1])
        if mutation == "board":
            staged.write_bytes(b"changed validation geometry")
        elif mutation == "settings":
            staged.with_suffix(".kicad_pro").write_bytes(b"changed validation rules")
        elif mutation == "new_table":
            (project / "fp-lib-table").write_text("(fp_lib_table)")
        else:
            (library / "B.kicad_mod").write_bytes(b"new footprint")
        Path(command[command.index("--output") + 1]).write_text(
            json.dumps(
                {
                    "kicad_version": "test",
                    "violations": [],
                    "unconnected_items": [],
                    "schematic_parity": [],
                }
            )
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_drc(board, evidence_ledger=EvidenceLedger(), project_context=project)
    assert result.status == "failed"
    assert "mutated" in result.failure_reason or "changed" in result.failure_reason
    assert board.read_bytes() == b"exact board bytes"
    assert settings.read_bytes() == b"authored rules"
