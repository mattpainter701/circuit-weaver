"""Auto-placer — positions components on schematic sheets with professional layout.

Layout strategy:
- ICs sorted by pin count (largest first) for visual hierarchy
- Connectors on left edge, ICs flow left-to-right in center
- Bypass caps grouped tightly below their IC
- Strap resistors placed beside their IC
- Power regulators placed in input→output chain
- Auto paper-size upgrade if content overflows
"""

import math
import re
from dataclasses import dataclass, field

from .component_db import (
    ComponentDef,
    PresentationWiringPolicy,
    component_explanation_lines,
    normalize_presentation_wiring_policy,
    normalize_support_passive_presentation,
)
from .primitives import (
    PAPER_SIZES,
    TITLE_BLOCK_H,
    create_generic_symbol,
    get_pin_positions,
    passive_pin_xy,
    pin_connection_point,
    snap,
    text_width_mm,
)


@dataclass
class PlacedComponent:
    """A component with its assigned position on a sheet."""

    comp: ComponentDef
    ref: str
    x: float
    y: float
    angle: int = 0


@dataclass
class PlacedPassive:
    """A passive (bypass cap or strap) with position and net assignments."""

    ref: str
    value: str
    footprint: str
    x: float
    y: float
    net1: str
    net2: str
    sym_type: str  # "C", "R", "L"
    angle: int = 0
    parent_ref: str = ""  # ref of the IC that owns this passive (for local wiring)
    owner_pin: str = ""  # parent pin identifier / semantic source of the passive
    role: str = "support"
    presentation: str = "literal_local"
    symbol_variant: str = "small"
    pin_span: float = 3.81


@dataclass
class LocalNetAnchor:
    """A local topology node used by presentation-aware rendering."""

    name: str
    x: float
    y: float
    angle: int = 0
    render_mode: str = "label"  # label, power, junction
    parent_ref: str = ""


@dataclass
class SheetLayout:
    """Complete layout for one sheet — all positions computed."""

    name: str
    title: str
    paper: str
    placed_ics: list[PlacedComponent] = field(default_factory=list)
    placed_passives: list[PlacedPassive] = field(default_factory=list)
    sheet_annotations: list[str] = field(default_factory=list)
    boundary_ports: list[tuple[str, str]] = field(default_factory=list)
    local_net_anchors: list[LocalNetAnchor] = field(default_factory=list)
    local_wires: list[tuple[float, float, float, float]] = field(default_factory=list)
    presentation_wiring_policy: PresentationWiringPolicy = field(default_factory=PresentationWiringPolicy)
    # True when the content overflowed every paper size up to A0 — the
    # caller must split the sheet or fail, not ship a crammed page (F15).
    overflow: bool = False


# Reference designator counters
_ref_counters = {}
_used_refs = set()
_claimed_explicit_refs = set()


def _next_ref(prefix: str) -> str:
    if prefix not in _ref_counters:
        _ref_counters[prefix] = 0
    while True:
        _ref_counters[prefix] += 1
        ref = f"{prefix}{_ref_counters[prefix]}"
        ref_key = ref.casefold()
        if ref_key not in _used_refs:
            _used_refs.add(ref_key)
            return ref


def _reserve_ref(ref: str):
    """Reserve an explicit BOM reference so auto-generated refs skip it."""
    if not ref:
        return
    _used_refs.add(ref.casefold())
    match = re.match(r"^([A-Za-z]+)(\d+)$", ref)
    if not match:
        return
    prefix, number = match.groups()
    prefix = prefix.upper()
    _ref_counters[prefix] = max(_ref_counters.get(prefix, 0), int(number))


def _component_ref(comp: ComponentDef) -> str:
    """Use an explicit BOM ref when present, otherwise allocate the next free ref."""
    if comp.source_ref:
        explicit_ref = comp.source_ref.strip()
        if not explicit_ref or explicit_ref != comp.source_ref:
            raise ValueError(
                f"Explicit reference {comp.source_ref!r} must not contain surrounding whitespace"
            )
        ref_key = explicit_ref.casefold()
        if ref_key in _claimed_explicit_refs:
            raise ValueError(f"Duplicate explicit reference '{explicit_ref}'")
        _claimed_explicit_refs.add(ref_key)
        _reserve_ref(explicit_ref)
        return explicit_ref
    return _next_ref(comp.ref_prefix)


def reserve_explicit_refs(components: list[ComponentDef]) -> None:
    """Reserve every explicit reference before allocating anonymous ones.

    Placement order is intentionally different from BOM order.  Without a
    preflight reservation, an anonymous quantity copy can consume ``U1`` and
    silently force the component explicitly named ``U1`` to another reference.
    """
    seen: dict[str, str] = {}
    for comp in components:
        raw_ref = comp.source_ref or ""
        ref = raw_ref.strip()
        if raw_ref and raw_ref != ref:
            raise ValueError(
                f"Explicit reference {raw_ref!r} must not contain surrounding whitespace"
            )
        if not ref:
            continue
        key = ref.casefold()
        if key in seen:
            raise ValueError(f"Duplicate explicit reference '{ref}' (already reserved as '{seen[key]}')")
        seen[key] = ref
    for ref in seen.values():
        _reserve_ref(ref)


def reset_ref_counters():
    _ref_counters.clear()
    _used_refs.clear()
    _claimed_explicit_refs.clear()


def _snapshot_ref_state() -> tuple[dict[str, int], set[str], set[str]]:
    """Capture the current global ref-allocation state."""
    return dict(_ref_counters), set(_used_refs), set(_claimed_explicit_refs)


def _restore_ref_state(state: tuple[dict[str, int], set[str], set[str]]) -> None:
    """Restore a previously captured global ref-allocation state."""
    counters, used, claimed = state
    _ref_counters.clear()
    _ref_counters.update(counters)
    _used_refs.clear()
    _used_refs.update(used)
    _claimed_explicit_refs.clear()
    _claimed_explicit_refs.update(claimed)


_BODY_LEFT_GUTTER = snap(18)
_BODY_RIGHT_GUTTER = snap(18)
_BODY_TOP_GUTTER = snap(10)
_BODY_BOTTOM_GUTTER = snap(8)
_SECTION_GAP = snap(18)
_ROW_GAP = snap(20)
_COL_GAP = snap(12)
_CONNECTOR_GAP = snap(10)
_REG_GRID_THRESHOLD = 3
_REG_GRID_COLS = 3
_CAP_GRID_THRESHOLD = 3
_CAP_GRID_COLS = 2
_CAP_COL_PITCH = snap(12)
_CAP_ROW_PITCH = snap(10)
_CAP_START_OFFSET = snap(12)
_CAP_BOTTOM_GUTTER = snap(8)
_STRAP_X_OFFSET = snap(15)
_STRAP_PITCH = snap(10)
_STRAP_RIGHT_GUTTER = snap(28)
_ANNOTATION_GAP = snap(8)
_ANNOTATION_PITCH = snap(3)
_ANNOTATION_BOTTOM_GUTTER = snap(4)
_MIN_PAGE_UTILIZATION = 0.30
_PAPER_ORDER = ("A4", "A3", "A2", "A1", "A0")
_LABEL_STUB_LEN = snap(7.62)
_SHEET_TEXT_X = snap(20)
_SHEET_TITLE_Y = snap(15)
_SHEET_DESCRIPTION_Y = snap(_SHEET_TITLE_Y + 5.08)
_SHEET_ANNOTATION_Y = snap(25)
_SHEET_ANNOTATION_PITCH = snap(3)
_TOPOLOGY_BLOCK_COL_PITCH = snap(12.70)
_TOPOLOGY_BLOCK_ROW_PITCH = snap(10.16)
_TOPOLOGY_BLOCK_PRIMARY_OFFSET = snap(12.70)
_TOPOLOGY_BLOCK_SECONDARY_OFFSET = snap(15.24)


def _component_symbol_name(comp: ComponentDef) -> str:
    if comp.lib_symbol_sexpr and not comp.prefer_multi_column_symbol():
        match = re.search(r'\(symbol\s+"([^"]+)"', comp.lib_symbol_sexpr)
        if match:
            return match.group(1)
    return comp.mpn.replace("-", "_").replace(".", "_")


def _component_symbol_sexpr(comp: ComponentDef) -> str:
    if comp.lib_symbol_sexpr and not comp.prefer_multi_column_symbol():
        return comp.lib_symbol_sexpr
    return create_generic_symbol(
        _component_symbol_name(comp),
        comp.pin_tuples(),
        comp.ref_prefix,
        column_segments=comp.preferred_symbol_column_segments(),
        pin_pitch_override=comp.preferred_symbol_pin_pitch_mm(),
    )


def _rowwise_gap_profile(
    components: list[ComponentDef], col_gap: float = _COL_GAP, row_gap: float = _ROW_GAP
) -> tuple[float, float]:
    """Return review-oriented row/column gaps for a component group."""
    review_dense = [
        comp for comp in components if (comp.preferred_symbol_pin_pitch_mm() or 0.0) >= 5.08 or len(comp.pins) >= 40
    ]
    very_dense = [comp for comp in review_dense if (comp.preferred_symbol_column_segments() or 0) >= 4]
    if len(very_dense) >= 2:
        return snap(col_gap * 4.0), snap(row_gap * 2.5)
    if len(review_dense) >= 2:
        return snap(col_gap * 3.5), snap(row_gap * 2.25)
    return col_gap, row_gap


