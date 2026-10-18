"""Run a real CLI/optional MCP demo and retain inspectable evidence.

Requires KiCad with pcbnew and Freerouting. Outputs are prototype artifacts;
the manufacturing export gate must still refuse an unapproved delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

REPO = Path(__file__).resolve().parents[1]
_BOARD_TARGETS = {"board", "board_outline", "pcb", "outline"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _design_board_size(design: dict, default: tuple[float, float] = (80.0, 60.0)) -> tuple[float, float]:
    """Resolve board dimensions with the same aliases and precedence as placement."""
    width, height = default
    constraints = design.get("pcb_constraints")
    if not isinstance(constraints, list):
        return width, height
    for raw in constraints:
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("target") or raw.get("ref") or "").strip().lower()
        nested = raw.get("board") if isinstance(raw.get("board"), dict) else {}
        explicit_board_keys = (
            "board_width_mm" in raw or "board_height_mm" in raw or bool(nested)
        )
        if target not in _BOARD_TARGETS and not explicit_board_keys:
            continue
        candidate_width = _finite_number(raw.get(
            "board_width_mm",
            nested.get("width_mm", raw.get("width_mm") if target in _BOARD_TARGETS else None),
        ))
        candidate_height = _finite_number(raw.get(
            "board_height_mm",
            nested.get("height_mm", raw.get("height_mm") if target in _BOARD_TARGETS else None),
        ))
        if candidate_width is not None:
            width = candidate_width
        if candidate_height is not None:
            height = candidate_height
    return width, height


def _apply_size_overrides(design: dict, width: float, height: float) -> None:
    """Make copied placement constraints agree with the prototype outline."""
    constraints = design.get("pcb_constraints")
    if constraints is None:
        constraints = []
    if not isinstance(constraints, list):
        raise RuntimeError("Cannot apply board-size overrides: pcb_constraints must be a list")

    for raw in constraints:
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("target") or raw.get("ref") or "").strip().lower()
        nested = raw.get("board") if isinstance(raw.get("board"), dict) else None
        explicit_board_keys = (
            "board_width_mm" in raw or "board_height_mm" in raw or bool(nested)
        )
        if target not in _BOARD_TARGETS and not explicit_board_keys:
            continue

        if "board_width_mm" in raw:
            raw["board_width_mm"] = width
        if "board_height_mm" in raw:
            raw["board_height_mm"] = height
        if nested is not None:
            nested["width_mm"] = width
            nested["height_mm"] = height
        if target in _BOARD_TARGETS:
            raw["width_mm"] = width
            raw["height_mm"] = height

    # A final canonical constraint ensures partial or conflicting duplicate
    # declarations cannot override either effective dimension later in the list.
    constraints.append({
        "kind": "placement", "target": "board", "width_mm": width, "height_mm": height,
    })
    design["pcb_constraints"] = constraints


def prepare_working_spec(source: Path, destination: Path, width: float | None,
                         height: float | None) -> dict:
    """Copy a design verbatim unless dimensions are explicitly overridden."""
    shutil.copy2(source, destination)
    original_bytes = source.read_bytes()
    design = yaml.safe_load(original_bytes.decode("utf-8-sig"))
    if not isinstance(design, dict):
        raise RuntimeError("Design specification must contain a YAML mapping")
    original_size = _design_board_size(design)
    effective_size = (width if width is not None else original_size[0],
                      height if height is not None else original_size[1])
    overridden = width is not None or height is not None
    if overridden:
        for name, value in (("width", effective_size[0]), ("height", effective_size[1])):
            if not math.isfinite(value) or value <= 0:
                raise RuntimeError(f"Effective board {name} must be a positive finite number")
        _apply_size_overrides(design, *effective_size)
        destination.write_text(yaml.safe_dump(design, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {
        "source_spec_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "working_spec_sha256": digest(destination),
        "board_size_original_mm": list(original_size),
        "board_size_effective_mm": list(effective_size),
        "board_size_overrides_mm": {"width_mm": width, "height_mm": height},
        "board_size_override_applied": overridden,
    }


def _payload_hash(files: dict[str, str]) -> str:
    hasher = hashlib.sha256()
    for name, content_hash in sorted(files.items()):
        hasher.update(f"{name}\0{content_hash}\n".encode("utf-8"))
    return hasher.hexdigest()


def verify_installed_wheel_payload(package_root: Path, wheel_path: Path) -> dict:
    """Require exact installed package bytes to match a wheel's package payload."""
    if not package_root.is_dir():
        raise RuntimeError(f"Installed package directory does not exist: {package_root}")
    if not wheel_path.is_file():
        raise RuntimeError(f"Expected wheel does not exist: {wheel_path}")

    expected: dict[str, str] = {}
    seen: set[str] = set()
    try:
        wheel_bytes = wheel_path.read_bytes()
        wheel_sha256 = hashlib.sha256(wheel_bytes).hexdigest()
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            for info in archive.infolist():
                name = info.filename
                if (not name or "\\" in name or name.startswith("/")
                        or PureWindowsPath(name).drive
                        or any(part in {".", ".."} for part in name.rstrip("/").split("/"))):
                    raise RuntimeError(f"Expected wheel contains an unsafe archive path: {name!r}")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise RuntimeError(f"Expected wheel contains a symbolic link: {name!r}")
                normalized = PurePosixPath(name).as_posix()
                if normalized in seen:
                    raise RuntimeError(f"Expected wheel contains a duplicate path: {name!r}")
                seen.add(normalized)
                if info.is_dir() or not normalized.startswith("circuit_weaver/"):
                    continue
                relative = normalized.removeprefix("circuit_weaver/")
                if not relative or relative.startswith("__pycache__/") or relative.endswith(".pyc"):
                    continue
                expected[relative] = hashlib.sha256(archive.read(info)).hexdigest()
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith("Expected wheel"):
            raise
        raise RuntimeError(f"Cannot inspect expected wheel: {exc}") from exc
    if not expected:
        raise RuntimeError("Expected wheel contains no circuit_weaver package payload")

    installed: dict[str, str] = {}
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Installed package contains a symbolic link: {path}")
        if path.is_dir():
            continue
        relative_path = path.relative_to(package_root)
        if "__pycache__" in relative_path.parts or path.suffix == ".pyc":
            continue
        if not path.is_file():
            raise RuntimeError(f"Installed package contains an unsupported payload entry: {path}")
        relative = relative_path.as_posix()
        installed[relative] = digest(path)

    missing = sorted(set(expected) - set(installed))
    unexpected = sorted(set(installed) - set(expected))
    changed = sorted(name for name in set(expected) & set(installed)
                     if expected[name] != installed[name])
    if missing or unexpected or changed:
        raise RuntimeError(
            "Installed package payload does not match expected wheel: "
            f"missing={missing}, unexpected={unexpected}, changed={changed}"
        )
    if digest(wheel_path) != wheel_sha256:
        raise RuntimeError("Expected wheel changed while its payload was being verified")
    return {
        "wheel_path": str(wheel_path.resolve()),
        "wheel_sha256": wheel_sha256,
        "package_path": str(package_root.resolve()),
        "payload_file_count": len(expected),
        "expected_payload_sha256": _payload_hash(expected),
        "installed_payload_sha256": _payload_hash(installed),
        "payload_matches": True,
    }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def exception_message(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", ())
    if nested:
        return "; ".join(exception_message(child) for child in nested)
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or "").strip()
        return f"{exc}: {detail}" if detail else str(exc)
    return str(exc)


