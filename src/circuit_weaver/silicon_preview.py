"""Read-only, non-signoff GDS/OASIS preview rendered by KLayout itself."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Any, Mapping


class SiliconPreviewError(ValueError):
    """Raised when a layout cannot safely be rendered as a preview."""


_PALETTE = (0x2A9DCA, 0xD9A441, 0x56A886, 0xD36C72, 0x9477C6, 0x5FBBC0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _layer_color(value: Any, fallback: int) -> int:
    if isinstance(value, str):
        raw = value.strip().lstrip("#")
        if len(raw) == 6:
            try:
                return int(raw, 16)
            except ValueError:
                pass
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            r, g, b = (max(0, min(255, int(component))) for component in value[:3])
            return (r << 16) | (g << 8) | b
        except (TypeError, ValueError):
            pass
    return fallback


def _layer_map(layers: Any) -> tuple[dict[tuple[int, int], dict[str, Any]], Path | None]:
    """Normalize mapping input; .lyp files are loaded directly by KLayout."""
    if layers is None:
        return {}, None
    if isinstance(layers, (str, os.PathLike)):
        path = Path(layers).expanduser().resolve()
        if path.suffix.lower() != ".lyp" or not path.is_file():
            raise SiliconPreviewError("layers path must name an existing .lyp layer-properties file")
        # Parse once so malformed input fails clearly; KLayout loads the original
        # .lyp below, preserving its native groups, visibility, filters and styles.
        try:
            ET.parse(path)
        except (ET.ParseError, OSError) as exc:
            raise SiliconPreviewError(f"could not read layer-properties file: {exc}") from exc
        return {}, path
    if not isinstance(layers, Mapping):
        raise SiliconPreviewError("layers must be a .lyp path or mapping keyed by 'layer/datatype'")
    result: dict[tuple[int, int], dict[str, Any]] = {}
    for key_text, config in layers.items():
        try:
            layer, datatype = (int(part) for part in str(key_text).split("/", 1))
        except (ValueError, TypeError):
            raise SiliconPreviewError(f"invalid layer key {key_text!r}; expected 'layer/datatype'") from None
        if layer < 0 or datatype < 0:
            raise SiliconPreviewError("layer and datatype numbers must be nonnegative")
        config = config if isinstance(config, Mapping) else {"color": config}
        result[(layer, datatype)] = dict(config)
    return result, None


def _cell_summary(top_cell: Any) -> tuple[list[dict[str, Any]], int]:
    """List unique cells/direct edges and count authored shapes once per cell."""
    pending = deque([top_cell])
    visited: set[int] = set()
    result: list[dict[str, Any]] = []
    shape_count = 0
    while pending:
        cell = pending.popleft()
        index = int(cell.cell_index())
        if index in visited:
            continue
        visited.add(index)
        instances = list(cell.each_inst())
        children = sorted({str(instance.cell.name) for instance in instances})
        result.append({"name": str(cell.name), "children": children})
        for layer_index in cell.layout().layer_indices():
            shape_count += int(cell.shapes(layer_index).size())
        for instance in instances:
            if int(instance.cell.cell_index()) not in visited:
                pending.append(instance.cell)
    return result, shape_count


def render_silicon_preview(
    layout: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    top: str | None = None,
    layers: Any = None,
    size: int = 1000,
) -> dict[str, Any]:
    """Render GDSII/OASIS geometry to a KLayout PNG image.

    ``layers`` may be a native KLayout ``.lyp`` file (loaded with its groups,
    filters, visibility and styling) or a mapping such as
    ``{"1/0": {"name": "active", "color": "#d9a441"}}``. The result is
    strictly a review preview; it is not verification or manufacturing output.
    """
    source = Path(layout).expanduser().resolve()
    destination = Path(output).expanduser().resolve()
    if not source.is_file():
        raise SiliconPreviewError(f"layout file does not exist: {source}")
    if destination == source:
        raise SiliconPreviewError("preview output must not overwrite the input layout")
    layer_config, layer_source = _layer_map(layers)
    if layer_source is not None and destination == layer_source:
        raise SiliconPreviewError("preview output must not overwrite the input layer-properties file")
    if destination.suffix.lower() != ".png":
        raise SiliconPreviewError("preview output must have a .png extension")
    if isinstance(size, bool) or not isinstance(size, int) or not 128 <= size <= 8192:
        raise SiliconPreviewError("size must be an integer from 128 through 8192 pixels")

    # Qt must use its headless platform in CI and server environments. This is
    # set before importing KLayout's rendering module, which initializes Qt.
    if not os.environ.get("DISPLAY") and os.name != "nt":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        import klayout.db as kdb
        import klayout.lay as klay
    except ImportError as exc:
        raise SiliconPreviewError("silicon preview requires KLayout; install the optional 'silicon' extra") from exc

    view = klay.LayoutView(options=klay.LayoutView.LV_NoGrid)
    try:
        index = view.load_layout(str(source))
    except Exception as exc:
        raise SiliconPreviewError(f"could not read layout: {exc}") from exc
    cellview = view.cellview(index)
    db = cellview.layout()
    roots = list(db.top_cells())
    if not roots:
        raise SiliconPreviewError("layout contains no top cells")
    if top is None:
        if len(roots) != 1:
            raise SiliconPreviewError("layout has multiple top cells; select one with top=<cell name>")
        selected = roots[0]
    else:
        selected = db.cell(top)
        if selected is None or all(int(selected.cell_index()) != int(root.cell_index()) for root in roots):
            available = ", ".join(sorted(str(cell.name) for cell in roots))
            raise SiliconPreviewError(f"top must name a root cell; available roots: {available}")

    bbox = selected.bbox()
    if bbox.empty() or bbox.width() <= 0 or bbox.height() <= 0:
        raise SiliconPreviewError(f"top cell {selected.name!r} has no physical geometry")
    dbu_um = float(db.dbu)
    if not math.isfinite(dbu_um) or dbu_um <= 0:
        raise SiliconPreviewError("layout has invalid database-unit metadata")
    bbox_um = [float(value) * dbu_um for value in (bbox.left, bbox.bottom, bbox.right, bbox.top)]
    dbbox = selected.dbbox()
    pad_um = max(float(dbbox.width()), float(dbbox.height())) / 24.0
    target = kdb.DBox(dbbox.left - pad_um, dbbox.bottom - pad_um, dbbox.right + pad_um, dbbox.top + pad_um)

    temporary: Path | None = None
    try:
        view.set_config("background-color", "#151a22")
        view.set_config("grid-visible", "false")
        view.set_config("grid-show-ruler", "false")
        view.set_config("text-visible", "false")
        cellview.set_cell(int(selected.cell_index()))
        view.max_hier()
        if layer_source is not None:
            view.load_layer_props(str(layer_source), index, True)
        else:
            # Set an explicit readable palette rather than depending on global
            # KLayout preferences; apply requested mappings by source layer.
            for layer_ref in view.each_layer():
                source_text = str(layer_ref.source)
                pair = source_text.split("@", 1)[0].split("/", 1)
                try:
                    key = (int(pair[0]), int(pair[1]))
                except (ValueError, IndexError):
                    continue
                config = layer_config.get(key, {})
                color = _layer_color(config.get("color"), _PALETTE[key[0] % len(_PALETTE)])
                layer_ref.fill_color = color
                layer_ref.frame_color = color
                layer_ref.name = str(config.get("name", f"L{key[0]}/{key[1]}"))
                layer_ref.visible = True
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".png", dir=destination.parent)
        os.close(fd)
        temporary = Path(temp_name)
        view.save_image_with_options(str(temporary), size, size, 1, 1, 1.0, target, False)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise SiliconPreviewError("KLayout did not produce a nonempty PNG")
        os.replace(temporary, destination)
        temporary = None
    except SiliconPreviewError:
        raise
    except Exception as exc:
        raise SiliconPreviewError(f"KLayout could not render the selected cell: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    hierarchy, shape_count = _cell_summary(selected)
    return {
        "path": str(destination),
        "sha256": _sha256(destination),
        "input_path": str(source),
        "input_sha256": _sha256(source),
        "top": str(selected.name),
        "bbox_um": bbox_um,
        "dbu_um": dbu_um,
        "size_px": [size, size],
        "shape_count": shape_count,
        "hierarchy": hierarchy,
        "layer_properties_path": str(layer_source) if layer_source else None,
        "role": "preview_only",
        "manufacturing_ready": False,
    }
