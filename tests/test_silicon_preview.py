"""Functional coverage for the optional native KLayout silicon preview."""

from __future__ import annotations

import hashlib
import struct
import zlib

import klayout.db as kdb
import klayout.lay as klay
import pytest

from circuit_weaver.silicon_preview import SiliconPreviewError, render_silicon_preview


def _rendered_pixel_bounds(path):
    """Decode KLayout's RGB PNG sufficiently to measure rendered geometry."""
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    compressed = bytearray()
    width = height = color_type = bit_depth = None
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        chunk = data[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, _interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
        elif kind == b"IDAT":
            compressed.extend(chunk)
        offset += length + 12
        if kind == b"IEND":
            break
    assert color_type == 2 and bit_depth == 8  # KLayout emits 8-bit RGB PNGs.
    bpp = 3
    stride = width * bpp
    raw = zlib.decompress(compressed)
    previous = bytearray(stride)
    min_x, min_y, max_x, max_y = width, height, -1, -1
    colored = 0
    cursor = 0
    for y in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        for i in range(stride):
            left = row[i - bpp] if i >= bpp else 0
            up = previous[i]
            upper_left = previous[i - bpp] if i >= bpp else 0
            if filter_type == 1:
                row[i] = (row[i] + left) & 0xFF
            elif filter_type == 2:
                row[i] = (row[i] + up) & 0xFF
            elif filter_type == 3:
                row[i] = (row[i] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                p = left + up - upper_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                predictor = left if pa <= pb and pa <= pc else up if pb <= pc else upper_left
                row[i] = (row[i] + predictor) & 0xFF
            else:
                assert filter_type == 0
        for x in range(width):
            pixel = row[x * bpp : x * bpp + bpp]
            if pixel != b"\x15\x1a\x22":  # configured #151a22 background
                colored += 1
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
        previous = row
    return width, height, colored, (min_x, min_y, max_x, max_y)


def _write_hierarchical_layout(path):
    layout = kdb.Layout()
    layout.dbu = 0.001  # one DBU is one nanometre
    metal = layout.layer(1, 0)
    via = layout.layer(2, 0)
    die = layout.create_cell("DIE")
    die.shapes(metal).insert(kdb.Box(0, 0, 100_000, 50_000))
    die.shapes(via).insert(kdb.Box(10_000, 10_000, 20_000, 20_000))
    tile = layout.create_cell("TILE")
    tile.shapes(metal).insert(kdb.Box(0, 0, 2_000, 3_000))
    die.insert(kdb.CellInstArray(tile.cell_index(), kdb.Trans(1_000, 1_000)))
    carrier = layout.create_cell("CARRIER")
    carrier.insert(kdb.CellInstArray(die.cell_index(), kdb.Trans(1_000_000, 2_000_000)))
    # An array exercises hierarchical physical bounds without Python polygon
    # flattening or repeated traversal of the referenced cell definition.
    carrier.insert(
        kdb.CellInstArray(
            die.cell_index(),
            kdb.Trans(1_500_000, 2_000_000),
            kdb.Vector(200_000, 0),
            kdb.Vector(0, 150_000),
            2,
            3,
        )
    )
    # The carrier outline gives the renderer a large, clearly measurable
    # top-level polygon while the die/via geometry remains nested below it.
    carrier.shapes(layout.layer(3, 0)).insert(kdb.Box(1_000_000, 2_000_000, 1_800_000, 2_350_000))
    layout.write(str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_renders_real_hierarchical_geometry_and_reports_physical_units(tmp_path):
    source = tmp_path / "carrier.gds"
    source_hash = _write_hierarchical_layout(source)
    output = tmp_path / "carrier.png"

    result = render_silicon_preview(
        source,
        output,
        layers={"1/0": {"name": "metal", "color": "#d9a441"}, "2/0": "#2a9dca"},
        size=640,
    )

    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    width, height, colored_pixels, pixel_bounds = _rendered_pixel_bounds(output)
    assert colored_pixels > width * height * 0.03
    assert pixel_bounds[2] - pixel_bounds[0] > width * 0.85
    assert pixel_bounds[3] - pixel_bounds[1] > height * 0.35
    rendered_aspect = (pixel_bounds[2] - pixel_bounds[0]) / (pixel_bounds[3] - pixel_bounds[1])
    assert rendered_aspect == pytest.approx(800 / 350, rel=0.15)
    assert result["top"] == "CARRIER"
    assert result["bbox_um"] == pytest.approx([1000, 2000, 1800, 2350])
    assert result["dbu_um"] == pytest.approx(0.001)
    assert result["size_px"] == [640, 640]
    assert result["shape_count"] == 4  # unique cell definitions, not array expansions
    assert result["hierarchy"] == [
        {"name": "CARRIER", "children": ["DIE"]},
        {"name": "DIE", "children": ["TILE"]},
        {"name": "TILE", "children": []},
    ]
    assert result["input_sha256"] == source_hash
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["role"] == "preview_only"
    assert result["manufacturing_ready"] is False
    assert source.read_bytes() and hashlib.sha256(source.read_bytes()).hexdigest() == source_hash


def test_multiple_roots_require_explicit_root_selection(tmp_path):
    source = tmp_path / "multiple.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    for name, x in (("ALPHA", 0), ("BETA", 1000)):
        cell = layout.create_cell(name)
        cell.shapes(layout.layer(1, 0)).insert(kdb.Box(x, 0, x + 100, 100))
    layout.write(str(source))

    with pytest.raises(SiliconPreviewError, match="multiple top cells"):
        render_silicon_preview(source, tmp_path / "out.png")
    result = render_silicon_preview(source, tmp_path / "beta.png", top="BETA")
    assert result["top"] == "BETA"
    assert result["bbox_um"] == pytest.approx([1, 0, 1.1, 0.1])


def test_rejects_empty_layout_and_protected_output_paths(tmp_path):
    empty = tmp_path / "empty.gds"
    layout = kdb.Layout()
    layout.create_cell("EMPTY")
    layout.write(str(empty))
    with pytest.raises(SiliconPreviewError, match="no physical geometry"):
        render_silicon_preview(empty, tmp_path / "empty.png")
    with pytest.raises(SiliconPreviewError, match="overwrite the input layout"):
        render_silicon_preview(empty, empty)


def test_rejects_output_that_would_replace_layer_properties(tmp_path):
    source = tmp_path / "source.gds"
    _write_hierarchical_layout(source)
    layer_file = tmp_path / "layers.lyp"
    layer_file.write_text("<layer-properties/>", encoding="utf-8")

    with pytest.raises(SiliconPreviewError, match="overwrite the input layer-properties"):
        render_silicon_preview(source, layer_file, layers=layer_file)
    assert layer_file.read_text(encoding="utf-8") == "<layer-properties/>"


def test_loads_native_klayout_layer_properties_without_modifying_them(tmp_path):
    source = tmp_path / "source.gds"
    _write_hierarchical_layout(source)
    properties = tmp_path / "layers.lyp"
    view = klay.LayoutView()
    view.load_layout(str(source))
    view.save_layer_props(str(properties))
    original = properties.read_bytes()

    result = render_silicon_preview(source, tmp_path / "with-layers.png", layers=properties)

    assert result["layer_properties_path"] == str(properties.resolve())
    assert properties.read_bytes() == original
