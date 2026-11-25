"""Locate and highlight one Tiny Tapeout project in a native KLayout die.

This is a review image only.  A marker is drawn over the provider layout in a
temporary in-memory layer; the source OAS/GDS is never modified.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from pathlib import Path
from typing import Any


class TTSlotDieError(ValueError):
    """Raised when the provider macro cannot be located unambiguously."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _macro_name(macro: str) -> str:
    if not isinstance(macro, str):
        raise TTSlotDieError("macro must be the exact published provider macro name")
    name = macro.strip()
    if not name or any(ch.isspace() for ch in name):
        raise TTSlotDieError("macro must be a nonempty provider macro name")
    return name


def _target_occurrences(root: Any, target_index: int, layout: Any) -> list[dict[str, Any]]:
    """Walk only hierarchy branches that can contain target; preserve arrays."""
    ancestors = {int(target_index)}
    pending = [int(target_index)]
    while pending:
        child = layout.cell(pending.pop())
        for parent in child.each_parent_inst():
            parent_index = int(parent.parent_cell_index())
            if parent_index not in ancestors:
                ancestors.add(parent_index)
                pending.append(parent_index)

    found: list[dict[str, Any]] = []

    def visit(cell: Any, transform: Any, chain: tuple[str, ...], active: frozenset[int]) -> None:
        cell_index = int(cell.cell_index())
        if len(found) >= 2:
            return
        if cell_index in active:
            return
        next_active = active | {cell_index}
        for inst in cell.each_inst():
            child = inst.cell
            child_index = int(child.cell_index())
            if child_index not in ancestors:
                continue
            child_name = str(child.name)
            path = chain + (child_name,)
            # each_cplx_trans expands regular arrays one physical instance at a time
            for array_index, local in enumerate(inst.cell_inst.each_cplx_trans()):
                if len(found) >= 2:
                    return
                combined = transform * local
                if child_index == target_index:
                    found.append({"transform": combined, "path": path, "array_index": array_index})
                else:
                    visit(child, combined, path, next_active)

    import klayout.db as kdb

    visit(root, kdb.CplxTrans(), (str(root.name),), frozenset())
    return found