def verify_routing(payload: dict, board: Path) -> None:
    """Accept only native DRC evidence bound to the exact final board bytes."""
    drc = payload.get("drc", {})
    require(payload.get("routing_complete") is True, "Routing is incomplete")
    require(payload.get("output_kind") == "routed_kicad_board", "Router returned no native board")
    require(drc.get("status") == "ok" and drc.get("passed") is True, "Native DRC did not pass")
    require(drc.get("blocker_count") == 0, "Native DRC has blockers")
    require(Path(drc.get("board", "")).resolve() == board.resolve(), "DRC refers to a different board")
    require(drc.get("board_sha256") == digest(board), "DRC does not match final board bytes")


def verify_preview(payload: dict, board: Path, svg: Path, side: str, appearance: str = "layers") -> None:
    """Bind a native preview to the exact routed board and SVG bytes."""
    require(payload.get("status") == "ok", "Native board preview failed")
    require(payload.get("source_sha256") == digest(board), "Preview refers to different board bytes")
    require(payload.get("svg_sha256") == digest(svg), "Preview hash does not match SVG bytes")
    require(payload.get("side") == side, "Preview shows the wrong board side")
    require(payload.get("prototype") is True, "Preview omitted its review-only classification")
    root = ET.parse(svg).getroot()
    require(root.tag == "{http://www.w3.org/2000/svg}svg", "Preview is not an SVG document")
    if appearance == "assembly":
        require(payload.get("appearance") == "assembly", "Preview does not report assembly appearance")
        require(root.get("data-appearance") == "assembly", "SVG is not marked as an assembly preview")
        require(root.get("data-side") == side, "Assembly SVG shows the wrong board side")
        require(payload.get("model_preview", {}).get("status") == "ok", "Assembly model render failed")
        model = payload["model_preview"]
        require(model.get("source_sha256") == digest(board), "Model render refers to different board bytes")
        coverage = model.get("model_coverage")
        require(isinstance(coverage, dict), "Assembly preview omitted model coverage")
        expected = coverage.get("expected")
        resolved = coverage.get("resolved")
        missing = coverage.get("missing")
        require(isinstance(expected, int) and not isinstance(expected, bool) and expected > 0,
                "Assembly preview found no native 3D models")
        require(isinstance(resolved, int) and not isinstance(resolved, bool),
                "Assembly model resolution count is invalid")
        require(isinstance(missing, int) and not isinstance(missing, bool),
                "Assembly missing model count is invalid")
        require(resolved == expected and missing == 0, "Assembly preview has missing or unresolved models")
        models = coverage.get("models")
        require(isinstance(models, list) and len(models) == expected,
                "Assembly model coverage inventory is incomplete")
        require(all(isinstance(item, dict) and item.get("resolved_path") for item in models),
                "Assembly model inventory contains an unresolved model")
        for item in models:
            model_path = Path(item["resolved_path"])
            require(model_path.is_file(), "Assembly model source file is missing")
            require(item.get("sha256") == digest(model_path), "Assembly model source changed after rendering")
        components = payload.get("component_coverage")
        require(isinstance(components, dict), "Assembly preview omitted component coverage")
        component_expected = components.get("expected")
        component_represented = components.get("represented")
        component_missing = components.get("missing")
        require(isinstance(component_expected, int) and not isinstance(component_expected, bool)
                and component_expected > 0, "Assembly preview found no populated electrical components")
        require(isinstance(component_represented, int) and not isinstance(component_represented, bool)
                and isinstance(component_missing, int) and not isinstance(component_missing, bool),
                "Assembly component coverage counts are invalid")
        require(component_represented == component_expected and component_missing == 0,
                "Assembly preview omits one or more populated electrical components")
        require(components.get("missing_references") == [],
                "Assembly preview has components without visible native models")
        component_records = components.get("components")
        require(isinstance(component_records, list), "Assembly component coverage inventory is missing")
        references = [str(item.get("reference", "")).strip() for item in component_records
                      if isinstance(item, dict)]
        require(len(references) == len(component_records) and all(references),
                "Assembly component coverage inventory is malformed")
        require(len(references) == len(set(references)), "Assembly component coverage has duplicate references")
        required_components = [item for item in component_records if item.get("required") is True]
        require(len(required_components) == component_expected,
                "Assembly required component inventory disagrees with expected count")
        require(all(isinstance(item.get("visible_model_count"), int)
                    and not isinstance(item.get("visible_model_count"), bool)
                    and item["visible_model_count"] > 0 for item in required_components),
                "Assembly preview omits one or more populated electrical components")
        require(all(item.get("required") is True or item.get("required") is False
                    for item in component_records), "Assembly component required flags are invalid")

        image_nodes = [node for node in root.iter() if node.tag == "{http://www.w3.org/2000/svg}image"]
        require(len(image_nodes) == 1 and image_nodes[0].get("id") == "assembled-board-models",
                "Assembly SVG must contain exactly one embedded model image")
        image_href = image_nodes[0].get("href") or image_nodes[0].get("{http://www.w3.org/1999/xlink}href")
        require(isinstance(image_href, str) and image_href.startswith("data:image/png;base64,"),
                "Assembly model image is not embedded as a PNG data URL")
        xlink_href = image_nodes[0].get("{http://www.w3.org/1999/xlink}href")
        require(xlink_href is None or xlink_href == image_href,
                "Assembly SVG has conflicting SVG2 and xlink image references")
        try:
            png_bytes = base64.b64decode(image_href.partition(",")[2], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("Assembly model image contains invalid base64") from exc
        require(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"), "Embedded assembly image is not a PNG")
        offset = 8
        width = height = None
        seen_idat = seen_iend = False
        while offset < len(png_bytes):
            require(len(png_bytes) - offset >= 12, "Embedded assembly PNG has a truncated chunk")
            length = struct.unpack_from(">I", png_bytes, offset)[0]
            end = offset + 8 + length
            require(end + 4 <= len(png_bytes), "Embedded assembly PNG has a truncated chunk")
            chunk_type = png_bytes[offset + 4:offset + 8]
            chunk_data = png_bytes[offset + 8:end]
            crc = struct.unpack_from(">I", png_bytes, end)[0]
            require((zlib.crc32(chunk_type + chunk_data) & 0xffffffff) == crc,
                    "Embedded assembly PNG has an invalid chunk checksum")
            if offset == 8:
                require(chunk_type == b"IHDR" and length == 13,
                        "Embedded assembly PNG has an invalid header")
                width, height = struct.unpack_from(">II", chunk_data)
            if chunk_type == b"IDAT":
                seen_idat = True
            if chunk_type == b"IEND":
                require(length == 0 and end + 4 == len(png_bytes),
                        "Embedded assembly PNG has invalid trailing data")
                seen_iend = True
                break
            offset = end + 4
        require(seen_idat and seen_iend, "Embedded assembly PNG is incomplete")
        require(width is not None and height is not None, "Embedded assembly PNG has no dimensions")
        require(width > 0 and height > 0, "Embedded assembly PNG has invalid dimensions")
        png_hash = hashlib.sha256(png_bytes).hexdigest()
        require(payload.get("embedded_png_sha256") == png_hash,
                "Embedded assembly PNG hash does not match renderer evidence")
        require(model.get("png_sha256") == png_hash, "Model render hash does not match embedded PNG bytes")
        require(model.get("png_width") == width and model.get("png_height") == height,
                "Model render dimensions do not match embedded PNG bytes")
        require(payload.get("png_width") == width and payload.get("png_height") == height,
                "Assembly SVG dimensions do not match embedded PNG bytes")
        expected_model_side = "top" if side == "front" else "bottom"
        require(model.get("side") == expected_model_side, "Model render shows the wrong board side")
        require(root.get("width") == f"{width}px" and root.get("height") == f"{height}px",
                "Assembly SVG frame dimensions do not match the embedded PNG")
        require(root.get("viewBox") == f"0 0 {width} {height}",
                "Assembly SVG viewBox does not match the embedded PNG")
        require(image_nodes[0].get("x") == "0" and image_nodes[0].get("y") == "0"
                and image_nodes[0].get("width") == str(width)
                and image_nodes[0].get("height") == str(height)
                and image_nodes[0].get("preserveAspectRatio", "xMidYMid meet").endswith("meet"),
                "Assembly image does not preserve the PNG aspect ratio in the SVG frame")
        metadata_nodes = [node for node in root.iter()
                          if node.tag == "{http://www.w3.org/2000/svg}metadata"]
        require(len(metadata_nodes) == 1, "Assembly SVG metadata is missing or duplicated")
        metadata = metadata_nodes[0]
        require(metadata.get("source_sha256") == digest(board), "Assembly SVG metadata refers to different board bytes")
        require(metadata.get("png_sha256") == png_hash, "Assembly SVG metadata PNG hash is stale")
        require(metadata.get("png_width") == str(width) and metadata.get("png_height") == str(height),
                "Assembly SVG metadata dimensions are stale")
        require(metadata.get("side") == side and metadata.get("appearance") == "assembly",
                "Assembly SVG metadata has the wrong view")
        require(metadata.get("model_expected") == str(expected)
                and metadata.get("model_resolved") == str(resolved)
                and metadata.get("model_missing") == str(missing),
                "Assembly SVG metadata model counts are stale")
        require(metadata.get("component_expected") == str(component_expected)
                and metadata.get("component_represented") == str(component_represented)
                and metadata.get("component_missing") == str(component_missing),
                "Assembly SVG metadata component counts are stale")
        require(payload.get("output_path") and Path(payload["output_path"]).resolve() == svg.resolve(),
                "Assembly preview points to a different SVG")
        for node in root.iter():
            for name, value in node.attrib.items():
                if name.rsplit("}", 1)[-1] in {"href", "src"}:
                    require(value.startswith("data:image/png;base64,") or value.startswith("#"),
                            "Assembly SVG contains an external resource reference")
        svg_text = svg.read_text(encoding="utf-8")
        require(not re.search(r"url\(\s*(?!['\"]?#)(?:https?:|file:|//|[^)]+)", svg_text, re.I),
                "Assembly SVG contains an external CSS resource reference")
    else:
        require(appearance == "layers", f"Unsupported preview appearance: {appearance}")
        require(payload.get("outline_count", 0) > 0, "Preview has no actual board outline")
        require(bool(payload.get("layers")), "Preview has no native layer inventory")
        model = payload.get("model_preview")
        if model is not None:
            require(model.get("status") == "ok", "Requested model preview failed")
            require(model.get("source_sha256") == digest(board), "Model render refers to different board bytes")
            png = Path(model.get("output_path", ""))
            require(png.is_file() and png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"),
                    "Model render is not a PNG")
            require(model.get("png_sha256") == digest(png), "Model render hash does not match PNG bytes")


def verify_blocked_assembly_delivery(payload: dict, delivery_dir: Path) -> dict:
    """Verify blocked delivery still contains a complete, reconciled BOM."""
    require(payload.get("status") == "blocked", "Prototype assembly export was unexpectedly authorized")
    require(payload.get("fabrication_ready") is False, "Assembly export overclaimed fabrication readiness")

    bom_path = delivery_dir / "bom_jlcpcb.csv"
    assembly_path = delivery_dir / "assembly_manifest.json"
    delivery_path = delivery_dir / "delivery_manifest.json"
    for path in (bom_path, assembly_path, delivery_path):
        require(path.is_file(), f"Assembly delivery artifact is missing: {path.name}")

    assembly = json.loads(assembly_path.read_text(encoding="utf-8"))
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    items = assembly.get("items")
    require(isinstance(items, list), "Assembly manifest has no item inventory")
    references = [str(item.get("reference", "")).strip() for item in items if isinstance(item, dict)]
    require(len(references) == len(items), "Assembly manifest contains a malformed item")
    require(all(references), "Assembly manifest contains an empty reference")
    require(len(references) == len(set(references)), "Assembly manifest contains duplicate references")

    expected_refs = {
        str(item["reference"]).strip()
        for item in items
        if not item.get("dnp") and not item.get("exclude_from_bom")
    }
    require(assembly.get("item_count") == len(items), "Assembly manifest item count is inconsistent")
    require(payload.get("assembly_item_count") == len(items), "CLI assembly item count disagrees with its manifest")
    require(delivery.get("assembly_item_count") == len(items), "Delivery item count disagrees with assembly manifest")
    require(delivery.get("status") == "blocked", "Delivery manifest did not retain blocked status")
    require(delivery.get("fabrication_ready") is False, "Delivery manifest overclaimed fabrication readiness")

    artifacts = delivery.get("artifacts")
    require(isinstance(artifacts, list), "Delivery manifest has no artifact inventory")
    bom_artifacts = [item for item in artifacts if item.get("kind") == "bom"]
    require(len(bom_artifacts) == 1 and bom_artifacts[0].get("status") == "ready",
            "Delivery manifest does not mark exactly one BOM as ready")
    require(Path(bom_artifacts[0].get("path", "")).name == bom_path.name,
            "Delivery manifest points to a different BOM")
    cpl_artifacts = [item for item in artifacts if item.get("kind") == "cpl"]
    require(len(cpl_artifacts) == 1 and cpl_artifacts[0].get("status") == "blocked",
            "Delivery manifest did not preserve the blocked CPL gate")

    with bom_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        require(reader.fieldnames == ["Comment", "Designator", "Footprint", "LCSC Part#"],
                "JLCPCB BOM has an unexpected header")
        rows = list(reader)
    actual_refs: list[str] = []
    for row in rows:
        designators = [reference.strip() for reference in (row.get("Designator") or "").split(",")]
        require(all(designators), "JLCPCB BOM contains an empty designator")
        actual_refs.extend(designators)
    require(len(actual_refs) == len(set(actual_refs)), "JLCPCB BOM contains duplicate designators")
    require(set(actual_refs) == expected_refs,
            f"JLCPCB BOM designators disagree with active assembly inventory: "
            f"missing={sorted(expected_refs - set(actual_refs))}, "
            f"unexpected={sorted(set(actual_refs) - expected_refs)}")
    require(len(actual_refs) == len(expected_refs), "JLCPCB BOM designator count is inconsistent")
    require(payload.get("bom_rows") == len(rows), "CLI BOM row count disagrees with the CSV")
    return {"assembly_items": len(items), "bom_rows": len(rows), "bom_designators": len(actual_refs)}


class Demo:
    def __init__(self, output: Path, timeout: int, *, installed: bool = False):
        self.output = output
        self.timeout = timeout
        self.steps: list[dict] = []
        self.cwd = output if installed else REPO
        self.env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO / "src")}
        if installed:
            self.env.pop("PYTHONPATH", None)

    def command(self, name: str, arguments: list[str], *, json_output=True, exit_code=0) -> dict:
        command = [sys.executable, "-m", "circuit_weaver", *map(str, arguments)]
        return self.external(name, command, json_output=json_output, exit_code=exit_code)

    def external(self, name: str, command: list[str], *, json_output=False, exit_code=0) -> dict:
        print(f"[{name}] starting", flush=True)
        step = {"name": name, "command": command, "passed": False}
        self.steps.append(step)
        started = time.monotonic()
        try:
            process = subprocess.run(command, cwd=self.cwd, env=self.env, capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=self.timeout)
            step.update(exit_code=process.returncode, seconds=round(time.monotonic() - started, 2))
            (self.output / f"{name}.stdout.log").write_text(process.stdout, encoding="utf-8")
            (self.output / f"{name}.stderr.log").write_text(process.stderr, encoding="utf-8")
            expected_codes = {exit_code} if isinstance(exit_code, int) else set(exit_code)
            require(process.returncode in expected_codes,
                    f"{name}: exit {process.returncode}, expected {exit_code}; see stderr/stdout logs")
            payload = json.loads(process.stdout) if json_output else {}
            step["passed"] = True
            print(f"[{name}] passed ({step['seconds']}s)", flush=True)
            return payload
        except Exception as exc:
            step["error"] = str(exc)
            raise

    def report(self, summary: dict) -> None:
        summary["steps"] = self.steps
        (self.output / "evidence.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        rows = "".join(
            f"<tr><td>{html.escape(s['name'])}</td><td>{'PASS' if s['passed'] else 'FAIL'}</td>"
            f"<td>{s.get('seconds', '')}</td><td><a href='{s['name']}.stdout.log'>output</a> "
            f"<a href='{s['name']}.stderr.log'>diagnostics</a></td></tr>" for s in self.steps
        )
        preview_views = [
            ("assembly", "Assembly view", "board-preview.svg",
             "Native 3D component models embedded in a standalone SVG.")
            if summary.get("preview_appearance") == "assembly" else
            ("front", "Front layers", "board-preview.svg",
             "Actual copper, mask openings, silkscreen and footprint body outlines from KiCad."),
        ]
        if (self.output / "board-layers.svg").is_file():
            preview_views.append(("front-layers", "Front layers", "board-layers.svg",
                                  "Native copper, mask, silkscreen and footprint geometry from KiCad."))
        preview_views.append(("back", "Back layers", "board-preview-back.svg",
                              "Back-side native geometry, mirrored as viewed from beneath the board."))
        available = [view for view in preview_views if (self.output / view[2]).is_file()]
        buttons = "".join(
            f'<button type="button" data-board-view="{key}" aria-pressed="{str(i == 0).lower()}">{label}</button>'
            for i, (key, label, _, _) in enumerate(available)
        )
        panels = "".join(
            f'<figure id="board-view-{key}" class="board-view" {"" if i == 0 else "hidden"}>'
            f'<figcaption>{caption} <a href="{filename}" target="_blank" rel="noopener">Open full size</a></figcaption>'
            f'<img src="{filename}" alt="{label} of the routed prototype board"></figure>'
            for i, (key, label, filename, caption) in enumerate(available)
        )
        quality = summary.get("placement_quality", {})
        dimensions = summary.get("board_size_mm", [])
        layout_summary = (
            f'<p>{dimensions[0]:g} × {dimensions[1]:g} mm · '
            f'{len(quality.get("support_distances", []))} owner/support links · '
            f'{len(quality.get("overlaps", []))} courtyard overlaps · '
            f'{summary.get("silkscreen", {}).get("unresolved_count", "unchecked")} unresolved references</p>'
        ) if len(dimensions) == 2 else ""
        previews = (
            '<section aria-label="Board preview"><h2>Board preview</h2>'
            '<p>Engineering preview from the routed board. These views do not grant manufacturing approval.</p>'
            f'{layout_summary}<div class="board-controls" role="group" aria-label="Board view">'
            f'{buttons}</div>{panels}</section>'
        ) if available else ""
        schematic = "".join(
            f"<li><a href='{p.relative_to(self.output).as_posix()}'>{html.escape(p.name)}</a></li>"
            for p in sorted((self.output / "generated").rglob("*.svg"))
        )
        page = f"""<!doctype html><meta charset="utf-8"><title>Circuit Weaver workflow demo</title>
<style>body{{font:16px system-ui;background:#101827;color:#e3ecf7;margin:32px auto;max-width:1100px}}
a{{color:#79d3ff}}h1{{font-size:30px}}.badge{{color:#8ce8b0}}table{{width:100%;border-collapse:collapse}}
td,th{{padding:12px;border-bottom:1px solid #33445c;text-align:left}}
figure{{margin:24px 0}}code{{overflow-wrap:anywhere}}p{{line-height:1.6}}
.board-controls{{display:flex;gap:8px;flex-wrap:wrap}}button{{font:inherit;padding:10px 16px;border:1px solid #4a647c;
border-radius:8px;background:#17283b;color:#e3ecf7;cursor:pointer}}button[aria-pressed="true"]{{background:#276652}}
.board-view img{{display:block;width:100%;height:auto;max-height:78vh;object-fit:contain;background:#0b1220;
border-radius:10px}}figcaption{{margin:12px 0;color:#b9cbdc}}[hidden]{{display:none!important}}
details{{padding:14px 0;border-block:1px solid #33445c}}summary{{cursor:pointer;color:#79d3ff}}
@media(max-width:1164px){{body{{margin:20px}}}}</style>
<h1>Circuit Weaver · automated workflow</h1><p class="badge">{html.escape(summary['status'])}</p>
{previews}<details><summary>Workflow details and downloads</summary>
<p>Real KiCad schematic export, library footprints, placement, Freerouting, native DRC and delivery gates.
Manufacturing authorization remains <strong>not ready</strong>: identity evidence and placement approval
have not been supplied. A blocked manufacturing export is an expected successful gate check.</p>
<p>Source revision: <code>{html.escape(summary.get('revision', 'unknown'))}</code>.
<a href="evidence.json">Machine-readable evidence</a> · <a href="routed.kicad_pcb">Routed PCB</a> ·
<a href="generated/placement_editor.html">Placement review</a> ·
<a href="generated/confidence_report.html">Confidence report</a> ·
<a href="delivery/delivery_manifest.json">Delivery manifest</a> ·
<a href="prototype-positions.csv">Prototype positions (review only)</a></p></details>
<h2>Workflow evidence</h2>
<table><tr><th>Step</th><th>Check result</th><th>Seconds</th><th>Evidence</th></tr>{rows}</table>
<h2>Schematic sheets</h2><ul>{schematic}</ul>
<p>{html.escape(summary.get('error', ''))}</p>
<script>document.querySelectorAll('[data-board-view]').forEach(button => {{
button.addEventListener('click', () => {{
document.querySelectorAll('[data-board-view]').forEach(item => {{
item.setAttribute('aria-pressed', String(item === button)); }});
document.querySelectorAll('.board-view').forEach(panel => {{
panel.hidden = panel.id !== 'board-view-' + button.dataset.boardView; }});
}}); }});</script>"""
        (self.output / "index.html").write_text(page, encoding="utf-8")


async def mcp_smoke(spec: Path, output: Path, env: dict) -> dict:
    """Exercise the real JSON-RPC stdio server, including error propagation."""
    import yaml
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    transcript = []
    parameters = StdioServerParameters(command=sys.executable,
                                      args=["-m", "circuit_weaver.mcp_server"], cwd=str(output), env=env)
    with (output / "mcp.stderr.log").open("w", encoding="utf-8") as errors:
        async with stdio_client(parameters, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                inventory = await session.list_tools()
                names = sorted(tool.name for tool in inventory.tools)
                require(set(names) == {"generate_artifacts", "validate_design", "discover_projects",
                                       "research_component", "manufacturing_readiness"}, "MCP inventory mismatch")
                spec_json = json.dumps(yaml.safe_load(spec.read_text(encoding="utf-8")))
                for name, arguments in (
                    ("validate_design", {"spec_json": spec_json}),
                    ("generate_artifacts", {"spec_json": spec_json, "output_dir": str(output / "mcp-generated")}),
                    ("discover_projects", {"root_dir": str(output), "depth": 2}),
                ):
                    response = await session.call_tool(name, arguments)
                    require(not response.isError, f"MCP {name} failed: {response.content}")
                    payload = json.loads("\n".join(b.text for b in response.content if hasattr(b, "text")))
                    transcript.append({"tool": name, "result": payload})
                    if name in {"validate_design", "generate_artifacts"}:
                        require(payload.get("valid") is True, f"MCP {name} returned invalid design")
                    if name == "generate_artifacts":
                        require(payload.get("kicad_verified") is True, "MCP generation skipped native KiCad")
                bad = await session.call_tool("validate_design", {"spec_json": "{invalid"})
                require(bad.isError is True, "MCP invalid input did not set isError")
                research = await session.call_tool("research_component", {"query": ""})
                require(research.isError is True, "MCP empty research query did not set isError")
                readiness = await session.call_tool("manufacturing_readiness", {
                    "readiness_path": str(output / "missing-readiness.json")
                })
                require(readiness.isError is True, "MCP missing readiness was treated as manufacturing approval")
    result = {"tools": names, "calls": transcript, "invalid_input_is_error": True,
              "empty_research_is_error": True, "missing_readiness_is_error": True}
    (output / "mcp-transcript.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=REPO / "samples/led_power_indicator/led_power_indicator.yaml")
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory; existing output is refused")
    parser.add_argument("--iterations", type=int, default=1500)
    parser.add_argument("--width", type=float, help="Prototype board width in mm; defaults to design constraint or 80")
    parser.add_argument("--height", type=float,
                        help="Prototype board height in mm; defaults to design constraint or 60")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--route-timeout", type=int, default=180)
    parser.add_argument("--clearance", type=float, default=0.127,
                        help="Prototype default clearance in mm (0.127 accommodates the sample USB connector)")
    parser.add_argument("--mcp-smoke", action="store_true")
    parser.add_argument("--render-3d", action="store_true", help="Require a native assembly-model PNG (KiCad 9+)")
    parser.add_argument("--installed", action="store_true",
                        help="Test this Python's installed package, not checkout src/")
    parser.add_argument("--expected-wheel", type=Path,
                        help="Require the installed package payload to exactly match this wheel")
    args = parser.parse_args()
    for name, value in (("--width", args.width), ("--height", args.height)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"{name} must be a positive finite number")
    if args.expected_wheel is not None and not args.installed:
        parser.error("--expected-wheel requires --installed")
    if args.expected_wheel is not None and not args.expected_wheel.is_file():
        parser.error(f"--expected-wheel does not exist: {args.expected_wheel}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_spec = args.spec.resolve()
    spec = output / "design.yaml"
    demo = Demo(output, max(300, args.route_timeout + 120), installed=args.installed)
    summary = {"status": "FAILED", "spec": str(source_spec), "fabrication_ready": False}
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True)
        summary["revision"] = revision.stdout.strip()
        changes = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                                 capture_output=True, text=True, check=True)
        summary["source_dirty"] = bool(changes.stdout.strip())
        package_probe = subprocess.run(
            [sys.executable, "-c", "import circuit_weaver; print(circuit_weaver.__file__)"],
            cwd=demo.cwd, env=demo.env, capture_output=True, text=True, check=True,
        )
        package_root = Path(package_probe.stdout.strip()).parent
        require(not args.installed or not package_root.resolve().is_relative_to(REPO / "src"),
                "Installed-mode probe imported checkout source")
        summary["package_path"] = str(package_root)
        summary["package_mode"] = "installed" if args.installed else "source"
        if args.expected_wheel is not None:
            summary["wheel_payload_verification"] = verify_installed_wheel_payload(
                package_root, args.expected_wheel.resolve()
            )
        source_files = [p for p in package_root.rglob("*")
                        if p.suffix in {".py", ".json"}]
        package_hash = hashlib.sha256()
        for path in sorted(source_files):
            package_hash.update(f"{path.relative_to(package_root).as_posix()}:{digest(path)}\n".encode())
        summary["package_source_sha256"] = package_hash.hexdigest()
        summary["runner_sha256"] = digest(Path(__file__))
        original = digest(source_spec)
        summary.update(prepare_working_spec(source_spec, spec, args.width, args.height))
        width, height = summary["board_size_effective_mm"]
        demo.command("doctor", ["doctor"], json_output=False)
        valid = demo.command("validate", ["validate", str(spec)])
        require(valid.get("valid") is True, "Static design validation failed")
        generated = demo.command("generate", ["generate", str(spec), "--require-kicad",
                                              "-o", str(output / "generated")])
        require(generated.get("kicad_verified") is True, "Generation skipped native KiCad verification")
        root = Path(generated["root_schematic"])
        demo.command("confidence", ["confidence", str(spec), "--run-sims", "--json",
                                    "-o", str(output / "generated/confidence_report.html")])
        built = demo.command("build", ["build-pcb", str(root), "-o", str(output / "unplaced.kicad_pcb"),
                                      "--clearance", str(args.clearance), "--width", str(width),
                                      "--height", str(height)])
        require(built.get("fabrication_ready") is False, "Prototype board incorrectly claims fabrication readiness")
        summary["prototype_clearance_mm"] = args.clearance
        placement = demo.command("place", ["place-pcb", str(output / "unplaced.kicad_pcb"),
                                          "-o", str(output / "placed.kicad_pcb"),
                                          "--iterations", str(args.iterations), "--seed", "7",
                                          "--design", str(spec), "--tidy-references"])
        require(placement["placement"]["quality"]["review_required"] is False, "Placement constraints were violated")
        summary["layout_review"] = placement["placement"].get("layout_review", {})
        summary["placement_quality"] = placement["placement"]["quality"]
        summary["board_size_mm"] = [width, height]
        summary["silkscreen"] = placement["placement"].get("silkscreen", {})
        require(summary["silkscreen"].get("unresolved_count") == 0,
                "Native reference cleanup left unresolved collisions")
        require(summary["silkscreen"].get("remaining_conflict_count") == 0,
                "Native silkscreen conflict check did not pass")
        routed = output / "routed.kicad_pcb"
        routing = demo.command("route", ["autoroute", str(output / "placed.kicad_pcb"), "--routed-board", str(routed),
                                         "--attempts", str(args.attempts), "--timeout", str(args.route_timeout),
                                         "--max-passes", "20", "--optimization-threads", "0"])
        verify_routing(routing, routed)
        summary["drc"] = routing["drc"]
        delivery = demo.command("assembly-gate", ["export-jlcpcb", str(spec), "--pcb", str(routed),
                                            "-o", str(output / "delivery")], exit_code=(0, 1))
        assembly_summary = verify_blocked_assembly_delivery(delivery, output / "delivery")
        reasons = delivery.get("blocked_reasons", [])
        require(bool(reasons) and all("identity handoff bundle" in r or "no LCSC part number" in r
                                     for r in reasons), f"Unexpected assembly blocker: {reasons}")
        require(not delivery.get("cpl") and not list((output / "delivery").glob("cpl*.csv")),
                "Unapproved assembly export published a CPL")
        summary["assembly_delivery"] = {**delivery, "verified_inventory": assembly_summary}
        refused = demo.command("manufacturing-gate", ["export-gerbers", str(routed),
                                                     "-o", str(output / "manufacturing")], exit_code=1)
        require(refused.get("status") == "blocked", "Unapproved manufacturing export was not blocked")
        require(not (output / "manufacturing").exists(), "Blocked export created a manufacturing directory")
        status = demo.command("status", ["status", str(output / "generated"), "--json"])
        resumed = demo.command("resume", ["resume", str(output / "generated"), "--json"])
        require(status.get("has_manifest") is True and status.get("validation", {}).get("passed") is True,
                "Generation project state was not persisted")
        require(bool(status.get("project_id")) and resumed.get("project_id") == status["project_id"],
                "Resume did not restore the generated project")
        cli = shutil.which("kicad-cli") or next(
            (str(p) for version in ("10.0", "9.0", "8.0")
             if (p := Path(f"C:/Program Files/KiCad/{version}/bin/kicad-cli.exe")).is_file()), None
        )
        require(cli is not None, "KiCad CLI disappeared")
        demo.external("prototype-positions", [str(cli), "pcb", "export", "pos", "--format", "csv",
                                               "--units", "mm", "-o", str(output / "prototype-positions.csv"),
                                               str(routed)])
        summary["previews"] = {}
        summary["preview_appearance"] = "assembly" if args.render_3d else "layers"
        preview_jobs = [("front", "board-preview.svg", "assembly" if args.render_3d else "layers"),
                        ("back", "board-preview-back.svg", "layers")]
        if args.render_3d:
            preview_jobs.append(("front-layers", "board-layers.svg", "layers"))
        for key, filename, appearance in preview_jobs:
            preview = output / filename
            side = "back" if key == "back" else "front"
            payload = demo.command(f"board-preview-{key}", [
                "preview-pcb", str(routed), "-o", str(preview), "--side", side,
                "--kicad-cli-path", str(cli), "--appearance", appearance,
            ])
            verify_preview(payload, routed, preview, side, appearance)
            summary["previews"][key] = payload
        if args.mcp_smoke:
            step = {"name": "mcp", "passed": False}
            demo.steps.append(step)
            started = time.monotonic()
            result = asyncio.run(asyncio.wait_for(mcp_smoke(spec, output, demo.env), timeout=120))
            step.update(passed=True, seconds=round(time.monotonic() - started, 2))
            (output / "mcp.stdout.log").write_text(json.dumps(result, indent=2), encoding="utf-8")
        require(digest(source_spec) == original, "Workflow modified the input design")
        require(digest(spec) == summary["working_spec_sha256"],
                "Workflow modified the copied design specification")
        verify_routing(routing, routed)
        summary.update(status="PASS — prototype workflow validated; manufacturing export correctly blocked",
                       source_unchanged=True, board_sha256=digest(routed))
    except Exception as exc:
        summary["error"] = exception_message(exc)
        print(f"FAILED: {summary['error']}", file=sys.stderr)
    finally:
        demo.report(summary)
    print(f"Evidence: {output / 'index.html'}", flush=True)
    return 0 if summary["status"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
