"""Focused safety and styling tests for native PCB preview composition."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from circuit_weaver.pcb_preview import (
    _NATIVE_POLYGON_SCRIPT,
    _build_svg_export_command,
    _publish_staged_file,
    _source_hashes,
    _style_element,
    _verify_native_edge_anchor,
    render_board_preview,
)


def test_native_layer_recolor_keeps_copper_strokes_and_transparent_drills():
    native = ET.fromstring(
        '<g style="fill:#000000;stroke:#000000"><path style="fill:#FFFFFF;stroke:none" />'
        '<path style="fill:none;stroke:#000000" />'
        '<path style="fill:none;stroke:#FFFFFF;stroke-width:0.8" /></g>'
    )

    _style_element(native, fill="#e4bd72", stroke="#e4bd72")

    assert "stroke:#e4bd72" in native.get("style", "")
    paths = list(native)
    assert "fill:#10271b" in paths[0].get("style", "")
    assert "stroke:none" in paths[0].get("style", "")
    assert "fill:none" in paths[1].get("style", "")
    assert "stroke:#e4bd72" in paths[1].get("style", "")
    assert "stroke:#10271b" in paths[2].get("style", "")
    assert paths[2].get("data-drill-hole") == "true"


def test_black_and_white_native_export_is_requested_for_stable_drill_colors(tmp_path: Path):
    command = _build_svg_export_command("kicad-cli", Path("board.kicad_pcb"), "F.Cu",
                                        tmp_path / "F_Cu.svg", 10)
    assert "--black-and-white" in command
    assert "--mode-single" in command
    assert command[-2:] == [str(tmp_path / "F_Cu.svg"), "board.kicad_pcb"]

    legacy = _build_svg_export_command("kicad-cli", Path("board.kicad_pcb"), "F.Cu",
                                       tmp_path / "F_Cu.svg", 8)
    assert "--black-and-white" in legacy
    assert "--mode-single" not in legacy


def test_white_fab_foreground_is_recolored_as_artwork_not_a_drill():
    fab = ET.fromstring('<g><path style="fill:#FFFFFF;stroke:#FFFFFF" /></g>')

    _style_element(fab, fill="#d9e1d9", stroke="#d9e1d9", white_is_drill=False,
                   mark_drill=False)

    path = fab[0]
    assert "fill:#d9e1d9" in path.get("style", "")
    assert "stroke:#d9e1d9" in path.get("style", "")
    assert path.get("data-drill-hole") is None


def test_native_polygon_helper_selects_kicad_8_9_and_10_signatures():
    assert "candidates = ((poly, False, None, False, False), (poly, False))" in _NATIVE_POLYGON_SCRIPT
    assert "candidates = ((poly, None, False, False), (poly,))" in _NATIVE_POLYGON_SCRIPT
    assert "KiCad could not resolve a closed Edge.Cuts outline" in _NATIVE_POLYGON_SCRIPT
    assert "poly.OutlineCount() == 0" in _NATIVE_POLYGON_SCRIPT


def test_preview_rejects_non_svg_output_without_touching_source(tmp_path: Path):
    source = tmp_path / "board.kicad_pcb"
    source.write_bytes(b"native board bytes")
    destination = tmp_path / "preview.kicad_pcb"

    result = render_board_preview(source, destination, overwrite=True)

    assert result["status"] == "error"
    assert ".svg" in result["message"]
    assert source.read_bytes() == b"native board bytes"
    assert not destination.exists()


def test_preview_rejects_svg_hardlinked_to_source_even_when_overwrite_is_true(tmp_path: Path):
    source = tmp_path / "board.kicad_pcb"
    source.write_bytes(b"native board bytes")
    destination = tmp_path / "preview.svg"
    try:
        os.link(source, destination)
    except OSError as exc:
        pytest.skip(f"Hard links are unavailable on this filesystem: {exc}")

    result = render_board_preview(source, destination, overwrite=True)

    assert result["status"] == "error"
    assert "distinct" in result["message"]
    assert source.read_bytes() == b"native board bytes"
    assert destination.read_bytes() == b"native board bytes"


def test_input_hash_snapshot_covers_board_project_and_rules_sidecars(tmp_path: Path):
    source = tmp_path / "board.kicad_pcb"
    project = source.with_suffix(".kicad_pro")
    rules = source.with_suffix(".kicad_dru")
    source.write_bytes(b"board")
    project.write_bytes(b"project")
    rules.write_bytes(b"rules")

    initial = _source_hashes(source)
    rules.write_bytes(b"changed rules")
    after_change = _source_hashes(source)

    assert initial[".kicad_pcb"]
    assert initial[".kicad_pro"]
    assert initial[".kicad_dru"]
    assert initial != after_change


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "fast", 10**1000])
def test_preview_rejects_invalid_timeout_before_running_tools(tmp_path: Path, timeout):
    source = tmp_path / "board.kicad_pcb"
    source.write_bytes(b"board")

    result = render_board_preview(source, tmp_path / "preview.svg", timeout_seconds=timeout)

    assert result["status"] == "error"
    assert "timeout_seconds" in result["message"]
    assert not (tmp_path / "preview.svg").exists()


def test_preview_does_not_fall_back_when_explicit_kicad_cli_is_invalid(tmp_path: Path):
    source = tmp_path / "board.kicad_pcb"
    source.write_bytes(b"board")

    result = render_board_preview(source, tmp_path / "preview.svg", kicad_cli=tmp_path / "missing-kicad-cli.exe")

    assert result["status"] == "error"
    assert "kicad-cli is unavailable" in result["message"]
    assert not (tmp_path / "preview.svg").exists()


def test_native_svg_anchor_must_match_native_board_polygon_coordinates():
    root = ET.fromstring('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0.0000 0.0000 L10 0 M10 10 L10 0" /></svg>')
    polygon = {"bounds": {"x": 20.0, "y": 30.0},
               "outlines": [{"points": [[20.0, 30.0], [30.0, 30.0], [30.0, 40.0]]}]}

    assert _verify_native_edge_anchor(root, polygon, (0.0, 0.0, 10.0, 10.0)) == pytest.approx((0, 0))

    translated = ET.fromstring(
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0.1000 0.0000 L10 0 M10.1 10 L10 0" /></svg>'
    )
    assert _verify_native_edge_anchor(translated, polygon, (0.0, 0.0, 10.0, 10.0)) == pytest.approx((0.1, 0))

    inconsistent = ET.fromstring(
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0.1000 0.0000 L10 0 M10.1 10.1 L10 0" /></svg>'
    )
    with pytest.raises(RuntimeError, match="do not align"):
        _verify_native_edge_anchor(inconsistent, polygon, (0.0, 0.0, 10.0, 10.0))


def test_no_overwrite_publication_preserves_file_that_wins_output_race(tmp_path: Path):
    staging = tmp_path / ".preview-stage.svg"
    destination = tmp_path / "preview.svg"
    staging.write_bytes(b"complete preview")
    destination.write_bytes(b"competing output")

    with pytest.raises(FileExistsError):
        _publish_staged_file(staging, destination, overwrite=False)

    assert destination.read_bytes() == b"competing output"
    assert staging.read_bytes() == b"complete preview"

