"""Standalone SVG wrapper for KiCad's native assembled-board render."""

from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .pcb_assembly_inventory import assembly_component_coverage
from .pcb_model_preview import _publish_png, _validate_png, render_board_model

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _error(reason: str, message: str | None = None, **details: Any) -> dict[str, Any]:
    return {"status": "error", "reason": reason, "message": message or reason.replace("_", " ").capitalize(), **details}


def _output_conflicts(output: Path, protected: list[Path]) -> bool:
    for path in protected:
        try:
            if output == path or (output.exists() and path.exists() and os.path.samefile(output, path)):
                return True
        except OSError:
            return True
    return False


def _input_snapshot(board: Path) -> dict[str, str | None]:
    """Hash the PCB and project inputs, retaining missing companions for drift checks."""
    paths = [
        board,
        board.with_suffix(".kicad_pro"),
        board.with_suffix(".kicad_dru"),
        board.parent / "fp-lib-table",
        board.parent / "sym-lib-table",
    ]
    snapshot: dict[str, str | None] = {}
    for path in paths:
        try:
            resolved = path.resolve(strict=False)
            snapshot[str(resolved)] = hashlib.sha256(resolved.read_bytes()).hexdigest() if resolved.is_file() else None
        except (OSError, RuntimeError):
            snapshot[str(path)] = None
    return snapshot


def _snapshot_matches(expected: dict[str, str | None]) -> bool:
    for raw_path, digest in expected.items():
        path = Path(raw_path)
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        except OSError:
            current = None
        if current != digest:
            return False
    return True


def _renderer_label(value: Any) -> str:
    match = re.fullmatch(r"(?:KiCad\s+)?(\d+(?:\.\d+){1,2})(?:\s+.*)?", str(value).strip())
    return f"KiCad CLI {match.group(1)}" if match else "KiCad native 3D renderer"


