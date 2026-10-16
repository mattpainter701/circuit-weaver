"""Create a geometry-accurate, non-manufacturing PCB preview SVG.

The preview uses KiCad's own layer plotter for pad, copper and footprint
geometry.  A small native Python helper extracts the board polygon so the
mask can be filled while retaining real Edge.Cuts cutouts and NPTH holes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

_NATIVE_POLYGON_SCRIPT = r'''
import json, sys
import pcbnew

board = pcbnew.LoadBoard(sys.argv[1])
if board is None:
    raise RuntimeError("KiCad could not load the isolated board copy")
poly = pcbnew.SHAPE_POLY_SET()
try:
    major = int(pcbnew.Version().split(".", 1)[0])
except (TypeError, ValueError):
    raise RuntimeError("Could not determine KiCad Python version for board polygon extraction")
# KiCad 8/9 do not expose the v10 infer-outline argument. Keep NPTH as
# explicit drill geometry in the native layer SVG rather than as board holes.
if major >= 10:
    candidates = ((poly, False, None, False, False), (poly, False))
else:
    candidates = ((poly, None, False, False), (poly,))
for args in candidates:
    try:
        if not board.GetBoardPolygonOutlines(*args):
            raise RuntimeError("KiCad could not resolve a closed Edge.Cuts outline")
        break
    except TypeError:
        continue
else:
    raise RuntimeError(f"KiCad {major} board polygon API is unsupported")
if poly.IsNull() or poly.OutlineCount() == 0:
    raise RuntimeError("Board has no closed Edge.Cuts outline")

def contour(chain):
    points = chain.PointCount()
    return [[pcbnew.ToMM(chain.CPoint(i).x), pcbnew.ToMM(chain.CPoint(i).y)]
            for i in range(points)]

outlines = []
for i in range(poly.OutlineCount()):
    holes = [contour(poly.Hole(i, j)) for j in range(poly.HoleCount(i))]
    outlines.append({"points": contour(poly.Outline(i)), "holes": holes})
box = poly.BBox()
print("CW_PREVIEW_POLYGON=" + json.dumps({
    "version": pcbnew.Version(),
    "bounds": {"x": pcbnew.ToMM(box.GetX()), "y": pcbnew.ToMM(box.GetY()),
               "width": pcbnew.ToMM(box.GetWidth()), "height": pcbnew.ToMM(box.GetHeight())},
    "outlines": outlines,
}))
'''

_LAYER_COLORS: dict[str, tuple[str, str]] = {
    "F.Mask": ("#b99a54", "#b99a54"),
    "B.Mask": ("#b99a54", "#b99a54"),
    "F.Cu": ("#e4bd72", "#e4bd72"),
    "B.Cu": ("#e4bd72", "#e4bd72"),
    "F.Silkscreen": ("#f0eee5", "#f0eee5"),
    "B.Silkscreen": ("#f0eee5", "#f0eee5"),
    "F.Fab": ("#cbd9cd", "#cbd9cd"),
    "B.Fab": ("#cbd9cd", "#cbd9cd"),
    "Edge.Cuts": ("none", "#a8d3b1"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_hashes(source: Path) -> dict[str, str | None]:
    return {
        suffix: _sha256(path) if path.is_file() else None
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
        for path in (source.with_suffix(suffix),)
    }


def _find_cli(explicit: str | Path | None) -> str | None:
    if explicit is not None:
        candidate = str(explicit)
        return shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
    configured = os.getenv("CIRCUIT_WEAVER_KICAD_CLI") or os.getenv("KICAD_CLI")
    if configured:
        return shutil.which(configured) or (configured if Path(configured).is_file() else None)
    candidates = [shutil.which("kicad-cli") or ""]
    candidates.extend(f"C:/Program Files/KiCad/{version}/bin/kicad-cli.exe" for version in ("10.0", "9.0", "8.0"))
    return next(
        (candidate for candidate in candidates if candidate and (shutil.which(candidate) or Path(candidate).is_file())),
        None,
    )


def _version(cli: str, timeout: float) -> tuple[str, int]:
    result = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=min(timeout, 20))
    if result.returncode:
        raise RuntimeError(f"Could not query KiCad CLI version: {(result.stderr or result.stdout)[-500:]}")
    version = (result.stdout.strip() or result.stderr.strip()).splitlines()[0]
    match = re.search(r"\b(8|9|10)\.(\d+)", version)
    if not match:
        raise RuntimeError(f"Supported KiCad versions are 8–10; detected {version!r}")
    return version, int(match.group(1))


def _run_native_polygon(board: Path, python_path: str | Path | None, timeout: float) -> dict[str, Any]:
    from .kicad_bridge import find_kicad_python

    executable = find_kicad_python(python_path)
    if executable is None:
        raise RuntimeError("KiCad Python (pcbnew) is unavailable for native Edge.Cuts extraction")
    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False) as helper:
        helper.write(_NATIVE_POLYGON_SCRIPT)
        helper_path = Path(helper.name)
    try:
        process = subprocess.run([executable, str(helper_path), str(board)], capture_output=True,
                                 text=True, timeout=timeout)
    finally:
        helper_path.unlink(missing_ok=True)
    for line in reversed(process.stdout.splitlines()):
        if line.startswith("CW_PREVIEW_POLYGON="):
            if process.returncode:
                raise RuntimeError(f"KiCad outline extraction failed: {(process.stderr or process.stdout)[-500:]}")
            return json.loads(line.partition("=")[2])
    detail = (process.stderr or process.stdout)[-800:]
    raise RuntimeError(f"KiCad outline extraction failed: {detail or 'no polygon result'}")


def _copy_isolated_board(source: Path, folder: Path) -> Path:
    copy = folder / source.name
    shutil.copy2(source, copy)
    for suffix in (".kicad_pro", ".kicad_dru"):
        companion = source.with_suffix(suffix)
        if companion.is_file():
            shutil.copy2(companion, copy.with_suffix(suffix))
    return copy


def _parse_svg(path: Path) -> tuple[ET.Element, list[ET.Element]]:
    root = ET.parse(path).getroot()
    view_box = root.get("viewBox")
    if not view_box:
        raise RuntimeError(f"KiCad SVG has no viewBox: {path.name}")
    return root, list(root)


def _is_white(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"white", "#fff", "#ffffff"}:
        return True
    if normalized.startswith("#") and len(normalized) == 7:
        try:
            red, green, blue = (int(normalized[index:index + 2], 16) for index in (1, 3, 5))
            return min(red, green, blue) >= 248
        except ValueError:
            return False
    return False


def _style_element(
    element: ET.Element,
    fill: str,
    stroke: str,
    drill_fill: str = "#10271b",
    *,
    white_is_drill: bool = True,
    mark_drill: bool = True,
) -> None:
    style = element.get("style")
    if style:
        parts = [item.strip() for item in style.split(";") if item.strip()]
        updated: list[str] = []
        for item in parts:
            key, separator, value = item.partition(":")
            if not separator:
                updated.append(item)
                continue
            key = key.strip()
            old = value.strip()
            if key == "fill" and old.lower() != "none":
                if _is_white(old) and white_is_drill:
                    value = drill_fill
                    if mark_drill:
                        element.set("data-drill-hole", "true")
                else:
                    value = fill
            elif key == "stroke" and old.lower() != "none":
                if _is_white(old) and white_is_drill:
                    value = drill_fill
                    if mark_drill:
                        element.set("data-drill-hole", "true")
                else:
                    value = stroke
            updated.append(f"{key}:{value.strip()}")
        element.set("style", "; ".join(updated))
    for child in element:
        _style_element(child, fill, stroke, drill_fill,
                       white_is_drill=white_is_drill, mark_drill=mark_drill)


def _svg_point(point: list[float], board_bounds: dict[str, float], view_box: tuple[float, ...]) -> tuple[float, float]:
    x0, y0, width, height = view_box
    if board_bounds["width"] <= 0 or board_bounds["height"] <= 0:
        raise RuntimeError("KiCad returned invalid board bounds")
    # KiCad exports layer SVGs at 1 SVG unit per millimetre. Keep that exact
    # scale; the page-size SVG can be a fraction narrower than the physical
    # Edge.Cuts bbox because KiCad reserves line-width margins.
    return (x0 + point[0] - board_bounds["x"], y0 + point[1] - board_bounds["y"])


def _polygon_path(polygon: dict[str, Any], board_bounds: dict[str, float], view_box: tuple[float, ...]) -> str:
    commands: list[str] = []
    for contour in [polygon["points"], *polygon["holes"]]:
        if len(contour) < 3:
            continue
        mapped = [_svg_point(point, board_bounds, view_box) for point in contour]
        commands.append(f"M {mapped[0][0]:.5f},{mapped[0][1]:.5f}")
        commands.extend(f"L {x:.5f},{y:.5f}" for x, y in mapped[1:])
        commands.append("Z")
    return " ".join(commands)


def _verify_native_edge_anchor(
    edge_root: ET.Element, polygon: dict[str, Any], view_box: tuple[float, ...], tolerance_mm: float = 0.002,
) -> tuple[float, float]:
    """Measure the layer plotter's constant translation from native board coordinates.

    A translation is accepted only when multiple distinct SVG path starts align
    with multiple physical polygon vertices under the same offset.
    """
    move_points: list[tuple[float, float]] = []
    for element in edge_root.iter():
        if element.tag.rsplit("}", 1)[-1] != "path":
            continue
        data = element.get("d", "")
        for match in re.finditer(r"(?:^|\s)M\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)", data):
            move_points.append((float(match.group(1)), float(match.group(2))))
    if not move_points:
        raise RuntimeError("KiCad Edge.Cuts SVG contains no native coordinate anchor")

    x0, y0, _width, _height = view_box
    bounds = polygon["bounds"]
    anchors = [
        (x0 + point[0] - bounds["x"], y0 + point[1] - bounds["y"])
        for outline in polygon["outlines"]
        for point in outline["points"]
    ]
    offsets = [
        (round(x - ax, 4), round(y - ay, 4))
        for x, y in move_points
        for ax, ay in anchors
    ]
    best_offset: tuple[float, float] | None = None
    best_match_count = 0
    for dx, dy in offsets:
        matched_pairs = {
            ((x, y), (ax, ay))
            for x, y in move_points
            for ax, ay in anchors
            if abs((x - dx) - ax) <= tolerance_mm and abs((y - dy) - ay) <= tolerance_mm
        }
        matched_moves = {move for move, _anchor in matched_pairs}
        matched_anchors = {anchor for _move, anchor in matched_pairs}
        match_count = min(len(matched_moves), len(matched_anchors))
        if match_count > best_match_count:
            best_offset, best_match_count = (dx, dy), match_count
    if best_offset is None or best_match_count < 2:
        raise RuntimeError("Native Edge.Cuts SVG coordinates do not align with multiple pcbnew board vertices")
    return best_offset


def _publish_staged_file(staging: Path, destination: Path, *, overwrite: bool) -> None:
    if overwrite:
        os.replace(staging, destination)
        return
    # Same-directory hard-link publication is atomic and refuses a name that
    # appeared after preflight, including a symlink or another file type.
    os.link(staging, destination)


def _remove_fabrication_text(element: ET.Element) -> None:
    """Keep native body geometry without duplicate assembly text overlays.

    KiCad plots glyph strokes in explicitly marked groups alongside invisible
    text nodes. Only fabrication layers use this filter; printed silkscreen
    references remain native and visible.
    """
    for child in list(element):
        if (child.tag.rsplit("}", 1)[-1] == "text"
                or "stroked-text" in child.get("class", "").split()):
            element.remove(child)
        else:
            _remove_fabrication_text(child)


def _build_svg_export_command(
    cli: str, board: Path, layer: str, output: Path, major: int,
) -> list[str]:
    command = [cli, "pcb", "export", "svg"]
    if major >= 9:
        command.append("--mode-single")
    command.extend([
        "--black-and-white",
        "--exclude-drawing-sheet",
        "--page-size-mode",
        "2",
        "--layers",
        layer,
        "--drill-shape-opt",
        "2",
        "-o",
        str(output),
        str(board),
    ])
    return command


def render_board_preview(
    board_path: str | Path,
    output_path: str | Path,
    *,
    kicad_cli: str | Path | None = None,
    kicad_python_path: str | Path | None = None,
    side: str = "front",
    timeout_seconds: float = 120,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Render top-down front or mirrored physical back view from native board geometry."""
    source = Path(board_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if side not in {"front", "back"}:
        return {"status": "error", "message": "side must be 'front' or 'back'"}
    if not source.is_file() or source.suffix.lower() != ".kicad_pcb":
        return {"status": "error", "message": "board_path must name an existing .kicad_pcb file"}
    if output.suffix.lower() != ".svg":
        return {"status": "error", "message": "output_path must use the .svg extension"}
    try:
        timeout_valid = (
            not isinstance(timeout_seconds, bool)
            and isinstance(timeout_seconds, (int, float))
            and math.isfinite(timeout_seconds)
            and timeout_seconds > 0
        )
    except (OverflowError, TypeError, ValueError):
        timeout_valid = False
    if not timeout_valid:
        return {"status": "error", "message": "timeout_seconds must be finite and greater than zero"}
    if not isinstance(overwrite, bool):
        return {"status": "error", "message": "overwrite must be a boolean"}
    protected_paths = [source, source.with_suffix(".kicad_pro"), source.with_suffix(".kicad_dru")]
    if output == source or any(
        path.is_file() and output.exists() and os.path.samefile(output, path) for path in protected_paths
    ):
        return {
            "status": "error",
            "message": "Preview output must be distinct from the PCB and its project/rules sidecars",
        }
    if output.exists() and not output.is_file():
        return {"status": "error", "message": f"Preview output path is not a file: {output}"}
    if output.exists() and not overwrite:
        return {"status": "error", "message": f"Preview output already exists: {output}"}
    cli = _find_cli(kicad_cli)
    if cli is None:
        return {"status": "error", "message": "kicad-cli is unavailable; KiCad 8, 9, or 10 is required"}

    inputs_before = _source_hashes(source)
    source_hash = inputs_before[".kicad_pcb"]
    staging: Path | None = None
    layers = (["Edge.Cuts", "F.Mask", "F.Cu", "F.Silkscreen", "F.Fab"] if side == "front"
              else ["Edge.Cuts", "B.Mask", "B.Cu", "B.Silkscreen", "B.Fab"])
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cw-pcb-preview-") as folder_name:
            folder = Path(folder_name)
            board_copy = _copy_isolated_board(source, folder)
            version, major = _version(cli, timeout_seconds)
            plotted: dict[str, Path] = {}
            for layer in layers:
                layer_path = folder / f"{layer.replace('.', '_')}.svg"
                command = _build_svg_export_command(cli, board_copy, layer, layer_path, major)
                process = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
                if process.returncode != 0 or not layer_path.is_file():
                    detail = (process.stderr or process.stdout)[-800:]
                    raise RuntimeError(f"KiCad SVG export failed for {layer}: {detail or 'no SVG produced'}")
                plotted[layer] = layer_path

            polygon = _run_native_polygon(board_copy, kicad_python_path, timeout_seconds)
            edge_root, _ = _parse_svg(plotted["Edge.Cuts"])
            view_box_values = tuple(float(value) for value in edge_root.get("viewBox", "").split())
            if len(view_box_values) != 4 or view_box_values[2] <= 0 or view_box_values[3] <= 0:
                raise RuntimeError("KiCad Edge.Cuts SVG has an invalid viewBox")
            native_view_box = view_box_values
            offset_x, offset_y = _verify_native_edge_anchor(edge_root, polygon, native_view_box)
            # Use the exact native outline dimensions as the viewport. KiCad's
            # page-size viewBox may include a constant plot translation and
            # text margins. Measure the translation from native vertices and
            # crop using the physical outline's exact dimensions.
            view_box = (native_view_box[0] + offset_x, native_view_box[1] + offset_y,
                        polygon["bounds"]["width"], polygon["bounds"]["height"])
            root = ET.Element(f"{{{SVG_NS}}}svg", {
                "version": "1.1",
                "width": f"{view_box[2]:.5f}mm",
                "height": f"{view_box[3]:.5f}mm",
                "viewBox": " ".join(f"{value:.5f}" for value in view_box),
                "data-side-view": "native" if side == "front" else "mirrored-physical",
                "data-prototype": "true",
                "data-native-svg-offset-x-mm": f"{offset_x:.5f}",
                "data-native-svg-offset-y-mm": f"{offset_y:.5f}",
            })
            defs = ET.SubElement(root, f"{{{SVG_NS}}}defs")
            ET.SubElement(defs, f"{{{SVG_NS}}}style").text = (
                ".board-silhouette{fill:#176b43;fill-rule:evenodd;stroke:none}"
                ".board-outline{fill:none;stroke:#a8d3b1;stroke-width:.18;vector-effect:non-scaling-stroke}"
            )
            ET.SubElement(root, f"{{{SVG_NS}}}title").text = (
                f"PCB prototype preview — {'front' if side == 'front' else 'back, mirrored physical view'}"
            )
            metadata = ET.SubElement(root, f"{{{SVG_NS}}}metadata", {
                "source_sha256": source_hash,
                "source_file": source.name,
                "kicad_version": version,
                "side": side,
                "prototype": "true",
            })
            metadata.text = "Generated from an isolated copy using native KiCad SVG layers and board polygons."

            x0, y0, width, _height = view_box
            if side == "back":
                content = ET.SubElement(root, f"{{{SVG_NS}}}g", {
                    "id": "mirrored-physical-back-view",
                    "transform": f"translate({2*x0+width:.5f} 0) scale(-1 1)",
                })
            else:
                content = root

            silhouette = ET.SubElement(content, f"{{{SVG_NS}}}path", {
                "id": "board-silhouette",
                "class": "board-silhouette",
                "d": " ".join(_polygon_path(item, polygon["bounds"], view_box) for item in polygon["outlines"]),
                "fill-rule": "evenodd",
                "data-outline-count": str(len(polygon["outlines"])),
                "data-hole-count": str(sum(len(item["holes"]) for item in polygon["outlines"])),
            })
            silhouette.tail = "\n  "

            for layer in layers:
                layer_root, children = _parse_svg(plotted[layer])
                if layer_root.get("viewBox") != edge_root.get("viewBox"):
                    raise RuntimeError(f"KiCad layer SVG bounds differ for {layer}")
                fill, stroke = _LAYER_COLORS[layer]
                white_is_drill = layer in {"F.Cu", "B.Cu", "F.Mask", "B.Mask"}
                mark_drill = layer in {"F.Cu", "B.Cu"}
                group = ET.SubElement(content, f"{{{SVG_NS}}}g", {
                    "id": f"native-layer-{layer.replace('.', '-').lower()}",
                    "data-layer": layer,
                })
                for child in children:
                    if child.tag.rsplit("}", 1)[-1] in {"title", "desc"}:
                        continue
                    cloned = ET.fromstring(ET.tostring(child, encoding="unicode"))
                    if layer in {"F.Fab", "B.Fab"}:
                        # Wrap the child so a top-level text/glyph group can
                        # be filtered just like a nested native plot group.
                        wrapper = ET.Element("wrapper")
                        wrapper.append(cloned)
                        _remove_fabrication_text(wrapper)
                        if not len(wrapper):
                            continue
                    _style_element(cloned, fill, stroke, white_is_drill=white_is_drill,
                                   mark_drill=mark_drill)
                    group.append(cloned)

            drill_geometry_count = sum(
                1 for element in root.iter() if element.get("data-drill-hole") == "true"
            )

            outline_path = ET.SubElement(content, f"{{{SVG_NS}}}path", {
                "id": "board-outline",
                "class": "board-outline",
                "d": " ".join(_polygon_path(item, polygon["bounds"], view_box) for item in polygon["outlines"]),
                "data-outline-count": str(len(polygon["outlines"])),
                "data-hole-count": str(sum(len(item["holes"]) for item in polygon["outlines"])),
            })
            outline_path.tail = "\n"

            # Keep the label outside the mirrored geometry so it remains
            # readable while making the physical viewing side explicit.
            side_label = ET.SubElement(root, f"{{{SVG_NS}}}text", {
                "id": "side-view-label",
                "x": f"{view_box[0] + 1:.3f}",
                "y": f"{view_box[1] + 1.4:.3f}",
                "font-size": "1.0",
                "font-family": "sans-serif",
                "fill": "#e8f0e9",
            })
            side_label.text = "Front view" if side == "front" else "Back view · mirrored physical view"

            fd, stage_name = tempfile.mkstemp(
                prefix=f".{output.stem}.cw-stage-{uuid.uuid4().hex}-", suffix=".svg", dir=output.parent,
            )
            os.close(fd)
            staging = Path(stage_name)
            ET.ElementTree(root).write(staging, encoding="utf-8", xml_declaration=True)
            if inputs_before != _source_hashes(source):
                raise RuntimeError("Source PCB, project, or rules bytes changed during preview generation")
            _publish_staged_file(staging, output, overwrite=overwrite)
        return {
            "status": "ok",
            "path": str(output),
            "output_path": str(output),
            "source_sha256": source_hash,
            "input_sha256": inputs_before,
            "svg_sha256": _sha256(output),
            "prototype": True,
            "kicad_version": version,
            "layers": layers,
            "fabrication_text": "omitted; native silkscreen references preserved",
            "board_bounds_mm": polygon["bounds"],
            "native_svg_offset_mm": {"x": offset_x, "y": offset_y},
            "side": side,
            "frame": "native" if side == "front" else "mirrored-physical",
            "outline_count": len(polygon["outlines"]),
            "cutout_count": sum(len(item["holes"]) for item in polygon["outlines"]),
            "drill_geometry_count": drill_geometry_count,
        }
    except (OSError, ValueError, ET.ParseError, subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError) as exc:
        message = str(exc)
        try:
            inputs_changed = inputs_before != _source_hashes(source)
        except OSError:
            inputs_changed = True
        if inputs_changed:
            message = "Source PCB, project, or rules bytes changed during preview generation"
        return {"status": "error", "message": message, "source_sha256": source_hash, "prototype": True}
    finally:
        if staging is not None:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass

