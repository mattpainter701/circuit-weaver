"""Build a review-only, pad-bearing PCB from a KiCad schematic netlist.

Electrical symbols require exact symbol-pin/footprint-pad identity. A narrow,
explicitly mechanical/fiducial zero-pin symbol may retain its physical footprint
only when the exported netlist contains no node for it. This does not assert
component identity or manufacturing readiness.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .drc_runner import _kicad_cli_path, run_drc
from .evidence import EvidenceLedger
from .footprint_lib import KiCadFootprintLibrary, _parse_kicad_sexp
from .kicad_bridge import find_kicad_python
from .pcb_handoff import _pcb_output_lock, _publish_staged_transaction

_SHEET_FILE_RE = re.compile(r'\(property\s+"Sheetfile"\s+"([^"\\]*(?:\\.[^"\\]*)*)"', re.IGNORECASE)
_PINLESS_PHYSICAL_SYMBOLS = {("Mechanical", "MountingHole"), ("Mechanical", "Fiducial")}
_NATIVE_BUILD_BASE_TIMEOUT_SECONDS = 90
_NATIVE_BUILD_MAX_TIMEOUT_SECONDS = 600


def _native_build_timeout_seconds(component_count: int, sheet_count: int) -> int:
    """Return a hard-capped KiCad timeout scaled to the exported design size."""
    if (
        isinstance(component_count, bool)
        or not isinstance(component_count, int)
        or component_count < 0
        or isinstance(sheet_count, bool)
        or not isinstance(sheet_count, int)
        or sheet_count < 1
    ):
        raise ValueError("Native build component and sheet counts must be nonnegative and positive integers")
    scaled = _NATIVE_BUILD_BASE_TIMEOUT_SECONDS + math.ceil(component_count * 0.25) + 3 * (sheet_count - 1)
    return min(_NATIVE_BUILD_MAX_TIMEOUT_SECONDS, scaled)


def _annotation_errors_present(output: str) -> bool:
    for line in output.splitlines():
        if not re.search(r"annotation\s+errors?", line, re.IGNORECASE):
            continue
        if re.search(r"\bno\s+(?:schematic\s+)?annotation\s+errors?\b", line, re.IGNORECASE):
            continue
        if re.search(r"\bannotation\s+errors?\s*[:=]\s*(?:0|none)\b", line, re.IGNORECASE):
            continue
        if re.search(r"\b0\s+annotation\s+errors?\b", line, re.IGNORECASE):
            continue
        return True
    return False


def _xml_text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _footprint_pad_records(text: str) -> list[dict]:
    """Read direct pad declarations, retaining exact non-plated hole geometry."""
    tree = _parse_kicad_sexp(text)
    if not tree or tree[0] not in {"footprint", "module"}:
        raise ValueError("Footprint has no valid KiCad root")
    records = []
    verify_npth = any(isinstance(x, list) and len(x) >= 4 and x[0] == 'pad'
                      and x[1] and x[2] == 'np_thru_hole' for x in tree[1:])
    for node in tree[1:]:
        if not isinstance(node, list) or not node or node[0] != "pad":
            continue
        if len(node) < 4 or not all(isinstance(x, str) for x in node[1:4]):
            raise ValueError("Malformed footprint pad declaration")
        item = {"number": node[1], "kind": node[2]}
        if node[2] == "np_thru_hole" and verify_npth:
            def field(key, pad=node):
                values = [x[1:] for x in pad[4:] if isinstance(x, list) and x and x[0] == key]
                if len(values) != 1:
                    raise ValueError(f"NPTH pad requires one {key} declaration")
                return values[0]

            def numeric(values):
                try:
                    result = [round(float(x), 6) for x in values]
                except (TypeError, ValueError) as exc:
                    raise ValueError("Invalid NPTH pad geometry") from exc
                if not all(math.isfinite(x) for x in result):
                    raise ValueError("Nonfinite NPTH pad geometry")
                return result

            if node[3] not in {"circle", "oval"}:
                raise ValueError("Unsupported NPTH pad shape; cannot verify native normalization")
            at, size, drill = field("at"), field("size"), field("drill")
            if len(at) not in {2, 3} or len(size) != 2:
                raise ValueError("Invalid NPTH pad position or size")
            offsets = [x for x in drill if isinstance(x, list)]
            atoms = [x for x in drill if not isinstance(x, list)]
            offset = [0.0, 0.0]
            if offsets:
                if len(offsets) != 1 or offsets[0][0] != "offset" or len(offsets[0]) != 3:
                    raise ValueError("Unsupported NPTH drill geometry")
                offset = numeric(offsets[0][1:])
            if atoms and atoms[0] == "oval" and len(atoms) == 3:
                drill_size = numeric(atoms[1:])
            elif len(atoms) == 1:
                drill_size = numeric(atoms * 2)
            else:
                raise ValueError("Unsupported NPTH drill declaration")
            xy, wh = numeric(at[:2]), numeric(size)
            angle = numeric(at[2:])[0] % 360 if len(at) == 3 else 0.0
            if min(*wh, *drill_size) <= 0:
                raise ValueError("NPTH pad and drill sizes must be positive")
            item["geometry"] = [node[3], *xy, *wh, *drill_size, round(angle, 6), *offset]
        records.append(item)
    return records


def _normalized_npth_numbers(reference, records, symbol_pins, nets_root, physical_only, *, symbol_nc_pins=()):
    npth = [x for x in records if x["kind"] == "np_thru_hole"]
    numbers = {x["number"] for x in npth if x["number"]}
    if not numbers:
        return [], []
    conductive = {x["number"] for x in records if x["kind"] != "np_thru_hole" and x["number"]}
    if numbers & conductive:
        raise ValueError(f"{reference}: numbered NPTH hole collides with an electrical pad number")
    for number in sorted(numbers):
        if physical_only:
            continue
        nodes = [(net, node) for net in nets_root.findall("net") for node in net.findall("node")
                 if (node.get("ref") or "").strip() == reference and (node.get("pin") or "").strip() == number]
        if (number not in symbol_pins or number not in symbol_nc_pins or len(nodes) != 1 or
                len(nodes[0][0].findall("node")) != 1 or
                "no_connect" not in {x.strip() for x in (nodes[0][1].get("pintype") or "").lower().split("+")}):
            raise ValueError(
                f"{reference}.{number}: numbered NPTH hole requires a no-connect symbol pin "
                "and explicit singleton no-connect"
            )
    return sorted(numbers), npth


def _parse_netlist(netlist: Path, library: KiCadFootprintLibrary) -> dict[str, Any]:
    if netlist.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("KiCad netlist exceeds the 32 MiB safety limit")
    raw_xml = netlist.read_bytes()
    if b"<!DOCTYPE" in raw_xml.upper() or b"<!ENTITY" in raw_xml.upper():
        raise ValueError("KiCad netlist must not declare a document type or entity")
    try:
        root = ET.fromstring(raw_xml)
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"KiCad produced an invalid netlist: {exc}") from exc
    component_root = root.find("components")
    nets_root = root.find("nets")
    if component_root is None or nets_root is None:
        raise ValueError("KiCad netlist is missing components or nets")

    components: dict[str, dict[str, Any]] = {}
    references_with_net_nodes = {
        (node.get("ref") or "").strip()
        for node in nets_root.findall("./net/node")
        if (node.get("ref") or "").strip()
    }
    for raw in component_root.findall("comp"):
        reference = (raw.get("ref") or "").strip()
        if not reference or reference in components:
            raise ValueError("KiCad netlist contains an empty or duplicate component reference")
        # KiCad's native XML exporter encodes (on_board no) as a named
        # property, not an on_board attribute. Logical topology symbols may
        # still appear in the netlist to carry their schematic connectivity.
        excluded_from_board = any(
            prop.get("name") == "exclude_from_board" for prop in raw.findall("property")
        )
        if excluded_from_board or raw.get("on_board", "yes").lower() == "no":
            continue
        footprint = _xml_text(raw.find("footprint"))
        if not footprint:
            raise ValueError(f"{reference}: schematic component has no footprint binding")
        path = library.resolve(footprint)
        if path is None:
            raise ValueError(f"{reference}: unresolved KiCad footprint {footprint!r}")
        footprint_text = path.read_text(encoding="utf-8")
        pad_records = _footprint_pad_records(footprint_text)
        pad_numbers = {row['number'] for row in pad_records if row['number']}
        symbol_pins = {
            (pin.get("num") or "").strip()
            for pin in raw.findall("./units/unit/pins/pin")
            if (pin.get("num") or "").strip()
        }
        if not symbol_pins:
            libsource = raw.find("libsource")
            libparts = root.find("libparts")
            libpart = next(
                (
                    candidate
                    for candidate in (libparts.findall("libpart") if libparts is not None else [])
                    if candidate.get("lib") == (libsource.get("lib") if libsource is not None else None)
                    and candidate.get("part") == (libsource.get("part") if libsource is not None else None)
                ),
                None,
            )
            symbol_pins = {
                (pin.get("num") or "").strip()
                for pin in (libpart.findall("./pins/pin") if libpart is not None else [])
                if (pin.get("num") or "").strip()
            }
        physical_only = False
        if not symbol_pins:
            libsource = raw.find("libsource")
            symbol_identity = (
                libsource.get("lib") if libsource is not None else None,
                libsource.get("part") if libsource is not None else None,
            )
            if (symbol_identity in _PINLESS_PHYSICAL_SYMBOLS
                    and reference not in references_with_net_nodes):
                physical_only = True
            else:
                raise ValueError(
                    f"{reference}: schematic netlist has no symbol pin numbers; only an unconnected "
                    "Mechanical:MountingHole or Mechanical:Fiducial may be retained as physical-only"
                )
        if not physical_only and symbol_pins != pad_numbers:
            missing_pads = sorted(symbol_pins - pad_numbers)
            extra_pads = sorted(pad_numbers - symbol_pins)
            raise ValueError(
                f"{reference}: symbol pin numbers do not exactly match footprint pads "
                f"(missing pads: {missing_pads}; extra pads: {extra_pads})"
            )
        libsource = raw.find('libsource')
        symbol_nc_pins = set()
        if libsource is not None:
            matching_parts = [c for c in root.findall('./libparts/libpart')
                              if c.get('lib') == libsource.get('lib') and c.get('part') == libsource.get('part')]
            if len(matching_parts) == 1:
                pin_types = {}
                for pin in matching_parts[0].findall('./pins/pin'):
                    pin_types.setdefault((pin.get('num') or '').strip(), []).append(
                        (pin.get('type') or '').strip().lower()
                    )
                symbol_nc_pins = {n for n, types in pin_types.items() if types == ['no_connect']}
        normalized_npth, npth_records = _normalized_npth_numbers(
            reference, pad_records, symbol_pins, nets_root, physical_only, symbol_nc_pins=symbol_nc_pins
        )
        lib, name = footprint.split(":", 1)
        components[reference] = {
            "reference": reference,
            "value": _xml_text(raw.find("value")),
            "footprint": footprint,
            "library_path": str(path.parent),
            "footprint_name": name,
            "symbol_pins": sorted(symbol_pins),
            "pad_numbers": sorted(pad_numbers - set(normalized_npth)),
            "nets": {},
            "library": lib,
            "physical_only": physical_only,
        }
        if npth_records:
            components[reference]['npth_pad_records'] = npth_records
            components[reference]['normalized_npth_pin_numbers'] = normalized_npth
    if not components:
        raise ValueError("KiCad netlist has no board components with footprints")

    net_names: set[str] = set()
    disconnected: list[dict[str, str]] = []
    for raw_net in nets_root.findall("net"):
        name = (raw_net.get("name") or "").strip()
        if not name or name in net_names:
            raise ValueError("KiCad netlist contains an empty or duplicate net name")
        net_names.add(name)
        nodes = raw_net.findall("node")
        for node in nodes:
            reference = (node.get("ref") or "").strip()
            pin = (node.get("pin") or "").strip()
            if reference not in components:
                continue
            if pin not in components[reference]["symbol_pins"]:
                raise ValueError(f"{reference}.{pin}: netlist node is not a declared symbol pin")
            pintype = (node.get("pintype") or "").lower()
            if "no_connect" in pintype:
                if len(nodes) != 1:
                    raise ValueError(f"{reference}.{pin}: explicit no-connect pin appears on a shared net")
                disconnected.append({"reference": reference, "pin": pin})
                continue
            prior = components[reference]["nets"].get(pin)
            if prior is not None and prior != name:
                raise ValueError(f"{reference}.{pin}: netlist assigns the pin to multiple nets")
            components[reference]["nets"][pin] = name

    used_nets = sorted({name for component in components.values() for name in component["nets"].values()})
    return {
        "components": list(components.values()),
        "nets": used_nets,
        "explicit_no_connects": sorted(disconnected, key=lambda item: (item["reference"], item["pin"])),
    }


def _pack_components(
    components: list[dict[str, Any]],
    width_mm: float,
    height_mm: float,
    library: KiCadFootprintLibrary | None = None,
) -> None:
    library = library or KiCadFootprintLibrary()
    items: list[tuple[float, float, float, float, dict[str, Any]]] = []
    for component in components:
        geometry = library.geometry(component["footprint"])
        if geometry.source == "heuristic" or geometry.confidence == "heuristic":
            raise ValueError(f"{component['reference']}: footprint geometry is not locally verified")
        items.append((geometry.width_mm, geometry.height_mm, geometry.center_x_mm, geometry.center_y_mm, component))
    margin, gap = 5.0, 3.0
    x, y, row_height = margin, margin, 0.0
    ordered = sorted(items, key=lambda item: (-item[1], -item[0], item[4]["reference"]))
    positions = []
    for item_width, item_height, center_x, center_y, component in ordered:
        if item_width + margin * 2 > width_mm or item_height + margin * 2 > height_mm:
            raise ValueError(f"{component['reference']}: footprint does not fit inside the requested board")
        if x + item_width + margin > width_mm:
            x = margin
            y += row_height + gap
            row_height = 0.0
        if y + item_height + margin > height_mm:
            # A tall RF module can leave enough space beside it for every
            # support part while a shelf pack wastes that entire column.
            positions = _pack_free_rectangles(ordered, width_mm, height_mm, margin, gap)
            break
        positions.append((component, x + item_width / 2 - center_x, y + item_height / 2 - center_y))
        row_height = max(row_height, item_height)
        x += item_width + gap
    for component, px, py in positions:
        component["x_mm"], component["y_mm"] = px, py


def _pack_free_rectangles(items, width, height, margin, gap):
    """Deterministic nonrotating fallback retaining verified courtyard gaps."""
    free = [(margin, margin, width - 2 * margin + gap, height - 2 * margin + gap)]
    positions = []
    for item_width, item_height, cx, cy, component in items:
        w, h = item_width + gap, item_height + gap
        candidates = [r for r in free if w <= r[2] + 1e-9 and h <= r[3] + 1e-9]
        if not candidates:
            raise ValueError("Components do not fit inside the requested board dimensions")
        x, y, _rw, _rh = min(candidates, key=lambda r: (min(r[2] - w, r[3] - h), r[1], r[0]))
        positions.append((component, x + item_width / 2 - cx, y + item_height / 2 - cy))
        next_free = []
        for fx, fy, fw, fh in free:
            if x >= fx + fw or x + w <= fx or y >= fy + fh or y + h <= fy:
                next_free.append((fx, fy, fw, fh))
                continue
            if x > fx:
                next_free.append((fx, fy, x - fx, fh))
            if x + w < fx + fw:
                next_free.append((x + w, fy, fx + fw - x - w, fh))
            if y > fy:
                next_free.append((fx, fy, fw, y - fy))
            if y + h < fy + fh:
                next_free.append((fx, y + h, fw, fy + fh - y - h))
        # Remove contained rectangles; the remaining rectangles may overlap,
        # but each future occupied area is subtracted from every one of them.
        unique = sorted(set(next_free))
        free = [r for r in unique if not any(
            r != other and r[0] >= other[0] and r[1] >= other[1]
            and r[0] + r[2] <= other[0] + other[2] + 1e-9
            and r[1] + r[3] <= other[1] + other[3] + 1e-9
            for other in unique
        )]
    return positions


def _copy_schematic_project(
    source: Path, destination: Path, project_root: Path | None = None
) -> tuple[Path, dict[Path, str]]:
    """Copy local hierarchy and symbol tables so export cannot touch source files."""
    snapshots: dict[Path, str] = {}
    project = (project_root or source.parent).resolve()
    if not project.is_dir():
        raise ValueError("project_root must be an existing directory")
    schematic_files = sorted(source.parent.glob("*.kicad_sch"))
    for item in schematic_files:
        text = item.read_text(encoding="utf-8")
        for match in _SHEET_FILE_RE.finditer(text):
            child_name = match.group(1)
            child = Path(child_name)
            if (
                not child_name
                or child.is_absolute()
                or child.name != child_name
                or "/" in child_name
                or "\\" in child_name
                or not child_name.lower().endswith(".kicad_sch")
            ):
                raise ValueError(
                    f"Unsupported hierarchical sheet path in {item.name}: {child_name!r}; "
                    "only direct sibling .kicad_sch files are supported"
                )
            resolved = (source.parent / child_name).resolve(strict=False)
            if resolved.parent != source.parent.resolve() or not resolved.is_file():
                raise ValueError(f"Missing or out-of-project hierarchical sheet: {child_name}")
    for pattern in ("*.kicad_sch", "*.kicad_sym", "sym-lib-table", "*.kicad_pro", "*.kicad_dru"):
        for item in source.parent.glob(pattern):
            if item.is_file():
                snapshots[item.resolve()] = hashlib.sha256(item.read_bytes()).hexdigest()
                shutil.copy2(item, destination / item.name)
    # Copy project footprint context to the isolated native stage, retaining KIPRJMOD layout.
    from .kicad_project_context import inspect_project_footprint_context, stage_project_footprint_context
    context_inventory = inspect_project_footprint_context(project)
    stage_project_footprint_context(project, destination)
    snapshots.update({Path(path): digest for path, digest in context_inventory.get("source_hashes", {}).items()})
    snapshots.update({
        Path(path): digest
        for path, digest in context_inventory.get("library_manifest_sha256", {}).items()
    })
    for path in context_inventory.get("watch_paths", []):
        snapshots.setdefault(Path(path), None)
    copied = destination / source.name
    if not copied.is_file():
        raise ValueError("Could not stage the source schematic")
    return copied, snapshots


def _target_snapshot(target: Path) -> dict[Path, str | None]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        for path in (target, target.with_suffix(".kicad_pro"), target.with_suffix(".kicad_dru"))
    }


def _source_project_has_clearance_rules(project_path: Path, rules_path: Path) -> bool:
    """Return whether the source project explicitly owns board/netclass rules."""
    if rules_path.is_file():
        return True
    if not project_path.is_file():
        return False
    try:
        settings = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not safely inspect source KiCad project settings: {exc}") from exc
    if not isinstance(settings, dict):
        raise ValueError("Source KiCad project settings must be a JSON object")
    board = settings.get("board")
    board_rules = board.get("design_settings", {}).get("rules") if isinstance(board, dict) else None
    return "net_settings" in settings or bool(board_rules)


def _check_snapshot(snapshot: dict[Path, str | None]) -> None:
    from .kicad_project_context import footprint_library_tree_sha256
    for path, digest in snapshot.items():
        if path.is_dir():
            current = footprint_library_tree_sha256(path)
        else:
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if current != digest:
            raise ValueError(f"Input or output changed during schematic-to-PCB processing: {path}")


def _restore_authored_project_settings(
    board: Path, payloads: dict[str, bytes | None], *, clearance_mm=None, minimum_drill_mm=None,
) -> dict[Path, str]:
    """Undo native SaveBoard defaults without discarding explicit prototype assumptions."""
    restored = {}
    for suffix, original in payloads.items():
        if original is None:
            continue
        destination = board.with_suffix(suffix)
        content = original
        if suffix == ".kicad_pro":
            try:
                settings = json.loads(original)
            except (ValueError, UnicodeError) as exc:
                raise ValueError("Source KiCad project settings are not valid JSON") from exc
            if not isinstance(settings, dict):
                raise ValueError("Source KiCad project settings must be a JSON object")
            if clearance_mm is not None:
                # The public preflight permits this only when the source does
                # not own board/netclass rules. Reuse the native schema rather
                # than inventing a KiCad-version-specific NET_SETTINGS payload.
                if "net_settings" in settings:
                    raise ValueError("Cannot override authored source netclass rules")
                native = json.loads(destination.read_bytes())
                if not isinstance(native, dict) or not isinstance(native.get("net_settings"), dict):
                    raise ValueError("Native project has no verifiable netclass settings")
                net_settings = native.get("net_settings", {})
                if not isinstance(net_settings.get("classes"), list):
                    raise ValueError("Native project has no verifiable netclass list")
                defaults = [x for x in net_settings.get("classes", [])
                            if isinstance(x, dict) and x.get("name") == "Default"]
                if len(defaults) != 1 or defaults[0].get("clearance") != clearance_mm:
                    raise ValueError("Native project did not retain the requested prototype clearance")
                settings["net_settings"] = net_settings
            if minimum_drill_mm is not None:
                current = settings
                for key in ("board", "design_settings", "rules"):
                    current = current.setdefault(key, {})
                    if not isinstance(current, dict):
                        raise ValueError("Source KiCad board settings are malformed")
                current["min_through_hole_diameter"] = float(minimum_drill_mm)
            if clearance_mm is not None or minimum_drill_mm is not None:
                content = (json.dumps(settings, indent=2) + "\n").encode("utf-8")
        destination.write_bytes(content)
        restored[destination] = hashlib.sha256(content).hexdigest()
    return restored


def schematic_to_pcb(
    schematic_path: str | Path,
    output_path: str | Path,
    *,
    width_mm: float = 80,
    height_mm: float = 60,
    clearance_mm: float | None = None,
    minimum_drill_mm: float | None = None,
    overwrite: bool = False,
    kicad_python_path: str | Path | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Create and validate a review-only pad-bearing PCB from a real schematic."""
    source = Path(schematic_path).expanduser().resolve()
    target = Path(output_path).expanduser().resolve()
    try:
        if source.suffix.lower() != ".kicad_sch" or not source.is_file():
            raise ValueError("Input must be an existing .kicad_sch schematic")
        if target.suffix.lower() != ".kicad_pcb":
            raise ValueError("Output must use the .kicad_pcb suffix")
        if source == target:
            raise ValueError("Schematic and PCB output paths must differ")
        if isinstance(width_mm, bool) or isinstance(height_mm, bool) or not all(
            isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0
            for value in (width_mm, height_mm)
        ):
            raise ValueError("Board width and height must be finite positive millimeter values")
        if clearance_mm is not None and (
            isinstance(clearance_mm, bool)
            or not isinstance(clearance_mm, (int, float))
            or not math.isfinite(float(clearance_mm))
            or clearance_mm <= 0
        ):
            raise ValueError("Clearance must be a finite positive millimeter value")
        if target.exists() and not overwrite:
            raise FileExistsError(f"Output already exists; pass overwrite=True to replace it: {target}")
        if minimum_drill_mm is not None and (
            isinstance(minimum_drill_mm, bool) or not isinstance(minimum_drill_mm, (int, float))
            or not math.isfinite(float(minimum_drill_mm)) or minimum_drill_mm <= 0
        ):
            raise ValueError("Minimum drill must be a finite positive millimeter value")
        project = Path(project_root).expanduser().resolve() if project_root is not None else source.parent
        if not project.is_dir():
            raise ValueError("project_root must be an existing directory")
        target_snapshot = _target_snapshot(target)
        source_project_settings = {
            suffix: source.with_suffix(suffix)
            for suffix in (".kicad_pro", ".kicad_dru")
        }
        if source.with_suffix(".pro").is_file() and not source_project_settings[".kicad_pro"].is_file():
            raise ValueError("Legacy project rules require conversion to .kicad_pro before schematic-to-PCB generation")
        source_settings_payloads = {
            suffix: path.read_bytes() if path.is_file() else None
            for suffix, path in source_project_settings.items()
        }
        source_settings_snapshot = {
            source_project_settings[suffix]: hashlib.sha256(payload).hexdigest() if payload is not None else None
            for suffix, payload in source_settings_payloads.items()
        }
        if clearance_mm is not None and _source_project_has_clearance_rules(
            source_project_settings[".kicad_pro"], source_project_settings[".kicad_dru"]
        ):
            raise ValueError(
                "Cannot override clearance when source project already defines board rules, "
                "net classes, or a design-rules file"
            )
        if minimum_drill_mm is not None and _source_project_has_clearance_rules(
            source_project_settings[".kicad_pro"], source_project_settings[".kicad_dru"]
        ):
            raise ValueError("Cannot override minimum drill when source project already defines board rules")
        for suffix, source_settings in source_project_settings.items():
            destination_settings = target.with_suffix(suffix)
            if destination_settings.exists():
                source_hash = (
                    hashlib.sha256(source_settings.read_bytes()).hexdigest()
                    if source_settings.is_file()
                    else None
                )
                destination_hash = hashlib.sha256(destination_settings.read_bytes()).hexdigest()
                if source_hash is None or source_hash != destination_hash:
                    raise ValueError(
                        "Output has unrelated project settings; choose a fresh output name: "
                        f"{destination_settings}"
                    )

        # Conflicts and invalid source constraints are user-input failures and
        # can be identified before requiring optional native KiCad executables.
        cli_path = _kicad_cli_path()
        cli = str(cli_path) if cli_path is not None else None
        if cli is None:
            raise RuntimeError("KiCad CLI was not found; install KiCad or add kicad-cli to PATH")
        python = find_kicad_python(kicad_python_path)
        if python is None:
            raise RuntimeError("KiCad Python (pcbnew) is unavailable; set CIRCUIT_WEAVER_KICAD_PYTHON")

        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".cw-c-", dir=target.parent) as tmp:
            staging = Path(tmp)
            staged_project = staging / "project"
            staged_project.mkdir()
            staged_schematic, source_snapshot = _copy_schematic_project(source, staged_project, project)
            sheet_count = max(1, sum(path.suffix.lower() == ".kicad_sch" for path in source_snapshot))
            netlist_export_timeout = _native_build_timeout_seconds(0, sheet_count)
            netlist = staging / "schematic.net.xml"
            command = [
                cli,
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                str(netlist),
                str(staged_schematic),
            ]
            exported = subprocess.run(
                command, capture_output=True, text=True, timeout=netlist_export_timeout
            )
            if exported.returncode != 0 or not netlist.is_file():
                detail = (exported.stderr or exported.stdout).strip()[-1500:]
                raise RuntimeError(f"KiCad schematic netlist export failed: {detail or 'no netlist produced'}")
            export_log = f"{exported.stdout}\n{exported.stderr}"
            if _annotation_errors_present(export_log):
                raise RuntimeError(
                    "KiCad schematic netlist export reported annotation errors; "
                    "repair the schematic first"
                )
            footprint_library = KiCadFootprintLibrary(project_root=project)
            model = _parse_netlist(netlist, footprint_library)
            _pack_components(model["components"], float(width_mm), float(height_mm), footprint_library)
            worker_timeout = _native_build_timeout_seconds(len(model["components"]), sheet_count)
            staged_board = staging / target.name
            staged_project_file = staged_board.with_suffix(".kicad_pro")
            staged_rules_file = staged_board.with_suffix(".kicad_dru")
            if source_project_settings[".kicad_pro"].is_file():
                shutil.copy2(source_project_settings[".kicad_pro"], staged_project_file)
            if source_project_settings[".kicad_dru"].is_file():
                shutil.copy2(source_project_settings[".kicad_dru"], staged_rules_file)
            request = {
                "operation": "build",
                "board": str(staged_board),
                "width_mm": float(width_mm),
                "height_mm": float(height_mm),
                "clearance_mm": float(clearance_mm) if clearance_mm is not None else None,
                "minimum_drill_mm": float(minimum_drill_mm) if minimum_drill_mm is not None else None,
                "components": model["components"],
                "nets": model["nets"],
                "explicit_no_connects": model["explicit_no_connects"],
            }
            worker_path = Path(__file__).with_name("_schematic_pcb_worker.py")
            result = subprocess.run(
                [python, str(worker_path)],
                input=json.dumps(request, ensure_ascii=True),
                capture_output=True,
                text=True,
                timeout=worker_timeout,
            )
            worker = _worker_result(result)
            if worker["status"] != "ok":
                raise RuntimeError(worker.get("message", "KiCad failed to create the prototype board"))
            if not staged_board.is_file():
                raise RuntimeError("KiCad worker did not create the board")

            _check_snapshot(source_settings_snapshot)
            # Native SaveBoard can replace even an existing staged .kicad_pro
            # with fresh defaults. DRC and publication must use authored rules.
            restored_settings = _restore_authored_project_settings(
                staged_board, source_settings_payloads,
                clearance_mm=clearance_mm, minimum_drill_mm=minimum_drill_mm,
            )
            drc = run_drc(staged_board, evidence_ledger=EvidenceLedger(), project_context=project)
            _check_snapshot(restored_settings)
            non_connect_blockers = [
                finding for finding in drc.findings
                if finding.severity == "blocker" and finding.rule_id != "CW-DRC-002"
            ]
            if drc.status != "ok" or non_connect_blockers:
                details = "; ".join(
                    f"{finding.rule_id}: {finding.message}" for finding in non_connect_blockers[:8]
                )
                if drc.status != "ok":
                    details = "; ".join(item for item in (drc.failure_reason, details) if item)
                raise RuntimeError(f"Prototype board failed physical DRC validation: {details or drc.status}")
            _check_snapshot(source_snapshot)
            _check_snapshot(source_settings_snapshot)
            publications = [(staged_board, target)]
            for staged_settings in (staged_project_file, staged_rules_file):
                destination = target.with_suffix(staged_settings.suffix)
                if staged_settings.is_file() and (
                    not destination.is_file() or staged_settings.read_bytes() != destination.read_bytes()
                ):
                    publications.append((staged_settings, destination))
            with _pcb_output_lock(target.parent):
                _check_snapshot(target_snapshot)
                for source_file, destination in publications:
                    if destination.exists() and not overwrite:
                        raise FileExistsError(
                            "Output already exists; pass overwrite=True to replace it: "
                            f"{destination}"
                        )
                _publish_staged_transaction(staging, publications)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            physical_only_components = sorted(
                component["reference"] for component in model["components"] if component.get("physical_only")
            )
            validation = {
                "netlist_export": "passed",
                "netlist_tool": _tool_version(exported.stdout, exported.stderr),
                "footprint_source": "local_kicad_library",
                "pin_mapping": (
                    "exact_number_match_except_explicit_unconnected_physical_only"
                    if physical_only_components else "exact_number_match"
                ),
                "pcbnew_round_trip": "passed",
                "native_timeouts_seconds": {
                    "netlist_export": netlist_export_timeout,
                    "pcbnew_worker": worker_timeout,
                    "hard_cap_per_process": _NATIVE_BUILD_MAX_TIMEOUT_SECONDS,
                },
                "physical_drc": {
                    "status": drc.status,
                    "non_connect_blockers": 0,
                    "expected_unconnected_count": sum(
                        1 for finding in drc.findings if finding.rule_id == "CW-DRC-002"
                    ),
                },
                "board_sha256": digest,
                "manufacturing_readiness": "not_assessed",
                "prototype_assumptions": (
                    {**({"default_netclass_clearance_mm": float(clearance_mm)}
                        if clearance_mm is not None else {}),
                     **({"minimum_drill_mm": float(minimum_drill_mm)}
                        if minimum_drill_mm is not None else {})}
                ),
            }
            if physical_only_components:
                validation["physical_only_components"] = {
                    "count": len(physical_only_components),
                    "references": physical_only_components,
                    "native_pad_count": int(worker.get("physical_only_pad_count", 0)),
                    "net_assignments": "forbidden_and_verified_after_save_reload",
                }
            normalized_holes = [
                {"reference": c["reference"], "pins": c["normalized_npth_pin_numbers"]}
                for c in model["components"] if c.get("normalized_npth_pin_numbers")
            ]
            if normalized_holes:
                validation["pin_mapping"] = "exact_electrical_number_match_with_verified_non_electrical_holes"
                validation["non_electrical_hole_pins"] = {
                    "components": normalized_holes,
                    "mapping": "native_npth_number_normalization_only",
                    "geometry_and_no_net_metadata": "verified_after_save_reload",
                }
            return {
                "status": "ok",
                "output_path": str(target),
                "output_kind": "prototype_pad_bearing_board",
                "fabrication_ready": False,
                "counts": {
                    "components": len(model["components"]),
                    "footprints": int(worker["footprints"]),
                    "pads": int(worker["pads"]),
                    "nets": len(model["nets"]),
                    "explicit_no_connects": len(model["explicit_no_connects"]),
                },
                "validation": validation,
                "message": (
                    "Real footprints and schematic nets were forward-annotated for review; "
                    "placement and routing still require review."
                ),
            }
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "message": str(exc), "fabrication_ready": False}


def _worker_result(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(process.stdout.splitlines()):
        if line.startswith("CW_SCHEMATIC_PCB_RESULT="):
            try:
                value = json.loads(line.partition("=")[2])
            except json.JSONDecodeError:
                break
            if isinstance(value, dict) and value.get("status") in {"ok", "error"}:
                if value.get("status") == "ok" and process.returncode != 0:
                    return {
                        "status": "error",
                        "message": "KiCad worker returned success with a failing process exit code",
                    }
                return value
    detail = (process.stderr or process.stdout).strip()[-1000:]
    return {"status": "error", "message": f"KiCad PCB worker failed: {detail or 'invalid worker result'}"}


def _tool_version(*outputs: str) -> str:
    text = "\n".join(outputs)
    match = re.search(r"(?:KiCad|Eeschema)\s+([0-9]+(?:\.[0-9]+){1,2})", text, re.IGNORECASE)
    return match.group(1) if match else "available"
