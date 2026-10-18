from __future__ import annotations

import base64
import hashlib
import os
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import pytest

from circuit_weaver import pcb_assembly_preview

SVG = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}href"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0))
    + _chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff\xff\x00\x00\x00"))
    + _chunk(b"IEND", b"")
)


def _board(tmp_path: Path) -> tuple[Path, Path]:
    board = tmp_path / "board.kicad_pcb"
    model = tmp_path / "part.step"
    model.write_bytes(b"native model")
    board.write_text(
        '(kicad_pcb (footprint "x" (property "Reference" "U1") '
        '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")) '
        '(model "part.step")))\n',
        encoding="utf-8",
    )
    return board, model


def _mock_native(monkeypatch, board: Path, model: Path, *, mutate: bool = False):
    source_sha = hashlib.sha256(board.read_bytes()).hexdigest()

    def render(source, output, **kwargs):
        assert Path(source) == board
        assert kwargs["side"] in {"front", "back"}
        Path(output).write_bytes(PNG)
        if mutate:
            board.write_text("changed while rendering", encoding="utf-8")
        return {
            "status": "ok",
            "reason": "rendered",
            "source_sha256": source_sha,
            "cli_version": "KiCad 10.0.4",
            "side": "top" if kwargs["side"] == "front" else "bottom",
            "output_path": str(output),
            "source_hashes": {str(board): source_sha},
            "png_sha256": hashlib.sha256(PNG).hexdigest(),
            "png_width": 2,
            "png_height": 1,
            "model_coverage": {
                "expected": 1,
                "resolved": 1,
                "missing": 0,
                "models": [{"resolved_path": str(model), "sha256": hashlib.sha256(model.read_bytes()).hexdigest()}],
            },
        }

    monkeypatch.setattr(pcb_assembly_preview, "render_board_model", render)
    return source_sha


def test_embeds_exact_native_png_as_standalone_svg(tmp_path, monkeypatch):
    board, model = _board(tmp_path)
    source_sha = _mock_native(monkeypatch, board, model)
    output = tmp_path / "assembly.svg"

    result = pcb_assembly_preview.render_board_assembly(board, output, side="back")

    assert result["status"] == "ok"
    assert result["prototype"] is True and result["appearance"] == "assembly"
    assert result["source_sha256"] == source_sha
    assert result["embedded_png_sha256"] == hashlib.sha256(PNG).hexdigest()
    assert result["png_width"] == 2 and result["png_height"] == 1
    root = ET.parse(output).getroot()
    assert root.get("viewBox") == "0 0 2 1"
    assert root.get("data-appearance") == "assembly" and root.get("data-side") == "back"
    image = root.find(f"{SVG}image")
    assert image is not None and image.get("id") == "assembled-board-models"
    uri = image.get("href") or image.get(XLINK, "")
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.partition(",")[2]) == PNG
    assert image.get("width") == "2" and image.get("height") == "1"
    assert root.find(f"{SVG}title") is not None and root.find(f"{SVG}desc") is not None
    metadata = root.find(f"{SVG}metadata")
    assert metadata is not None and metadata.get("source_sha256") == source_sha
    assert metadata.get("component_expected") == "1"
    assert metadata.get("component_represented") == "1"
    assert metadata.get("component_missing") == "0"
    assert result["component_coverage"] == {
        "expected": 1,
        "represented": 1,
        "missing": 0,
        "missing_references": [],
        "components": [{"reference": "U1", "required": True, "visible_model_count": 1, "hidden_model_count": 0}],
    }
    assert str(tmp_path) not in ET.tostring(root, encoding="unicode")
    assert "path" not in result["model_preview"]


def test_native_failure_does_not_publish_or_replace(tmp_path, monkeypatch):
    board, _model = _board(tmp_path)
    output = tmp_path / "assembly.svg"
    output.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(
        pcb_assembly_preview,
        "render_board_model",
        lambda *_a, **_k: {
            "status": "unavailable",
            "reason": "native_3d_render_requires_kicad_9_or_newer",
            "message": "unsupported",
        },
    )

    result = pcb_assembly_preview.render_board_assembly(board, output, overwrite=True)

    assert result["status"] == "error"
    assert result["reason"] == "native_3d_render_requires_kicad_9_or_newer"
    assert output.read_text(encoding="utf-8") == "previous"


def test_validation_and_no_clobber(tmp_path, monkeypatch):
    board, model = _board(tmp_path)
    _mock_native(monkeypatch, board, model)
    existing = tmp_path / "existing.svg"
    existing.write_bytes(b"keep")

    assert (
        pcb_assembly_preview.render_board_assembly(board, tmp_path / "bad.png")["reason"] == "invalid_output_extension"
    )
    assert (
        pcb_assembly_preview.render_board_assembly(board, tmp_path / "bad.svg", timeout_seconds=True)["reason"]
        == "invalid_timeout"
    )
    assert (
        pcb_assembly_preview.render_board_assembly(board, tmp_path / "huge.svg", timeout_seconds=10**10000)["reason"]
        == "invalid_timeout"
    )
    assert pcb_assembly_preview.render_board_assembly(board, existing)["reason"] == "output_exists"
    assert existing.read_bytes() == b"keep"


@pytest.mark.skip_category("platform")
def test_output_symlink_protection(tmp_path, monkeypatch):
    board, model = _board(tmp_path)
    _mock_native(monkeypatch, board, model)
    existing = tmp_path / "existing.svg"
    existing.write_bytes(b"keep")
    link = tmp_path / "linked.svg"
    try:
        os.symlink(existing, link)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    assert (
        pcb_assembly_preview.render_board_assembly(board, link, overwrite=True)["reason"]
        == "output_conflicts_with_source"
    )
    assert existing.read_bytes() == b"keep"