def render_tt_slot(
    layout: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    macro: str,
    top: str | None = None,
    size: int = 1000,
) -> dict[str, Any]:
    """Render the full die and mark one unambiguously placed provider macro.

    ``macro`` is the exact published top name (for example
    ``tt_um_factory_test``). The
    renderer resolves the provider's ``JA_TO_`` wrapper when present, follows
    native KLayout hierarchy and instance arrays, and refuses zero or multiple
    physical occurrences instead of highlighting every cell of that type.
    """
    source = Path(layout).expanduser().resolve()
    destination = Path(output).expanduser().resolve()
    if not source.is_file():
        raise TTSlotDieError(f"layout file does not exist: {source}")
    if destination == source:
        raise TTSlotDieError("preview output must not overwrite the input layout")
    if destination.suffix.lower() != ".png":
        raise TTSlotDieError("preview output must have a .png extension")
    if isinstance(size, bool) or not isinstance(size, int) or not 128 <= size <= 8192:
        raise TTSlotDieError("size must be an integer from 128 through 8192 pixels")

    name = _macro_name(macro)
    try:
        import klayout.db as kdb
        import klayout.lay as klay
    except ImportError as exc:
        raise TTSlotDieError("Tiny Tapeout die preview requires KLayout; install the optional 'silicon' extra") from exc

    if not os.environ.get("DISPLAY") and os.name != "nt":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    view = klay.LayoutView(options=klay.LayoutView.LV_NoGrid)
    try:
        view_index = view.load_layout(str(source))
    except Exception as exc:
        raise TTSlotDieError(f"could not read provider layout: {exc}") from exc
    cellview = view.cellview(view_index)
    db = cellview.layout()
    roots = list(db.top_cells())
    if not roots:
        raise TTSlotDieError("layout contains no root cells")
    if top is None:
        if len(roots) != 1:
            raise TTSlotDieError("layout has multiple root cells; select one with top=<cell name>")
        selected = roots[0]
    else:
        selected = db.cell(top)
        if selected is None or all(int(selected.cell_index()) != int(root.cell_index()) for root in roots):
            raise TTSlotDieError("top must name a root cell in the provider layout")

    # Namespace wrapper names are the actual cells integrated by the TT
    # multiplexer. Fall back to the published top only for unwrapped layouts.
    wrapper_name = f"JA_TO_{name}"
    wrapper = db.cell(wrapper_name)
    exact = db.cell(name)
    if wrapper is None and exact is None:
        raise TTSlotDieError(f"published macro {name!r} is absent from the selected layout edition")
    target = wrapper if wrapper is not None else exact
    bbox = target.bbox()
    if bbox.empty() or bbox.width() <= 0 or bbox.height() <= 0:
        raise TTSlotDieError(f"macro cell {target.name!r} has no physical geometry")

    occurrences = _target_occurrences(selected, int(target.cell_index()), db)
    if not occurrences:
        raise TTSlotDieError(
            f"macro {name!r} exists in the layout library but has no instance below root {selected.name!r}"
        )
    if len(occurrences) != 1:
        raise TTSlotDieError(
            f"macro {name!r} has {len(occurrences)} physical occurrences below {selected.name!r}; "
            "a unique provider placement could not be established"
        )
    if wrapper is not None and exact is not None and int(wrapper.cell_index()) != int(exact.cell_index()):
        exact_occurrences = _target_occurrences(selected, int(exact.cell_index()), db)
        independent = [item for item in exact_occurrences if wrapper_name not in item["path"]]
        if independent:
            raise TTSlotDieError(
                f"both provider wrapper {wrapper_name!r} and exact top {name!r} have independent physical occurrences"
            )

    dbu = float(db.dbu)
    if not math.isfinite(dbu) or dbu <= 0:
        raise TTSlotDieError("layout has invalid database-unit metadata")
    occurrence = occurrences[0]
    transform = occurrence["transform"]
    global_box_dbu = bbox.transformed(transform)
    box_um = [float(v) * dbu for v in (global_box_dbu.left, global_box_dbu.bottom,
                                       global_box_dbu.right, global_box_dbu.top)]
    if not all(math.isfinite(value) for value in box_um):
        raise TTSlotDieError("macro instance transform produced invalid die coordinates")

    whole = selected.dbbox()
    if whole.empty() or whole.width() <= 0 or whole.height() <= 0:
        raise TTSlotDieError(f"root cell {selected.name!r} has no physical geometry")
    pad = max(float(whole.width()), float(whole.height())) / 24.0
    target_box = kdb.DBox(whole.left - pad, whole.bottom - pad, whole.right + pad, whole.top + pad)

    # Add a bright four-sided outline and crosshair to the root in the in-memory
    # view. Since it's in root coordinates it overlays the exact transformed
    # occurrence while every native die layer remains visible underneath.
    marker_layer = db.layer(kdb.LayerInfo(999, 0, "TT_ASSIGNED_SLOT_HIGHLIGHT"))
    shapes = selected.shapes(marker_layer)
    left, bottom, right, upper = box_um
    width = max(min(right - left, upper - bottom) * 0.12, 18.0)
    margin = width * 0.7
    shapes.insert(kdb.DBox(left - margin, bottom - margin, right + margin, bottom + width))
    shapes.insert(kdb.DBox(left - margin, upper - width, right + margin, upper + margin))
    shapes.insert(kdb.DBox(left - margin, bottom + width, left + width, upper - width))
    shapes.insert(kdb.DBox(right - width, bottom + width, right + margin, upper - width))
    cx, cy = (left + right) / 2.0, (bottom + upper) / 2.0
    cross = max(width * 1.5, 12.0)
    shapes.insert(kdb.DBox(cx - cross, cy - width / 2, cx + cross, cy + width / 2))
    shapes.insert(kdb.DBox(cx - width / 2, cy - cross, cx + width / 2, cy + cross))

    temporary: Path | None = None
    try:
        view.set_config("background-color", "#151a22")
        view.set_config("grid-visible", "false")
        view.set_config("grid-show-ruler", "false")
        view.set_config("text-visible", "false")
        cellview.set_cell(int(selected.cell_index()))
        view.max_hier()
        view.add_missing_layers()
        for layer_ref in view.each_layer():
            source_text = str(layer_ref.source).split("@", 1)[0].rsplit(" ", 1)[-1]
            pair = source_text.split("/", 1)
            if pair == ["999", "0"]:
                layer_ref.name = "Assigned project"
                layer_ref.fill_color = 0xFF6500
                layer_ref.frame_color = 0xFF6500
                layer_ref.visible = True
            else:
                # Keep the provider geometry readable while making the
                # assigned project marker the strongest color on the die.
                try:
                    layer_number = int(pair[0])
                except (ValueError, IndexError):
                    layer_number = 0
                gray = 72 + (layer_number * 11 % 52)
                muted = (gray << 16) | (gray << 8) | gray
                layer_ref.fill_color = muted
                layer_ref.frame_color = muted
                layer_ref.visible = True
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".png", dir=destination.parent)
        os.close(fd)
        temporary = Path(temp_name)
        view.save_image_with_options(str(temporary), size, size, 1, 1, 1.0, target_box, False)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise TTSlotDieError("KLayout did not produce a nonempty die preview")
        os.replace(temporary, destination)
        temporary = None
    except TTSlotDieError:
        raise
    except Exception as exc:
        raise TTSlotDieError(f"KLayout could not render the selected die: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return {
        "path": str(destination),
        "sha256": _sha256(destination),
        "input_sha256": _sha256(source),
        "top": str(selected.name),
        "macro": name,
        "layout_cell": str(target.name),
        "occurrence_count": 1,
        "hierarchy_path": list(occurrence["path"]),
        "array_index": int(occurrence["array_index"]),
        "bbox_um": box_um,
        "center_um": [(left + right) / 2.0, (bottom + upper) / 2.0],
        "dbu_um": dbu,
        "size_px": [size, size],
        "marker_layer": "999/0",
        "integrity": "native_layout_sha256_and_unique_hierarchy_occurrence",
        "role": "preview_only",
        "manufacturing_ready": False,
    }
