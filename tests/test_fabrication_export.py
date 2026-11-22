from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from circuit_weaver.drc_runner import _kicad_cli_path
from circuit_weaver.fabrication_export import GerberExportError, export_gerbers_archive
from circuit_weaver.kicad_bridge import find_kicad_python


def _manifest(layers: int = 2, *, omit_copper: int | None = None) -> dict:
    attributes = [
        {"Path": "board-F_Cu.gtl", "FileFunction": "Copper,L1,Top"},
        {"Path": f"board-L{layers}_Cu.gbl", "FileFunction": f"Copper,L{layers},Bot"},
        {"Path": "board-Edge_Cuts.gm1", "FileFunction": "Profile"},
    ]
    for index in range(2, layers):
        attributes.append(
            {"Path": f"board-In{index - 1}_Cu.g{index}", "FileFunction": f"Copper,L{index},Inr"}
        )
    if omit_copper is not None:
        attributes = [row for row in attributes if row["FileFunction"] != f"Copper,L{omit_copper},Inr"]
    return {"GeneralSpecs": {"LayerNumber": layers}, "FilesAttributes": attributes}


def _fake_cli(expected: dict, calls: list[tuple[str, Path, bytes]]):
    def run(command, **kwargs):
        kind = command[3]
        output = Path(command[command.index("-o") + 1])
        board = Path(command[-1])
        calls.append((kind, board, board.read_bytes()))
        if kind == "gerbers":
            for row in expected["FilesAttributes"]:
                (output / row["Path"]).write_bytes(row["FileFunction"].encode())
            (output / "board-job.gbrjob").write_text(json.dumps(expected), encoding="utf-8")
        else:
            (output / "board.drl").write_bytes(b"drill data")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_archive_contains_current_manifest_layers_and_drills_only(tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"exact board bytes")
    board.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
    output = tmp_path / "delivery"
    output.mkdir()
    (output / "stale-F_Cu.gtl").write_bytes(b"stale copper")
    (output / "stale.drl").write_bytes(b"stale drill")
    calls: list[tuple[str, Path, bytes]] = []
    monkeypatch.setattr("circuit_weaver.fabrication_export.subprocess.run", _fake_cli(_manifest(), calls))

    result = export_gerbers_archive(
        board,
        output,
        kicad_cli="kicad-cli",
        expected_board_sha256=hashlib.sha256(board.read_bytes()).hexdigest(),
    )

    archive_path = Path(str(result["zip"]))
    with zipfile.ZipFile(archive_path) as archive:
        members = set(archive.namelist())
        assert members == {
            "board-F_Cu.gtl", "board-L2_Cu.gbl", "board-Edge_Cuts.gm1",
            "board-job.gbrjob", "board.drl",
        }
        assert all("stale" not in name for name in members)
    assert result["file_count"] == 5
    assert [row[0] for row in calls] == ["gerbers", "drill"]
    assert all(row[1] != board and row[2] == b"exact board bytes" for row in calls)
    assert (output / "stale-F_Cu.gtl").is_file()  # loose files remain untouched


def test_archive_requires_every_copper_layer_outline_and_drill(tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"board")
    output = tmp_path / "delivery"
    calls: list[tuple[str, Path, bytes]] = []
    monkeypatch.setattr(
        "circuit_weaver.fabrication_export.subprocess.run",
        _fake_cli(_manifest(4, omit_copper=3), calls),
    )

    with pytest.raises(GerberExportError, match="L3"):
        export_gerbers_archive(
            board,
            output,
            kicad_cli="kicad-cli",
            expected_board_sha256=hashlib.sha256(board.read_bytes()).hexdigest(),
        )
    assert not output.exists()


def test_changed_board_hash_blocks_export_before_creating_output(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"changed board")
    output = tmp_path / "delivery"

    with pytest.raises(GerberExportError, match="changed after"):
        export_gerbers_archive(board, output, kicad_cli="kicad-cli", expected_board_sha256="0" * 64)
    assert not output.exists()


def test_failed_export_preserves_prior_archive_bytes(tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"board")
    output = tmp_path / "delivery"
    output.mkdir()
    archive = output / "board_gerbers.zip"
    archive.write_bytes(b"prior valid archive")
    calls = []

    def fail_drill(command, **kwargs):
        if command[3] == "gerbers":
            return _fake_cli(_manifest(), calls)(command, **kwargs)
        return SimpleNamespace(returncode=1, stdout="", stderr="drill failed")

    monkeypatch.setattr("circuit_weaver.fabrication_export.subprocess.run", fail_drill)
    with pytest.raises(GerberExportError, match="drill export failed"):
        export_gerbers_archive(
            board,
            output,
            kicad_cli="kicad-cli",
            expected_board_sha256=hashlib.sha256(board.read_bytes()).hexdigest(),
        )
    assert archive.read_bytes() == b"prior valid archive"


@pytest.mark.skip_category("optional-tool")
def test_native_kicad_export_archives_real_fixture_layers_and_drill(tmp_path):
    native_python = find_kicad_python()
    cli = _kicad_cli_path()
    roots = [Path(os.getenv("KICAD_FOOTPRINT_DIR", "/usr/share/kicad/footprints"))]
    roots += [Path(f"C:/Program Files/KiCad/{version}/share/kicad/footprints") for version in ("10.0", "9.0", "8.0")]
    footprint_root = next((root for root in roots if (root / "Resistor_SMD.pretty").is_dir()), None)
    if not native_python or not cli or not footprint_root:
        pytest.skip("Native Gerber regression requires KiCad CLI, pcbnew, and standard footprints")

    board = tmp_path / "native_rc.kicad_pcb"
    fixture = Path(__file__).parent / "fixtures" / "make_basic_pcbs.py"
    generated = subprocess.run(
        [native_python, str(fixture), str(footprint_root), str(board), "rc"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    digest = hashlib.sha256(board.read_bytes()).hexdigest()
    result = export_gerbers_archive(
        board,
        tmp_path / "delivery",
        kicad_cli=cli,
        expected_board_sha256=digest,
    )
    with zipfile.ZipFile(str(result["zip"])) as archive:
        members = set(archive.namelist())
        job_names = [name for name in members if name.casefold().endswith(".gbrjob")]
        assert len(job_names) == 1
        manifest = json.loads(archive.read(job_names[0]))
        functions = {
            row["FileFunction"]: row["Path"]
            for row in manifest["FilesAttributes"]
            if isinstance(row, dict) and isinstance(row.get("FileFunction"), str)
        }
        assert "Copper,L1,Top" in functions
        assert f"Copper,L{manifest['GeneralSpecs']['LayerNumber']},Bot" in functions
        assert "Profile" in functions
        required = {
            functions["Copper,L1,Top"],
            functions[f"Copper,L{manifest['GeneralSpecs']['LayerNumber']},Bot"],
            functions["Profile"],
        }
        assert required.issubset(members)
        assert any(name.casefold().endswith(".drl") for name in members)
        assert all(archive.getinfo(name).file_size > 0 for name in required)
        assert all(
            archive.getinfo(name).file_size > 0
            for name in members
            if name.casefold().endswith(".drl")
        )
    assert Path(str(result["zip"])).is_file()
    assert board.read_bytes() and hashlib.sha256(board.read_bytes()).hexdigest() == digest