def _density_scaled_gaps(
    components: list[ComponentDef],
    base_col_gap: float,
    base_row_gap: float,
    paper_width: float,
    usable_height: float,
    x_start: float = 0.0,
) -> tuple[float, float]:
    """Scale inter-component gaps so components spread across the available page area.

    When a sheet has many components on a large paper size, fixed gap values
    leave them clustered in a corner with 80%+ whitespace. This function
    computes the natural footprint area vs available page area and scales
    gaps so the layout fills ~30-40% of the sheet — enough to spread
    components out but not so much that they lose visual cohesion.

    The scaling is clamped between 1.0x (no spreading for small sheets) and
    3.0x (maximum spreading for very large sparse sheets). Returns
    (col_gap, row_gap) snapped to the KiCad grid.
    """
    if len(components) < 4:
        return base_col_gap, base_row_gap

    # Total component footprint area (approximate)
    total_fp_area = 0.0
    for comp in components:
        w, h = component_block_size(comp)
        total_fp_area += w * h

    # Available page area (less headers/title block)
    avail_w = max(1.0, paper_width - x_start - 20.0)
    avail_h = max(1.0, usable_height)
    page_area = avail_w * avail_h

    # Natural fill fraction: what fraction of paper area do components
    # occupy with default gaps? (~5-15% for most designs)
    fill_fraction = total_fp_area / page_area if page_area > 0 else 0.0

    # Target fill: spread components to ~35% of page for best readability.
    # If they already fill more, don't compress.
    if fill_fraction <= 0 or fill_fraction >= 0.35:
        return base_col_gap, base_row_gap

    # Scale gaps to expand the content area toward the target fill.
    # Since area ∝ gap² (grid spacing affects both axes), we take sqrt.
    scale = min(3.0, max(1.0, math.sqrt(0.35 / fill_fraction) * 0.7))

    return snap(base_col_gap * scale), snap(base_row_gap * scale)


def _component_local_extents(comp: ComponentDef) -> tuple[float, float, float, float]:
    """Return (left, right, top, bottom) extents from the symbol origin in mm."""
    symbol_sexpr = _component_symbol_sexpr(comp)
    symbol_name = _component_symbol_name(comp)

    xs = []
    ys = []

    for x1, y1, x2, y2 in re.findall(
        r"\(rectangle\s+\(start\s+([-\d.]+)\s+([-\d.]+)\)\s+\(end\s+([-\d.]+)\s+([-\d.]+)\)",
        symbol_sexpr,
    ):
        xs.extend((float(x1), float(x2)))
        ys.extend((float(y1), float(y2)))

    for px, py, _, _, _, _ in get_pin_positions(symbol_sexpr, symbol_name).values():
        xs.append(px)
        ys.append(py)

    if not xs:
        return (0.0, 25.40, 6.35, 6.35)

    left = max(0.0, -min(xs))
    right = max(0.0, max(xs))
    top = max(0.0, max(ys))
    bottom = max(0.0, -min(ys))
    return left, right, top, bottom


def component_body_size(comp: ComponentDef) -> tuple[float, float]:
    """Return the placed symbol span including pin endpoints, excluding extras."""
    left, right, top, bottom = _component_local_extents(comp)
    return snap(left + right), snap(top + bottom)


def _component_body_rect_extents(comp: ComponentDef) -> tuple[float, float, float, float]:
    """Return rectangle-only extents for the visible symbol body."""
    symbol_sexpr = _component_symbol_sexpr(comp)
    xs = []
    ys = []
    for x1, y1, x2, y2 in re.findall(
        r"\(rectangle\s+\(start\s+([-\d.]+)\s+([-\d.]+)\)\s+\(end\s+([-\d.]+)\s+([-\d.]+)\)",
        symbol_sexpr,
    ):
        xs.extend((float(x1), float(x2)))
        ys.extend((float(y1), float(y2)))
    if not xs:
        return _component_local_extents(comp)
    return (
        max(0.0, -min(xs)),
        max(0.0, max(xs)),
        max(0.0, max(ys)),
        max(0.0, -min(ys)),
    )


def component_body_bounds(pc: PlacedComponent) -> tuple[float, float, float, float]:
    """Return the placed symbol body's exact bounds, excluding passives and notes.

    The corners are deliberately not snapped: callers use these bounds as
    collision keep-outs, and rounding to the 1.27mm wire grid can shrink a
    box past a wire running just inside its true edge, hiding the collision.
    """
    left, right, top, bottom = _component_body_rect_extents(pc.comp)
    return (
        pc.x - left,
        pc.y - top,
        pc.x + right,
        pc.y + bottom,
    )


def _symbol_body_width(comp: ComponentDef) -> float:
    left, right, _, _ = _component_local_extents(comp)
    return snap(left + right)


def _symbol_body_height(comp: ComponentDef) -> float:
    _, _, top, bottom = _component_local_extents(comp)
    return snap(top + bottom)


def _component_pin_lookup(pc: PlacedComponent) -> dict[str, tuple[float, float, int, str, str]]:
    """Return pin-number -> (x, y, angle, electrical_type, pin_name) for a placed component."""
    symbol_sexpr = _component_symbol_sexpr(pc.comp)
    symbol_name = _component_symbol_name(pc.comp)
    pin_lookup = {}
    for pin_num, (pin_x, pin_y, pin_angle, pin_length, pin_name, pin_type) in get_pin_positions(
        symbol_sexpr, symbol_name
    ).items():
        x, y = pin_connection_point(pc.x, pc.y, pin_x, pin_y, pin_angle, pin_length)
        pin_lookup[pin_num] = (snap(x), snap(y), int(pin_angle), pin_type, pin_name)
    return pin_lookup


def _component_net_lookup(pc: PlacedComponent) -> dict[str, list[tuple[float, float, int, str]]]:
    """Return net-name -> [(x, y, angle, pin_num), ...] for a placed component."""
    pin_lookup = _component_pin_lookup(pc)
    nets: dict[str, list[tuple[float, float, int, str]]] = {}
    for pin_num, net_name in {**pc.comp.pin_nets, **pc.comp.power_pins}.items():
        if not net_name or pin_num not in pin_lookup:
            continue
        x, y, angle, _ptype, _pname = pin_lookup[pin_num]
        nets.setdefault(net_name, []).append((x, y, angle, pin_num))
    return nets


def _parent_pin_point(pc: PlacedComponent, owner_pin: str, net_name: str) -> tuple[float, float, int] | None:
    """Return the preferred parent-pin connection point for a passive/net."""
    pin_lookup = _component_pin_lookup(pc)
    if owner_pin and owner_pin in pin_lookup:
        x, y, angle, _ptype, _pname = pin_lookup[owner_pin]
        return (x, y, angle)
    net_lookup = _component_net_lookup(pc)
    if net_name in net_lookup and net_lookup[net_name]:
        x, y, angle, _pin_num = net_lookup[net_name][0]
        return (x, y, angle)
    return None


def _bypass_sym_type_and_prefix(value: str, footprint: str, role: str) -> tuple[str, str]:
    """Infer the rendered passive symbol/ref prefix for a bypass/support part."""
    value_l = (value or "").lower()
    footprint_l = (footprint or "").lower()
    role_l = (role or "").lower()
    if role_l == "inductor" or "inductor" in footprint_l or value_l.endswith("h"):
        return ("L", "L")
    return ("C", "C")


def _support_passive_symbol_variant(sym_type: str, role: str) -> tuple[str, float]:
    """Return (symbol_variant, pin_span_mm) for a rendered support passive."""
    if sym_type in {"R", "C", "L"}:
        return ("review", 5.08)
    return ("small", 3.81)


def _total_footprint(comp: ComponentDef) -> tuple[float, float]:
    """Total space a component needs including wire stubs and labels."""
    body_w, body_h = component_body_size(comp)
    total_w = body_w + 2 * 7.62 + 30
    total_h = body_h + 15
    return total_w, total_h


def _annotation_line_count(comp: ComponentDef) -> int:
    return min(len(component_explanation_lines(comp)), 5)


def _support_cluster_cols(comp: ComponentDef) -> int:
    count = len(comp.bypass_caps)
    if count <= 2:
        return max(1, count)
    body_w, _body_h = component_body_size(comp)
    if count >= 8 and body_w >= 76.2:
        return 4
    if count >= 5 and body_w >= 38.1:
        return 3
    return 2


def _support_cluster_col_pitch(comp: ComponentDef) -> float:
    cols = max(1, _support_cluster_cols(comp))
    body_w, _body_h = component_body_size(comp)
    review_floor = 19.05 if comp.bypass_caps else _CAP_COL_PITCH
    review_ceiling = 25.40 if comp.bypass_caps else 20.32
    spread = max(review_floor, min(review_ceiling, body_w / max(1.5, cols - 0.5)))
    return snap(spread)


def _support_cluster_row_pitch(comp: ComponentDef) -> float:
    count = len(comp.bypass_caps)
    if count >= 3:
        return snap(15.24)
    return _CAP_ROW_PITCH


def _cap_row_count(comp: ComponentDef) -> int:
    if not comp.bypass_caps:
        return 0
    return math.ceil(len(comp.bypass_caps) / max(1, _support_cluster_cols(comp)))


def _cap_bottom_extent(comp: ComponentDef) -> float:
    rows = _cap_row_count(comp)
    if rows == 0:
        return 0.0
    return snap(_CAP_START_OFFSET + (rows - 1) * _support_cluster_row_pitch(comp) + _CAP_BOTTOM_GUTTER)


def _strap_bottom_extent(comp: ComponentDef) -> float:
    if not comp.straps:
        return 0.0
    strap_stack = snap((len(comp.straps) - 1) * _strap_pitch(comp) + 4)
    _, _, _, bottom = _component_local_extents(comp)
    visible_below_origin = bottom + _BODY_BOTTOM_GUTTER
    return max(0.0, snap(strap_stack - visible_below_origin))


def _strap_pitch(comp: ComponentDef) -> float:
    """Return readable vertical spacing for side-mounted strap ladders."""
    count = len(comp.straps)
    if count >= 6:
        return snap(12.70)
    if count >= 3:
        return snap(11.43)
    return _STRAP_PITCH


def _annotation_bottom_extent(comp: ComponentDef) -> float:
    lines = _annotation_line_count(comp)
    if lines == 0:
        return 0.0
    return snap(_ANNOTATION_GAP + (lines - 1) * _ANNOTATION_PITCH + _ANNOTATION_BOTTOM_GUTTER)


