"""DFM (Design for Manufacture) checker — validates PCB designs against fab capabilities.

Checks track widths, via dimensions and annular rings, and via copper-to-edge
clearance on rectangular outlines. Native KiCad DRC remains required for spacing,
pads, zones and unsupported outline geometry. Profile thresholds are static inputs.

Usage:
    from circuit_weaver.dfm_checker import check_dfm

    violations = check_dfm("design.kicad_pcb", profile="jlcpcb")
    for v in violations:
        print(f"{v.severity}: {v.type} at {v.location} - {v.message}")
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DFMViolation:
    """Represents a single DFM violation."""

    severity: str  # "critical", "warning"
    type: str  # "trace_width", "spacing", "via_drill", etc.
    location: str  # net name, component ref, or coordinate
    actual: float | None  # actual measured value in mm
    minimum: float | None  # minimum allowed value in mm
    message: str
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "severity": self.severity,
            "type": self.type,
            "location": self.location,
            "actual": self.actual,
            "minimum": self.minimum,
            "message": self.message,
            "suggestion": self.suggestion,
        }


# DFM Profiles: fab-specific design rules (in mm)
_DFM_PROFILES = {
    "jlcpcb": {
        "name": "JLCPCB Standard (2-layer)",
        "trace_width_min": 0.127,
        "trace_spacing_min": 0.127,
        "via_diameter_min": 0.45,
        "via_drill_min": 0.2,
        "annular_ring_min": 0.125,
        "solder_mask_clearance_min": 0.1,
        "board_edge_clearance_min": 0.3,
        "pad_to_pad_min": 0.2,
    },
    "jlcpcb_4layer": {
        "name": "JLCPCB 4-layer",
        "trace_width_min": 0.09,
        "trace_spacing_min": 0.09,
        "via_diameter_min": 0.3,
        "via_drill_min": 0.15,
        "annular_ring_min": 0.1,
        "solder_mask_clearance_min": 0.1,
        "board_edge_clearance_min": 0.3,
        "pad_to_pad_min": 0.15,
    },
    "pcbway": {
        "name": "PCBWay Standard (2-layer)",
        "trace_width_min": 0.1,
        "trace_spacing_min": 0.1,
        "via_diameter_min": 0.3,
        "via_drill_min": 0.15,
        "annular_ring_min": 0.15,
        "solder_mask_clearance_min": 0.1,
        "board_edge_clearance_min": 0.3,
        "pad_to_pad_min": 0.2,
    },
}


def _parse_pcb_s_expr(content: str) -> dict[str, Any]:
    """Parse KiCad PCB S-expression format into structured data.

    Extracts legacy net names, via geometry and rectangular board bounds.
    Track centerlines and pad geometry are not inspected here.
    """
    pcb = {
        "nets": {},
        "tracks": [],
        "track_geometry_coverage": "not_inspected",
        "vias": [],
        "via_errors": [],
        "pads": [],
        "board_width_mm": None,
        "board_height_mm": None,
        "board_bounds_mm": None,
    }

    # Extract net list (net N "name")
    for match in re.finditer(r'\(net\s+(\d+)\s+"([^"]+)"\)', content):
        net_num = match.group(1)
        net_name = match.group(2)
        pcb["nets"][net_name] = int(net_num)

    # Track centerline geometry is not currently consumed by this checker. Do not
    # report coverage based on a partial regex that misses legacy start/end syntax.

    for index, (block, complete) in enumerate(_iter_named_blocks(content, "via"), start=1):
        via, error = _parse_via_mm(block, complete=complete)
        if error:
            pcb["via_errors"].append((index, error))
        else:
            pcb["vias"].append(via)

    pcb["board_bounds_mm"] = _rectangular_edge_bounds_mm(content)
    if pcb["board_bounds_mm"] is not None:
        min_x, min_y, max_x, max_y = pcb["board_bounds_mm"]
        pcb["board_width_mm"] = max_x - min_x
        pcb["board_height_mm"] = max_y - min_y

    return pcb


def _numeric_values(raw: str, count: int) -> list[float] | None:
    tokens = raw.split()
    number_pattern = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    if len(tokens) != count or any(not re.fullmatch(number_pattern, token) for token in tokens):
        return None
    values = [float(token) for token in tokens]
    return values if all(math.isfinite(value) for value in values) else None


def _coordinate_field(block: str, name: str) -> tuple[float, float] | None:
    matches = re.findall(rf"\({name}(?=\s|\))([^()]*)\)", block)
    if len(matches) != 1:
        return None
    values = _numeric_values(matches[0], 2)
    return (values[0], values[1]) if values is not None else None


def _parse_via_mm(block: str, *, complete: bool) -> tuple[dict[str, float] | None, str | None]:
    """Parse one balanced via block; KiCad geometry numbers are millimeters."""
    if not complete:
        return None, "via S-expression is incomplete"

    def field(name: str, count: int, expected: int, *, positive: bool) -> tuple[list[float] | None, str | None]:
        matches = re.findall(rf"\({name}(?=\s|\))([^()]*)\)", block)
        if len(matches) != count:
            return None, f"via must contain exactly one {name} field"
        values = _numeric_values(matches[0], expected)
        if values is None:
            return None, f"via {name} field is malformed or non-finite"
        if positive and any(value <= 0 for value in values):
            return None, f"via {name} values must be positive"
        return values, None

    at, error = field("at", 1, 2, positive=False)
    if error:
        return None, error
    size, error = field("size", 1, 1, positive=True)
    if error:
        return None, error
    drill, error = field("drill", 1, 1, positive=True)
    if error:
        return None, error
    assert at is not None and size is not None and drill is not None
    return {"x": at[0], "y": at[1], "diameter": size[0], "drill": drill[0]}, None


def _iter_named_blocks(content: str, name: str) -> list[tuple[str, bool]]:
    """Return balanced named S-expression blocks and whether each is complete."""
    blocks: list[tuple[str, bool]] = []
    index = 0
    quoted = False
    escaped = False
    while index < len(content):
        char = content[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quoted and char == "\\":
            escaped = True
            index += 1
            continue
        if char == '"':
            quoted = not quoted
            index += 1
            continue
        if quoted:
            index += 1
            continue
        if char == ";":
            newline = content.find("\n", index)
            index = len(content) if newline < 0 else newline + 1
            continue
        if char != "(":
            index += 1
            continue
        name_start = index + 1
        name_end = name_start + len(name)
        delimiter = content[name_end : name_end + 1]
        if content[name_start:name_end] != name or (delimiter and not delimiter.isspace() and delimiter != ")"):
            index += 1
            continue

        start = index
        depth = 0
        quoted = False
        escaped = False
        end = start
        complete = False
        while end < len(content):
            current = content[end]
            if not quoted and current == ";":
                newline = content.find("\n", end)
                if newline < 0:
                    end = len(content)
                    continue
                end = newline + 1
                continue
            if escaped:
                escaped = False
                end += 1
                continue
            if quoted and current == "\\":
                escaped = True
                end += 1
                continue
            if current == '"':
                quoted = not quoted
                end += 1
                continue
            if quoted:
                end += 1
                continue
            if current == "(":
                depth += 1
            elif current == ")":
                depth -= 1
                if depth == 0:
                    blocks.append((content[start : end + 1], True))
                    index = end + 1
                    complete = True
                    break
            end += 1
        if not complete:
            blocks.append((content[start:], False))
            break
    return blocks


def _rectangular_edge_bounds_mm(content: str) -> tuple[float, float, float, float] | None:
    """Return bounds only for a complete rectangular Edge.Cuts outline."""
    edge_layer = re.compile(r'\(layer\s+"?Edge\.Cuts"?\s*\)', re.IGNORECASE)
    edges: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    for unsupported_name in ("gr_arc", "gr_circle", "gr_poly", "gr_curve"):
        if any(edge_layer.search(block) for block, _ in _iter_named_blocks(content, unsupported_name)):
            return None
    line_blocks = _iter_named_blocks(content, "gr_line")
    rectangle_blocks = [
        (block, complete)
        for block, complete in _iter_named_blocks(content, "gr_rect")
        if edge_layer.search(block)
    ]
    if rectangle_blocks:
        if len(rectangle_blocks) != 1 or any(edge_layer.search(block) for block, _ in line_blocks):
            return None
        block, complete = rectangle_blocks[0]
        if not complete:
            return None
        start = _coordinate_field(block, "start")
        end = _coordinate_field(block, "end")
        if start is None or end is None:
            return None
        min_x, max_x = sorted((start[0], end[0]))
        min_y, max_y = sorted((start[1], end[1]))
        if min_x == max_x or min_y == max_y:
            return None
        return min_x, min_y, max_x, max_y

    for block, complete in line_blocks:
        if not edge_layer.search(block):
            continue
        if not complete:
            return None
        start = _coordinate_field(block, "start")
        end = _coordinate_field(block, "end")
        if start is None or end is None:
            return None
        first = start
        second = end
        if first == second or (first[0] != second[0] and first[1] != second[1]):
            return None
        edges.add(tuple(sorted((first, second))))

    if len(edges) != 4:
        return None
    points = {point for edge in edges for point in edge}
    if len(points) != 4:
        return None
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    corners = {(min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)}
    if points != corners:
        return None
    expected = {
        tuple(sorted(((min_x, min_y), (min_x, max_y)))),
        tuple(sorted(((min_x, min_y), (max_x, min_y)))),
        tuple(sorted(((max_x, min_y), (max_x, max_y)))),
        tuple(sorted(((min_x, max_y), (max_x, max_y)))),
    }
    return (min_x, min_y, max_x, max_y) if edges == expected else None


def _segment_width_mm(segment: str, *, complete: bool) -> tuple[float | None, str | None]:
    """Read one KiCad track width, whose numeric S-expression value is millimeters."""
    width_count = len(re.findall(r"\(width(?=\s|\))", segment))
    if not complete:
        return None, "track segment S-expression is incomplete"
    if width_count != 1:
        return None, "track segment must contain exactly one width field"
    width_match = re.search(r"\(width(?=\s|\))([^()]*)\)", segment)
    if width_match is None:
        return None, "track segment width field is malformed"
    tokens = width_match.group(1).split()
    if len(tokens) != 1:
        return None, "track segment width field is malformed"
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", tokens[0]):
        return None, "track segment width is not a numeric millimeter value"
    width = float(tokens[0])
    if not math.isfinite(width) or width <= 0:
        return None, "track segment width must be a finite positive millimeter value"
    return width, None


def _unknown_dfm_input(path: Path, reason: str) -> DFMViolation:
    return DFMViolation(
        severity="critical",
        type="dfm_input_unknown",
        location=str(path),
        actual=None,
        minimum=None,
        message=f"DFM checks could not inspect the PCB input: {reason}",
        suggestion="Provide a readable, valid KiCad PCB file and rerun the DFM check.",
    )


def _is_ignorable_outside_root(text: str) -> bool:
    """Allow only whitespace and semicolon comments around the PCB root."""
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text[index] == ";":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
        else:
            return False
    return True


def _has_one_complete_pcb_root(content: str) -> bool:
    roots = _iter_named_blocks(content, "kicad_pcb")
    if len(roots) != 1 or not roots[0][1]:
        return False
    root = roots[0][0]
    start = content.find(root)
    if start < 0:
        return False
    end = start + len(root)
    return _is_ignorable_outside_root(content[:start]) and _is_ignorable_outside_root(content[end:])


def check_dfm(
    kicad_pcb_path: str | Path,
    profile: str = "jlcpcb",
    custom_rules: dict[str, float] | None = None,
) -> list[DFMViolation]:
    """Check PCB design for DFM violations.

    Args:
        kicad_pcb_path: Path to .kicad_pcb file
        profile: Fab profile ("jlcpcb", "jlcpcb_4layer", "pcbway")
        custom_rules: Override profile rules with custom dict

    Returns:
        List of DFMViolation objects sorted by severity
    """
    path = Path(kicad_pcb_path)
    if not path.exists():
        logger.error(f"PCB file not found: {path}")
        return [_unknown_dfm_input(path, "file does not exist")]

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to read PCB file: {e}")
        return [_unknown_dfm_input(path, f"file could not be read ({type(e).__name__})")]

    if not _has_one_complete_pcb_root(content):
        logger.error(f"PCB file does not contain exactly one complete top-level KiCad PCB expression: {path}")
        return [_unknown_dfm_input(path, "content is not exactly one complete top-level (kicad_pcb ...) expression")]

    # Get DFM rules
    if profile not in _DFM_PROFILES:
        logger.warning(f"Unknown profile '{profile}'. Using 'jlcpcb'.")
        profile = "jlcpcb"

    rules = _DFM_PROFILES[profile].copy()
    if custom_rules:
        rules.update(custom_rules)

    # Parse PCB
    pcb = _parse_pcb_s_expr(content)
    violations = []

    for index, reason in pcb["via_errors"]:
        violations.append(
            DFMViolation(
                severity="critical",
                type="via_geometry_unknown",
                location=f"Via {index}",
                actual=None,
                minimum=None,
                message=f"Via geometry could not be determined: {reason}",
                suggestion="Repair the via coordinates, size, and drill fields and rerun DFM.",
            )
        )

    # KiCad PCB S-expressions store widths in millimeters across supported file versions.
    track_blocks = (
        (kind, index, block, complete)
        for kind in ("segment", "arc")
        for index, (block, complete) in enumerate(_iter_named_blocks(content, kind), start=1)
    )
    for kind, index, segment, complete in track_blocks:
        width, width_error = _segment_width_mm(segment, complete=complete)
        if width is None:
            violations.append(
                DFMViolation(
                    severity="critical",
                    type="trace_width_unknown",
                    location=f"{kind} {index}",
                    actual=None,
                    minimum=rules["trace_width_min"],
                    message=f"Trace width could not be determined: {width_error}",
                    suggestion=(
                        "Repair the track width field and rerun DFM; "
                        "the checker will not assume zero or borrow another track's width."
                    ),
                )
            )
            continue
        if width < rules["trace_width_min"]:
            violations.append(
                DFMViolation(
                    severity="critical",
                    type="trace_width",
                    location=f"{kind} {index}",
                    actual=width,
                    minimum=rules["trace_width_min"],
                    message=f"Trace width {width:.3f}mm is below minimum {rules['trace_width_min']:.3f}mm",
                    suggestion="Increase trace width in PCB editor or use wider traces for power nets.",
                )
            )

    # Check via diameter and drill
    for i, via in enumerate(pcb["vias"]):
        if via["diameter"] < rules["via_diameter_min"]:
            violations.append(
                DFMViolation(
                    severity="critical",
                    type="via_diameter",
                    location=f"Via {i} at ({via['x']:.1f}, {via['y']:.1f})",
                    actual=via["diameter"],
                    minimum=rules["via_diameter_min"],
                    message=f"Via diameter {via['diameter']:.3f}mm is below minimum {rules['via_diameter_min']:.3f}mm",
                    suggestion="Use larger via diameter or check fab DRC settings.",
                )
            )

        if via["drill"] < rules["via_drill_min"]:
            violations.append(
                DFMViolation(
                    severity="critical",
                    type="via_drill",
                    location=f"Via {i} at ({via['x']:.1f}, {via['y']:.1f})",
                    actual=via["drill"],
                    minimum=rules["via_drill_min"],
                    message=f"Via drill {via['drill']:.3f}mm is below minimum {rules['via_drill_min']:.3f}mm",
                    suggestion="Increase drill size to meet fab minimum.",
                )
            )

        # Check annular ring (difference between diameter and drill)
        annular_ring = (via["diameter"] - via["drill"]) / 2
        if annular_ring < rules["annular_ring_min"]:
            violations.append(
                DFMViolation(
                    severity="warning",
                    type="annular_ring",
                    location=f"Via {i} at ({via['x']:.1f}, {via['y']:.1f})",
                    actual=annular_ring,
                    minimum=rules["annular_ring_min"],
                    message=f"Annular ring {annular_ring:.3f}mm is below minimum {rules['annular_ring_min']:.3f}mm",
                    suggestion="Increase via diameter or reduce drill size.",
                )
            )

    # Check via copper-to-edge clearance only when a rectangular Edge.Cuts outline was parsed.
    edge_margin = rules["board_edge_clearance_min"]
    if pcb["vias"] and pcb["board_bounds_mm"] is None:
        violations.append(
            DFMViolation(
                severity="critical",
                type="board_edge_clearance_unknown",
                location="board outline",
                actual=None,
                minimum=edge_margin,
                message="Via edge clearance could not be determined from a complete rectangular Edge.Cuts outline.",
                suggestion="Use KiCad DRC or provide a supported rectangular Edge.Cuts outline.",
            )
        )
    elif pcb["board_bounds_mm"] is not None:
        min_x, min_y, max_x, max_y = pcb["board_bounds_mm"]
        for i, via in enumerate(pcb["vias"]):
            center_margin = min(
                via["x"] - min_x,
                via["y"] - min_y,
                max_x - via["x"],
                max_y - via["y"],
            )
            copper_margin = center_margin - via["diameter"] / 2
            if copper_margin < edge_margin:
                violations.append(
                    DFMViolation(
                        severity="warning",
                        type="board_edge_clearance",
                        location=f"Via {i} at ({via['x']:.1f}, {via['y']:.1f})",
                        actual=copper_margin,
                        minimum=edge_margin,
                        message=(
                            f"Via copper is too close to board edge (clearance {copper_margin:.3f}mm; "
                            f"requires {edge_margin}mm)"
                        ),
                        suggestion="Move via further from board edge.",
                    )
                )

    # Sort by severity (critical first, then warning)
    violations.sort(key=lambda v: (v.severity != "critical", v.type, v.location))

    # Log to design.log via bridge
    from .logging_bridge import get_design_logger

    dl = get_design_logger()
    if dl:
        critical = sum(1 for v in violations if v.severity == "critical")
        warns = sum(1 for v in violations if v.severity == "warning")
        dl.log_erc_drc(
            check_type="dfm",
            file=str(path),
            errors=critical,
            warnings=warns,
            details=[v.message for v in violations[:5]],
        )

    return violations


def dfm_report(violations: list[DFMViolation]) -> str:
    """Generate human-readable DFM violation report."""
    if not violations:
        return "✓ No DFM violations detected."

    critical = [v for v in violations if v.severity == "critical"]
    warnings = [v for v in violations if v.severity == "warning"]

    lines = []
    if critical:
        lines.append(f"\n❌ CRITICAL ({len(critical)}):")
        for v in critical:
            lines.append(f"  {v.type.upper()} at {v.location}")
            lines.append(f"    {v.message}")
            if v.suggestion:
                lines.append(f"    → {v.suggestion}")

    if warnings:
        lines.append(f"\n⚠️  WARNINGS ({len(warnings)}):")
        for v in warnings:
            lines.append(f"  {v.type.upper()} at {v.location}")
            lines.append(f"    {v.message}")
            if v.suggestion:
                lines.append(f"    → {v.suggestion}")

    return "\n".join(lines)
