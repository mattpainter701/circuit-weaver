"""Native hierarchy lookup and highlighted Tiny Tapeout die preview tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from circuit_weaver.tt_slot_die import TTSlotDieError, render_tt_slot


def _kdb():
    return pytest.importorskip("klayout.db")


def _assert_orange_marker(path: Path) -> None:
    from klayout.lay import PixelBuffer

    image = PixelBuffer.read_png(str(path))
    assert any(
        image.pixel(x, y) & 0xFFFFFF == 0xFF6500
        for y in range(image.height()) for x in range(image.width())
    )


def _layout(path: Path, *, count: int = 1, mirror: bool = True) -> None:
    kdb = _kdb()
    db = kdb.Layout()
    db.dbu = 0.001
    macro = db.create_cell("tt_um_synthetic")
    macro.shapes(db.layer(1, 0)).insert(kdb.Box(0, 0, 100_000, 60_000))
    wrapper = db.create_cell("JA_TO_tt_um_synthetic")
    wrapper.insert(kdb.CellInstArray(macro.cell_index(), kdb.Trans()))
    parent = db.create_cell("provider_wrapper")
    root = db.create_cell("chip_top")
    parent.insert(kdb.CellInstArray(wrapper.cell_index(), kdb.Trans()))
    for i in range(count):
        trans = kdb.Trans(kdb.Trans.M90 if mirror else kdb.Trans.R90, 300_000 + i * 220_000, 500_000)
        root.insert(kdb.CellInstArray(parent.cell_index(), trans))
    root.shapes(db.layer(2, 0)).insert(kdb.Box(-100_000, -100_000, 1_000_000, 1_000_000))
    db.write(str(path))


def test_transformed_hierarchy_renders_highlight_and_global_microns(tmp_path: Path):
    _kdb()
    source, output = tmp_path / "fixture.oas", tmp_path / "die.png"
    _layout(source, mirror=True)

    result = render_tt_slot(source, output, macro="tt_um_synthetic", size=512)

    assert output.is_file() and output.stat().st_size > 1000
    _assert_orange_marker(output)
    assert result["layout_cell"] == "JA_TO_tt_um_synthetic"
    assert result["occurrence_count"] == 1
    assert result["hierarchy_path"] == ["chip_top", "provider_wrapper", "JA_TO_tt_um_synthetic"]
    assert result["bbox_um"] == pytest.approx([200.0, 500.0, 300.0, 560.0])
    assert result["center_um"] == pytest.approx([250.0, 530.0])
    assert result["marker_layer"] == "999/0"
    assert result["integrity"].startswith("native_layout_sha256")
    assert result["sha256"] and result["input_sha256"]


def test_repeated_macro_occurrence_fails_closed(tmp_path: Path):
    _kdb()
    source = tmp_path / "ambiguous.oas"
    _layout(source, count=2)

    with pytest.raises(TTSlotDieError, match="2 physical occurrences"):
        render_tt_slot(source, tmp_path / "ambiguous.png", macro="tt_um_synthetic")


def test_macro_in_library_but_not_instantiated_fails_closed(tmp_path: Path):
    kdb = _kdb()
    source = tmp_path / "incomplete.oas"
    db = kdb.Layout()
    db.create_cell("chip_top")
    db.create_cell("JA_TO_tt_um_synthetic").shapes(db.layer(1, 0)).insert(kdb.Box(0, 0, 1000, 1000))
    db.write(str(source))

    with pytest.raises(TTSlotDieError, match="no instance below root"):
        render_tt_slot(source, tmp_path / "incomplete.png", macro="tt_um_synthetic", top="chip_top")


def test_official_index_record_and_real_ttsky26d_layout(tmp_path: Path):
    _kdb()
    layout_value = os.environ.get("TT_SLOT_DIE_OAS")
    index_value = os.environ.get("TT_SLOT_DIE_INDEX")
    if not layout_value or not index_value:
        pytest.skip("set TT_SLOT_DIE_OAS and TT_SLOT_DIE_INDEX to run the pinned provider example")
    layout, index_path = Path(layout_value), Path(index_value)
    if not layout.is_file() or not index_path.is_file():
        pytest.skip("official pinned TTSKY26d OAS and index are not available")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    record = next(row for row in index["projects"] if row["macro"] == "tt_um_factory_test")
    output = tmp_path / "factory-test.png"

    result = render_tt_slot(layout, output, macro=record["macro"])

    assert result["input_sha256"] == "5b0287919a58015f31599f77df3e4dd2a8fd3722bb362fad7fa086117cefd6d1"
    assert result["top"] == "caravel_5454"
    assert result["layout_cell"] == "JA_TO_tt_um_factory_test"
    assert record["address"] == 1
    assert index["commit"] == "d16e0c27431d2539e419db888a6e1ab046d50d5c"
    assert result["occurrence_count"] == 1
    assert result["bbox_um"] == pytest.approx([1544.245, 2313.805, 1705.245, 2425.325])
    assert output.stat().st_size > 100_000
    _assert_orange_marker(output)