def _component_render_padding(comp: ComponentDef) -> tuple[float, float, float, float]:
    """Estimate extra rendered extents beyond the raw symbol body."""
    left = right = top = bottom = 0.0

    for pin in comp.pins:
        net_name = comp.power_pins.get(pin.number) or comp.pin_nets.get(pin.number)
        if not net_name:
            continue

        if pin.number in comp.power_pins:
            label_extent = snap(_LABEL_STUB_LEN + max(6.0, text_width_mm(net_name, 1.27)) + 4.0)
        else:
            label_extent = snap(_LABEL_STUB_LEN + max(3.0, text_width_mm(net_name, 1.0)) + 2.0)

        side = pin.side.upper()
        if side == "L":
            left = max(left, label_extent)
        elif side == "R":
            right = max(right, label_extent)
        elif side == "T":
            top = max(top, label_extent)
        elif side == "B":
            bottom = max(bottom, label_extent)

    explanation_lines = component_explanation_lines(comp)
    if explanation_lines:
        annotation_w = max(text_width_mm(line, 1.0) for line in explanation_lines[:5])
        right = max(right, snap(5.0 + annotation_w + 2.0))

    return left, right, top, bottom


def _component_block_padding(comp: ComponentDef) -> tuple[float, float, float, float]:
    """Return conservative padding around the raw symbol body for layout packing."""
    render_left, render_right, render_top, render_bottom = _component_render_padding(comp)
    bottom_stack = max(_cap_bottom_extent(comp), _strap_bottom_extent(comp))
    bottom_stack += _annotation_bottom_extent(comp)
    left_pad = max(_BODY_LEFT_GUTTER, render_left)
    top_pad = max(_BODY_TOP_GUTTER, render_top)
    right_pad = max(_BODY_RIGHT_GUTTER, render_right) + (_STRAP_RIGHT_GUTTER if comp.straps else 0.0)
    bottom_pad = max(_BODY_BOTTOM_GUTTER, render_bottom) + bottom_stack
    return snap(left_pad), snap(top_pad), snap(right_pad), snap(bottom_pad)


def component_annotation_start_y(comp: ComponentDef, center_y: float) -> float:
    """Return the y-coordinate where per-component notes should begin."""
    _, _, _, bottom = _component_local_extents(comp)
    attachments = max(_cap_bottom_extent(comp), _strap_bottom_extent(comp))
    return snap(center_y + bottom + attachments + _ANNOTATION_GAP)


def component_block_size(comp: ComponentDef) -> tuple[float, float]:
    """Estimate the occupied block size including labels, passives, and notes."""
    body_w, body_h = component_body_size(comp)
    left_pad, top_pad, right_pad, bottom_pad = _component_block_padding(comp)
    block_w = snap(left_pad + body_w + right_pad)
    block_h = snap(top_pad + body_h + bottom_pad)
    return block_w, block_h


def _component_block_origin(comp: ComponentDef, left: float, top: float) -> tuple[float, float]:
    local_left, _, local_top, _ = _component_local_extents(comp)
    left_pad, top_pad, _right_pad, _bottom_pad = _component_block_padding(comp)
    return snap(left + left_pad + local_left), snap(top + top_pad + local_top)


def _natural_ref_key(ref: str) -> tuple[str, int, str]:
    match = re.match(r"^([A-Za-z]+)(\d+)$", ref or "")
    if not match:
        return (ref or "", -1, ref or "")
    prefix, number = match.groups()
    return (prefix.upper(), int(number), ref or "")


def _component_sort_key(comp: ComponentDef) -> tuple[str, int, str]:
    return _natural_ref_key(comp.source_ref or "")


def _resolve_support_passive_presentation(
    item_presentation: str,
    comp: ComponentDef,
    sheet_policy: PresentationWiringPolicy,
) -> str:
    """Resolve final support-passive rendering mode for a generated passive."""
    if item_presentation and item_presentation != "inherit":
        return normalize_support_passive_presentation(item_presentation)
    comp_policy = getattr(comp, "presentation_wiring_policy", None)
    if comp_policy is not None:
        comp_policy = normalize_presentation_wiring_policy(comp_policy)
        return normalize_support_passive_presentation(
            comp_policy.support_passives, default=sheet_policy.support_passives
        )
    return normalize_support_passive_presentation(sheet_policy.support_passives)


def _power_component_priority(comp: ComponentDef) -> tuple[int, int, tuple[str, int, str]]:
    desc = (comp.description or "").lower()
    if "usb pd" in desc or "poe" in desc:
        bucket = 0
    elif any(kw in desc for kw in ("buck", "ldo", "regulator")):
        bucket = 1
    elif any(kw in desc for kw in ("transformer", "bridge", "schottky")):
        bucket = 2
    else:
        bucket = 3
    return (bucket, -len(comp.pins), _component_sort_key(comp))


def _layout_components_rowwise(
    components: list[ComponentDef],
    x_start: float,
    y_start: float,
    x_limit: float,
    col_gap: float = _COL_GAP,
    row_gap: float = _ROW_GAP,
    max_cols: int | None = None,
) -> tuple[list[PlacedComponent], float]:
    """Pack components left-to-right using block sizes and row wrapping."""
    placed = []
    row_top = snap(y_start)
    cursor_x = snap(x_start)
    row_h = 0.0
    col = 0

    for comp in components:
        block_w, block_h = component_block_size(comp)
        wrap_width = max_cols is None and cursor_x + block_w > x_limit and cursor_x > x_start
        wrap_cols = max_cols is not None and col >= max_cols
        if wrap_width or wrap_cols:
            cursor_x = snap(x_start)
            row_top = snap(row_top + row_h + row_gap)
            row_h = 0.0
            col = 0

        ref = _component_ref(comp)
        comp_x, comp_y = _component_block_origin(comp, cursor_x, row_top)
        placed.append(PlacedComponent(comp=comp, ref=ref, x=comp_x, y=comp_y))

        cursor_x = snap(cursor_x + block_w + col_gap)
        row_h = max(row_h, block_h)
        col += 1

    bottom = snap(row_top + row_h) if placed else snap(y_start)
    return placed, bottom


