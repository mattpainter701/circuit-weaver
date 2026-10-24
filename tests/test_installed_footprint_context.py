"""Bounded identity and mutation checks for KiCad's installed footprint assets."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from circuit_weaver.drc_runner import run_drc
from circuit_weaver.evidence import EvidenceLedger
from circuit_weaver.kicad_project_context import stage_installed_footprint_context


def _fake_install(root: Path, major: int = 9) -> tuple[Path, Path, Path]:
    install = root / "KiCad" / f"{major}.0"
    cli = install / "bin" / "kicad-cli"
    cli.parent.mkdir(parents=True)
    stock_root = install / "share" / "kicad" / "footprints"
    library = stock_root / "Stock.pretty"
    library.mkdir(parents=True)
    (library / "Quoted.kicad_mod").write_bytes(b"quoted stock footprint")
    (library / "Bare.kicad_mod").write_bytes(b"unquoted stock footprint")
    (library / "Unused.kicad_mod").write_bytes(b"unreferenced stock footprint")
    table = install / "share" / "kicad" / "template" / "fp-lib-table"
    table.parent.mkdir(parents=True)
    table.write_text(
        f'(fp_lib_table (lib (name "Stock") (type "KiCad") (uri "${{KICAD{major}_FOOTPRINT_DIR}}/Stock.pretty")))\n',
        encoding="utf-8",
    )
    return cli, table, stock_root


def _empty_report(command: list[str]) -> None:
    report = Path(command[command.index("--output") + 1])
    report.write_text(
        json.dumps(
            {
                "kicad_version": "9.0.2",
                "violations": [],
                "unconnected_items": [],
                "schematic_parity": [],
            }
        ),
        encoding="utf-8",
    )


def test_stock_identity_hashes_quoted_and_unquoted_references(tmp_path: Path) -> None:
    cli, _table, _stock_root = _fake_install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()

    result = stage_installed_footprint_context(
        cli,
        9,
        stage,
        board_bytes=b'(kicad_pcb (footprint "Stock:Quoted") (footprint Stock:Bare))',
        project_root=project,
    )

    identity = result["asset_identity"]
    assert result["status"] == "staged"
    assert identity["status"] == "verified"
    assert identity["file_count"] == 2
    assert {(item["library"], item["footprint"]) for item in identity["files"]} == {
        ("Stock", "Quoted"),
        ("Stock", "Bare"),
    }
    assert all(len(item["sha256"]) == 64 for item in identity["files"])
    effective = Path(result["effective_root"]) / "Stock.pretty"
    assert (effective / "Quoted.kicad_mod").read_bytes() == b"quoted stock footprint"
    assert (effective / "Bare.kicad_mod").read_bytes() == b"unquoted stock footprint"
    assert not (effective / "Unused.kicad_mod").exists()


def test_unknown_legacy_module_footprints_remain_unverified(tmp_path: Path) -> None:
    cli, _table, _stock_root = _fake_install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()

    result = stage_installed_footprint_context(
        cli,
        9,
        stage,
        board_bytes=b'(kicad_pcb (module "Legacy_Footprint"))',
        project_root=project,
    )

    assert result["status"] == "staged"
    assert result["asset_identity"]["status"] == "unverified"
    assert "legacy module" in result["asset_identity"]["reason"]


def test_project_nickname_override_is_not_mistaken_for_stock_identity(tmp_path: Path) -> None:
    cli, _table, _stock_root = _fake_install(tmp_path)
    project = tmp_path / "project"
    local_library = project / "libraries" / "Stock.pretty"
    local_library.mkdir(parents=True)
    (local_library / "Quoted.kicad_mod").write_bytes(b"project-owned footprint")
    (project / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "Stock") (type "KiCad") (uri "${KIPRJMOD}/libraries/Stock.pretty")))\n',
        encoding="utf-8",
    )
    stage = tmp_path / "stage"
    stage.mkdir()

    result = stage_installed_footprint_context(
        cli,
        9,
        stage,
        board_bytes=b'(kicad_pcb (footprint "Stock:Quoted"))',
        project_root=project,
    )

    assert result["status"] == "staged"
    assert result["asset_identity"]["status"] == "verified"
    assert result["asset_identity"]["file_count"] == 0
    assert result["asset_identity"]["_paths"] == []


def test_selected_major_version_mismatch_fails_closed(tmp_path: Path) -> None:
    cli, _table, _stock_root = _fake_install(tmp_path, major=9)
    project = tmp_path / "project"
    project.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()

    result = stage_installed_footprint_context(
        cli,
        10,
        stage,
        board_bytes=b'(kicad_pcb (footprint "Stock:Quoted"))',
        project_root=project,
    )

    assert result["status"] == "invalid"
    assert "selected KiCad major version" in result["reason"]


@pytest.mark.parametrize("mutation", ["stock_table", "stock_asset", "staged_table"])
def test_drc_rejects_installed_footprint_context_mutation_during_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    cli, stock_table, stock_root = _fake_install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    board = project / "board.kicad_pcb"
    board.write_bytes(b'(kicad_pcb (footprint "Stock:Quoted"))')
    monkeypatch.setattr("circuit_weaver.drc_runner._kicad_cli_path", lambda: cli)

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="9.0.2\n", stderr="")
        if mutation == "stock_table":
            stock_table.write_text("(fp_lib_table)", encoding="utf-8")
        elif mutation == "stock_asset":
            (stock_root / "Stock.pretty" / "Quoted.kicad_mod").write_bytes(b"mutated footprint")
        else:
            staged_table = Path(kwargs["env"]["KICAD_CONFIG_HOME"]) / "9.0" / "fp-lib-table"  # type: ignore[index]
            staged_table.write_text("(fp_lib_table)", encoding="utf-8")
        _empty_report(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_drc(board, evidence_ledger=EvidenceLedger(), project_context=project)

    assert result.status == "failed"
    assert "changed during DRC" in result.failure_reason
    assert board.read_bytes() == b'(kicad_pcb (footprint "Stock:Quoted"))'


def test_explicit_missing_cli_override_does_not_fall_back_to_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    board = project / "board.kicad_pcb"
    board.write_bytes(b"board bytes")
    monkeypatch.setenv("CIRCUIT_WEAVER_KICAD_CLI", str(tmp_path / "missing" / "kicad-cli"))
    monkeypatch.setattr(shutil, "which", lambda _name: str(tmp_path / "unrelated" / "kicad-cli"))

    result = run_drc(board, evidence_ledger=EvidenceLedger(), project_context=project)

    assert result.status == "skipped"
    assert result.failure_reason == "KiCad CLI not available"


def test_drc_rejects_report_from_a_different_major_version(tmp_path, monkeypatch):
    cli, _table, _stock_root = _fake_install(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    board = project / "board.kicad_pcb"
    board.write_bytes(b'(kicad_pcb (footprint "Stock:Quoted"))')
    monkeypatch.setattr("circuit_weaver.drc_runner._kicad_cli_path", lambda: cli)

    def fake_run(command, **kwargs):
        if command[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="9.0.2\n", stderr="")
        _empty_report(command)
        report_path = Path(command[command.index("--output") + 1])
        report = json.loads(report_path.read_text())
        report["kicad_version"] = "10.0.4"
        report_path.write_text(json.dumps(report))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_drc(board, evidence_ledger=EvidenceLedger(), project_context=project)
    assert result.status == "failed"
    assert "version output did not match" in result.failure_reason
