"""PCB placement optimizer using simulated annealing.

Multi-objective optimizer that considers thermal, signal integrity,
DFM clearance, and cost constraints. Reads thermal/SI spec data from
Sprint 15's spec harvester output.

Usage:
    from circuit_weaver.placement_optimizer import optimize_placement
    placements = optimize_placement(components, board_width=100, board_height=80)
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
from dataclasses import dataclass, field, replace
from itertools import combinations
from pathlib import Path
from typing import Any

from .component_db import ComponentDef
from .pcb_export import _build_net_component_map

log = logging.getLogger(__name__)

# Footprint size estimates (mm) — width, height
_FOOTPRINT_SIZES: dict[str, tuple[float, float]] = {
    "0201": (0.6, 0.3),
    "0402": (1.0, 0.5),
    "0603": (1.6, 0.8),
    "0805": (2.0, 1.25),
    "1206": (3.2, 1.6),
    "2512": (6.3, 3.2),
    "1210": (3.2, 2.5),
    "SOT-23": (4.0, 3.0),
    "SOT-89": (4.5, 3.5),
    "SOT-223": (6.5, 3.5),
    "SOIC-8": (6.0, 5.0),
    "SOIC-16": (10.0, 6.0),
    "TSSOP-8": (4.4, 3.0),
    "TSSOP-16": (5.0, 4.4),
    "QFN-16": (3.0, 3.0),
    "QFN-20": (4.0, 4.0),
    "QFN-24": (4.0, 4.0),
    "QFN-32": (6.0, 6.0),
    "QFN-48": (7.0, 7.0),
    "LQFP-48": (9.0, 9.0),
    "LQFP-64": (12.0, 12.0),
    "LQFP-100": (16.0, 16.0),
    "BGA-256": (23.0, 23.0),
    "BGA": (15.0, 15.0),
    "ESP32-WROOM": (18.0, 25.5),
    "USB-C": (9.0, 7.5),
}

# Category placement anchors are absolute millimetres from named board edges.
# ``center`` is deliberately explicit; unknown categories use the general anchor
# and never silently acquire digital placement semantics.
_ZONE_ANCHORS: dict[str, tuple[str, float, str, float]] = {
    "power": ("left", 20.0, "top", 22.0),
    "regulator": ("left", 20.0, "top", 22.0),
    "power_management": ("left", 20.0, "top", 22.0),
    "poe": ("left", 20.0, "center", 0.0),
    "motor": ("left", 20.0, "bottom", 20.0),
    "motor_driver": ("left", 20.0, "bottom", 20.0),
    "digital": ("center", 0.0, "center", 0.0),
    "mcu": ("center", 0.0, "center", 0.0),
    "fpga": ("center", 0.0, "center", 0.0),
    "analog": ("center", 0.0, "bottom", 25.0),
    "audio": ("right", 30.0, "bottom", 24.0),
    "audio_amplifier": ("right", 30.0, "bottom", 24.0),
    "comms": ("right", 28.0, "center", 0.0),
    "communication": ("right", 28.0, "center", 0.0),
    "ethernet": ("right", 20.0, "center", 0.0),
    "usb": ("center", 0.0, "bottom", 8.0),
    "rf": ("right", 18.0, "top", 15.0),
    "transceiver": ("right", 28.0, "top", 22.0),
    "clock": ("center", 0.0, "top", 22.0),
    "connector": ("center", 0.0, "bottom", 7.0),
    "debug": ("left", 22.0, "bottom", 8.0),
    "sensor": ("right", 24.0, "bottom", 24.0),
    "sensors": ("right", 24.0, "bottom", 24.0),
    "storage": ("right", 34.0, "center", 0.0),
    "protection": ("left", 9.0, "center", 0.0),
    "passive": ("center", 0.0, "bottom", 18.0),
    "other": ("center", 0.0, "center", 0.0),
}


def _axis_anchor_mm(edge: str, offset_mm: float, extent_mm: float) -> float:
    # Very small auto-sized boards cannot sustain the nominal edge distance.
    # Saturate at 22.5% of the axis while retaining the absolute distance
    # on ordinary boards.
    effective_offset = min(offset_mm, extent_mm * 0.225)
    if edge == "left" or edge == "top":
        return effective_offset
    if edge == "right" or edge == "bottom":
        return extent_mm - effective_offset
    if edge == "center":
        return extent_mm / 2.0
    raise ValueError(f"unknown placement edge {edge!r}")


def _zone_center_mm(category: str, config: PlacementConfig) -> tuple[float, float]:
    anchor = _ZONE_ANCHORS.get(category, _ZONE_ANCHORS["other"])
    return (
        _axis_anchor_mm(anchor[0], anchor[1], config.board_width_mm),
        _axis_anchor_mm(anchor[2], anchor[3], config.board_height_mm),
    )

_PLACEMENT_CONSTRAINT_KINDS = {"placement", "keepout"}
_BOARD_TARGETS = {"board", "board_outline", "pcb", "outline"}
_EDGES = {"left", "right", "top", "bottom"}
_WITHIN_RE = re.compile(
    r"\bwithin\s+(?P<distance>\d+(?:\.\d+)?)\s*mm\s+of\s+(?P<ref>[A-Za-z]+\d+)\b",
    re.IGNORECASE,
)
_EDGE_RE = re.compile(r"\b(left|right|top|bottom)\s+edge\b", re.IGNORECASE)
_FOOTPRINT_DIMENSION_RE = re.compile(
    r"(?<![A-Z0-9.])(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)MM\b",
    re.IGNORECASE,
)

_GROUND_NET_PREFIXES = ("GND", "AGND", "DGND", "PGND", "VSS", "GNDA", "GNDD")
_POWER_NET_PREFIXES = ("VDD", "VCC", "VBUS", "VIN", "VBAT", "VSYS", "AVDD", "DVDD")
_HIGH_SPEED_NET_TOKENS = (
    "USB",
    "ETH",
    "RGMII",
    "RMII",
    "MII",
    "SPI",
    "QSPI",
    "I2C",
    "UART",
    "CLK",
    "XTAL",
    "SCL",
    "SDA",
    "MOSI",
    "MISO",
    "SCK",
    "TX",
    "RX",
    "DP",
    "DM",
)


@dataclass
class PlacementConfig:
    """Configuration for the placement optimizer."""

    board_width_mm: float = 100.0
    board_height_mm: float = 80.0
    edge_clearance_mm: float = 1.0
    min_component_gap_mm: float = 0.5
    support_body_clearance_mm: float = 1.5
    strategy: str = "balanced"  # simple, thermal, si, cost, balanced
    ambient_temp_c: float = 25.0
    iterations: int = 5000
    initial_temp: float = 100.0
    cooling_rate: float = 0.995
    seed: int | None = None
    restarts: int = 3
    refinement_passes: int = 2


@dataclass
class ComponentPlacement:
    """Placement state for a single component."""

    ref: str
    x: float
    y: float
    rotation: float = 0.0
    layer: str = "front"
    width: float = 2.0
    height: float = 1.0
    category: str = "other"
    is_power: bool = False
    thermal_dissipation_w: float = 0.0
    requires_impedance_control: bool = False
    parent_ref: str = ""
    placement_role: str = ""
    block_id: str = ""
    functional_section: str = ""
    locked: bool = False
    constraint_locked: bool = False
    edge_constrained: bool = False
    rotation_locked: bool = False
    geometry_status: str = "estimated"
    net_pad_offsets: dict[str, list[tuple[float, float]]] | None = None


@dataclass
class _ConstraintPlan:
    """Normalized machine-applicable placement constraints."""

    supplied_count: int = 0
    board_dimension_source: str = "default"
    fixed: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    affinities: list[dict[str, Any]] = field(default_factory=list)
    keepouts: list[dict[str, Any]] = field(default_factory=list)
    applied: list[dict[str, Any]] = field(default_factory=list)
    deferred: list[dict[str, Any]] = field(default_factory=list)
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def component_part_number(comp: ComponentDef) -> str:
    """Return the original BOM MPN when available, else the registry MPN."""
    return str(comp.source_mpn or comp.mpn or "").strip()


def estimate_footprint_size(footprint: str, reference: str = "") -> tuple[float, float]:
    """Estimate a physical courtyard from one KiCad footprint identifier."""
    fp = str(footprint or "").upper()
    normalized_fp = fp.replace("_", "-")
    for pattern, size in _FOOTPRINT_SIZES.items():
        if pattern.upper() in normalized_fp:
            return size
    dimensions = [
        (float(match.group(1)), float(match.group(2)))
        for match in _FOOTPRINT_DIMENSION_RE.finditer(normalized_fp)
    ]
    if dimensions:
        return max(dimensions, key=lambda size: size[0] * size[1])
    # Default based on category
    if not fp:
        # Missing geometry is rendered as an obvious placeholder and blocked
        # elsewhere; do not let a reference prefix imply physical accuracy.
        return (4.0, 3.0)
    prefix = str(reference or "")[:1].upper()
    if prefix in ("R", "C", "L"):
        return (1.6, 0.8)  # 0603 default
    if prefix == "U":
        return (12.0, 12.0)  # conservative generic IC/courtyard
    if prefix == "J":
        return (8.0, 5.0)  # connector
    return (2.0, 2.0)


def estimate_component_size(comp: ComponentDef) -> tuple[float, float]:
    """Estimate component physical size using the shared footprint model."""
    width = getattr(comp, "placement_width_mm", None)
    height = getattr(comp, "placement_height_mm", None)
    if all(isinstance(v, (float, int)) and math.isfinite(v) and v > 0 for v in (width, height)):
        return float(width), float(height)
    return estimate_footprint_size(comp.footprint, comp.source_ref)


# Backward-compatible private alias for integrations that imported the old
# helper.  The viewer now consumes the public helper so both use one table.
_estimate_size = estimate_component_size


def _load_thermal_specs(specs_dir: Path | None) -> dict[str, dict]:
    """Load thermal specs from specs/ic_thermal.json if available."""
    if not specs_dir:
        return {}
    path = specs_dir / "ic_thermal.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_si_specs(specs_dir: Path | None) -> dict[str, dict]:
    """Load SI specs from specs/si_params.json if available."""
    if not specs_dir:
        return {}
    path = specs_dir / "si_params.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _finite_number(value: Any) -> float | None:
    """Return a finite float for numeric constraint input, else ``None``."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _constraint_kind(raw: dict[str, Any]) -> str:
    return str(raw.get("kind") or raw.get("type") or "").strip().lower()