def _preferred_max_cols(
    components: list[ComponentDef],
    x_start: float,
    x_limit: float,
    col_gap: float = _COL_GAP,
) -> int | None:
    """Choose a width-aware column cap for rowwise packing."""
    if len(components) < 4:
        return None
    available_w = max(0.0, x_limit - x_start)
    widest = max(component_block_size(comp)[0] for comp in components)
    if widest <= 0:
        return None
    max_by_width = max(1, int((available_w + col_gap) // (widest + col_gap)))
    if max_by_width <= 1:
        return 1
    target = max(2, math.ceil(math.sqrt(len(components) * 1.6)))
    return max(2, min(max_by_width, target))


def _component_block_bounds(pc: PlacedComponent) -> tuple[float, float, float, float]:
    """Return conservative bounds for a placed component and its attached items."""
    local_left, _, local_top, _ = _component_local_extents(pc.comp)
    left_pad, top_pad, _right_pad, _bottom_pad = _component_block_padding(pc.comp)
    block_w, block_h = component_block_size(pc.comp)
    left = snap(pc.x - (local_left + left_pad))
    top = snap(pc.y - (local_top + top_pad))
    right = snap(left + block_w)
    bottom = snap(top + block_h)
    return left, top, right, bottom


def _sheet_text_bounds(layout: SheetLayout) -> tuple[float, float, float, float]:
    """Estimate bounds for the sheet title and sheet-level annotation block."""
    xs: list[float] = []
    ys: list[float] = []

    def update(min_x: float, min_y: float, max_x: float, max_y: float):
        xs.extend((min_x, max_x))
        ys.extend((min_y, max_y))

    title_w = max(3.0, text_width_mm(layout.title, 3.0))
    update(_SHEET_TEXT_X, _SHEET_TITLE_Y - 2.4, _SHEET_TEXT_X + title_w, _SHEET_TITLE_Y + 2.4)

    desc = f"{layout.name}"
    desc_w = max(1.5, text_width_mm(desc, 1.5))
    update(
        _SHEET_TEXT_X,
        _SHEET_DESCRIPTION_Y - 1.5,
        _SHEET_TEXT_X + desc_w,
        _SHEET_DESCRIPTION_Y + 1.5,
    )

    for i, line in enumerate(layout.sheet_annotations):
        y = snap(_SHEET_ANNOTATION_Y + i * _SHEET_ANNOTATION_PITCH)
        width = max(1.27, text_width_mm(line, 1.27))
        update(_SHEET_TEXT_X, y - 1.2, _SHEET_TEXT_X + width, y + 1.2)

    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _layout_bounds(layout: SheetLayout) -> tuple[float, float, float, float]:
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    for pc in layout.placed_ics:
        left, top, right, bottom = _component_block_bounds(pc)
        min_x = min(min_x, left)
        min_y = min(min_y, top)
        max_x = max(max_x, right)
        max_y = max(max_y, bottom)

    for pp in layout.placed_passives:
        min_x = min(min_x, pp.x - 10)
        min_y = min(min_y, pp.y - 5)
        max_x = max(max_x, pp.x + 10)
        max_y = max(max_y, pp.y + 5)

    for x1, y1, x2, y2 in layout.local_wires:
        min_x = min(min_x, x1, x2)
        min_y = min(min_y, y1, y2)
        max_x = max(max_x, x1, x2)
        max_y = max(max_y, y1, y2)

    for anchor in layout.local_net_anchors:
        min_x = min(min_x, anchor.x)
        min_y = min(min_y, anchor.y)
        max_x = max(max_x, anchor.x)
        max_y = max(max_y, anchor.y)
        if anchor.render_mode == "junction":
            continue
        if anchor.render_mode == "power" or anchor.name == "GND" or anchor.name.startswith("V"):
            min_x = min(min_x, anchor.x - 4.0)
            min_y = min(min_y, anchor.y - 4.0)
            max_x = max(max_x, anchor.x + 4.0)
            max_y = max(max_y, anchor.y + 4.0)
            continue

        label_w = max(1.27, text_width_mm(anchor.name, 1.27)) + 2.0
        label_h = 2.2
        angle = anchor.angle % 360
        if angle == 180:
            min_x = min(min_x, anchor.x - label_w)
            max_x = max(max_x, anchor.x)
            min_y = min(min_y, anchor.y - label_h / 2.0)
            max_y = max(max_y, anchor.y + label_h / 2.0)
        elif angle == 90:
            min_x = min(min_x, anchor.x - label_h / 2.0)
            max_x = max(max_x, anchor.x + label_h / 2.0)
            min_y = min(min_y, anchor.y - label_w)
            max_y = max(max_y, anchor.y)
        elif angle == 270:
            min_x = min(min_x, anchor.x - label_h / 2.0)
            max_x = max(max_x, anchor.x + label_h / 2.0)
            min_y = min(min_y, anchor.y)
            max_y = max(max_y, anchor.y + label_w)
        else:
            min_x = min(min_x, anchor.x)
            max_x = max(max_x, anchor.x + label_w)
            min_y = min(min_y, anchor.y - label_h / 2.0)
            max_y = max(max_y, anchor.y + label_h / 2.0)

    text_left, text_top, text_right, text_bottom = _sheet_text_bounds(layout)
    if text_right > text_left or text_bottom > text_top:
        min_x = min(min_x, text_left)
        min_y = min(min_y, text_top)
        max_x = max(max_x, text_right)
        max_y = max(max_y, text_bottom)

    if min_x == float("inf"):
        return 0.0, 0.0, 0.0, 0.0
    return min_x, min_y, max_x, max_y


def _layout_fits(layout: SheetLayout, margin: float = 20.0) -> bool:
    """Check whether the conservative layout bounds fit within the paper."""
    _, _, max_x, max_y = _layout_bounds(layout)
    pw, ph = PAPER_SIZES.get(layout.paper, PAPER_SIZES["A3"])
    usable_h = ph - TITLE_BLOCK_H
    title_block_clearance = min(margin, 5.0)
    return max_x <= pw - margin and max_y <= usable_h - title_block_clearance


def _layout_metrics(layout: SheetLayout) -> dict[str, float]:
    min_x, min_y, max_x, max_y = _layout_bounds(layout)
    pw, ph = PAPER_SIZES.get(layout.paper, PAPER_SIZES["A3"])
    usable_h = ph - TITLE_BLOCK_H
    content_w = max(0.0, max_x - min_x)
    content_h = max(0.0, max_y - min_y)
    content_area = content_w * content_h
    page_area = pw * usable_h
    utilization = (content_area / page_area) if page_area > 0 else 0.0
    content_aspect = (content_w / content_h) if content_h > 0 else 0.0
    page_aspect = pw / usable_h if usable_h > 0 else 0.0
    return {
        "content_w": content_w,
        "content_h": content_h,
        "content_area": content_area,
        "page_area": page_area,
        "utilization": utilization,
        "content_aspect": content_aspect,
        "page_aspect": page_aspect,
    }


def _layout_score(layout: SheetLayout) -> tuple[float, float, int]:
    metrics = _layout_metrics(layout)
    utilization_penalty = max(0.0, _MIN_PAGE_UTILIZATION - metrics["utilization"])
    aspect_penalty = (
        abs(math.log(metrics["content_aspect"] / metrics["page_aspect"]))
        if metrics["content_aspect"] > 0 and metrics["page_aspect"] > 0
        else 0.0
    )
    paper_rank = _PAPER_ORDER.index(layout.paper)
    # Sprint 44 T193 — penalise wire crossings (scaled to 0.0-0.1 range)
    wire_xing_penalty = _count_wire_crossings(layout) * 0.001
    return (
        paper_rank + utilization_penalty * 10.0 + aspect_penalty + wire_xing_penalty,
        -metrics["utilization"],
        paper_rank,
    )


def _count_wire_crossings(layout: SheetLayout) -> int:
    """Estimate the number of schematic wire crossings.

    Collects all horizontal and vertical wire segments and counts
    how many perpendicular pairs intersect. This is a heuristic —
    it does not account for junction-connected wires or bus spines
    — but provides a useful optimization signal for the placement
    scoring function.
    """
    horiz: list[tuple[float, float, float]] = []  # (y, x1, x2) with x1 < x2
    vert: list[tuple[float, float, float]] = []   # (x, y1, y2) with y1 < y2

    for x1, y1, x2, y2 in layout.local_wires:
        x1, y1, x2, y2 = snap(x1), snap(y1), snap(x2), snap(y2)

        if x1 == x2 and y1 == y2:
            continue

        if x1 == x2:
            y_lo, y_hi = (y1, y2) if y1 < y2 else (y2, y1)
            vert.append((x1, y_lo, y_hi))
        elif y1 == y2:
            x_lo, x_hi = (x1, x2) if x1 < x2 else (x2, x1)
            horiz.append((y1, x_lo, x_hi))

    crossings = 0
    for h_y, h_x1, h_x2 in horiz:
        for v_x, v_y1, v_y2 in vert:
            if v_y1 < h_y < v_y2 and h_x1 < v_x < h_x2:
                crossings += 1
                if crossings > 100:
                    return crossings

    return crossings


def _bus_net_groups(components: list) -> dict[str, list[str]]:
    """Detect bus signal groups from component pin nets.

    Returns dict mapping bus name (e.g. 'ADDR', 'DATA') to sorted
    list of net names (e.g. ['ADDR0', 'ADDR1', ...]) for buses with
    4+ members.
    """
    all_nets: set[str] = set()
    for comp in components:
        for net in (comp.pin_nets or {}).values():
            if net:
                all_nets.add(net)

    groups: dict[str, list[str]] = {}
    bus_re = re.compile(r"^([A-Z][A-Z0-9_]*?)(\d+)$")
    for net in sorted(all_nets):
        m = bus_re.match(net)
        if m:
            prefix = m.group(1)
            groups.setdefault(prefix, []).append(net)

    return {k: sorted(v) for k, v in groups.items() if len(v) >= 4}




def _passive_pin_side(pc: PlacedComponent, pin_point: tuple[float, float, int] | None) -> str:
    """Infer which body side a parent pin exits from.

    The pin's own angle is authoritative — a pin points into the body, so
    angle 0 exits the left face, 180 the right, 270 the top, 90 the bottom.
    Distance-based inference (the old behavior) misclassifies pins near a
    body corner (e.g. a top-face pin 2.5mm from the left edge read as
    "left"), which parked support passives on the wrong face and forced
    their wires straight through the symbol. Distance remains the fallback
    for nonstandard angles only.
    """
    if pin_point is None:
        return "right"
    px, py, angle = pin_point
    by_angle = {0: "left", 180: "right", 270: "top", 90: "bottom"}.get(int(angle) % 360)
    if by_angle is not None:
        return by_angle
    left, top, right, bottom = component_body_bounds(pc)
    distances = {
        "left": abs(px - left),
        "right": abs(px - right),
        "top": abs(py - top),
        "bottom": abs(py - bottom),
    }
    return min(distances, key=distances.get)


def _set_passive_pose(pp: PlacedPassive, x: float, y: float, angle: int) -> None:
    pp.x = snap(x)
    pp.y = snap(y)
    pp.angle = angle % 360


def _add_local_anchor(
    layout: SheetLayout,
    name: str,
    x: float,
    y: float,
    angle: int = 0,
    render_mode: str = "label",
    parent_ref: str = "",
) -> LocalNetAnchor:
    anchor = LocalNetAnchor(
        name=name,
        x=snap(x),
        y=snap(y),
        angle=angle % 360,
        render_mode=render_mode,
        parent_ref=parent_ref,
    )
    layout.local_net_anchors.append(anchor)
    return anchor


def _wire_points(layout: SheetLayout, start: tuple[float, float], end: tuple[float, float]) -> None:
    layout.local_wires.append((snap(start[0]), snap(start[1]), snap(end[0]), snap(end[1])))


def _segment_crosses_rect(
    x1: float, y1: float, x2: float, y2: float, rect: tuple[float, float, float, float]
) -> bool:
    """True when an orthogonal segment passes through a rect interior."""
    eps = 0.01
    left, top, right, bottom = rect
    if abs(x1 - x2) < eps:
        if not (left + eps < x1 < right - eps):
            return False
        seg_top, seg_bottom = sorted((y1, y2))
        return max(seg_top, top + eps) < min(seg_bottom, bottom - eps)
    if abs(y1 - y2) < eps:
        if not (top + eps < y1 < bottom - eps):
            return False
        seg_left, seg_right = sorted((x1, x2))
        return max(seg_left, left + eps) < min(seg_right, right - eps)
    return False


def _wire_points_around(
    layout: SheetLayout,
    pc: PlacedComponent,
    start: tuple[float, float],
    end: tuple[float, float],
) -> None:
    """Emit local wire segments from ``start`` to ``end`` avoiding the IC body.

    Cluster motifs historically assumed their pins exit on a side face and
    drew straight pin-to-anchor lines; parts whose SW/FB pins sit on the top
    face got a wire straight through the symbol body. Try the direct line
    and both L-bends first, then detour around the nearer vertical edge of
    the body. Falls back to the straight line if nothing clears.
    """
    body = component_body_bounds(pc)
    sx, sy = snap(start[0]), snap(start[1])
    ex, ey = snap(end[0]), snap(end[1])

    candidates: list[list[tuple[float, float]]] = [
        [(sx, sy), (ex, ey)],
        [(sx, sy), (ex, sy), (ex, ey)],
        [(sx, sy), (sx, ey), (ex, ey)],
    ]
    left, _top, right, _bottom = body
    detour_margin = snap(3.81)
    detour_x = snap(right + detour_margin) if abs(sx - right) <= abs(sx - left) else snap(left - detour_margin)
    candidates.append([(sx, sy), (detour_x, sy), (detour_x, ey), (ex, ey)])

    for candidate in candidates:
        segments = [
            (p1[0], p1[1], p2[0], p2[1])
            for p1, p2 in zip(candidate, candidate[1:])
            if abs(p1[0] - p2[0]) > 0.01 or abs(p1[1] - p2[1]) > 0.01
        ]
        if all(abs(x1 - x2) < 0.01 or abs(y1 - y2) < 0.01 for x1, y1, x2, y2 in segments) and not any(
            _segment_crosses_rect(x1, y1, x2, y2, body) for x1, y1, x2, y2 in segments
        ):
            for x1, y1, x2, y2 in segments:
                layout.local_wires.append((snap(x1), snap(y1), snap(x2), snap(y2)))
            return

    _wire_points(layout, start, end)


# Minimum center-to-center separation between sidecar passive bodies. A
# rendered review passive body is ~3.81mm plus ref/value labels; 7.62mm
# (3 grid units) keeps bodies and text clear of each other.
_SIDECAR_MIN_SEP = snap(7.62)
_SIDECAR_INLINE_OFFSET = snap(8.89)


def _sidecar_slot_is_free(occupied: list[tuple[float, float]], x: float, y: float) -> bool:
    """True when a candidate passive center keeps clearance from placed peers."""
    for ox, oy in occupied:
        if abs(x - ox) < _SIDECAR_MIN_SEP and abs(y - oy) < _SIDECAR_MIN_SEP:
            return False
    return True


def _reserve_occupancy(occupied: list[tuple[float, float]] | None, *points: tuple[float, float]) -> None:
    """Record occupied passive/anchor centers for later topology motifs."""
    if occupied is None:
        return
    for x, y in points:
        point = (snap(x), snap(y))
        if point not in occupied:
            occupied.append(point)


def _first_free_cluster_origin(
    base_x: float,
    base_y: float,
    occupied: list[tuple[float, float]] | None,
    footprint: list[tuple[float, float]],
    *,
    x_step: float = _SIDECAR_MIN_SEP,
    y_step: float = _SIDECAR_MIN_SEP,
) -> tuple[float, float]:
    """Return a nearby origin whose footprint clears the sheet occupancy list."""
    if not occupied:
        return snap(base_x), snap(base_y)

    offsets = [(0.0, 0.0)]
    for ring in range(1, 8):
        for dx in (0.0, ring * x_step, -ring * x_step):
            for dy in (0.0, ring * y_step, -ring * y_step):
                if dx != 0.0 or dy != 0.0:
                    offsets.append((dx, dy))

    for dx, dy in offsets:
        ox = snap(base_x + dx)
        oy = snap(base_y + dy)
        if all(_sidecar_slot_is_free(occupied, snap(ox + px), snap(oy + py)) for px, py in footprint):
            return ox, oy
    return snap(base_x), snap(base_y)


def _sidecar_candidates(pin_x: float, pin_y: float, side: str):
    """Yield (x, y, angle) candidate poses walking away from the pin.

    Candidates start inline with the pin and step outward along the pin's
    exit axis, then fan one lane to either side, so a taken slot pushes the
    next passive into a clean stagger instead of a stack.
    """
    for k in range(6):
        primary = snap(_SIDECAR_INLINE_OFFSET + k * _SIDECAR_MIN_SEP)
        for lateral in (0.0, _SIDECAR_MIN_SEP, -_SIDECAR_MIN_SEP):
            if side == "left":
                yield (snap(pin_x - primary), snap(pin_y + lateral), 180)
            elif side == "right":
                yield (snap(pin_x + primary), snap(pin_y + lateral), 0)
            elif side == "top":
                yield (snap(pin_x + lateral), snap(pin_y - primary), 270)
            else:
                yield (snap(pin_x + lateral), snap(pin_y + primary), 90)


def _apply_topology_sidecar_cluster(
    layout: SheetLayout,
    pc: PlacedComponent,
    passives: list[PlacedPassive],
    occupied: list[tuple[float, float]] | None = None,
) -> set[str]:
    """Place shunt/bias/filter passives near their owning pins with short explicit topology.

    Groups by the *resolved* parent-pin location rather than the owner-pin
    label: distinct owner labels (e.g. ``CVDD`` / ``CVDD_BULK`` / ``RRES``)
    frequently resolve to the same physical pin, and treating them as
    independent groups used to stack their passives at identical
    coordinates. Every pose is checked against an occupancy list so no two
    sidecar passives (including those from other parents on the same sheet)
    ever land closer than ``_SIDECAR_MIN_SEP``.
    """
    processed: set[str] = set()
    if not passives:
        return processed
    if occupied is None:
        occupied = []

    # Group passives by resolved pin coordinate so co-located owners share
    # one cluster instead of stacking.
    point_groups: dict[tuple[float, float], list[tuple[PlacedPassive, tuple[float, float, int]]]] = {}
    for pp in passives:
        if not pp.owner_pin:
            continue
        parent_pin = _parent_pin_point(pc, pp.owner_pin, pp.net1)
        if parent_pin is None:
            continue
        key = (round(parent_pin[0], 2), round(parent_pin[1], 2))
        point_groups.setdefault(key, []).append((pp, parent_pin))

    for key in sorted(point_groups):
        group = point_groups[key]
        parent_pin = group[0][1]
        pin_x, pin_y, _pin_angle = parent_pin
        side = _passive_pin_side(pc, parent_pin)
        ordered = sorted((pp for pp, _pt in group), key=lambda item: (item.role, item.ref))

        for pp in ordered:
            placed = False
            for x, y, angle in _sidecar_candidates(pin_x, pin_y, side):
                if _sidecar_slot_is_free(occupied, x, y):
                    _set_passive_pose(pp, x, y, angle)
                    occupied.append((x, y))
                    processed.add(pp.ref)
                    placed = True
                    break
            if not placed:
                # Every nearby slot is taken — fall back to the farthest
                # inline slot rather than silently stacking.
                primary = snap(_SIDECAR_INLINE_OFFSET + (6 + len(occupied) % 4) * _SIDECAR_MIN_SEP)
                if side == "left":
                    pose = (snap(pin_x - primary), pin_y, 180)
                elif side == "right":
                    pose = (snap(pin_x + primary), pin_y, 0)
                elif side == "top":
                    pose = (pin_x, snap(pin_y - primary), 270)
                else:
                    pose = (pin_x, snap(pin_y + primary), 90)
                _set_passive_pose(pp, *pose)
                occupied.append((pose[0], pose[1]))
                processed.add(pp.ref)

    return processed


def _apply_topology_buck_cluster(layout: SheetLayout, pc: PlacedComponent, passives: list[PlacedPassive]) -> set[str]:
    """Lay out a buck regulator using explicit local junctions and textbook placement."""
    processed: set[str] = set()
    if not passives:
        return processed

    by_role: dict[str, list[PlacedPassive]] = {}
    for pp in passives:
        by_role.setdefault(pp.role, []).append(pp)

    required = {"input_cap", "bootstrap_cap", "inductor", "output_cap", "feedback_bottom"}
    if not required.issubset(by_role):
        return processed

    cin = sorted(by_role["input_cap"], key=lambda item: item.ref)[0]
    cbst = sorted(by_role["bootstrap_cap"], key=lambda item: item.ref)[0]
    ind = sorted(by_role["inductor"], key=lambda item: item.ref)[0]
    cout = sorted(by_role["output_cap"], key=lambda item: item.ref)[0]
    fbb = sorted(by_role["feedback_bottom"], key=lambda item: item.ref)[0]
    fbt = sorted(by_role.get("feedback_top", []), key=lambda item: item.ref)
    fbt_item = fbt[0] if fbt else None

    vin_pin = _parent_pin_point(pc, cin.owner_pin, cin.net1)
    bst_pin = _parent_pin_point(pc, cbst.owner_pin, cbst.net1)
    sw_pin = _parent_pin_point(pc, ind.owner_pin, ind.net1)
    fb_pin = _parent_pin_point(pc, fbb.owner_pin, fbb.net1)
    if vin_pin is None or bst_pin is None or sw_pin is None or fb_pin is None:
        return processed

    left, top, right, bottom = component_body_bounds(pc)
    vin_x, vin_y, _vin_angle = vin_pin
    bst_x, bst_y, _bst_angle = bst_pin
    sw_x, sw_y, _sw_angle = sw_pin
    fb_x, fb_y, _fb_angle = fb_pin

    # Face-aware placement: the textbook motif historically assumed side-face
    # pins and hung everything below the IC. Parts whose VIN/SW/FB pins sit
    # on the top face got junction anchors and caps on the far side of the
    # body — every connection then ran straight through the symbol.
    def _away(side: str) -> tuple[float, float]:
        return {"left": (-1.0, 0.0), "right": (1.0, 0.0), "top": (0.0, -1.0), "bottom": (0.0, 1.0)}[side]

    vin_side = _passive_pin_side(pc, vin_pin)
    sw_side = _passive_pin_side(pc, sw_pin)
    fb_side = _passive_pin_side(pc, fb_pin)

    vin_dx, vin_dy = _away(vin_side)
    if vin_side in ("top", "bottom"):
        cin_pos = (vin_x, snap(vin_y + vin_dy * 10.16))
    else:
        cin_pos = (snap(vin_x + vin_dx * 10.16), snap(vin_y + 5.08))
    _set_passive_pose(cin, cin_pos[0], cin_pos[1], 90)

    sw_dx, sw_dy = _away(sw_side)
    if sw_side in ("top", "bottom"):
        sw_anchor_y = snap(sw_y + sw_dy * 7.62)
    else:
        sw_anchor_y = snap(max(bottom + 7.62, sw_y + 7.62))
    sw_anchor = _add_local_anchor(layout, ind.net1, snap(sw_x), sw_anchor_y, 270, "junction", pc.ref)
    fb_dx, fb_dy = _away(fb_side)
    if fb_side in ("top", "bottom"):
        fb_anchor = _add_local_anchor(
            layout, fbb.net1, snap(fb_x), snap(fb_y + fb_dy * 7.62), 0, "junction", pc.ref
        )
    else:
        fb_anchor = _add_local_anchor(layout, fbb.net1, snap(fb_x + 8.89), snap(fb_y), 0, "junction", pc.ref)

    if sw_side == "top":
        _set_passive_pose(cbst, snap(sw_anchor.x + 2.54), snap(sw_anchor.y - 7.62), 90)
    else:
        _set_passive_pose(cbst, snap(sw_anchor.x + 2.54), snap(top - 6.35), 90)
    _set_passive_pose(ind, snap(sw_anchor.x + 12.70), snap(sw_anchor.y), 0)

    (_sw_pin_x, _sw_pin_y), (vout_pin_x, vout_pin_y) = passive_pin_xy(ind.x, ind.y, ind.angle)
    vout_anchor = _add_local_anchor(layout, ind.net2, snap(vout_pin_x + 8.89), snap(vout_pin_y), 0, "label", pc.ref)

    # Grow the output/divider columns away from the body, not into it.
    grow = -1.0 if sw_side == "top" else 1.0
    _set_passive_pose(cout, vout_anchor.x, snap(vout_anchor.y + grow * 11.43), 90)

    if fbt_item is not None:
        _set_passive_pose(fbt_item, snap(fb_anchor.x + 12.70), snap(fb_anchor.y), 0)
        processed.add(fbt_item.ref)

    fb_grow = -1.0 if fb_side == "top" else 1.0
    _set_passive_pose(fbb, fb_anchor.x, snap(fb_anchor.y + fb_grow * 11.43), 90)

    _wire_points_around(layout, pc, (sw_x, sw_y), (sw_anchor.x, sw_anchor.y))
    _wire_points_around(layout, pc, (fb_x, fb_y), (fb_anchor.x, fb_anchor.y))

    processed.update({cin.ref, cbst.ref, ind.ref, cout.ref, fbb.ref})
    return processed


_DECOUPLING_BANK_PITCH = snap(7.62)
_STRAP_LADDER_PITCH = snap(7.62)


def _apply_topology_decoupling_bank(
    layout: SheetLayout,
    pc: PlacedComponent,
    passives: list[PlacedPassive],
    occupied: list[tuple[float, float]] | None = None,
) -> set[str]:
    """Stack decoupling caps that share a rail into a compact vertical bank.

    Groups caps by their rail net (net1).  Banks of 2+ caps get a shared
    rail label at the top and shared ground anchor at the bottom, placed
    below the IC body.
    """
    processed: set[str] = set()
    caps = [pp for pp in passives if pp.sym_type == "C" and pp.role == "decoupling"]
    if not caps:
        return processed

    by_rail: dict[str, list[PlacedPassive]] = {}
    for cap in caps:
        by_rail.setdefault(cap.net1, []).append(cap)

    left, _top, right, bottom = component_body_bounds(pc)
    center_x = snap((left + right) / 2.0)
    bank_y = snap(bottom + 12.70)
    bank_idx = 0

    for rail_net, bank in by_rail.items():
        if len(bank) < 2:
            continue
        ordered = sorted(bank, key=lambda c: c.ref)
        preferred_x = snap(center_x + bank_idx * _DECOUPLING_BANK_PITCH * 2)
        footprint = [(0.0, snap(-5.08))]
        footprint.extend((0.0, snap(i * _DECOUPLING_BANK_PITCH)) for i in range(len(ordered)))
        footprint.append((0.0, snap((len(ordered) - 1) * _DECOUPLING_BANK_PITCH + 5.08)))
        bank_x, bank_y_local = _first_free_cluster_origin(preferred_x, bank_y, occupied, footprint)

        rail_anchor = _add_local_anchor(layout, rail_net, bank_x, snap(bank_y_local - 5.08), 270, "label", pc.ref)

        for i, cap in enumerate(ordered):
            cap_y = snap(bank_y_local + i * _DECOUPLING_BANK_PITCH)
            _set_passive_pose(cap, bank_x, cap_y, 90)
            processed.add(cap.ref)

        gnd_net = ordered[0].net2
        gnd_y = snap(bank_y_local + (len(ordered) - 1) * _DECOUPLING_BANK_PITCH + 5.08)
        gnd_anchor = _add_local_anchor(layout, gnd_net, bank_x, gnd_y, 90, "power", pc.ref)
        _reserve_occupancy(occupied, (rail_anchor.x, rail_anchor.y), (gnd_anchor.x, gnd_anchor.y))
        _reserve_occupancy(occupied, *((cap.x, cap.y) for cap in ordered))
        bank_idx += 1

    return processed


def _apply_topology_strap_ladder(
    layout: SheetLayout,
    pc: PlacedComponent,
    passives: list[PlacedPassive],
    occupied: list[tuple[float, float]] | None = None,
) -> set[str]:
    """Align strap resistors that share a rail into a tidy vertical column.

    Groups straps by their rail net (net2 for pull-ups/downs).  Ladders of
    2+ straps get aligned placement with a shared rail anchor.
    """
    processed: set[str] = set()
    straps = [
        pp
        for pp in passives
        if pp.sym_type == "R"
        and pp.role
        in (
            "strap",
            "termination",
            "boot_strap",
            "pull_up",
            "pull_down",
        )
    ]
    if not straps:
        return processed

    by_rail: dict[str, list[PlacedPassive]] = {}
    for strap in straps:
        by_rail.setdefault(strap.net2, []).append(strap)

    left, _top, right, bottom = component_body_bounds(pc)
    center_x = snap((left + right) / 2.0)
    ladder_y = snap(bottom + 12.70)
    ladder_idx = 0

    for rail_net, ladder in by_rail.items():
        if len(ladder) < 2:
            continue
        ordered = sorted(ladder, key=lambda r: r.ref)
        preferred_x = snap(center_x - 20.32 + ladder_idx * _STRAP_LADDER_PITCH * 2)
        footprint = [(0.0, snap(i * _STRAP_LADDER_PITCH)) for i in range(len(ordered))]
        footprint.append((0.0, snap((len(ordered) - 1) * _STRAP_LADDER_PITCH + 5.08)))
        ladder_x, ladder_y_local = _first_free_cluster_origin(preferred_x, ladder_y, occupied, footprint)

        for i, strap in enumerate(ordered):
            strap_y = snap(ladder_y_local + i * _STRAP_LADDER_PITCH)
            _set_passive_pose(strap, ladder_x, strap_y, 90)
            processed.add(strap.ref)

        rail_y = snap(ladder_y_local + (len(ordered) - 1) * _STRAP_LADDER_PITCH + 5.08)
        rail_anchor = _add_local_anchor(layout, rail_net, ladder_x, rail_y, 90, "power", pc.ref)
        _reserve_occupancy(occupied, (rail_anchor.x, rail_anchor.y))
        _reserve_occupancy(occupied, *((strap.x, strap.y) for strap in ordered))
        ladder_idx += 1

    return processed


def _apply_topology_ldo_cluster(
    layout: SheetLayout,
    pc: PlacedComponent,
    passives: list[PlacedPassive],
    occupied: list[tuple[float, float]] | None = None,
) -> set[str]:
    """Place LDO support passives (CIN + COUT + optional enable) as a compact unit.

    Detects a power-category IC with one input cap and one output cap
    and places them in a row below the IC: CIN left-of-center,
    COUT right-of-center, with shared rail/ground labels.
    """
    processed: set[str] = set()
    if pc.comp.category != "power":
        return processed

    caps = [pp for pp in passives if pp.sym_type == "C" and pp.role == "decoupling"]
    if len(caps) != 2:
        return processed

    # Identify CIN (input) vs COUT (output) by owner_pin name
    cin = cout = None
    for cap in caps:
        pin_hint = (cap.owner_pin or "").upper()
        if "IN" in pin_hint or "CIN" in pin_hint:
            cin = cap
        elif "OUT" in pin_hint or "COUT" in pin_hint:
            cout = cap
    if cin is None or cout is None:
        # Fallback: first cap = CIN, second = COUT
        cin, cout = caps[0], caps[1]

    left, _top, right, bottom = component_body_bounds(pc)
    center_x = snap((left + right) / 2.0)
    cluster_y = snap(bottom + 10.16)
    spacing = snap(12.70)

    has_enable_strap = len([pp for pp in passives if pp.sym_type == "R" and pp.role in ("pull_up", "strap")]) == 1
    footprint = [
        (snap(-spacing / 2), 0.0),
        (snap(spacing / 2), 0.0),
        (snap(-spacing / 2), snap(-5.08)),
        (snap(spacing / 2), snap(-5.08)),
        (0.0, snap(5.08)),
    ]
    if has_enable_strap:
        footprint.append((spacing, 0.0))
    cluster_x, cluster_y = _first_free_cluster_origin(center_x, cluster_y, occupied, footprint)

    _set_passive_pose(cin, snap(cluster_x - spacing / 2), cluster_y, 90)
    _set_passive_pose(cout, snap(cluster_x + spacing / 2), cluster_y, 90)

    # Shared rail labels
    cin_anchor = _add_local_anchor(
        layout, cin.net1, snap(cluster_x - spacing / 2), snap(cluster_y - 5.08), 270, "label", pc.ref
    )
    cout_anchor = _add_local_anchor(
        layout, cout.net1, snap(cluster_x + spacing / 2), snap(cluster_y - 5.08), 270, "label", pc.ref
    )
    # Shared ground
    gnd_net = cin.net2
    gnd_y = snap(cluster_y + 5.08)
    gnd_anchor = _add_local_anchor(layout, gnd_net, snap(cluster_x), gnd_y, 90, "power", pc.ref)

    processed.update({cin.ref, cout.ref})
    _reserve_occupancy(
        occupied,
        (cin.x, cin.y),
        (cout.x, cout.y),
        (cin_anchor.x, cin_anchor.y),
        (cout_anchor.x, cout_anchor.y),
        (gnd_anchor.x, gnd_anchor.y),
    )

    # Handle enable strap if present
    straps = [pp for pp in passives if pp.sym_type == "R" and pp.role in ("pull_up", "strap")]
    if len(straps) == 1:
        en_strap = straps[0]
        _set_passive_pose(en_strap, snap(cluster_x + spacing), cluster_y, 90)
        processed.add(en_strap.ref)
        _reserve_occupancy(occupied, (en_strap.x, en_strap.y))

    return processed


def _apply_topology_cc_network(layout: SheetLayout, pc: PlacedComponent, passives: list[PlacedPassive]) -> set[str]:
    """Place USB-C CC pull-down resistors as a tight pair beside the connector.

    Detects a connector-category IC with 2 straps both pulling to GND
    with the same value (5.1k CC pull-downs).
    """
    processed: set[str] = set()
    if pc.comp.category != "connector":
        return processed

    straps = [pp for pp in passives if pp.sym_type == "R" and pp.role == "termination"]
    if len(straps) != 2:
        return processed

    # Check both pull to same GND rail with same value
    if straps[0].net2 != straps[1].net2 or straps[0].value != straps[1].value:
        return processed

    left, _top, right, bottom = component_body_bounds(pc)
    # Keep the pin-1 label stubs clear of the connector body. At the closer
    # 10.16 mm offset, their 5.08 mm pin reach leaves only 5.08 mm to the body,
    # so a standard 6.35 mm label stub runs back through it.
    cc_x = snap(right + 15.24)
    cc_y = snap((bottom + _top) / 2.0)
    pitch = snap(5.08)

    _set_passive_pose(straps[0], cc_x, snap(cc_y - pitch / 2), 0)
    _set_passive_pose(straps[1], cc_x, snap(cc_y + pitch / 2), 0)

    # Shared ground label
    gnd_net = straps[0].net2
    _add_local_anchor(layout, gnd_net, snap(cc_x + 6.35), cc_y, 0, "power", pc.ref)

    processed.update({straps[0].ref, straps[1].ref})
    return processed


def _apply_topology_local_circuits(layout: SheetLayout) -> None:
    """Promote topology-aware passive groups onto deliberate local motifs.

    Dispatch chain (most specific → most generic):
    1. Buck cluster (full switcher topology)
    2. LDO cluster (CIN + COUT as compact unit)
    3. CC network (USB-C pull-down pair)
    4. Decoupling bank (2+ caps sharing a rail)
    5. Strap ladder (2+ straps sharing a rail)
    6. Sidecar cluster (generic fallback)
    """
    by_parent: dict[str, list[PlacedPassive]] = {}
    placed_ic_map = {pc.ref: pc for pc in layout.placed_ics}
    for pp in layout.placed_passives:
        if pp.presentation != "topology_local" or not pp.parent_ref:
            continue
        by_parent.setdefault(pp.parent_ref, []).append(pp)

    # Sheet-wide occupancy for the sidecar fallback: seeded with every pose
    # decided by the deliberate cluster motifs so late sidecars never stack
    # onto them (or onto each other across parents).
    occupied: list[tuple[float, float]] = []
    sidecar_work: list[tuple[PlacedComponent, list[PlacedPassive]]] = []

    for parent_ref, passives in by_parent.items():
        pc = placed_ic_map.get(parent_ref)
        if pc is None:
            continue

        anchor_start = len(layout.local_net_anchors)
        processed = _apply_topology_buck_cluster(layout, pc, passives)
        remainder = [pp for pp in passives if pp.ref not in processed]
        if remainder:
            ldo_done = _apply_topology_ldo_cluster(layout, pc, remainder, occupied)
            processed.update(ldo_done)
            remainder = [pp for pp in remainder if pp.ref not in ldo_done]
        if remainder:
            cc_done = _apply_topology_cc_network(layout, pc, remainder)
            processed.update(cc_done)
            remainder = [pp for pp in remainder if pp.ref not in cc_done]
        if remainder:
            bank_done = _apply_topology_decoupling_bank(layout, pc, remainder, occupied)
            processed.update(bank_done)
            remainder = [pp for pp in remainder if pp.ref not in bank_done]
        if remainder:
            ladder_done = _apply_topology_strap_ladder(layout, pc, remainder, occupied)
            processed.update(ladder_done)
            remainder = [pp for pp in remainder if pp.ref not in ladder_done]
        for pp in passives:
            if pp.ref in processed:
                _reserve_occupancy(occupied, (pp.x, pp.y))
        _reserve_occupancy(occupied, *((anchor.x, anchor.y) for anchor in layout.local_net_anchors[anchor_start:]))
        if remainder:
            sidecar_work.append((pc, remainder))

    # Junction anchors are wiring targets — a sidecar body parked on top of
    # one forces the connecting wire straight through the passive.
    _reserve_occupancy(occupied, *((anchor.x, anchor.y) for anchor in layout.local_net_anchors))

    for pc, remainder in sidecar_work:
        _apply_topology_sidecar_cluster(layout, pc, remainder, occupied)


# ================================================================
# Layout engine
# ================================================================

# F13 — continuous connector-density strategy selection. A sheet whose
# estimated occupied area is mostly connectors packs them width-first
# across the page; below the threshold connectors stay in a side column.
_CONNECTOR_DOMINANCE_THRESHOLD = 0.7
_CONNECTOR_DENSITY_MIN_COUNT = 4


def _connector_dominance(connectors: list, ics: list) -> float:
    """Fraction of estimated occupied block area contributed by connectors.

    Replaces the old boolean cliff (``>= 8 connectors and no regulators and
    few ICs``): a 7-connector board or an 8-connector board with one small
    regulator used to fall back to the sparse single-column layout, while a
    12-connector board with one large MCU was forced into the dense
    connector grid even when the normal layout fit. Area-weighting makes
    the decision proportional — one big MCU pulls the score down as much
    as several small ICs, and a page genuinely dominated by connector area
    pulls it up regardless of the exact connector count.
    """
    if len(connectors) < _CONNECTOR_DENSITY_MIN_COUNT:
        return 0.0

    def _block_area(comps: list) -> float:
        total = 0.0
        for comp in comps:
            w, h = component_block_size(comp)
            total += max(w, 1.0) * max(h, 1.0)
        return total

    connector_area = _block_area(connectors)
    total_area = connector_area + _block_area(ics)
    if total_area <= 0:
        return 0.0
    return connector_area / total_area


def layout_sheet(
    sheet_alloc,
    presentation_wiring_policy: PresentationWiringPolicy | dict | None = None,
) -> SheetLayout:
    """Compute positions for all components on a sheet.

    Layout algorithm:
    1. Separate connectors (left) from ICs (center)
    2. Sort ICs: power regulators first (left-to-right chain), then by pin count
    3. Compute actual symbol dimensions for spacing
    4. Place bypass caps in a tight row directly below each IC
    5. Place straps beside their IC
    6. Auto-upgrade paper size if content overflows
    """

    # Separate by role
    connectors = sorted(
        [c for c in sheet_alloc.components if c.ref_prefix in ("J", "P")],
        key=_component_sort_key,
    )
    regulators = sorted(
        [c for c in sheet_alloc.components if c.category == "power" and c.ref_prefix not in ("J", "P")],
        key=_power_component_priority,
    )
    other_ics = sorted(
        [c for c in sheet_alloc.components if c.ref_prefix not in ("J", "P") and c.category != "power"],
        key=lambda c: (-len(c.pins), _component_sort_key(c)),
    )

    connector_heavy = _connector_dominance(connectors, regulators + other_ics) >= _CONNECTOR_DOMINANCE_THRESHOLD

    def _port_name(port) -> str:
        if isinstance(port, dict):
            return str(port.get("name", "")).strip()
        return str(getattr(port, "name", "")).strip()

    def _port_direction(port) -> str:
        if isinstance(port, dict):
            return str(port.get("direction", "bidirectional")).strip() or "bidirectional"
        return str(getattr(port, "direction", "bidirectional")).strip() or "bidirectional"

    def _wire_coords(wire) -> tuple[float, float, float, float]:
        if isinstance(wire, dict):
            return (
                float(wire.get("x1", 0.0)),
                float(wire.get("y1", 0.0)),
                float(wire.get("x2", 0.0)),
                float(wire.get("y2", 0.0)),
            )
        return (
            float(getattr(wire, "x1", 0.0)),
            float(getattr(wire, "y1", 0.0)),
            float(getattr(wire, "x2", 0.0)),
            float(getattr(wire, "y2", 0.0)),
        )

    sheet_policy = normalize_presentation_wiring_policy(
        getattr(sheet_alloc, "presentation_wiring_policy", None) or presentation_wiring_policy
    )

    def _build_layout(paper: str) -> SheetLayout:
        pw, ph = PAPER_SIZES.get(paper, PAPER_SIZES["A3"])
        layout = SheetLayout(
            name=sheet_alloc.name,
            title=sheet_alloc.title,
            paper=paper,
            sheet_annotations=list(sheet_alloc.sheet_annotations),
            presentation_wiring_policy=sheet_policy,
        )

        margin = snap(20)
        header_h = snap(32 + len(sheet_alloc.sheet_annotations) * 5)
        y_start = header_h
        x_limit = snap(pw - margin)
        section_top = y_start
        compact_sheet = len(sheet_alloc.components) <= 8

        # Density-scaled gap helper: spreads components across available
        # page area when the sheet is large but the component count is
        # modest — avoids corner-clustering. (T201)
        usable_h = ph - TITLE_BLOCK_H - margin

        if connectors and (not (regulators or other_ics) or connector_heavy):
            # Connector-heavy sheets need width-first packing; a single-column
            # connector rail forces A0/A1 pages even when most of the page is blank.
            if compact_sheet:
                conn_col_gap, conn_row_gap = _CONNECTOR_GAP, _CONNECTOR_GAP
            else:
                conn_col_gap, conn_row_gap = _density_scaled_gaps(
                    connectors, _CONNECTOR_GAP, _CONNECTOR_GAP, pw, usable_h, margin,
                )
            conn_max_cols = _preferred_max_cols(
                connectors,
                margin,
                x_limit,
                col_gap=conn_col_gap,
            )
            placed, bottom = _layout_components_rowwise(
                connectors,
                margin,
                y_start,
                x_limit,
                col_gap=conn_col_gap,
                row_gap=conn_row_gap,
                max_cols=conn_max_cols,
            )
            layout.placed_ics.extend(placed)
            section_top = snap(bottom + _SECTION_GAP)
            main_x_start = margin
        elif connectors:
            if compact_sheet:
                conn_col_gap, conn_row_gap = _CONNECTOR_GAP, _CONNECTOR_GAP
            else:
                conn_col_gap, conn_row_gap = _density_scaled_gaps(
                    connectors, _CONNECTOR_GAP, _CONNECTOR_GAP, pw, usable_h, margin,
                )
            connector_col_w = max(component_block_size(comp)[0] for comp in connectors)
            connector_cols = 1
            if compact_sheet and len(connectors) >= 4:
                connector_cols = min(2, len(connectors))
            connector_area_right = snap(margin + connector_cols * connector_col_w + (connector_cols - 1) * conn_col_gap)
            placed, _ = _layout_components_rowwise(
                connectors,
                margin,
                y_start,
                connector_area_right,
                col_gap=conn_col_gap,
                row_gap=conn_row_gap,
                max_cols=connector_cols,
            )
            layout.placed_ics.extend(placed)
            main_x_start = snap(connector_area_right + _SECTION_GAP)
        else:
            main_x_start = margin

        if regulators:
            reg_max_cols = (
                _preferred_max_cols(regulators, main_x_start, x_limit)
                if len(regulators) > _REG_GRID_THRESHOLD
                else None
            )
            reg_col_gap, reg_row_gap = _rowwise_gap_profile(regulators)
            if not compact_sheet:
                reg_col_gap, reg_row_gap = _density_scaled_gaps(
                    regulators, reg_col_gap, reg_row_gap, pw, usable_h, main_x_start,
                )
            placed, bottom = _layout_components_rowwise(
                regulators,
                main_x_start,
                section_top,
                x_limit,
                col_gap=reg_col_gap,
                row_gap=reg_row_gap,
                max_cols=max(_REG_GRID_COLS, reg_max_cols or 0) if reg_max_cols else None,
            )
            layout.placed_ics.extend(placed)
            section_top = snap(bottom + _SECTION_GAP)

        if other_ics:
            other_max_cols = _preferred_max_cols(other_ics, main_x_start, x_limit)
            other_col_gap, other_row_gap = _rowwise_gap_profile(other_ics)
            if not compact_sheet:
                other_col_gap, other_row_gap = _density_scaled_gaps(
                    other_ics, other_col_gap, other_row_gap, pw, usable_h, main_x_start,
                )
            placed, bottom = _layout_components_rowwise(
                other_ics,
                main_x_start,
                section_top,
                x_limit,
                col_gap=other_col_gap,
                row_gap=other_row_gap,
                max_cols=other_max_cols,
            )
            layout.placed_ics.extend(placed)
            section_top = snap(bottom + _SECTION_GAP)

        # ---- Place bypass caps directly below each IC ----
        for pc in layout.placed_ics:
            comp = pc.comp
            if not comp.bypass_caps:
                continue

            _, _, _, bottom = _component_local_extents(comp)
            cap_y = snap(pc.y + bottom + _CAP_START_OFFSET)
            cols = max(1, _support_cluster_cols(comp))
            col_pitch = _support_cluster_col_pitch(comp)
            row_pitch = _support_cluster_row_pitch(comp)
            x_origin = snap(pc.x - ((cols - 1) * col_pitch) / 2.0)

            for idx, bc in enumerate(comp.bypass_caps):
                sym_type, ref_prefix = _bypass_sym_type_and_prefix(bc.value, bc.footprint, bc.role)
                cap_ref = str(getattr(bc, "source_ref", "") or "")
                if cap_ref:
                    _reserve_ref(cap_ref)
                else:
                    cap_ref = _next_ref(ref_prefix)
                col = idx % cols
                row = idx // cols
                cap_x = snap(x_origin + col * col_pitch)
                row_y = snap(cap_y + row * row_pitch)
                layout.placed_passives.append(
                    PlacedPassive(
                        ref=cap_ref,
                        value=bc.value,
                        footprint=bc.footprint,
                        x=cap_x,
                        y=row_y,
                        net1=bc.net,
                        net2=bc.gnd_net,
                        sym_type=sym_type,
                        angle=90 if sym_type == "C" else 0,
                        parent_ref=pc.ref,
                        owner_pin=bc.pin,
                        role=bc.role,
                        presentation=_resolve_support_passive_presentation(bc.presentation, comp, sheet_policy),
                        symbol_variant=_support_passive_symbol_variant(sym_type, bc.role)[0],
                        pin_span=_support_passive_symbol_variant(sym_type, bc.role)[1],
                    )
                )

        # ---- Place strap resistors beside their IC ----
        for pc in layout.placed_ics:
            comp = pc.comp
            if not comp.straps:
                continue

            body_w, _ = component_body_size(comp)
            strap_x = snap(pc.x + body_w + _STRAP_X_OFFSET)
            strap_y = snap(pc.y)
            strap_pitch = _strap_pitch(comp)

            for strap in comp.straps:
                res_ref = str(getattr(strap, "source_ref", "") or "")
                if res_ref:
                    _reserve_ref(res_ref)
                else:
                    res_ref = _next_ref("R")
                layout.placed_passives.append(
                    PlacedPassive(
                        ref=res_ref,
                        value=strap.value,
                        footprint=strap.footprint,
                        x=strap_x,
                        y=strap_y,
                        net1=strap.net,
                        net2=strap.rail,
                        sym_type="R",
                        parent_ref=pc.ref,
                        owner_pin=strap.pin,
                        role=strap.role,
                        presentation=_resolve_support_passive_presentation(strap.presentation, comp, sheet_policy),
                        symbol_variant=_support_passive_symbol_variant("R", strap.role)[0],
                        pin_span=_support_passive_symbol_variant("R", strap.role)[1],
                    )
                )
                strap_y = snap(strap_y + strap_pitch)

        seen_boundary_ports: set[tuple[str, str]] = set()
        for pc in layout.placed_ics:
            comp = pc.comp
            for port in getattr(comp, "template_boundary_ports", []):
                name = _port_name(port)
                if not name:
                    continue
                key = (name, _port_direction(port))
                if key not in seen_boundary_ports:
                    seen_boundary_ports.add(key)
                    layout.boundary_ports.append(key)
            for wire in getattr(comp, "template_local_wires", []):
                x1, y1, x2, y2 = _wire_coords(wire)
                layout.local_wires.append(
                    (
                        snap(pc.x + x1),
                        snap(pc.y + y1),
                        snap(pc.x + x2),
                        snap(pc.y + y2),
                    )
                )

        _apply_topology_local_circuits(layout)

        return layout

    baseline_state = _snapshot_ref_state()
    fitting_layouts: list[tuple[SheetLayout, tuple[dict[str, int], set[str], set[str]]]] = []
    last_layout: SheetLayout | None = None
    last_state: tuple[dict[str, int], set[str], set[str]] = baseline_state

    # Start from the allocator's chosen paper size instead of A4.
    # Density-scaled gaps expand with paper area, which can cause
    # layouts on intermediate paper sizes (A3 for a 5-IC circuit)
    # to overflow while A2 with even larger gaps still fits.
    # Respecting the allocator's size prevents the cascade.
    alloc_paper = getattr(sheet_alloc, "paper", "A4")
    try:
        start_idx = _PAPER_ORDER.index(alloc_paper)
    except ValueError:
        start_idx = 0

    # Try the allocator's paper first — if it fits, use it.
    _restore_ref_state(baseline_state)
    first_layout = _build_layout(alloc_paper)
    first_state = _snapshot_ref_state()
    if _layout_fits(first_layout):
        _restore_ref_state(first_state)
        return first_layout

    # A locked paper size is a reviewed preference. Warn and promote
    # if it no longer fits.
    if getattr(sheet_alloc, "lock_paper_size", False):
        print(
            f"WARNING: sheet '{sheet_alloc.name}' locked paper size "
            f"{alloc_paper} no longer fits; promoting to the next fitting size"
        )

    # Allocator-chosen size doesn't fit — try larger sizes.
    last_layout = first_layout
    last_state = first_state

    for paper in _PAPER_ORDER[start_idx + 1 :]:
        _restore_ref_state(baseline_state)
        trial_layout = _build_layout(paper)
        trial_state = _snapshot_ref_state()
        if _layout_fits(trial_layout):
            fitting_layouts.append((trial_layout, trial_state))
        last_layout = trial_layout
        last_state = trial_state

    if fitting_layouts:
        layout, final_state = min(fitting_layouts, key=lambda item: _layout_score(item[0]))
        _restore_ref_state(final_state)
        return layout

    layout = last_layout if last_layout is not None else _build_layout("A0")
    _restore_ref_state(last_state)
    layout.overflow = True

    pw, ph = PAPER_SIZES.get(layout.paper, PAPER_SIZES["A3"])
    usable_h = ph - TITLE_BLOCK_H
    min_x, min_y, max_x, max_y = _layout_bounds(layout)
    if max_x > pw - 20:
        print(
            f"WARNING: sheet '{layout.name}' exceeds right page edge ({max_x:.1f} > {pw - 20:.1f}mm) [{layout.paper}]"
        )
    if max_y > usable_h - 20:
        print(
            f"WARNING: sheet '{layout.name}' exceeds bottom page edge "
            f"({max_y:.1f} > {usable_h - 20:.1f}mm) [{layout.paper}]"
        )
    if min_x < 20 or min_y < 20:
        print(
            f"WARNING: sheet '{layout.name}' crowds the page margin (min=({min_x:.1f}, {min_y:.1f})) [{layout.paper}]"
        )

    return layout


def split_sheet_allocation(sheet_alloc) -> list | None:
    """Split an overflowing sheet allocation into two area-balanced halves.

    Components keep their allocation order (they were grouped by function),
    and the cut point balances estimated block area rather than count so a
    half with a few huge parts doesn't immediately overflow again. Returns
    ``None`` when the sheet cannot be split (fewer than two components) —
    the caller must then fail with a clear "design too large" error instead
    of emitting a crammed page (F15).
    """
    from .allocator import SheetAllocation

    components = list(sheet_alloc.components)
    if len(components) < 2:
        return None

    areas = []
    for comp in components:
        w, h = component_block_size(comp)
        areas.append(max(w, 1.0) * max(h, 1.0))
    total = sum(areas)

    cut = 1
    running = areas[0]
    for idx in range(1, len(components)):
        if running >= total / 2.0:
            break
        running += areas[idx]
        cut = idx + 1
    cut = min(cut, len(components) - 1)

    halves = []
    for suffix, part in (("1", components[:cut]), ("2", components[cut:])):
        # Sheet-level bypass/strap lists mirror the member components' own
        # support passives — recompute per half rather than duplicating.
        half_bypass = [cap for comp in part for cap in comp.bypass_caps]
        half_straps = [strap for comp in part for strap in comp.straps]
        halves.append(
            SheetAllocation(
                name=f"{sheet_alloc.name}_{suffix}",
                title=f"{sheet_alloc.title} ({suffix}/2)",
                paper=sheet_alloc.paper,
                components=part,
                bypass_caps=half_bypass,
                straps=half_straps,
                sheet_annotations=list(sheet_alloc.sheet_annotations),
                lock_paper_size=False,
                presentation_wiring_policy=sheet_alloc.presentation_wiring_policy,
                explicit_group=sheet_alloc.explicit_group,
            )
        )
    return halves
