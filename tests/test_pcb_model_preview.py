from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

from circuit_weaver import pcb_model_preview


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


# Genuine one-pixel RGBA PNG, suitable for strict chunk and CRC validation.
PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
    + _png_chunk(b"IEND", b"")
)


def _source_board(tmp_path: Path, model_path: str = "models/body.step") -> tuple[Path, Path, Path, Path]:
    project = tmp_path / "demo.kicad_pro"
    rules = tmp_path / "demo.kicad_dru"
    board = tmp_path / "demo.kicad_pcb"
    model = tmp_path / "models" / "body.step"
    model.parent.mkdir()
    model.write_bytes(b"STEP model data")
    project.write_text("{}\n", encoding="utf-8")
    rules.write_text("(version 1)\n", encoding="utf-8")
    board.write_text(
        f'(kicad_pcb (version 20250114) (general (thickness 1.6)) '
        f'(footprint "Demo" (at 1 2) (model "{model_path}")) )\n',
        encoding="utf-8",
    )
    return board, project, rules, model


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_render_uses_isolated_board_and_preserves_sources(tmp_path, monkeypatch):
    board, project, rules, model = _source_board(tmp_path)
    output = tmp_path / "preview.png"
    cli = tmp_path / "kicad-cli"
    cli.touch()
    monkeypatch.setattr(pcb_model_preview, "_find_kicad_cli", lambda _explicit: cli)
    source_hashes = {path: _sha256(path) for path in (board, project, rules, model)}
    calls: list[list[str]] = []
    staged_board_seen: Path | None = None

    def fake_run(command, **_kwargs):
        nonlocal staged_board_seen
        command = [str(part) for part in command]
        calls.append(command)
        if command[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="10.0.4\n", stderr="")
        if command[1:4] == ["pcb", "render", "--help"]:
            return SimpleNamespace(returncode=0, stdout="Usage: kicad-cli pcb render ...", stderr="")
        staged_board_seen = Path(command[-1])
        staged_text = staged_board_seen.read_text(encoding="utf-8")
        assert str(model.resolve()).replace("\\", "/") in staged_text
        assert str(model) not in staged_text
        staged_png = Path(command[command.index("--output") + 1])
        staged_png.write_bytes(PNG)
        return SimpleNamespace(returncode=0, stdout="Successfully created 3D render image", stderr="")

    monkeypatch.setattr(pcb_model_preview.subprocess, "run", fake_run)
    result = pcb_model_preview.render_board_model(board, output, side="back")

    assert result["status"] == "ok"
    assert result["reason"] == "rendered"
    assert result["source_sha256"] == source_hashes[board]
    assert result["png_sha256"] == hashlib.sha256(PNG).hexdigest()
    assert result["model_coverage"]["expected"] == result["model_coverage"]["resolved"] == 1
    assert result["model_coverage"]["missing"] == 0
    assert result["side"] == "bottom"
    assert result["png_width"] == result["png_height"] == 1
    assert output.read_bytes() == PNG
    assert staged_board_seen is not None and staged_board_seen != board
    assert staged_board_seen.parent != board.parent
    assert [path for path, digest in source_hashes.items() if _sha256(path) != digest] == []
    assert calls[-1][-1] == str(staged_board_seen)
    assert "--side" in calls[-1] and calls[-1][calls[-1].index("--side") + 1] == "bottom"


def test_kicad_8_reports_render_unavailable_without_rendering(tmp_path, monkeypatch):
    board, _project, _rules, _model = _source_board(tmp_path)
    cli = tmp_path / "kicad-cli"
    cli.touch()
    monkeypatch.setattr(pcb_model_preview, "_find_kicad_cli", lambda _explicit: cli)
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        command = [str(part) for part in command]
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="8.0.9", stderr="")

    monkeypatch.setattr(pcb_model_preview.subprocess, "run", fake_run)
    result = pcb_model_preview.render_board_model(board, tmp_path / "preview.png")

    assert result["status"] == "unavailable"
    assert result["reason"] == "native_3d_render_requires_kicad_9_or_newer"
    assert "2D SVG preview remains available" in result["evidence"]
    assert not (tmp_path / "preview.png").exists()
    assert len(calls) == 1 and calls[0][1:] == ["version"]


def test_unresolved_model_fails_without_calling_native_render(tmp_path, monkeypatch):
    board, _project, _rules, _model = _source_board(tmp_path, "models/missing.step")
    cli = tmp_path / "kicad-cli"
    cli.touch()
    monkeypatch.setattr(pcb_model_preview, "_find_kicad_cli", lambda _explicit: cli)
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        command = [str(part) for part in command]
        calls.append(command)
        if command[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="10.0.4", stderr="")
        return SimpleNamespace(returncode=0, stdout="Usage: kicad-cli pcb render ...", stderr="")

    monkeypatch.setattr(pcb_model_preview.subprocess, "run", fake_run)
    result = pcb_model_preview.render_board_model(board, tmp_path / "preview.png")

    assert result["status"] == "failed"
    assert result["reason"] == "3d_model_files_missing"
    assert result["model_coverage"]["missing"] == 1
    assert result["model_coverage"]["models"][0]["source_path"] == "models/missing.step"
    assert not any(command[1:3] == ["pcb", "render"] and command[3:4] != ["--help"] for command in calls)
    assert not (tmp_path / "preview.png").exists()