def _constraint_target(raw: dict[str, Any]) -> str:
    return str(raw.get("target") or raw.get("ref") or "").strip()


def _position_values(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    nested = raw.get("position") if isinstance(raw.get("position"), dict) else {}
    x = _finite_number(raw.get("x_mm", raw.get("x", nested.get("x_mm", nested.get("x")))))
    y = _finite_number(raw.get("y_mm", raw.get("y", nested.get("y_mm", nested.get("y")))))
    return x, y


def _derived_review_board(components: list[ComponentDef], config: PlacementConfig) -> tuple[float, float]:
    """Build a compact review canvas when no mechanical outline was supplied."""
    if not components:
        return (30.0, 25.0)
    packing_margin = config.min_component_gap_mm + 0.75
    expanded_area = 0.0
    largest_width = 0.0
    largest_height = 0.0
    for component in components:
        width, height = estimate_component_size(component)
        expanded_area += (width + 2 * packing_margin) * (height + 2 * packing_margin)
        largest_width = max(largest_width, width)
        largest_height = max(largest_height, height)

    # A 32% density leaves room for support rings, routing channels, and
    # interactive edits while avoiding the mostly-empty legacy 100x80 canvas.
    required_area = expanded_area / 0.32
    aspect_ratio = 1.35
    width = math.sqrt(required_area * aspect_ratio)
    height = required_area / width
    width = max(30.0, largest_width + 2 * config.edge_clearance_mm + 8.0, width)
    height = max(25.0, largest_height + 2 * config.edge_clearance_mm + 8.0, height)
    width = min(100.0, math.ceil(width / 5.0) * 5.0)
    height = min(80.0, math.ceil(height / 5.0) * 5.0)
    return (width, height)


def _board_dimensions(
    config: PlacementConfig,
    constraints: list[dict[str, Any]],
    plan: _ConstraintPlan,
) -> PlacementConfig:
    width = float(config.board_width_mm)
    height = float(config.board_height_mm)
    for raw in constraints:
        target = _constraint_target(raw).lower()
        nested = raw.get("board") if isinstance(raw.get("board"), dict) else {}
        explicit_board_keys = "board_width_mm" in raw or "board_height_mm" in raw or bool(nested)
        if target not in _BOARD_TARGETS and not explicit_board_keys:
            continue
        candidate_width = _finite_number(
            raw.get(
                "board_width_mm",
                nested.get("width_mm", raw.get("width_mm") if target in _BOARD_TARGETS else None),
            )
        )
        candidate_height = _finite_number(
            raw.get(
                "board_height_mm",
                nested.get("height_mm", raw.get("height_mm") if target in _BOARD_TARGETS else None),
            )
        )
        if candidate_width is not None:
            width = candidate_width
        if candidate_height is not None:
            height = candidate_height
        if candidate_width is not None or candidate_height is not None:
            plan.board_dimension_source = "constraints"
            plan.applied.append(
                {
                    "kind": "board_outline",
                    "target": target or "board",
                    "width_mm": width,
                    "height_mm": height,
                }
            )

    for name, value in (("board_width_mm", width), ("board_height_mm", height)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")
    if (
        config.edge_clearance_mm < 0
        or config.min_component_gap_mm < 0
        or config.support_body_clearance_mm < 0
    ):
        raise ValueError("Placement clearances must be non-negative")
    return replace(config, board_width_mm=width, board_height_mm=height)


def _normalize_keepout(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
    x = _finite_number(raw.get("x_mm", raw.get("x")))
    y = _finite_number(raw.get("y_mm", raw.get("y")))
    width = _finite_number(raw.get("width_mm", raw.get("width")))
    height = _finite_number(raw.get("height_mm", raw.get("height")))
    x1 = _finite_number(raw.get("x1_mm", raw.get("x1")))
    y1 = _finite_number(raw.get("y1_mm", raw.get("y1")))
    x2 = _finite_number(raw.get("x2_mm", raw.get("x2")))
    y2 = _finite_number(raw.get("y2_mm", raw.get("y2")))
    if None not in (x1, y1, x2, y2):
        left, right = sorted((float(x1), float(x2)))
        top, bottom = sorted((float(y1), float(y2)))
        x, y, width, height = left, top, right - left, bottom - top
    if None in (x, y, width, height) or float(width) <= 0 or float(height) <= 0:
        return None
    return {
        "id": str(raw.get("id") or _constraint_target(raw) or f"keepout_{index + 1}"),
        "x_mm": float(x),
        "y_mm": float(y),
        "width_mm": float(width),
        "height_mm": float(height),
    }


def _build_constraint_plan(
    components: list[ComponentDef],
    config: PlacementConfig,
    constraints: list[dict[str, Any]] | None,
) -> tuple[PlacementConfig, _ConstraintPlan]:
    """Normalize the documented placement subset and expose everything else."""
    raw_constraints = [item for item in (constraints or []) if isinstance(item, dict)]
    plan = _ConstraintPlan(supplied_count=len(raw_constraints))
    if (config.board_width_mm, config.board_height_mm) != (100.0, 80.0):
        plan.board_dimension_source = "config"
    else:
        derived_width, derived_height = _derived_review_board(components, config)
        config = replace(
            config,
            board_width_mm=derived_width,
            board_height_mm=derived_height,
        )
        plan.board_dimension_source = "derived_review"
    effective_config = _board_dimensions(config, raw_constraints, plan)
    refs = {str(comp.source_ref or "").strip() for comp in components if comp.source_ref}

    for index, raw in enumerate(raw_constraints):
        kind = _constraint_kind(raw)
        target = _constraint_target(raw)
        target_lower = target.lower()
        if kind not in _PLACEMENT_CONSTRAINT_KINDS:
            plan.deferred.append(
                {
                    "index": index,
                    "kind": kind or "unspecified",
                    "target": target,
                    "reason": "Constraint belongs to a non-placement stage.",
                }
            )
            continue

        has_board_keys = (
            target_lower in _BOARD_TARGETS
            or "board_width_mm" in raw
            or "board_height_mm" in raw
            or isinstance(raw.get("board"), dict)
        )
        if kind == "placement" and has_board_keys:
            # Already recorded by _board_dimensions. A board entry may also
            # carry clearance policy, which is applied below.
            edge_clearance = _finite_number(raw.get("edge_clearance_mm"))
            min_gap = _finite_number(raw.get("min_component_gap_mm"))
            if edge_clearance is not None or min_gap is not None:
                if edge_clearance is not None and edge_clearance < 0:
                    raise ValueError("edge_clearance_mm must be non-negative")
                if min_gap is not None and min_gap < 0:
                    raise ValueError("min_component_gap_mm must be non-negative")
                effective_config = replace(
                    effective_config,
                    edge_clearance_mm=(
                        edge_clearance
                        if edge_clearance is not None
                        else effective_config.edge_clearance_mm
                    ),
                    min_component_gap_mm=(
                        min_gap if min_gap is not None else effective_config.min_component_gap_mm
                    ),
                )
                plan.applied.append(
                    {
                        "kind": "board_clearance",
                        "target": target or "board",
                        "edge_clearance_mm": effective_config.edge_clearance_mm,
                        "min_component_gap_mm": effective_config.min_component_gap_mm,
                    }
                )
            continue

        if kind == "keepout":
            keepout = _normalize_keepout(raw, index)
            if keepout is None:
                plan.unsupported.append(
                    {
                        "index": index,
                        "kind": kind,
                        "target": target,
                        "reason": "Rectangular keepout requires x/y/width/height (mm).",
                    }
                )
            else:
                plan.keepouts.append(keepout)
                plan.applied.append({"kind": "keepout", "target": target, **keepout})
            continue

        if not target or target not in refs:
            plan.unsupported.append(
                {
                    "index": index,
                    "kind": kind,
                    "target": target,
                    "reason": "Placement target is missing from the assembly inventory.",
                }
            )
            continue

        description = str(raw.get("constraint") or raw.get("description") or "")
        x, y = _position_values(raw)
        rotation = _finite_number(raw.get("rotation"))
        layer = str(raw.get("layer") or "front").lower()
        if x is not None and y is not None:
            if layer not in {"front", "back"}:
                plan.unsupported.append(
                    {
                        "index": index,
                        "kind": kind,
                        "target": target,
                        "reason": "Fixed placement layer must be 'front' or 'back'.",
                    }
                )
                continue
            fixed = {
                "x_mm": x,
                "y_mm": y,
                "rotation": (rotation or 0.0) % 360,
                "layer": layer,
            }
            if target in plan.fixed:
                plan.warnings.append(f"Later fixed placement replaced an earlier constraint for {target}.")
            plan.fixed[target] = fixed
            plan.applied.append({"kind": "fixed_position", "target": target, **fixed})
            continue

        edge = str(raw.get("edge") or "").lower()
        edge_match = _EDGE_RE.search(description)
        if not edge and edge_match:
            edge = edge_match.group(1).lower()
        if edge in _EDGES:
            edge_rotation = None
            if "rotation" in raw:
                edge_rotation = _finite_number(raw.get("rotation"))
                if edge_rotation is None:
                    plan.unsupported.append(
                        {
                            "index": index,
                            "kind": kind,
                            "target": target,
                            "reason": "Edge placement rotation must be a finite number of degrees.",
                        }
                    )
                    continue
                edge_rotation %= 360.0
            tolerance = _finite_number(raw.get("max_distance_mm", raw.get("tolerance_mm", 1.0)))
            plan.edges[target] = {
                "edge": edge,
                "max_distance_mm": max(0.0, tolerance or 0.0),
                **({"rotation": edge_rotation} if edge_rotation is not None else {}),
            }
            plan.applied.append({"kind": "edge", "target": target, **plan.edges[target]})
            continue

        near = str(raw.get("near") or raw.get("near_ref") or "").strip()
        max_distance = _finite_number(raw.get("max_distance_mm", raw.get("distance_mm")))
        within_match = _WITHIN_RE.search(description)
        if within_match and "pin" in description.lower() and not near:
            plan.unsupported.append(
                {
                    "index": index,
                    "kind": kind,
                    "target": target,
                    "reason": (
                        "Pin-relative placement requires resolved footprint pad coordinates; "
                        "the component-center optimizer cannot apply it safely."
                    ),
                }
            )
            continue
        if within_match:
            near = near or within_match.group("ref")
            max_distance = max_distance or float(within_match.group("distance"))
        if near and max_distance is not None and max_distance >= 0 and near in refs:
            affinity = {"target": target, "near": near, "max_distance_mm": max_distance}
            plan.affinities.append(affinity)
            plan.applied.append({"kind": "affinity", **affinity})
            continue

        plan.unsupported.append(
            {
                "index": index,
                "kind": kind,
                "target": target,
                "reason": (
                    "Placement constraint needs fixed x/y, a named edge, or near/max_distance_mm."
                ),
            }
        )

    return effective_config, plan


def _init_placements(
    components: list[ComponentDef],
    config: PlacementConfig,
    thermal_specs: dict[str, dict],
    si_specs: dict[str, dict],
    constraint_plan: _ConstraintPlan | None = None,
) -> list[ComponentPlacement]:
    """Create initial placement state with zone-based positions."""
    placements: list[ComponentPlacement] = []
    placement_by_ref: dict[str, ComponentPlacement] = {}
    zone_counters: dict[str, int] = {}
    support_counters: dict[str, int] = {}

    # Owners must be placed before generated support parts so bypass,
    # bootstrap, feedback, and strap parts start next to the correct IC.
    ordered = sorted(
        enumerate(components),
        key=lambda item: (bool(getattr(item[1], "placement_parent_ref", "")), item[0]),
    )

    for _original_index, comp in ordered:
        if not comp.source_ref:
            continue

        w, h = estimate_component_size(comp)
        cat = (comp.category or "other").lower()
        if cat not in _ZONE_ANCHORS:
            cat = "passive" if comp.source_ref[0] in "RCL" else "other"
        parent_ref = str(getattr(comp, "placement_parent_ref", "") or "")
        placement_role = str(getattr(comp, "placement_role", "") or "")
        parent = placement_by_ref.get(parent_ref)

        if parent is not None:
            support_idx = support_counters.get(parent_ref, 0)
            support_counters[parent_ref] = support_idx + 1
            # Walk clockwise around the owner. The first support part lands
            # on the owner's power-entry side; later parts fan out in a tight
            # ring instead of falling into a board-wide passive bank.
            offsets = ((-1, 0), (0, -1), (1, 0), (0, 1), (-1, -1), (1, -1), (1, 1), (-1, 1))
            ox, oy = offsets[support_idx % len(offsets)]
            ring = support_idx // len(offsets)
            radius_x = parent.width / 2 + w / 2 + config.min_component_gap_mm + 0.6 + ring * (w + 0.6)
            radius_y = parent.height / 2 + h / 2 + config.min_component_gap_mm + 0.6 + ring * (h + 0.6)
            x = parent.x + ox * radius_x
            y = parent.y + oy * radius_y
        else:
            zone_cx, zone_cy = _zone_center_mm(cat, config)

            idx = zone_counters.get(cat, 0)
            zone_counters[cat] = idx + 1
            cols = max(1, int(math.sqrt(idx + 1)))
            row, col = divmod(idx, cols)
            offset_x = col * (w + config.min_component_gap_mm + 1.0)
            offset_y = row * (h + config.min_component_gap_mm + 1.0)
            x = zone_cx + offset_x
            y = zone_cy + offset_y

        x = max(
            config.edge_clearance_mm + w / 2,
            min(x, config.board_width_mm - config.edge_clearance_mm - w / 2),
        )
        y = max(
            config.edge_clearance_mm + h / 2,
            min(y, config.board_height_mm - config.edge_clearance_mm - h / 2),
        )

        fixed = (constraint_plan.fixed if constraint_plan else {}).get(comp.source_ref)
        edge_constraint = (constraint_plan.edges if constraint_plan else {}).get(comp.source_ref)
        rotation = (
            float(fixed.get("rotation", 0.0))
            if fixed
            else float(edge_constraint.get("rotation", 0.0))
            if edge_constraint
            else 0.0
        )
        layer = str(fixed.get("layer", "front")) if fixed else str(getattr(comp, "placement_layer", "front"))
        effective_w, effective_h = _rotated_dimensions(w, h, rotation)
        if edge_constraint:
            edge = edge_constraint["edge"]
            if edge == "left":
                x = config.edge_clearance_mm + effective_w / 2
            elif edge == "right":
                x = config.board_width_mm - config.edge_clearance_mm - effective_w / 2
            elif edge == "top":
                y = config.edge_clearance_mm + effective_h / 2
            elif edge == "bottom":
                y = config.board_height_mm - config.edge_clearance_mm - effective_h / 2
        if fixed:
            x = float(fixed["x_mm"])
            y = float(fixed["y_mm"])

        mpn = component_part_number(comp)
        thermal = thermal_specs.get(mpn, {})
        si = si_specs.get(mpn, {})

        placement = ComponentPlacement(
            ref=comp.source_ref,
            x=x,
            y=y,
            rotation=rotation,
            layer=layer,
            width=w,
            height=h,
            category=cat,
            is_power=cat in {"power", "regulator", "poe"},
            thermal_dissipation_w=thermal.get("pdiss_max_w", 0.0)
            if isinstance(thermal.get("pdiss_max_w"), (int, float))
            else 0.0,
            requires_impedance_control=si.get("requires_impedance_control", False),
            parent_ref=parent_ref,
            placement_role=placement_role,
            block_id=str(getattr(comp, "block_id", "") or "").strip(),
            functional_section=str(getattr(comp, "functional_section", "") or "").strip(),
            locked=bool(fixed),
            constraint_locked=bool(fixed),
            edge_constrained=bool(edge_constraint),
            rotation_locked=bool(edge_constraint and "rotation" in edge_constraint and not fixed),
            net_pad_offsets=getattr(comp, "placement_pad_offsets", None),
            geometry_status=str(
                getattr(comp, "placement_geometry_status", "estimated") or "estimated"
            ),
        )
        placements.append(placement)
        placement_by_ref[placement.ref] = placement

    return placements


def _overlap_area(a: ComponentPlacement, b: ComponentPlacement, gap: float = 0.0) -> float:
    """Calculate overlap area between two components (including gap)."""
    aw, ah = _effective_dimensions(a)
    bw, bh = _effective_dimensions(b)
    # Split the requested edge-to-edge gap between the two courtyards.
    ax1, ay1 = a.x - (aw + gap) / 2, a.y - (ah + gap) / 2
    ax2, ay2 = a.x + (aw + gap) / 2, a.y + (ah + gap) / 2
    bx1, by1 = b.x - (bw + gap) / 2, b.y - (bh + gap) / 2
    bx2, by2 = b.x + (bw + gap) / 2, b.y + (bh + gap) / 2

    dx = min(ax2, bx2) - max(ax1, bx1)
    dy = min(ay2, by2) - max(ay1, by1)
    if dx > 0 and dy > 0:
        return dx * dy
    return 0.0


def _effective_dimensions(placement: ComponentPlacement) -> tuple[float, float]:
    """Conservative axis-aligned extent, including non-orthogonal rotation."""
    return _rotated_dimensions(placement.width, placement.height, placement.rotation)


def _rotated_dimensions(width: float, height: float, rotation: float) -> tuple[float, float]:
    rotation %= 180
    if rotation == 0:
        return width, height
    if rotation == 90:
        return height, width
    angle = math.radians(rotation)
    cosine, sine = abs(math.cos(angle)), abs(math.sin(angle))
    return (
        round(width * cosine + height * sine, 12),
        round(width * sine + height * cosine, 12),
    )


def _rectangle_clearance(a: ComponentPlacement, b: ComponentPlacement) -> float:
    """Return edge-to-edge clearance between rotated rectangular courtyards."""
    aw, ah = _effective_dimensions(a)
    bw, bh = _effective_dimensions(b)
    dx = max(0.0, abs(a.x - b.x) - (aw + bw) / 2)
    dy = max(0.0, abs(a.y - b.y) - (ah + bh) / 2)
    return math.hypot(dx, dy)


def _is_parent_support_pair(a: ComponentPlacement, b: ComponentPlacement) -> bool:
    return a.parent_ref == b.ref or b.parent_ref == a.ref


def _intersects_keepout(placement: ComponentPlacement, keepout: dict[str, Any]) -> bool:
    width, height = _effective_dimensions(placement)
    left = placement.x - width / 2
    right = placement.x + width / 2
    top = placement.y - height / 2
    bottom = placement.y + height / 2
    keepout_left = float(keepout["x_mm"])
    keepout_top = float(keepout["y_mm"])
    keepout_right = keepout_left + float(keepout["width_mm"])
    keepout_bottom = keepout_top + float(keepout["height_mm"])
    return (
        right > keepout_left
        and left < keepout_right
        and bottom > keepout_top
        and top < keepout_bottom
    )


def _cost_overlap(placements: list[ComponentPlacement], gap: float) -> float:
    """Penalty for overlapping components."""
    total = 0.0
    for i in range(len(placements)):
        for j in range(i + 1, len(placements)):
            total += _overlap_area(placements[i], placements[j], gap)
    return total * 1000.0  # Heavy penalty


def _cost_support_body_clearance(
    placements: list[ComponentPlacement], config: PlacementConfig
) -> float:
    by_ref = {placement.ref: placement for placement in placements}
    total = 0.0
    for support in placements:
        if not support.parent_ref:
            continue
        parent = by_ref.get(support.parent_ref)
        if parent is None:
            continue
        clearance = _rectangle_clearance(support, parent)
        if clearance < config.support_body_clearance_mm:
            total += (config.support_body_clearance_mm - clearance) ** 2 * 10_000.0
    return total


def _cost_keepouts(
    placements: list[ComponentPlacement], constraint_plan: _ConstraintPlan | None
) -> float:
    if not constraint_plan or not constraint_plan.keepouts:
        return 0.0
    return 100_000.0 * sum(
        1
        for placement in placements
        for keepout in constraint_plan.keepouts
        if _intersects_keepout(placement, keepout)
    )


def _cost_boundary(placements: list[ComponentPlacement], config: PlacementConfig) -> float:
    """Penalty for components outside board boundary."""
    total = 0.0
    for p in placements:
        margin = config.edge_clearance_mm
        width, height = _effective_dimensions(p)
        if p.x - width / 2 < margin:
            total += (margin - (p.x - width / 2)) ** 2
        if p.x + width / 2 > config.board_width_mm - margin:
            total += ((p.x + width / 2) - (config.board_width_mm - margin)) ** 2
        if p.y - height / 2 < margin:
            total += (margin - (p.y - height / 2)) ** 2
        if p.y + height / 2 > config.board_height_mm - margin:
            total += ((p.y + height / 2) - (config.board_height_mm - margin)) ** 2
    return total * 500.0


def _cost_thermal(placements: list[ComponentPlacement]) -> float:
    """Penalty for hot components placed too close together."""
    total = 0.0
    hot = [p for p in placements if p.thermal_dissipation_w > 0.1]
    for i in range(len(hot)):
        for j in range(i + 1, len(hot)):
            dist = math.hypot(hot[i].x - hot[j].x, hot[i].y - hot[j].y)
            combined_heat = hot[i].thermal_dissipation_w + hot[j].thermal_dissipation_w
            if dist < 15.0:  # Within 15mm
                total += combined_heat * (15.0 - dist)
    return total * 10.0


def _cost_zone(placements: list[ComponentPlacement], config: PlacementConfig) -> float:
    """Penalty for components far from their preferred zone."""
    total = 0.0
    for p in placements:
        ideal_x, ideal_y = _zone_center_mm(p.category, config)
        dist = math.hypot(p.x - ideal_x, p.y - ideal_y)
        total += dist * 0.1
    return total


def _net_weight(net_name: str) -> float:
    upper = (net_name or "").upper()
    if not upper or upper.startswith(_GROUND_NET_PREFIXES):
        return 0.0
    if upper.startswith(_POWER_NET_PREFIXES):
        return 0.5
    if any(token in upper for token in _HIGH_SPEED_NET_TOKENS):
        return 3.0
    return 1.0


def _build_connectivity_pairs(components: list[ComponentDef]) -> dict[tuple[str, str], float]:
    """Collapse shared nets into weighted functional-block attractions.

    Generated passives inherit their owner's identity here. Treating every
    decoupler on a power rail as an independent graph vertex creates a dense
    all-to-all clique that overwhelms useful block-to-block connectivity.
    """
    pair_weights: dict[tuple[str, str], float] = {}
    owner_by_ref = {
        str(component.source_ref): str(
            getattr(component, "placement_parent_ref", "") or component.source_ref
        )
        for component in components
        if component.source_ref
    }
    for net_name, refs in _build_net_component_map(components).items():
        block_refs = sorted({owner_by_ref.get(ref, ref) for ref in refs})
        if len(block_refs) < 2:
            continue
        weight = _net_weight(net_name)
        if weight <= 0:
            continue
        for a, b in combinations(block_refs, 2):
            key = tuple(sorted((a, b)))
            # A shared bus must not grow quadratically stronger than an
            # equally important point-to-point net as its fanout increases.
            pair_weights[key] = pair_weights.get(key, 0.0) + weight / (len(block_refs) - 1)
    return pair_weights


def _cost_connectivity(
    placements: list[ComponentPlacement],
    connectivity_pairs: dict[tuple[str, str], float],
) -> float:
    """Penalty for placing connected components far apart."""
    if placements and all(p.net_pad_offsets is not None for p in placements):
        # Real-board placement evaluates the rotated pad endpoints. Component
        # centers cannot distinguish a useful 180-degree rotation from one
        # that points every signal pad away from its destination.
        endpoints: dict[str, list[tuple[float, float]]] = {}
        for p in placements:
            angle = math.radians(p.rotation)
            c, s = math.cos(angle), math.sin(angle)
            for net, offsets in (p.net_pad_offsets or {}).items():
                endpoints.setdefault(net, []).extend(
                    (p.x + x * c + y * s, p.y - x * s + y * c) for x, y in offsets
                )
        total = 0.0
        for net, points in endpoints.items():
            if len(points) < 2:
                continue
            xs, ys = zip(*points)
            hpwl = max(xs) - min(xs) + max(ys) - min(ys)
            total += hpwl * hpwl * _net_weight(net) / (len(points) - 1)
        return total * 0.2
    if not connectivity_pairs:
        return 0.0
    placement_by_ref = {p.ref: p for p in placements}
    total = 0.0
    for (ref_a, ref_b), weight in connectivity_pairs.items():
        a = placement_by_ref.get(ref_a)
        b = placement_by_ref.get(ref_b)
        if a is None or b is None:
            continue
        dx = a.x - b.x
        dy = a.y - b.y
        total += (dx * dx + dy * dy) * weight
    return total * 0.2


def _cost_parent_affinity(placements: list[ComponentPlacement]) -> float:
    """Keep generated support parts close to the IC they electrically serve."""
    by_ref = {placement.ref: placement for placement in placements}
    total = 0.0
    for placement in placements:
        if not placement.parent_ref:
            continue
        parent = by_ref.get(placement.parent_ref)
        if parent is None:
            total += 10_000.0
            continue
        distance, _distance_model = _parent_affinity_distance(parent, placement)
        critical = placement.placement_role in {"decoupling", "bootstrap", "feedback", "crystal_load"}
        preferred = 4.0 if critical else 8.0
        weight = 35.0 if critical else 12.0
        total += distance * weight
        if distance > preferred:
            total += (distance - preferred) ** 2 * weight * 5.0
    return total


def _rotated_net_endpoints(
    placement: ComponentPlacement, net: str,
) -> list[tuple[float, float]]:
    offsets = (placement.net_pad_offsets or {}).get(net, [])
    angle = math.radians(placement.rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    return [
        (placement.x + x * cosine + y * sine, placement.y - x * sine + y * cosine)
        for x, y in offsets
    ]


def _parent_affinity_distance(
    parent: ComponentPlacement, support: ComponentPlacement,
) -> tuple[float, str]:
    """Measure support proximity at shared non-ground pads, falling back to centers.

    Ground-only coincidence is deliberately not treated as a placement anchor:
    the shared return plane connects nearly every block and would pull unrelated
    support parts toward arbitrary ground pads.
    """
    if parent.net_pad_offsets is not None and support.net_pad_offsets is not None:
        common_signal_nets = [
            net
            for net in parent.net_pad_offsets.keys() & support.net_pad_offsets.keys()
            if _net_weight(net) > 0
            and parent.net_pad_offsets.get(net)
            and support.net_pad_offsets.get(net)
        ]
        endpoint_distances = [
            math.hypot(parent_point[0] - support_point[0], parent_point[1] - support_point[1])
            for net in common_signal_nets
            for parent_point in _rotated_net_endpoints(parent, net)
            for support_point in _rotated_net_endpoints(support, net)
        ]
        if endpoint_distances:
            return min(endpoint_distances), "pad_endpoints"
    return math.hypot(parent.x - support.x, parent.y - support.y), "component_centers"


def _cost_edge_affinity(placements: list[ComponentPlacement], config: PlacementConfig) -> float:
    """Penalize externally accessible and antenna parts placed deep inside the board."""
    total = 0.0
    for placement in placements:
        if placement.category not in {"connector", "usb", "debug", "rf"}:
            continue
        width, height = _effective_dimensions(placement)
        distances = (
            placement.x - width / 2,
            config.board_width_mm - (placement.x + width / 2),
            placement.y - height / 2,
            config.board_height_mm - (placement.y + height / 2),
        )
        nearest_edge = max(0.0, min(distances))
        target = config.edge_clearance_mm + (3.0 if placement.category == "rf" else 1.0)
        if nearest_edge > target:
            total += (nearest_edge - target) ** 2 * (20.0 if placement.category == "rf" else 8.0)
    return total


def _cost_noise_separation(placements: list[ComponentPlacement]) -> float:
    """Keep RF/clock/analog circuitry away from switching power blocks."""
    noisy = [p for p in placements if p.category in {"power", "regulator", "poe"} and not p.parent_ref]
    sensitive = [
        p
        for p in placements
        if p.category in {"rf", "clock", "analog", "sensor", "sensors"} and not p.parent_ref
    ]
    total = 0.0
    for source in noisy:
        for victim in sensitive:
            distance = math.hypot(source.x - victim.x, source.y - victim.y)
            if distance < 12.0:
                total += (12.0 - distance) ** 2 * 8.0
    return total


def _cost_compactness(
    placements: list[ComponentPlacement], config: PlacementConfig
) -> float:
    """Discourage sparse island placement when connectivity data is incomplete."""
    internal = [
        placement
        for placement in placements
        if placement.category not in {"connector", "usb", "debug", "rf"}
        and not placement.parent_ref
        and not placement.constraint_locked
    ]
    if len(internal) < 2:
        return 0.0
    center_x = sum(placement.x for placement in internal) / len(internal)
    center_y = sum(placement.y for placement in internal) / len(internal)
    radial = sum(
        math.hypot(placement.x - center_x, placement.y - center_y) for placement in internal
    )
    min_x = min(placement.x for placement in internal)
    max_x = max(placement.x for placement in internal)
    min_y = min(placement.y for placement in internal)
    max_y = max(placement.y for placement in internal)
    content_center_x = (min_x + max_x) / 2
    content_center_y = (min_y + max_y) / 2
    board_center_x = config.board_width_mm / 2
    board_center_y = config.board_height_mm / 2
    centering = (content_center_x - board_center_x) ** 2 + (
        content_center_y - board_center_y
    ) ** 2
    return radial * 2.5 + (max_x - min_x) * (max_y - min_y) * 0.4 + centering * 20.0


def _functional_group_key(placement: ComponentPlacement) -> tuple[str, str] | None:
    """Use the narrowest available design grouping identifier."""
    block_id = placement.block_id.strip()
    if block_id:
        return ("block_id", block_id.casefold())
    section = placement.functional_section.strip()
    if section:
        return ("functional_section", section.casefold())
    return None


def _functional_groups(
    placements: list[ComponentPlacement],
) -> dict[tuple[str, str], list[ComponentPlacement]]:
    groups: dict[tuple[str, str], list[ComponentPlacement]] = {}
    for placement in placements:
        if placement.parent_ref:
            continue
        key = _functional_group_key(placement)
        if key is not None:
            groups.setdefault(key, []).append(placement)
    return groups


def _functional_group_metrics(
    placements: list[ComponentPlacement],
) -> list[dict[str, Any]]:
    metrics = []
    for (kind, key), members in sorted(_functional_groups(placements).items()):
        if len(members) < 2:
            continue
        distances = [
            math.hypot(a.x - b.x, a.y - b.y)
            for a, b in combinations(members, 2)
        ]
        metrics.append(
            {
                "grouping": kind,
                "id": key,
                "refs": sorted(member.ref for member in members),
                "member_count": len(members),
                "mean_pairwise_distance_mm": round(sum(distances) / len(distances), 2),
                "max_pairwise_distance_mm": round(max(distances, default=0.0), 2),
            }
        )
    return metrics


def _cost_functional_grouping(placements: list[ComponentPlacement]) -> float:
    """Encourage functional blocks to stay compact with a bounded influence.

    Pair distances are capped at 20 mm so an isolated block cannot dominate
    electrical, thermal, or hard mechanical objectives.
    """
    total = 0.0
    for members in _functional_groups(placements).values():
        if len(members) < 2:
            continue
        pair_cost = sum(
            min(math.hypot(a.x - b.x, a.y - b.y), 20.0) ** 2
            for a, b in combinations(members, 2)
        )
        total += pair_cost / (len(members) - 1) * 0.2
    return total


def _distance_to_edge(
    placement: ComponentPlacement, edge: str, config: PlacementConfig
) -> float:
    width, height = _effective_dimensions(placement)
    if edge == "left":
        return placement.x - width / 2 - config.edge_clearance_mm
    if edge == "right":
        return config.board_width_mm - config.edge_clearance_mm - (placement.x + width / 2)
    if edge == "top":
        return placement.y - height / 2 - config.edge_clearance_mm
    return config.board_height_mm - config.edge_clearance_mm - (placement.y + height / 2)


def _cost_supplied_constraints(
    placements: list[ComponentPlacement],
    config: PlacementConfig,
    constraint_plan: _ConstraintPlan | None,
) -> float:
    if not constraint_plan:
        return 0.0
    by_ref = {placement.ref: placement for placement in placements}
    total = _cost_keepouts(placements, constraint_plan)
    for ref, edge_rule in constraint_plan.edges.items():
        placement = by_ref.get(ref)
        if placement is None:
            continue
        distance = max(0.0, _distance_to_edge(placement, edge_rule["edge"], config))
        total += distance * distance * 2_000.0
    for affinity in constraint_plan.affinities:
        target = by_ref.get(affinity["target"])
        neighbor = by_ref.get(affinity["near"])
        if target is None or neighbor is None:
            continue
        distance = math.hypot(target.x - neighbor.x, target.y - neighbor.y)
        maximum = float(affinity["max_distance_mm"])
        if distance > maximum:
            total += (distance - maximum) ** 2 * 5_000.0
    return total


def _total_cost(
    placements: list[ComponentPlacement],
    config: PlacementConfig,
    connectivity_pairs: dict[tuple[str, str], float] | None = None,
    constraint_plan: _ConstraintPlan | None = None,
) -> float:
    """Compute total placement cost based on strategy."""
    cost = _cost_overlap(placements, config.min_component_gap_mm)
    cost += _cost_boundary(placements, config)
    cost += _cost_parent_affinity(placements)
    cost += _cost_support_body_clearance(placements, config)
    cost += _cost_edge_affinity(placements, config)
    cost += _cost_functional_grouping(placements)
    cost += _cost_supplied_constraints(placements, config, constraint_plan)

    if config.strategy in ("thermal", "balanced"):
        cost += _cost_thermal(placements)
    if config.strategy in ("si", "balanced"):
        cost += _cost_connectivity(placements, connectivity_pairs or {})
        cost += _cost_noise_separation(placements)
    if config.strategy in ("cost", "balanced"):
        cost += _cost_zone(placements, config)
        cost += _cost_compactness(placements, config)

    return cost


def _perturb(
    placements: list[ComponentPlacement], config: PlacementConfig, rng: random.Random,
    move_scale: float = 1.0,
) -> list[ComponentPlacement]:
    """Create a neighbor solution by moving one component."""
    new = [ComponentPlacement(**p.__dict__) for p in placements]
    movable = [index for index, placement in enumerate(new) if not placement.constraint_locked]
    if not movable:
        return new
    idx = rng.choice(movable)
    p = new[idx]

    move_type = rng.random()
    if move_type < 0.22:
        # Translate an electrical block as a unit. Moving only an owner first
        # creates a large parent-affinity barrier, which previously trapped
        # complete power/sensor islands in their initial corner zones.
        root_ref = p.parent_ref or p.ref
        root = next((placement for placement in new if placement.ref == root_ref), None)
        group = (
            []
            if root is not None and root.constraint_locked
            else [
                placement
                for placement in new
                if (placement.ref == root_ref or placement.parent_ref == root_ref)
                and not placement.constraint_locked
            ]
        )
        if group:
            dx = rng.gauss(0, 8.0 * move_scale)
            dy = rng.gauss(0, 8.0 * move_scale)
            margin = config.edge_clearance_mm
            minimum_dx = max(
                margin + _effective_dimensions(item)[0] / 2 - item.x for item in group
            )
            maximum_dx = min(
                config.board_width_mm - margin - _effective_dimensions(item)[0] / 2 - item.x
                for item in group
            )
            minimum_dy = max(
                margin + _effective_dimensions(item)[1] / 2 - item.y for item in group
            )
            maximum_dy = min(
                config.board_height_mm - margin - _effective_dimensions(item)[1] / 2 - item.y
                for item in group
            )
            dx = max(minimum_dx, min(dx, maximum_dx))
            dy = max(minimum_dy, min(dy, maximum_dy))
            for item in group:
                item.x += dx
                item.y += dy
            return new
    if move_type < 0.55:
        # Small move
        p.x += rng.gauss(0, 2.0 * move_scale)
        p.y += rng.gauss(0, 2.0 * move_scale)
    elif move_type < 0.75:
        # Occasional broad moves let sparse zone seeds converge on a compact
        # layout instead of remaining isolated across a large default canvas.
        p.x += rng.gauss(0, 8.0)
        p.y += rng.gauss(0, 8.0)
    elif move_type < 0.9 and len(movable) > 1:
        # Swap two components
        compatible = [
            index
            for index in movable
            if bool(new[index].parent_ref) == bool(p.parent_ref)
        ]
        other = rng.choice(compatible)
        if other != idx:
            new[idx].x, new[other].x = new[other].x, new[idx].x
            new[idx].y, new[other].y = new[other].y, new[idx].y
    else:
        # Rotate
        if not p.rotation_locked:
            p.rotation = rng.choice([0, 90, 180, 270])

    # Clamp to board using the rotated footprint extent.
    margin = config.edge_clearance_mm
    width, height = _effective_dimensions(p)
    p.x = max(margin + width / 2, min(p.x, config.board_width_mm - margin - width / 2))
    p.y = max(margin + height / 2, min(p.y, config.board_height_mm - margin - height / 2))

    return new


def _inside_board(
    placement: ComponentPlacement,
    config: PlacementConfig,
    constraint_plan: _ConstraintPlan | None = None,
) -> bool:
    width, height = _effective_dimensions(placement)
    margin = config.edge_clearance_mm
    inside = (
        placement.x - width / 2 >= margin
        and placement.x + width / 2 <= config.board_width_mm - margin
        and placement.y - height / 2 >= margin
        and placement.y + height / 2 <= config.board_height_mm - margin
    )
    return inside and not any(
        _intersects_keepout(placement, keepout)
        for keepout in (constraint_plan.keepouts if constraint_plan else [])
    )


def _legalize_overlaps(
    state: list[ComponentPlacement],
    config: PlacementConfig,
    constraint_plan: _ConstraintPlan | None = None,
) -> tuple[list[ComponentPlacement], int]:
    """Deterministically move colliding parts to the nearest free location.

    Simulated annealing is a proposal engine, not a packing guarantee.  This
    final legalization pass processes owners before their support parts and
    searches an expanding 0.5 mm grid, preventing a visually plausible result
    from still containing physically overlapping courtyards.
    """
    legalized: list[ComponentPlacement] = []
    moved = 0
    step = max(0.5, min(config.min_component_gap_mm, 1.0))
    max_ring = int(math.ceil(max(config.board_width_mm, config.board_height_mm) / step))

    ordered = [
        placement
        for _index, placement in sorted(
            enumerate(state),
            key=lambda item: (
                not item[1].constraint_locked,
                bool(item[1].parent_ref),
                item[0],
            ),
        )
    ]
    for original in ordered:
        placement = ComponentPlacement(**original.__dict__)
        # Follow an owner displaced by legalization instead of leaving its
        # passives stranded around the owner's old coordinates.
        parent = next((p for p in legalized if p.ref == placement.parent_ref), None)
        old_parent = next((p for p in state if p.ref == placement.parent_ref), None)
        if parent is not None and old_parent is not None and not placement.constraint_locked:
            placement.x += parent.x - old_parent.x
            placement.y += parent.y - old_parent.y
        desired_x, desired_y = placement.x, placement.y

        def is_free() -> bool:
            if not _inside_board(placement, config, constraint_plan):
                return False
            edge = (constraint_plan.edges if constraint_plan else {}).get(placement.ref)
            if edge and _distance_to_edge(placement, edge["edge"], config) > edge["max_distance_mm"] + 0.001:
                return False
            if edge and "rotation" in edge and abs(
                (placement.rotation - edge["rotation"] + 180.0) % 360.0 - 180.0
            ) > 0.001:
                return False
            for other in legalized:
                if _overlap_area(placement, other, config.min_component_gap_mm) > 0:
                    return False
                if (
                    _is_parent_support_pair(placement, other)
                    and _rectangle_clearance(placement, other)
                    < config.support_body_clearance_mm
                ):
                    return False
            return True

        # Machine-fixed positions are preserved exactly. Any impossible or
        # colliding fixed constraint is surfaced in the result violations.
        if placement.constraint_locked:
            legalized.append(placement)
            continue
        if not is_free():
            found = False
            seen: set[tuple[float, float]] = set()
            for ring in range(1, max_ring + 1):
                offsets: list[tuple[int, int]] = []
                for delta in range(-ring, ring + 1):
                    offsets.extend(
                        ((delta, -ring), (delta, ring), (-ring, delta), (ring, delta))
                    )
                offsets = sorted(
                    set(offsets),
                    key=lambda value: (value[0] ** 2 + value[1] ** 2, value[1], value[0]),
                )
                for dx, dy in offsets:
                    candidate = (round(desired_x + dx * step, 3), round(desired_y + dy * step, 3))
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    placement.x, placement.y = candidate
                    if is_free():
                        found = True
                        break
                if found:
                    break
            if found:
                moved += 1
            else:
                placement.x, placement.y = desired_x, desired_y
        legalized.append(placement)

    return legalized, moved


def _constraint_evaluation(
    state: list[ComponentPlacement],
    config: PlacementConfig,
    plan: _ConstraintPlan,
) -> dict[str, Any]:
    by_ref = {placement.ref: placement for placement in state}
    violations: list[dict[str, Any]] = []
    for ref, fixed in sorted(plan.fixed.items()):
        placement = by_ref.get(ref)
        if placement is None:
            continue
        if not _inside_board(placement, config):
            violations.append(
                {"kind": "fixed_position", "target": ref, "reason": "Fixed footprint is outside the board."}
            )
        if any(_intersects_keepout(placement, keepout) for keepout in plan.keepouts):
            violations.append(
                {"kind": "fixed_position", "target": ref, "reason": "Fixed footprint intersects a keepout."}
            )
        if (
            abs(placement.x - float(fixed["x_mm"])) > 0.001
            or abs(placement.y - float(fixed["y_mm"])) > 0.001
        ):
            violations.append(
                {"kind": "fixed_position", "target": ref, "reason": "Fixed coordinate was not preserved."}
            )

    for ref, edge_rule in sorted(plan.edges.items()):
        placement = by_ref.get(ref)
        if placement is None:
            continue
        distance = max(0.0, _distance_to_edge(placement, edge_rule["edge"], config))
        if distance > float(edge_rule["max_distance_mm"]) + 0.001:
            violations.append(
                {
                    "kind": "edge",
                    "target": ref,
                    "reason": f"Footprint is {distance:.2f} mm from the requested {edge_rule['edge']} edge.",
                }
            )
        if "rotation" in edge_rule:
            rotation_delta = abs((placement.rotation - float(edge_rule["rotation"]) + 180.0) % 360.0 - 180.0)
            if rotation_delta > 0.001:
                violations.append(
                    {
                        "kind": "edge_orientation",
                        "target": ref,
                        "reason": (
                            f"Footprint rotation is {placement.rotation:.2f}°; "
                            f"the requested edge orientation is {float(edge_rule['rotation']):.2f}°."
                        ),
                    }
                )

    for affinity in plan.affinities:
        target = by_ref.get(affinity["target"])
        neighbor = by_ref.get(affinity["near"])
        if target is None or neighbor is None:
            continue
        distance = math.hypot(target.x - neighbor.x, target.y - neighbor.y)
        maximum = float(affinity["max_distance_mm"])
        if distance > maximum + 0.001:
            violations.append(
                {
                    "kind": "affinity",
                    "target": affinity["target"],
                    "reason": (
                        f"Center is {distance:.2f} mm from {affinity['near']} "
                        f"(maximum {maximum:.2f} mm)."
                    ),
                }
            )

    for placement in state:
        for keepout in plan.keepouts:
            if _intersects_keepout(placement, keepout):
                violations.append(
                    {
                        "kind": "keepout",
                        "target": placement.ref,
                        "reason": f"Footprint intersects keepout {keepout['id']}.",
                    }
                )

    return {
        "supplied_count": plan.supplied_count,
        "board_dimension_source": plan.board_dimension_source,
        "effective_board": {
            "width_mm": config.board_width_mm,
            "height_mm": config.board_height_mm,
            "edge_clearance_mm": config.edge_clearance_mm,
            "min_component_gap_mm": config.min_component_gap_mm,
            "dimensions_verified": plan.board_dimension_source in {"config", "constraints"},
        },
        "applied_count": len(plan.applied),
        "applied": plan.applied,
        "deferred": plan.deferred,
        "unsupported": plan.unsupported,
        "warnings": plan.warnings,
        "keepouts": plan.keepouts,
        "violations": violations,
        "all_placement_constraints_applied": not plan.unsupported,
        "all_applied_constraints_satisfied": not violations,
    }


def _validate_config(config: PlacementConfig) -> None:
    """Reject invalid search budgets and geometry before any optimization."""
    for name in ("board_width_mm", "board_height_mm", "initial_temp"):
        raw = getattr(config, name)
        value = _finite_number(raw) if isinstance(raw, (int, float)) else None
        if value is None or value <= 0:
            raise ValueError(f"{name} must be a finite positive number")
    for name in ("edge_clearance_mm", "min_component_gap_mm", "support_body_clearance_mm"):
        raw = getattr(config, name)
        value = _finite_number(raw) if isinstance(raw, (int, float)) else None
        if value is None or value < 0:
            raise ValueError(f"{name} must be a finite nonnegative number")
    for name, minimum, maximum in (("iterations", 0, 1_000_000), ("restarts", 1, 32), ("refinement_passes", 0, 20)):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    rate = _finite_number(config.cooling_rate) if isinstance(config.cooling_rate, (int, float)) else None
    if rate is None or not 0 < rate <= 1:
        raise ValueError("cooling_rate must be greater than zero and at most one")
    if config.strategy not in {"simple", "balanced", "thermal", "si", "cost"}:
        raise ValueError("Unknown placement strategy")
    if config.seed is not None and (isinstance(config.seed, bool) or not isinstance(config.seed, int)):
        raise ValueError("seed must be an integer or None")


def _quantize_state(state: list[ComponentPlacement]) -> list[ComponentPlacement]:
    # Score precisely the coordinates that will be published. Fixed positions
    # retain supplied precision, and edge-normal coordinates remain exact.
    return [
        replace(p, x=round(p.x, 2), y=round(p.y, 2))
        if not p.constraint_locked and not p.edge_constrained
        else replace(p)
        for p in state
    ]


def _placement_rank(
    state: list[ComponentPlacement], config: PlacementConfig,
    pairs: dict[tuple[str, str], float], plan: _ConstraintPlan,
) -> tuple[int, float]:
    """Hard defects outrank every weighted placement preference."""
    by_ref = {p.ref: p for p in state}
    defects = sum(not _inside_board(p, config) for p in state)
    defects += sum(_overlap_area(a, b, config.min_component_gap_mm) > 0 for a, b in combinations(state, 2))
    for p in state:
        if p.parent_ref:
            parent = by_ref.get(p.parent_ref)
            defects += parent is None or _rectangle_clearance(p, parent) + 0.001 < config.support_body_clearance_mm
    defects += len(_constraint_evaluation(state, config, plan)["violations"])
    return int(defects), _total_cost(state, config, pairs, plan)


def _refine_placement(
    state: list[ComponentPlacement], config: PlacementConfig,
    pairs: dict[tuple[str, str], float], plan: _ConstraintPlan,
) -> tuple[list[ComponentPlacement], int]:
    """Polish legal candidates with deterministic component and block moves."""
    rank = _placement_rank(state, config, pairs, plan)
    evaluations = 0
    for pass_index in range(config.refinement_passes):
        step = 1.0 / (2 ** pass_index)
        changed = False
        for index, placement in enumerate(state):
            if placement.constraint_locked:
                continue
            groups = [[index]]
            family = [i for i, p in enumerate(state) if p.ref == placement.ref or p.parent_ref == placement.ref]
            if len(family) > 1 and not any(state[i].constraint_locked for i in family):
                groups.append(family)
            for group in groups:
                moves = [(step, 0), (-step, 0), (0, step), (0, -step)]
                if not placement.parent_ref:
                    by_ref = {p.ref: p for p in state}
                    neighbors = []
                    for (a, b), weight in pairs.items():
                        other = b if a == placement.ref else a if b == placement.ref else None
                        if other in by_ref:
                            neighbors.append((by_ref[other], weight))
                    total_weight = sum(weight for _, weight in neighbors)
                    if total_weight:
                        dx = sum((p.x - state[index].x) * w for p, w in neighbors) / total_weight
                        dy = sum((p.y - state[index].y) * w for p, w in neighbors) / total_weight
                        # Try a connected-block attraction as well as fine
                        # steps. Only a legal improvement can be accepted.
                        moves.extend([(dx / 2, dy / 2), (dx / 2, 0), (0, dy / 2)])
                for dx, dy in moves:
                    candidate = [replace(p) for p in state]
                    for i in group:
                        candidate[i].x += dx
                        candidate[i].y += dy
                    candidate = _quantize_state(candidate)
                    candidate_rank = _placement_rank(candidate, config, pairs, plan)
                    evaluations += 1
                    if candidate_rank < rank:
                        state, rank = candidate, candidate_rank
                        changed = True
            if not placement.rotation_locked:
                for rotation in (90, 180, 270):
                    candidate = [replace(p) for p in state]
                    candidate[index].rotation = (state[index].rotation + rotation) % 360
                    candidate_rank = _placement_rank(candidate, config, pairs, plan)
                    evaluations += 1
                    if candidate_rank < rank:
                        state, rank = candidate, candidate_rank
                        changed = True
        if not changed:
            break
    return state, evaluations


def _routing_estimate(state: list[ComponentPlacement], components: list[ComponentDef]) -> dict[str, Any]:
    """Center-based net spans are routing estimates, never routed copper lengths."""
    by_ref = {p.ref: p for p in state}
    spans = []
    for name, refs in sorted(_build_net_component_map(components).items()):
        points = [by_ref[ref] for ref in sorted(set(refs)) if ref in by_ref]
        if len(points) < 2 or _net_weight(name) <= 0:
            continue
        span = max(p.x for p in points) - min(p.x for p in points)
        span += max(p.y for p in points) - min(p.y for p in points)
        spans.append({"net": name, "component_count": len(points), "hpwl_mm": round(span, 3)})
    return {
        "model": "component_center_hpwl",
        "estimated": True,
        "excludes_ground_nets": True,
        "total_hpwl_mm": round(sum(row["hpwl_mm"] for row in spans), 3),
        "nets": spans,
    }


def optimize_placement(
    components: list[ComponentDef],
    *,
    config: PlacementConfig | None = None,
    specs_dir: str | Path | None = None,
    constraints: list[dict[str, Any]] | None = None,
) -> dict:
    """Run simulated annealing placement optimizer.

    Args:
        components: List of ComponentDef from compiled design.
        config: Optimizer configuration (defaults to balanced strategy).
        specs_dir: Path to specs/ directory with thermal/SI JSON files.
        constraints: Machine-readable placement/keepout constraints. Routing
            constraints are reported as deferred and never claimed as applied.

    Returns:
        {
            "status": "ok",
            "placements": {ref: {x, y, rotation, layer}},
            "board_width_mm": float,
            "board_height_mm": float,
            "iterations": int,
            "final_cost": float,
            "initial_cost": float,
            "strategy": str,
            "thermal_warnings": [...],
        }
    """
    if config is None:
        config = PlacementConfig()
    _validate_config(config)
    config, constraint_plan = _build_constraint_plan(components, config, constraints)
    _validate_config(config)

    specs_path = Path(specs_dir) if specs_dir else None
    thermal_specs = _load_thermal_specs(specs_path)
    si_specs = _load_si_specs(specs_path)
    connectivity_pairs = _build_connectivity_pairs(components)

    state = _init_placements(components, config, thermal_specs, si_specs, constraint_plan)

    if not state:
        constraint_evaluation = _constraint_evaluation(state, config, constraint_plan)
        return {
            "status": "ok",
            "placements": {},
            "board_width_mm": config.board_width_mm,
            "board_height_mm": config.board_height_mm,
            "iterations": 0,
            "final_cost": 0.0,
            "initial_cost": 0.0,
            "strategy": config.strategy,
            "thermal_warnings": [],
            "constraint_evaluation": constraint_evaluation,
            "quality": {
                "overlaps": [],
                "outside_board": [],
                "missing_parents": [],
                "support_body_violations": [],
                "max_support_distance_mm": 0.0,
                "support_distance_model": "none",
                "support_distances": [],
                "legalization_moves": 0,
                "review_required": bool(
                    constraint_evaluation["unsupported"] or constraint_evaluation["violations"]
                ),
            },
        }

    if config.strategy == "simple":
        # Skip optimization, return initial zone-based placement
        legalized, moved = _legalize_overlaps(_quantize_state(state), config, constraint_plan)
        return _build_result(
            _quantize_state(legalized),
            config,
            0,
            0.0,
            0.0,
            legalization_moves=moved,
            constraint_plan=constraint_plan,
        )

    # Keep the legal initial layout as a fallback. Every trial is compared
    # after legalization, so a cheaper but physically worse layout cannot win.
    initial_state = state
    baseline, baseline_moves = _legalize_overlaps(_quantize_state(state), config, constraint_plan)
    baseline = _quantize_state(baseline)
    initial_cost = _total_cost(initial_state, config, connectivity_pairs, constraint_plan)
    winner = baseline
    winner_rank = _placement_rank(winner, config, connectivity_pairs, constraint_plan)
    winner_index, winner_moves = 0, baseline_moves
    trials = [{"candidate": 0, "kind": "legalized_baseline", "defects": winner_rank[0],
               "cost": round(winner_rank[1], 2), "iterations": 0}]
    refinement_evaluations = 0
    restarts = min(config.restarts, config.iterations)
    seed_rng = random.Random(config.seed)
    for restart in range(restarts):
        budget = config.iterations // restarts + (restart < config.iterations % restarts)
        trial_seed = config.seed if restart == 0 else seed_rng.getrandbits(63)
        rng = random.Random(trial_seed)
        state = [replace(p) for p in baseline]
        current_cost = _total_cost(state, config, connectivity_pairs, constraint_plan)
        best_state, best_cost = state, current_cost
        temp = config.initial_temp
        for i in range(budget):
            scale = max(0.1, 1.0 - i / max(1, budget))
            candidate = _perturb(state, config, rng, move_scale=scale)
            candidate_cost = _total_cost(candidate, config, connectivity_pairs, constraint_plan)
            delta = candidate_cost - current_cost
            if delta < 0 or rng.random() < math.exp(-delta / max(temp, 0.001)):
                state, current_cost = candidate, candidate_cost
                if current_cost < best_cost:
                    best_state, best_cost = state, current_cost
            temp *= config.cooling_rate ** restarts
        candidate, moved = _legalize_overlaps(_quantize_state(best_state), config, constraint_plan)
        candidate, evaluations = _refine_placement(
            _quantize_state(candidate), config, connectivity_pairs, constraint_plan,
        )
        refinement_evaluations += evaluations
        rank = _placement_rank(candidate, config, connectivity_pairs, constraint_plan)
        trials.append({"candidate": restart + 1, "kind": "annealed", "seed": trial_seed,
                       "iterations": budget, "defects": rank[0], "cost": round(rank[1], 2)})
        if rank < winner_rank:
            winner, winner_rank = candidate, rank
            winner_index, winner_moves = restart + 1, moved
    result = _build_result(
        winner,
        config,
        config.iterations,
        initial_cost,
        winner_rank[1],
        legalization_moves=winner_moves,
        constraint_plan=constraint_plan,
    )
    result["search"] = {
        "selected_candidate": winner_index,
        "restarts": restarts,
        "iteration_budget": config.iterations,
        "refinement_evaluations": refinement_evaluations,
        "candidates": trials,
    }
    result["routing_estimate"] = {
        "initial": _routing_estimate(baseline, components),
        "final": _routing_estimate(winner, components),
    }
    return result


def _build_result(
    state: list[ComponentPlacement],
    config: PlacementConfig,
    iterations: int,
    initial_cost: float,
    final_cost: float,
    *,
    legalization_moves: int = 0,
    constraint_plan: _ConstraintPlan | None = None,
) -> dict:
    """Build the result dict from optimizer state."""
    placements = {}
    thermal_warnings = []
    overlap_pairs: list[list[str]] = []
    outside_board: list[str] = []
    missing_parents: list[str] = []
    support_body_violations: list[dict[str, Any]] = []
    support_distances: list[dict[str, Any]] = []
    by_ref = {placement.ref: placement for placement in state}

    for idx, first in enumerate(state):
        for second in state[idx + 1 :]:
            if _overlap_area(first, second, config.min_component_gap_mm) > 0:
                overlap_pairs.append([first.ref, second.ref])
        if first.parent_ref:
            parent = by_ref.get(first.parent_ref)
            if parent is None:
                missing_parents.append(first.ref)
            else:
                support_distance, distance_model = _parent_affinity_distance(parent, first)
                support_distances.append(
                    {
                        "support_ref": first.ref,
                        "parent_ref": parent.ref,
                        "distance_mm": round(support_distance, 2),
                        "model": distance_model,
                    }
                )
                clearance = _rectangle_clearance(first, parent)
                if clearance + 0.001 < config.support_body_clearance_mm:
                    support_body_violations.append(
                        {
                            "support_ref": first.ref,
                            "parent_ref": parent.ref,
                            "clearance_mm": round(clearance, 2),
                            "required_mm": config.support_body_clearance_mm,
                        }
                    )
        if not _inside_board(first, config):
            outside_board.append(first.ref)

    for p in state:
        placements[p.ref] = {
            "x": p.x if p.constraint_locked or p.edge_constrained else round(p.x, 2),
            "y": p.y if p.constraint_locked or p.edge_constrained else round(p.y, 2),
            "rotation": p.rotation,
            "layer": p.layer,
            "locked": p.locked,
            "constraint_locked": p.constraint_locked,
            "edge_constrained": p.edge_constrained,
            "rotation_locked": p.rotation_locked,
            "width_mm": p.width,
            "height_mm": p.height,
            "geometry_status": p.geometry_status,
        }
        if p.thermal_dissipation_w > 1.0:
            thermal_warnings.append(f"{p.ref}: {p.thermal_dissipation_w}W dissipation — ensure adequate copper area")

    plan = constraint_plan or _ConstraintPlan()
    constraint_evaluation = _constraint_evaluation(state, config, plan)
    return {
        "status": "ok",
        "placements": placements,
        "board_width_mm": config.board_width_mm,
        "board_height_mm": config.board_height_mm,
        "iterations": iterations,
        "final_cost": round(final_cost, 2),
        "initial_cost": round(initial_cost, 2),
        "strategy": config.strategy,
        "thermal_warnings": thermal_warnings,
        "constraint_evaluation": constraint_evaluation,
        "functional_grouping": {
            "model": "bounded_pairwise_center_distance",
            "groups": _functional_group_metrics(state),
            "objective_cost": round(_cost_functional_grouping(state), 2),
        },
        "quality": {
            "overlaps": overlap_pairs,
            "outside_board": sorted(outside_board),
            "missing_parents": sorted(missing_parents),
            "support_body_violations": support_body_violations,
            "max_support_distance_mm": round(
                max((item["distance_mm"] for item in support_distances), default=0.0), 2
            ),
            "support_distance_model": (
                next(iter({item["model"] for item in support_distances}))
                if len({item["model"] for item in support_distances}) == 1
                else "mixed" if support_distances else "none"
            ),
            "support_distances": support_distances,
            "legalization_moves": legalization_moves,
            "review_required": bool(
                overlap_pairs
                or outside_board
                or missing_parents
                or support_body_violations
                or constraint_evaluation["unsupported"]
                or constraint_evaluation["violations"]
            ),
        },
    }