def render_board_assembly(
    board_path: str | Path,
    output_path: str | Path,
    *,
    kicad_cli: str | Path | None = None,
    side: str = "front",
    timeout_seconds: float = 120,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Embed an actual KiCad native 3D render in a self-contained SVG."""
    try:
        source = Path(board_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _error("invalid_board_path", str(exc))
    if not source.is_file() or source.suffix.lower() != ".kicad_pcb":
        return _error("invalid_board_path", "board_path must name an existing .kicad_pcb file")
    try:
        requested = Path(output_path).expanduser()
        output = requested.parent.resolve() / requested.name
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _error("invalid_output_path", str(exc))
    if output.suffix.lower() != ".svg":
        return _error("invalid_output_extension", "output_path must use the .svg extension")
    if not isinstance(side, str) or side not in {"front", "back"}:
        return _error("invalid_side", "side must be 'front' or 'back'")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return _error("invalid_timeout", "timeout_seconds must be finite and greater than zero")
    try:
        if not math.isfinite(timeout_seconds):
            return _error("invalid_timeout", "timeout_seconds must be finite and greater than zero")
    except (OverflowError, TypeError, ValueError):
        return _error("invalid_timeout", "timeout_seconds must be finite and greater than zero")
    if not isinstance(overwrite, bool):
        return _error("invalid_overwrite", "overwrite must be a boolean")

    protected = [source, source.with_suffix(".kicad_pro"), source.with_suffix(".kicad_dru")]
    if output.is_symlink() or _output_conflicts(output, protected):
        return _error(
            "output_conflicts_with_source", "SVG output must be a regular path distinct from the PCB and sidecars"
        )
    if output.exists() and not output.is_file():
        return _error("invalid_output_path", "output_path must be a file path")
    if output.exists() and not overwrite:
        return _error("output_exists", f"Output already exists: {output}", output_path=str(output))

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        source_snapshot = _input_snapshot(source)
        source_sha = source_snapshot.get(str(source))
        if not source_sha:
            return _error("board_read_failed", "Could not read the board source")
        try:
            board_text = source.read_text(encoding="utf-8")
            component_audit = assembly_component_coverage(board_text)
        except (OSError, UnicodeError, ValueError) as exc:
            return _error("component_coverage_unavailable", str(exc))
        expected_components = component_audit.get("expected")
        represented_components = component_audit.get("represented")
        missing_components = component_audit.get("missing")
        if (
            isinstance(expected_components, bool)
            or not isinstance(expected_components, int)
            or expected_components <= 0
            or represented_components != expected_components
            or missing_components != 0
            or component_audit.get("missing_references")
        ):
            return _error(
                "component_models_incomplete",
                "Every populated electrical footprint needs a visible 3D model",
                component_coverage=component_audit,
            )
        component_model_count = component_audit.get("model_declaration_total")
        component_coverage = {
            key: component_audit[key]
            for key in ("expected", "represented", "missing", "missing_references", "components")
        }
        with tempfile.TemporaryDirectory(prefix="cw-pcb-assembly-") as temp:
            staged_png = Path(temp) / "assembly.png"
            model = render_board_model(
                source,
                staged_png,
                kicad_cli=kicad_cli,
                side=side,
                timeout_seconds=timeout_seconds,
                overwrite=False,
            )
            if model.get("status") != "ok":
                return {
                    "status": "error",
                    "reason": model.get("reason", "native_render_failed"),
                    "message": model.get("message", "Native KiCad assembly rendering failed"),
                    "model_preview": model,
                }
            if model.get("source_sha256") != source_sha or not _snapshot_matches(source_snapshot):
                return _error(
                    "source_changed_during_render",
                    "PCB or project inputs changed during native rendering",
                    model_preview=model,
                )
            model_records = (model.get("model_coverage") or {}).get("models", [])
            protected.extend(Path(record["resolved_path"]) for record in model_records if record.get("resolved_path"))
            if _output_conflicts(output, protected):
                return _error(
                    "output_conflicts_with_source",
                    "SVG output aliases a PCB, sidecar, or referenced 3D model",
                    model_preview=model,
                )
            try:
                png = staged_png.read_bytes()
            except OSError as exc:
                return _error("render_output_missing", str(exc), model_preview=model)
            valid, diagnostic, width, height = _validate_png(png)
            if not valid or width is None or height is None:
                return _error("render_output_invalid_png", diagnostic, model_preview=model)
            png_sha = _sha256_bytes(png)
            if (
                model.get("png_sha256") != png_sha
                or model.get("png_width") != width
                or model.get("png_height") != height
                or model.get("side") != ("top" if side == "front" else "bottom")
            ):
                return _error(
                    "native_render_metadata_mismatch",
                    "Native PNG metadata does not match its image or requested side",
                    model_preview=model,
                )
            coverage = model.get("model_coverage") or {}
            counts = [coverage.get(key) for key in ("expected", "resolved", "missing")]
            if (
                any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts)
                or counts[0] <= 0
                or counts[0] != counts[1]
                or counts[2] != 0
            ):
                return _error(
                    "model_coverage_incomplete",
                    "Native renderer did not resolve every referenced model",
                    model_preview=model,
                )
            if len(model_records) != counts[0]:
                return _error(
                    "native_render_metadata_mismatch",
                    "Native model inventory count is inconsistent",
                    model_preview=model,
                )
            if component_model_count != counts[0]:
                return _error(
                    "native_render_metadata_mismatch",
                    "Board model declarations do not match native model inventory",
                    component_coverage=component_coverage,
                    model_preview=model,
                )
            for record in model_records:
                resolved_path = record.get("resolved_path")
                expected_sha = record.get("sha256")
                if not resolved_path or not expected_sha:
                    return _error(
                        "model_coverage_incomplete",
                        "Native model inventory contains an unresolved model",
                        model_preview=model,
                    )
                try:
                    actual_sha = hashlib.sha256(Path(resolved_path).read_bytes()).hexdigest()
                except OSError:
                    actual_sha = None
                if actual_sha != expected_sha:
                    return _error(
                        "source_changed_during_render",
                        "A referenced 3D model changed during rendering",
                        model_preview=model,
                    )
            model_preview = dict(model)
            model_preview.pop("output_path", None)
            model_preview["embedded"] = True
            model_summary = {key: coverage.get(key) for key in ("expected", "resolved", "missing")}
            root = ET.Element(
                f"{{{SVG_NS}}}svg",
                {
                    "version": "1.1",
                    "width": f"{width}px",
                    "height": f"{height}px",
                    "viewBox": f"0 0 {width} {height}",
                    "data-prototype": "true",
                    "data-appearance": "assembly",
                    "data-side": side,
                },
            )
            ET.SubElement(root, f"{{{SVG_NS}}}title").text = f"Assembled PCB preview ({side})"
            ET.SubElement(
                root, f"{{{SVG_NS}}}desc"
            ).text = "Native KiCad 3D render embedded at its original pixel aspect ratio."
            ET.SubElement(
                root,
                f"{{{SVG_NS}}}metadata",
                {
                    "source_sha256": source_sha,
                    "renderer": _renderer_label(model.get("cli_version", "")),
                    "appearance": "assembly",
                    "side": side,
                    "model_expected": str(model_summary["expected"]),
                    "model_resolved": str(model_summary["resolved"]),
                    "model_missing": str(model_summary["missing"]),
                    "component_expected": str(component_coverage["expected"]),
                    "component_represented": str(component_coverage["represented"]),
                    "component_missing": str(component_coverage["missing"]),
                    "png_sha256": png_sha,
                    "png_width": str(width),
                    "png_height": str(height),
                },
            )
            image = ET.SubElement(
                root,
                f"{{{SVG_NS}}}image",
                {
                    "id": "assembled-board-models",
                    "x": "0",
                    "y": "0",
                    "width": str(width),
                    "height": str(height),
                    "preserveAspectRatio": "xMidYMid meet",
                    "href": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                },
            )
            # Keep legacy SVG consumers working while the SVG2 href remains authoritative.
            image.set("{http://www.w3.org/1999/xlink}href", image.get("href", ""))
            svg_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            staged_svg = Path(temp) / "assembly.svg"
            staged_svg.write_bytes(svg_bytes)
            # Recheck board and renderer-reported model hashes immediately before publication.
            expected_sources = dict(source_snapshot)
            for raw_path, expected in (model.get("source_hashes") or {}).items():
                expected_sources.setdefault(raw_path, expected)
            for record in model_records:
                expected_sources[record["resolved_path"]] = record["sha256"]
            if not _snapshot_matches(expected_sources):
                return _error(
                    "source_changed_during_render",
                    "A referenced 3D model changed after native render",
                    model_preview=model,
                )
            publish_error = _publish_png(staged_svg, output, overwrite=overwrite)
            if publish_error:
                return _error("output_publish_failed", publish_error, model_preview=model)

            return {
                "status": "ok",
                "prototype": True,
                "appearance": "assembly",
                "source_sha256": source_sha,
                "svg_sha256": _sha256_bytes(svg_bytes),
                "path": str(output),
                "output_path": str(output),
                "side": side,
                "model_preview": model_preview,
                "model_coverage": model_summary,
                "component_coverage": component_coverage,
                "embedded_png_sha256": png_sha,
                "png_width": width,
                "png_height": height,
            }
    except (OSError, RuntimeError, ValueError) as exc:
        return _error("render_failed", str(exc))