def test_overwrite_replaces_output_and_blocks_source_aliases(tmp_path, monkeypatch):
    board, model = _board(tmp_path)
    _mock_native(monkeypatch, board, model)
    output = tmp_path / "assembly.svg"
    output.write_bytes(b"old")

    result = pcb_assembly_preview.render_board_assembly(board, output, overwrite=True)
    source_alias = tmp_path / "board-copy.svg"
    source_alias.hardlink_to(board)
    alias_result = pcb_assembly_preview.render_board_assembly(board, source_alias, overwrite=True)

    assert result["status"] == "ok" and output.read_bytes() != b"old"
    assert alias_result["reason"] == "output_conflicts_with_source"
    assert board.read_text(encoding="utf-8").startswith("(kicad_pcb")


def test_source_drift_after_native_render_prevents_publication(tmp_path, monkeypatch):
    board, model = _board(tmp_path)
    _mock_native(monkeypatch, board, model, mutate=True)
    output = tmp_path / "assembly.svg"

    result = pcb_assembly_preview.render_board_assembly(board, output)

    assert result["reason"] == "source_changed_during_render"
    assert not output.exists()


def test_project_sidecar_added_during_native_render_is_detected(tmp_path, monkeypatch):
    board, model = _board(tmp_path)
    _mock_native(monkeypatch, board, model)
    original_render = pcb_assembly_preview.render_board_model

    def render_and_add_project(*args, **kwargs):
        result = original_render(*args, **kwargs)
        board.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(pcb_assembly_preview, "render_board_model", render_and_add_project)
    output = tmp_path / "assembly.svg"

    result = pcb_assembly_preview.render_board_assembly(board, output)

    assert result["reason"] == "source_changed_during_render"
    assert not output.exists()


def test_model_drift_after_native_render_prevents_publication(tmp_path, monkeypatch):
    board, model = _board(tmp_path)
    _mock_native(monkeypatch, board, model)
    original_render = pcb_assembly_preview.render_board_model

    def render_then_change_model(*args, **kwargs):
        result = original_render(*args, **kwargs)
        model.write_bytes(b"changed after native render")
        return result

    monkeypatch.setattr(pcb_assembly_preview, "render_board_model", render_then_change_model)
    output = tmp_path / "assembly.svg"

    result = pcb_assembly_preview.render_board_assembly(board, output)

    assert result["reason"] == "source_changed_during_render"
    assert not output.exists()


def test_destination_appearing_during_render_is_preserved(tmp_path, monkeypatch):
    board, model = _board(tmp_path)
    output = tmp_path / "assembly.svg"
    _mock_native(monkeypatch, board, model)
    original_render = pcb_assembly_preview.render_board_model

    def render_and_create_destination(*args, **kwargs):
        result = original_render(*args, **kwargs)
        output.write_bytes(b"created concurrently")
        return result

    monkeypatch.setattr(pcb_assembly_preview, "render_board_model", render_and_create_destination)

    result = pcb_assembly_preview.render_board_assembly(board, output)

    assert result["reason"] == "output_publish_failed"
    assert output.read_bytes() == b"created concurrently"


def test_native_png_metadata_must_match_payload_and_requested_side(tmp_path, monkeypatch):
    board, model = _board(tmp_path)
    _mock_native(monkeypatch, board, model)
    original_render = pcb_assembly_preview.render_board_model

    def wrong_native_metadata(*args, **kwargs):
        result = original_render(*args, **kwargs)
        result["side"] = "bottom"
        result["png_width"] = 99
        return result

    monkeypatch.setattr(pcb_assembly_preview, "render_board_model", wrong_native_metadata)
    output = tmp_path / "assembly.svg"

    result = pcb_assembly_preview.render_board_assembly(board, output, side="front")

    assert result["reason"] == "native_render_metadata_mismatch"
    assert not output.exists()


def test_populated_component_without_visible_model_fails_before_render(tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    board.write_text(
        '(kicad_pcb (footprint "x" (property "Reference" "U1") '
        '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))))\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pcb_assembly_preview,
        "render_board_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("native render should not run")),
    )

    result = pcb_assembly_preview.render_board_assembly(board, tmp_path / "assembly.svg")

    assert result["reason"] == "component_models_incomplete"
    assert result["component_coverage"]["missing_references"] == ["U1"]


def test_output_aliasing_referenced_model_is_rejected(tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    output = tmp_path / "model.svg"
    output.write_bytes(b"model")
    board.write_text(
        '(kicad_pcb (footprint "x" (property "Reference" "U1") '
        '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")) '
        '(model "model.svg")))\n',
        encoding="utf-8",
    )

    def native(_board, _png, **_kwargs):
        return {
            "status": "ok",
            "source_sha256": hashlib.sha256(board.read_bytes()).hexdigest(),
            "side": "top",
            "png_sha256": hashlib.sha256(PNG).hexdigest(),
            "png_width": 2,
            "png_height": 1,
            "model_coverage": {
                "expected": 1,
                "resolved": 1,
                "missing": 0,
                "models": [{"resolved_path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}],
            },
        }

    monkeypatch.setattr(pcb_assembly_preview, "render_board_model", native)

    result = pcb_assembly_preview.render_board_assembly(board, output, overwrite=True)

    assert result["reason"] == "output_conflicts_with_source"