def test_existing_output_requires_explicit_overwrite(tmp_path, monkeypatch):
    board, _project, _rules, _model = _source_board(tmp_path)
    output = tmp_path / "preview.png"
    output.write_bytes(b"keep me")
    monkeypatch.setattr(pcb_model_preview, "_find_kicad_cli", lambda _explicit: None)

    result = pcb_model_preview.render_board_model(board, output)

    assert result["status"] == "failed"
    assert result["reason"] == "output_exists"
    assert output.read_bytes() == b"keep me"


def test_truncated_png_is_rejected_without_publishing_output(tmp_path, monkeypatch):
    board, _project, _rules, _model = _source_board(tmp_path)
    output = tmp_path / "preview.png"
    cli = tmp_path / "kicad-cli"
    cli.touch()
    monkeypatch.setattr(pcb_model_preview, "_find_kicad_cli", lambda _explicit: cli)

    def fake_run(command, **_kwargs):
        command = [str(part) for part in command]
        if command[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="10.0.4", stderr="")
        if command[1:4] == ["pcb", "render", "--help"]:
            return SimpleNamespace(returncode=0, stdout="Usage: kicad-cli pcb render ...", stderr="")
        staged_png = Path(command[command.index("--output") + 1])
        staged_png.write_bytes(PNG[:-5])
        return SimpleNamespace(returncode=0, stdout="Successfully created 3D render image", stderr="")

    monkeypatch.setattr(pcb_model_preview.subprocess, "run", fake_run)
    result = pcb_model_preview.render_board_model(board, output)

    assert result["status"] == "failed"
    assert result["reason"] == "render_output_invalid_png"
    assert "truncated" in result["evidence"].lower()
    assert "message" in result and result["message"]
    assert "output_path" not in result
    assert not output.exists()


def test_png_extension_and_boolean_arguments_are_validated(tmp_path):
    board, _project, _rules, _model = _source_board(tmp_path)

    extension_result = pcb_model_preview.render_board_model(board, tmp_path / "preview.jpeg")
    timeout_result = pcb_model_preview.render_board_model(board, tmp_path / "preview.png", timeout_seconds=True)
    overwrite_result = pcb_model_preview.render_board_model(board, tmp_path / "preview.png", overwrite=1)

    assert extension_result["reason"] == "invalid_output_extension"
    assert timeout_result["reason"] == "invalid_timeout"
    assert overwrite_result["reason"] == "invalid_overwrite"


def test_unset_custom_model_alias_does_not_fall_back_to_kicad_roots(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_3DMODEL_DIR", raising=False)
    root = tmp_path / "installed-kicad-models"
    model = root / "body.step"
    root.mkdir()
    model.write_bytes(b"same-name model")
    roots = {"KICAD10_3DMODEL_DIR": [root]}

    resolved_custom = pcb_model_preview._resolve_model_path(
        "${MY_3DMODEL_DIR}/body.step", tmp_path, roots
    )
    resolved_standard = pcb_model_preview._resolve_model_path(
        "${KICAD10_3DMODEL_DIR}/body.step", tmp_path, roots
    )

    assert resolved_custom is None
    assert resolved_standard == model.resolve()


def test_failed_atomic_publish_preserves_existing_output(tmp_path, monkeypatch):
    board, _project, _rules, _model = _source_board(tmp_path)
    output = tmp_path / "preview.png"
    output.write_bytes(b"previous preview")
    cli = tmp_path / "kicad-cli"
    cli.touch()
    monkeypatch.setattr(pcb_model_preview, "_find_kicad_cli", lambda _explicit: cli)

    original_copyfile = pcb_model_preview.shutil.copyfile

    def fail_preview_copy(source, destination, *args, **kwargs):
        if Path(source).name == "render.png":
            raise OSError("simulated full disk")
        return original_copyfile(source, destination, *args, **kwargs)

    monkeypatch.setattr(pcb_model_preview.shutil, "copyfile", fail_preview_copy)

    def fake_run(command, **_kwargs):
        command = [str(part) for part in command]
        if command[1:] == ["version"]:
            return SimpleNamespace(returncode=0, stdout="10.0.4", stderr="")
        if command[1:4] == ["pcb", "render", "--help"]:
            return SimpleNamespace(returncode=0, stdout="Usage: kicad-cli pcb render ...", stderr="")
        staged_png = Path(command[command.index("--output") + 1])
        staged_png.write_bytes(PNG)
        return SimpleNamespace(returncode=0, stdout="Successfully created 3D render image", stderr="")

    monkeypatch.setattr(pcb_model_preview.subprocess, "run", fake_run)
    result = pcb_model_preview.render_board_model(board, output, overwrite=True)

    assert result["status"] == "failed"
    assert result["reason"] == "output_publish_failed"
    assert output.read_bytes() == b"previous preview"

