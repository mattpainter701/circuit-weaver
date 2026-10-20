"""KiCad schematic S-expression primitives — grid, wiring, symbols, placement.

Generic KiCad S-expression builders. Pure functions with no
project-specific state. All coordinates are in mm on the 1.27mm grid.
"""

import math
import re
import uuid
from dataclasses import dataclass

# ================================================================
# Grid
# ================================================================
GRID = 1.27  # mm (50 mil) — KiCad schematic connection grid
ANNOTATION_MARGIN_X = 20.0  # mm minimum left margin for viewer-safe text placement


def snap(val):
    """Snap a coordinate to the KiCad 1.27mm schematic connection grid."""
    return round(val / GRID) * GRID


# Paper sizes in mm (landscape orientation, width × height)
PAPER_SIZES = {
    "A4": (297, 210),
    "A3": (420, 297),
    "A2": (594, 420),
    "A1": (841, 594),
    "A0": (1189, 841),
}

TITLE_BLOCK_H = 30  # mm reserved at page bottom

# Standard passive footprint strings
FP_0402C = "Capacitor_SMD:C_0402_1005Metric"
FP_0805C = "Capacitor_SMD:C_0805_2012Metric"
FP_1206C = "Capacitor_SMD:C_1206_3216Metric"
FP_0402R = "Resistor_SMD:R_0402_1005Metric"
FP_0805R = "Resistor_SMD:R_0805_2012Metric"
FP_0805L = "Inductor_SMD:L_0805_2012Metric"


# ================================================================
# UUID
# ================================================================
_deterministic_uid_seed = None
_deterministic_uid_counter = 0


def configure_deterministic_uids(seed: str | None):
    """Enable deterministic UUID generation for diff-friendly output."""
    global _deterministic_uid_seed, _deterministic_uid_counter
    _deterministic_uid_seed = seed or None
    _deterministic_uid_counter = 0


def disable_deterministic_uids():
    configure_deterministic_uids(None)


def uid(key: str | None = None):
    global _deterministic_uid_counter
    if _deterministic_uid_seed:
        if key is None:
            _deterministic_uid_counter += 1
            key = f"seq:{_deterministic_uid_counter}"
        token = f"schematic_engine:{_deterministic_uid_seed}:{key}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, token))
    return str(uuid.uuid4())


def sexpr_safe(s: str) -> str:
    """Sanitize a string for use inside KiCad S-expression quoted fields.

    KiCad quoted strings use double quotes. Internal double quotes, backslashes,
    parentheses, and newlines would break the S-expression parser.
    """
    if not s:
        return s
    return (
        s.replace("\\", "\\\\")
        .replace('"', "'")
        .replace("(", "[")
        .replace(")", "]")
        .replace("\n", " ")
        .replace("\r", "")
    )


# ================================================================
# S-expression builders
# ================================================================
def sexpr_header(
    title,
    subtitle="",
    paper="A3",
    date=None,
    company="",
    rev="1.0",
    project="",
    comment2=None,
    comment3=None,
    uuid_str=None,
):
    """Generate KiCad schematic file header."""
    if date is None:
        import datetime

        date = datetime.date.today().isoformat()
    if comment2 is None:
        comment2 = subtitle
    if comment3 is None:
        comment3 = "AUTO-GENERATED"
    if uuid_str is None:
        uuid_str = uid()
    title, subtitle, company, project, comment2, comment3 = (
        sexpr_safe(s) for s in (title, subtitle, company, project, comment2, comment3)
    )
    return f'''(kicad_sch (version 20231120) (generator "schematic_engine")

  (uuid "{uuid_str}")

  (paper "{paper}")

  (title_block
    (title "{title}")
    (date "{date}")
    (rev "{rev}")
    (company "{company}")
    (comment 1 "{project}")
    (comment 2 "{comment2}")
    (comment 3 "{comment3}")
  )
'''


def sexpr_global_label(x, y, name, angle=0, shape="bidirectional"):
    """Generate a global label at (x,y) with given name, grid-snapped."""
    x, y = snap(x), snap(y)
    name = sexpr_safe(name)
    return f'''  (global_label "{name}" (shape {shape}) (at {x:.2f} {y:.2f} {angle})
    (effects (font (size 1.0 1.0)))
    (uuid "{uid()}")
    (property "Intersheetref" "${{INTERSHEET_REFS}}" (at 0 0 0)
      (effects (font (size 1.0 1.0)) hide)
    )
  )
'''


def sexpr_hierarchical_label(x, y, name, angle=0, shape="bidirectional"):
    """Generate a hierarchical label at (x,y) with given name, grid-snapped.

    Hierarchical labels connect a sub-sheet to the parent sheet's sheet symbol
    pins.  In KiCad, each hierarchical label on a sub-sheet must have a
    matching ``(pin ...)`` entry inside the parent's ``(sheet ...)`` block.
    """
    x, y = snap(x), snap(y)
    name = sexpr_safe(name)
    return f'''  (hierarchical_label "{name}" (shape {shape}) (at {x:.2f} {y:.2f} {angle})
    (effects (font (size 1.0 1.0)))
    (uuid "{uid()}")
  )
'''


def sexpr_sheet_pin(name, shape="bidirectional", side="R", x=0, y=0):
    """Generate a ``(pin ...)`` element for a ``(sheet ...)`` block on the parent sheet.

    ``side`` controls the pin direction displayed on the sheet symbol:
        'R' — pin on the right edge  (angle 0)
        'L' — pin on the left edge   (angle 180)
        'T' — pin on the top edge    (angle 90)
        'B' — pin on the bottom edge (angle 270)

    ``x`` and ``y`` are the absolute schematic coordinates; the caller is
    expected to position pins at the correct edge of the sheet rectangle.
    """
    angle_map = {"R": 0, "L": 180, "T": 90, "B": 270}
    angle = angle_map.get(side, 0)
    x, y = snap(x), snap(y)
    name = sexpr_safe(name)
    return f'''    (pin "{name}" {shape} (at {x:.2f} {y:.2f} {angle})
      (effects (font (size 1.0 1.0)))
      (uuid "{uid()}")
    )'''


def sexpr_wire(x1, y1, x2, y2):
    """Generate a wire segment between two grid-snapped points."""
    x1, y1 = snap(x1), snap(y1)
    x2, y2 = snap(x2), snap(y2)
    return f'''  (wire (pts (xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f}))
    (stroke (width 0) (type default))
    (uuid "{uid()}")
  )
'''


def sexpr_no_connect(x, y):
    x, y = snap(x), snap(y)
    return f'  (no_connect (at {x:.2f} {y:.2f}) (uuid "{uid()}"))\n'


def sexpr_junction(x, y):
    x, y = snap(x), snap(y)
    return f'  (junction (at {x:.2f} {y:.2f}) (diameter 0) (color 0 0 0 0)\n    (uuid "{uid()}")\n  )\n'


# ================================================================
# Wiring
# ================================================================
def connect_pin_to_label(cx, cy, pin_angle, net_name, wires, labels, shape="bidirectional", wire_len=7.62):
    """Add a wire stub from a pin endpoint and a global label at the wire's end."""
    shape = shape or "bidirectional"
    if pin_angle == 0:
        wx, wy = cx - wire_len, cy
        label_angle = 0
    elif pin_angle == 180:
        wx, wy = cx + wire_len, cy
        label_angle = 180
    elif pin_angle == 270:
        wx, wy = cx, cy - wire_len
        label_angle = 90
    elif pin_angle == 90:
        wx, wy = cx, cy + wire_len
        label_angle = 270
    else:
        wx, wy = cx - wire_len, cy
        label_angle = 0
    wires.append(sexpr_wire(cx, cy, wx, wy))
    labels.append(sexpr_global_label(wx, wy, net_name, label_angle, shape))


def connect_pin_to_hierarchical_label(cx, cy, pin_angle, net_name, wires, labels, shape="bidirectional", wire_len=7.62):
    """Add a wire stub from a pin endpoint and a hierarchical label at the wire's end.

    Same geometry as ``connect_pin_to_label`` but emits a ``hierarchical_label``
    instead of a ``global_label``.
    """
    shape = shape or "bidirectional"
    if pin_angle == 0:
        wx, wy = cx - wire_len, cy
        label_angle = 0
    elif pin_angle == 180:
        wx, wy = cx + wire_len, cy
        label_angle = 180
    elif pin_angle == 270:
        wx, wy = cx, cy - wire_len
        label_angle = 90
    elif pin_angle == 90:
        wx, wy = cx, cy + wire_len
        label_angle = 270
    else:
        wx, wy = cx - wire_len, cy
        label_angle = 0
    wires.append(sexpr_wire(cx, cy, wx, wy))
    labels.append(sexpr_hierarchical_label(wx, wy, net_name, label_angle, shape))


# ================================================================
# Power symbols
# ================================================================
def sexpr_power_lib_entry(net_name: str) -> str:
    """Generate a lib_symbols entry for a power symbol.

    GND: three-bar ladder (symbol_name "GND", pin at angle 270 pointing up)
    VCC/VDD/other power nets: upward chevron (pin at angle 90 pointing down)
    """
    net_name = sexpr_safe(net_name)

    if net_name == "GND":
        # GND: three horizontal bars stacked below the pin
        return f'''  (symbol "GND" (power) (pin_names (offset 0)) (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#PWR" (at 0 -6.35 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Value" "{net_name}" (at 0 -3.81 0)
      (effects (font (size 1.27 1.27)))
    )
    (symbol "GND_0_1"
      (polyline (pts (xy 0 0) (xy 0 -1.27)) (stroke (width 0)) (fill (type none)))
      (polyline (pts (xy -1.27 -1.27) (xy 1.27 -1.27)) (stroke (width 0.254)) (fill (type none)))
      (polyline (pts (xy -0.762 -1.778) (xy 0.762 -1.778)) (stroke (width 0.254)) (fill (type none)))
      (polyline (pts (xy -0.254 -2.286) (xy 0.254 -2.286)) (stroke (width 0.254)) (fill (type none)))
    )
    (symbol "GND_1_1"
      (pin power_in line (at 0 0 270) (length 0) hide) )
  )
'''
    else:
        # VCC/VDD/other power: upward chevron
        return f'''  (symbol "{net_name}" (power) (pin_names (offset 0)) (exclude_from_sim no)
    (in_bom yes) (on_board yes)
    (property "Reference" "#PWR" (at 0 3.81 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Value" "{net_name}" (at 0 2.54 0)
      (effects (font (size 1.27 1.27)))
    )
    (symbol "{net_name}_0_1"
      (polyline (pts (xy 0 0) (xy 0 1.27)) (stroke (width 0)) (fill (type none)))
      (polyline (pts (xy -1.27 1.27) (xy 0 2.54) (xy 1.27 1.27)) (stroke (width 0.254)) (fill (type none)))
    )
    (symbol "{net_name}_1_1"
      (pin power_in line (at 0 0 90) (length 0) hide) )
  )
'''


def sexpr_power_instance(
    net_name: str,
    x: float,
    y: float,
    pin_angle: int,
    project_name: str = "project",
    root_uuid: str = "",
    sheet_uuid: str = "",
) -> str:
    """Generate a symbol instance for a power symbol at (x, y).

    pin_angle controls the instance rotation:
        0 (wire left):  GND rotation 90, VCC rotation 270
        90 (wire down): GND rotation 0,  VCC rotation 180
        180 (wire right): GND rotation 270, VCC rotation 90
        270 (wire up):   GND rotation 180, VCC rotation 0
    """
    net_name = sexpr_safe(net_name)
    x, y = snap(x), snap(y)
    is_gnd = net_name == "GND" or net_name.startswith(("GNDA", "DGND", "AGND"))

    # Rotation maps: pin_angle → symbol rotation
    rotation_map = {
        (0, True): 90,  # pin left, GND
        (0, False): 270,  # pin left, VCC
        (90, True): 0,  # pin down, GND
        (90, False): 180,  # pin down, VCC
        (180, True): 270,  # pin right, GND
        (180, False): 90,  # pin right, VCC
        (270, True): 180,  # pin up, GND
        (270, False): 0,  # pin up, VCC
    }
    rotation = rotation_map.get((pin_angle, is_gnd), 0)
    ref = "#PWR0" if is_gnd else "#PWR0"
    ref_local = (0.0, -6.35) if is_gnd else (0.0, 3.81)
    value_local = (0.0, -3.81) if is_gnd else (0.0, 2.54)
    ref_dx, ref_dy = _rotate_point(*ref_local, rotation)
    value_dx, value_dy = _rotate_point(*value_local, rotation)
    ref_x = snap(x + ref_dx)
    ref_y = snap(y + ref_dy)
    value_x = snap(x + value_dx)
    value_y = snap(y + value_dy)

    inst_uuid = uid(f"power:{net_name}:{x:.2f}:{y:.2f}")
    inst_path = f"/{root_uuid}/{sheet_uuid}" if root_uuid and sheet_uuid else "/"

    return f'''  (symbol (lib_id "{net_name}") (at {x:.2f} {y:.2f} {rotation})
    (uuid "{inst_uuid}")
    (property "Reference" "{ref}" (at {ref_x:.2f} {ref_y:.2f} 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Value" "{net_name}" (at {value_x:.2f} {value_y:.2f} 0)
      (effects (font (size 1.27 1.27)))
    )
    (instances
      (project "{project_name}"
        (path "{inst_path}"
          (reference "{ref}")
          (unit 1)
        )
      )
    )
  )
'''


def sexpr_pwr_flag_lib_entry() -> str:
    """Generate the lib_symbols entry for KiCad's PWR_FLAG symbol."""
    return """  (symbol "PWR_FLAG" (power) (pin_names (offset 0)) (exclude_from_sim no)
    (in_bom yes) (on_board yes)
    (property "Reference" "#FLG" (at 0 1.905 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Value" "PWR_FLAG" (at 0 3.81 0)
      (effects (font (size 1.27 1.27)))
    )
    (symbol "PWR_FLAG_0_0"
      (pin power_out line (at 0 0 90) (length 0)
        (name "pwr" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27))))
      )
    )
  )
"""


def sexpr_pwr_flag_instance(
    x: float,
    y: float,
    project_name: str = "project",
    root_uuid: str = "",
    sheet_uuid: str = "",
) -> str:
    """Place a PWR_FLAG instance at (x, y) to satisfy KiCad ERC."""
    x, y = snap(x), snap(y)
    inst_uuid = uid(f"pwr_flag:{x:.2f}:{y:.2f}")
    inst_path = f"/{root_uuid}/{sheet_uuid}" if root_uuid and sheet_uuid else "/"

    return f'''  (symbol (lib_id "PWR_FLAG") (at {x:.2f} {y:.2f} 0)
    (uuid "{inst_uuid}")
    (property "Reference" "#FLG" (at {x:.2f} {y + 1.905:.2f} 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Value" "PWR_FLAG" (at {x:.2f} {y + 3.81:.2f} 0)
      (effects (font (size 1.27 1.27)))
    )
    (instances
      (project "{project_name}"
        (path "{inst_path}"
          (reference "#FLG")
          (unit 1)
        )
      )
    )
  )
'''


# ================================================================
# Bus notation (Phase 3)
# ================================================================
def sexpr_bus(x1, y1, x2, y2):
    """Generate a bus segment (thicker wire) between two grid-snapped points."""
    x1, y1 = snap(x1), snap(y1)
    x2, y2 = snap(x2), snap(y2)
    return f'''  (bus (pts (xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f}))
    (stroke (width 0) (type default))
    (uuid "{uid()}")
  )
'''


def sexpr_bus_entry(x, y, angle=0):
    """Generate a 45° bus entry stub at (x, y) connecting to a bus.

    KiCad 10 stores bus entries as a start point plus a signed ``size`` vector,
    not as an inline angle inside ``(at ...)``.  We keep ``angle`` here as an
    internal preset selector so the existing generator code can continue to ask
    for left/right/top/bottom-oriented entries.

    angle presets in schematic coordinates:
      0   -> upper-right
      90  -> lower-right
      180 -> lower-left
      270 -> upper-left
    """
    x, y = snap(x), snap(y)
    step = 2.54
    dx, dy = {
        0: (step, -step),
        90: (step, step),
        180: (-step, step),
        270: (-step, -step),
    }.get(angle % 360, (step, -step))
    return f'''  (bus_entry
    (at {x:.2f} {y:.2f})
    (size {dx:.2f} {dy:.2f})
    (stroke (width 0) (type default))
    (uuid "{uid()}")
  )
'''


def sexpr_bus_label(x, y, name, angle=0):
    """Generate a vector bus label (e.g., DDR_DQ[0..15]) at (x, y)."""
    x, y = snap(x), snap(y)
    safe_name = sexpr_safe(name)
    return f'''  (label "{safe_name}" (at {x:.2f} {y:.2f} {angle})
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "{uid()}")
  )
'''


def connect_points(x1, y1, x2, y2, wires, route="h_first"):
    """Manhattan L-route between two grid-snapped points."""
    x1, y1, x2, y2 = snap(x1), snap(y1), snap(x2), snap(y2)
    if abs(x1 - x2) < 0.01 and abs(y1 - y2) < 0.01:
        return
    if abs(x1 - x2) < 0.01 or abs(y1 - y2) < 0.01:
        wires.append(sexpr_wire(x1, y1, x2, y2))
    elif route == "h_first":
        wires.append(sexpr_wire(x1, y1, x2, y1))
        wires.append(sexpr_wire(x2, y1, x2, y2))
    else:
        wires.append(sexpr_wire(x1, y1, x1, y2))
        wires.append(sexpr_wire(x1, y2, x2, y2))


@dataclass
class Anchor:
    """A named local node in a subcircuit — junction point for wires."""

    name: str
    x: float
    y: float

    def __post_init__(self):
        self.x = snap(self.x)
        self.y = snap(self.y)

    @property
    def xy(self):
        return (self.x, self.y)


# ================================================================
# Passive pin offsets
# ================================================================
def passive_pin_xy(x, y, angle=0, pin_span=3.81):
    """Return absolute (pin1_xy, pin2_xy) for a passive placed at (x, y, angle)."""
    pin_span = snap(pin_span)
    passive_pin_offsets = {
        0: ((-pin_span, 0.00), (pin_span, 0.00)),
        90: ((0.00, -pin_span), (0.00, pin_span)),
        180: ((pin_span, 0.00), (-pin_span, 0.00)),
        270: ((0.00, pin_span), (0.00, -pin_span)),
    }
    if angle not in passive_pin_offsets:
        raise ValueError(f"Unsupported passive angle: {angle}. Use 0/90/180/270.")
    (d1x, d1y), (d2x, d2y) = passive_pin_offsets[angle]
    return (snap(x + d1x), snap(y + d1y)), (snap(x + d2x), snap(y + d2y))


def passive_pin_angles(angle=0):
    """Return schematic breakout angles for passive pin1 and pin2 at ``angle``."""
    passive_angles = {
        0: (0, 180),
        90: (270, 90),
        180: (180, 0),
        270: (90, 270),
    }
    if angle not in passive_angles:
        raise ValueError(f"Unsupported passive angle: {angle}. Use 0/90/180/270.")
    return passive_angles[angle]


# ================================================================
# Symbol creation
# ================================================================
_CHAR_WIDTH_RATIO = 0.65
_MULTI_COLUMN_PIN_THRESHOLD = 100
_TARGET_PINS_PER_COLUMN = 40
_MAX_PIN_COLUMNS = 6
_COLUMN_GAP = 10.16


def _snap_box_dimension(raw_dim: float) -> float:
    grids = math.ceil(raw_dim / GRID)
    if grids % 2 != 0:
        grids += 1
    return grids * GRID


def _split_evenly(items, segments):
    if segments <= 1 or len(items) <= 1:
        return [items]
    size = len(items) / segments
    chunks = []
    start = 0.0
    for idx in range(segments):
        end = round((idx + 1) * size)
        chunks.append(items[round(start) : end])
        start = end
    while len(chunks) < segments:
        chunks.append([])
    return chunks


def _auto_pin_columns(left_pins, right_pins, top_pins, bottom_pins, column_segments):
    if column_segments is not None:
        return max(1, min(_MAX_PIN_COLUMNS, int(column_segments)))
    if top_pins or bottom_pins:
        return 1
    max_side = max(len(left_pins), len(right_pins))
    if max_side <= _MULTI_COLUMN_PIN_THRESHOLD:
        return 1
    target = _TARGET_PINS_PER_COLUMN
    if max_side >= 220:
        target = 32
    elif max_side >= 160:
        target = 36
    return min(_MAX_PIN_COLUMNS, max(2, math.ceil(max_side / target)))


def text_width_mm(text, font_size=1.0):
    return len(text) * font_size * _CHAR_WIDTH_RATIO


def _adaptive_pin_pitch(max_side: int, pin_columns: int) -> float:
    """Increase left/right pin pitch on dense multi-column symbols."""
    if pin_columns <= 1:
        return 2.54
    if max_side >= 56:
        return 5.08
    if max_side >= 20:
        return 3.81
    return 2.54


def _adaptive_column_gap(max_name_width: float, pin_columns: int) -> float:
    if pin_columns <= 1:
        return _COLUMN_GAP
    base_gap = 10.16 + max_name_width * 0.90
    if pin_columns >= 4:
        base_gap += 38.10
        return snap(max(60.96, min(76.20, base_gap)))
    if pin_columns == 3:
        base_gap += 15.24
        return snap(max(45.72, min(60.96, base_gap)))
    return snap(max(_COLUMN_GAP, min(30.48, base_gap)))


def create_generic_symbol(
    name,
    pins,
    ref_prefix="U",
    column_segments=None,
    pin_pitch_override=None,
    pin_sort_fn=None,
):
    """Create a rectangular box symbol with given pins.

    pins: list of (pin_number, pin_name, pin_type, side)
        side: 'L' (left), 'R' (right), 'T' (top), 'B' (bottom)
        pin_type: 'input', 'output', 'bidirectional', 'passive', 'power_in', 'power_out'

    pin_sort_fn: optional function(pin_tuple) -> sortable key
        Used to group and order pins so related signals are adjacent. The callback
        receives the full pin tuple ``(number, name, type, side)`` so callers can
        sort by resolved board-net context instead of only the package pin name.
        For backward compatibility, callbacks that still accept only ``pin_name``
        are also supported.
    """
    left_pins = [p for p in pins if p[3] == "L"]
    right_pins = [p for p in pins if p[3] == "R"]
    top_pins = [p for p in pins if p[3] == "T"]
    bottom_pins = [p for p in pins if p[3] == "B"]

    # Apply pin sorting if a classifier function is provided
    if pin_sort_fn:

        def pin_sort_key(pin_tuple):
            try:
                return pin_sort_fn(pin_tuple)
            except TypeError:
                return pin_sort_fn(pin_tuple[1])

        left_pins = sorted(left_pins, key=pin_sort_key)
        right_pins = sorted(right_pins, key=pin_sort_key)
        top_pins = sorted(top_pins, key=pin_sort_key)
        bottom_pins = sorted(bottom_pins, key=pin_sort_key)

    pin_columns = _auto_pin_columns(left_pins, right_pins, top_pins, bottom_pins, column_segments)
    left_chunks = _split_evenly(left_pins, pin_columns)
    right_chunks = _split_evenly(right_pins, pin_columns)

    max_side = max(
        max((len(chunk) for chunk in left_chunks), default=0),
        max((len(chunk) for chunk in right_chunks), default=0),
    )
    pin_pitch = (
        float(pin_pitch_override) if pin_pitch_override is not None else _adaptive_pin_pitch(max_side, pin_columns)
    )
    box_h = _snap_box_dimension(max(max_side * pin_pitch + 7.62, 15.24))
    pin_len = 5.08

    max_left_name_w = max((text_width_mm(p[1], 1.0) for p in left_pins), default=0.0)
    max_right_name_w = max((text_width_mm(p[1], 1.0) for p in right_pins), default=0.0)
    max_name_w = max(max_left_name_w, max_right_name_w)
    segment_w = _snap_box_dimension(
        max(max_left_name_w + max_right_name_w + 2 * pin_len + 10.16, 35.56 if pin_columns > 1 else 25.40)
    )
    column_gap = _adaptive_column_gap(max_name_w, pin_columns)
    segmented_w = pin_columns * segment_w + max(0, pin_columns - 1) * column_gap
    tb_width = max(len(top_pins), len(bottom_pins)) * 2.54 + 5.08
    box_w = _snap_box_dimension(max(segmented_w, tb_width, 25.40))
    segment_origin = (box_w - segmented_w) / 2 if segmented_w < box_w else 0.0

    # Calculate vertical offset to ensure all geometry has Y >= 0
    # Minimum Y values: bottom pins at -box_h/2 - pin_len, left/right at box_h/2 - max_side*2.54
    min_bottom_pin_y = -box_h / 2 - pin_len
    min_lr_pin_y = box_h / 2 - max_side * 2.54
    min_property_y = -box_h / 2 - 2.54  # Value property
    min_y = min(min_bottom_pin_y, min_lr_pin_y, min_property_y)
    y_offset = -min_y if min_y < 0 else 0
    lines = []
    lines.append(f'    (symbol "{name}" (pin_names (offset 2.54)) (in_bom yes) (on_board yes)')

    val_font = 1.0 if len(name) > 10 else 1.27
    if pin_columns > 1 and len(name) > 16:
        val_font = 0.9
    lines.append(f'      (property "Reference" "{ref_prefix}" (at {box_w / 2:.2f} {box_h / 2 + 2.54 + y_offset:.2f} 0)')
    lines.append("        (effects (font (size 1.27 1.27)))")
    lines.append("      )")
    lines.append(f'      (property "Value" "{name}" (at {box_w / 2:.2f} {-box_h / 2 - 2.54 + y_offset:.2f} 0)')
    lines.append(f"        (effects (font (size {val_font:.2f} {val_font:.2f})))")
    lines.append("      )")
    lines.append('      (property "Footprint" "" (at 0 0 0)')
    lines.append("        (effects (font (size 1.27 1.27)) hide)")
    lines.append("      )")
    lines.append('      (property "Datasheet" "" (at 0 0 0)')
    lines.append("        (effects (font (size 1.27 1.27)) hide)")
    lines.append("      )")

    lines.append(f'      (symbol "{name}_0_1"')
    for idx in range(pin_columns):
        x0 = segment_origin + idx * (segment_w + column_gap)
        x1 = x0 + segment_w
        lines.append(
            f"        (rectangle (start {x0:.2f} {box_h / 2 + y_offset:.2f}) (end {x1:.2f} {-box_h / 2 + y_offset:.2f})"
        )
        lines.append("          (stroke (width 0.254) (type default))")
        lines.append("          (fill (type background))")
        lines.append("        )")
    lines.append("      )")

    lines.append(f'      (symbol "{name}_1_1"')

    for idx, chunk in enumerate(left_chunks):
        x0 = segment_origin + idx * (segment_w + column_gap)
        for i, (num, pname, ptype, _) in enumerate(chunk):
            y = box_h / 2 - pin_pitch - i * pin_pitch + y_offset
            lines.append(f"        (pin {ptype} line (at {x0 - pin_len:.2f} {y:.2f} 0) (length {pin_len:.2f})")
            lines.append(f'          (name "{pname}" (effects (font (size 1.0 1.0))))')
            lines.append(f'          (number "{num}" (effects (font (size 1.0 1.0))))')
            lines.append("        )")

    for idx, chunk in enumerate(right_chunks):
        x1 = segment_origin + idx * (segment_w + column_gap) + segment_w
        for i, (num, pname, ptype, _) in enumerate(chunk):
            y = box_h / 2 - pin_pitch - i * pin_pitch + y_offset
            lines.append(f"        (pin {ptype} line (at {x1 + pin_len:.2f} {y:.2f} 180) (length {pin_len:.2f})")
            lines.append(f'          (name "{pname}" (effects (font (size 1.0 1.0))))')
            lines.append(f'          (number "{num}" (effects (font (size 1.0 1.0))))')
            lines.append("        )")

    for i, (num, pname, ptype, _) in enumerate(top_pins):
        x = 2.54 + i * 2.54
        lines.append(
            f"        (pin {ptype} line (at {x:.2f} {box_h / 2 + pin_len + y_offset:.2f} 270) (length {pin_len:.2f})"
        )
        lines.append(f'          (name "{pname}" (effects (font (size 1.0 1.0))))')
        lines.append(f'          (number "{num}" (effects (font (size 1.0 1.0))))')
        lines.append("        )")

    for i, (num, pname, ptype, _) in enumerate(bottom_pins):
        x = 2.54 + i * 2.54
        lines.append(
            f"        (pin {ptype} line (at {x:.2f} {-box_h / 2 - pin_len + y_offset:.2f} 90) (length {pin_len:.2f})"
        )
        lines.append(f'          (name "{pname}" (effects (font (size 1.0 1.0))))')
        lines.append(f'          (number "{num}" (effects (font (size 1.0 1.0))))')
        lines.append("        )")

    lines.append("      )")
    lines.append("    )")

    return "\n".join(lines)


def passive_lib_symbol(sym_name, ref_prefix):
    """Create a compact 2-pin passive symbol (capacitor, resistor, or inductor)."""
    is_review = sym_name.endswith("_Review")
    body_half_w = 1.91 if is_review else 1.27
    body_half_h = 1.91 if is_review else 1.27
    pin_at = 5.08 if is_review else 3.81
    pin_len = 3.81 if is_review else 2.54
    return f'''    (symbol "{sym_name}" (pin_names hide) (in_bom yes) (on_board yes)
      (property "Reference" "{ref_prefix}" (at 0 2.54 0)
        (effects (font (size 1.0 1.0)))
      )
      (property "Value" "{sym_name}" (at 0 -2.54 0)
        (effects (font (size 1.0 1.0)))
      )
      (property "Footprint" "" (at 0 0 0)
        (effects (font (size 1.27 1.27)) hide)
      )
      (property "Datasheet" "" (at 0 0 0)
        (effects (font (size 1.27 1.27)) hide)
      )
      (symbol "{sym_name}_0_1"
        (rectangle (start -{body_half_w:.2f} {body_half_h:.2f}) (end {body_half_w:.2f} -{body_half_h:.2f})
          (stroke (width 0.254) (type default))
          (fill (type background))
        )
      )
      (symbol "{sym_name}_1_1"
        (pin passive line (at -{pin_at:.2f} 0 0) (length {pin_len:.2f})
          (name "~" (effects (font (size 1.0 1.0))))
          (number "1" (effects (font (size 1.0 1.0))))
        )
        (pin passive line (at {pin_at:.2f} 0 180) (length {pin_len:.2f})
          (name "~" (effects (font (size 1.0 1.0))))
          (number "2" (effects (font (size 1.0 1.0))))
        )
      )
    )
'''


def get_pin_positions(symbol_sexpr, symbol_name):
    """Parse pin positions from a symbol S-expression.
    Returns dict: pin_number -> (x, y, angle, length, name, type)
    """
    pins = re.findall(
        r"\(pin\s+(\w+)\s+\w+\s+\(at\s+([\d.-]+)\s+([\d.-]+)\s+(\d+)\)\s+\(length\s+([\d.]+)\)"
        r'\s*\(name\s+"([^"]*)"\s.*?\)\s*\(number\s+"([^"]*)"\s',
        symbol_sexpr,
        re.DOTALL,
    )
    result = {}
    for ptype, x, y, angle, length, name, num in pins:
        result[num] = (float(x), float(y), int(angle), float(length), name, ptype)
    return result


def pin_connection_point(sym_x, sym_y, pin_x, pin_y, pin_angle, pin_length):
    """Calculate the schematic coordinate where a wire connects to a pin."""
    return (snap(sym_x) + pin_x, snap(sym_y) - pin_y)


# ================================================================
# Footprint qualification — bare names → Library:Name
# ================================================================
_FOOTPRINT_LIBRARY_MAP = {
    "SOT-23": "Package_TO_SOT_SMD",
    "SOT-23-3": "Package_TO_SOT_SMD",
    "SOT-23-5": "Package_TO_SOT_SMD",
    "SOT-23-6": "Package_TO_SOT_SMD",
    "SOT-223": "Package_TO_SOT_SMD",
    "SOT-223-3": "Package_TO_SOT_SMD",
    "SOT-89-3": "Package_TO_SOT_SMD",
    "TSOT-23-5": "Package_TO_SOT_SMD",
    "TSOT-23-6": "Package_TO_SOT_SMD",
    "TSOT-23-8": "Package_TO_SOT_SMD",
}

# Pattern-based rules for footprints not in the explicit map
_FOOTPRINT_LIBRARY_PATTERNS = [
    (r"^SOT-\d", "Package_TO_SOT_SMD"),
    (r"^TSOT-", "Package_TO_SOT_SMD"),
    (r"^TO-\d", "Package_TO_SOT_SMD"),
    (r"^SOIC-", "Package_SO"),
    (r"^SOP-", "Package_SO"),
    (r"^MSOP-", "Package_SO"),
    (r"^TSSOP-", "Package_SO"),
    (r"^SSOP-", "Package_SO"),
    (r"^QFN-", "Package_DFN_QFN"),
    (r"^DFN-", "Package_DFN_QFN"),
    (r"^QFP-", "Package_QFP"),
    (r"^LQFP-", "Package_QFP"),
    (r"^TQFP-", "Package_QFP"),
    (r"^BGA-", "Package_BGA"),
    (r"^LFCSP-", "Package_CSP"),
]


def qualify_footprint(footprint: str) -> str:
    """Ensure a footprint string has a library prefix (e.g., 'Package_TO_SOT_SMD:SOT-23-6').

    Returns the input unchanged if it already contains ':' or is empty.
    """
    if not footprint or ":" in footprint:
        return footprint
    lib = _FOOTPRINT_LIBRARY_MAP.get(footprint)
    if lib:
        return f"{lib}:{footprint}"
    for pattern, lib_name in _FOOTPRINT_LIBRARY_PATTERNS:
        if re.match(pattern, footprint):
            return f"{lib_name}:{footprint}"
    return footprint


# ================================================================
# Component placement
# ================================================================
def place_component(
    lib_id,
    ref,
    value,
    footprint,
    x,
    y,
    unit=1,
    angle=0,
    dnp=False,
    project_name="project",
    root_uuid="",
    sheet_uuid="",
    property_center_x=None,
    in_bom=True,
    on_board=True,
):
    """Generate S-expression for a placed component instance."""
    x, y = snap(x), snap(y)
    footprint = qualify_footprint(footprint)
    property_center_x = snap(property_center_x) if property_center_x is not None else None
    value = sexpr_safe(value)
    ref = sexpr_safe(ref)
    inst_uuid = uid()
    inst_path = f"/{root_uuid}/{sheet_uuid}" if root_uuid and sheet_uuid else "/"

    props = []
    passive_variant = None
    if lib_id.endswith("_Small"):
        passive_variant = "small"
    elif lib_id.endswith("_Review"):
        passive_variant = "review"
    if angle in (90, 270):
        if passive_variant == "review":
            ref_dx, ref_dy = 7.62, -2.54
            val_dx, val_dy = 7.62, 2.54
        elif passive_variant == "small":
            ref_dx, ref_dy = 6.35, -1.91
            val_dx, val_dy = 6.35, 1.91
        else:
            ref_dx, ref_dy = 5.08, -1.27
            val_dx, val_dy = 5.08, 1.27
        ref_x = x + ref_dx
        val_x = x + val_dx
        if passive_variant is not None:
            ref_x = snap(ref_x - (text_width_mm(ref, 1.27) / 2.0))
            val_x = snap(val_x - (text_width_mm(value, 1.0) / 2.0))
        props.append(
            f'    (property "Reference" "{ref}" (at {ref_x:.2f} {y + ref_dy:.2f} 0)\n'
            f"      (effects (font (size 1.27 1.27)))\n    )"
        )
        props.append(
            f'    (property "Value" "{value}" (at {val_x:.2f} {y + val_dy:.2f} 0)\n'
            f"      (effects (font (size 1.0 1.0)))\n    )"
        )
    else:
        if passive_variant == "review":
            ref_dy, val_dy = -5.08, -10.16
        elif passive_variant == "small":
            ref_dy, val_dy = -3.81, -7.62
        else:
            ref_dy, val_dy = -2.54, -5.08
        ref_x = property_center_x if property_center_x is not None else x
        val_x = property_center_x if property_center_x is not None else x
        if passive_variant is not None or property_center_x is not None:
            ref_x = snap(ref_x - (text_width_mm(ref, 1.27) / 2.0))
            val_x = snap(val_x - (text_width_mm(value, 1.0) / 2.0))
        props.append(
            f'    (property "Reference" "{ref}" (at {ref_x:.2f} {y + ref_dy:.2f} 0)\n'
            f"      (effects (font (size 1.27 1.27)))\n    )"
        )
        props.append(
            f'    (property "Value" "{value}" (at {val_x:.2f} {y + val_dy:.2f} 0)\n'
            f"      (effects (font (size 1.0 1.0)))\n    )"
        )
    props.append(
        f'    (property "Footprint" "{footprint}" (at {x:.2f} {y:.2f} 0)\n'
        f"      (effects (font (size 1.27 1.27)) hide)\n    )"
    )

    dnp_str = "yes" if dnp else "no"
    bom_str = "yes" if in_bom else "no"
    board_str = "yes" if on_board else "no"
    return f"""  (symbol (lib_id "{lib_id}") (at {x:.2f} {y:.2f} {angle}) (unit {unit})
    (in_bom {bom_str}) (on_board {board_str}) (dnp {dnp_str})
    (uuid "{inst_uuid}")
{chr(10).join(props)}
    (instances
      (project "{project_name}"
        (path "{inst_path}"
          (reference "{ref}")
          (unit {unit})
        )
      )
    )
  )
"""


def place_passive(
    ref,
    value,
    footprint,
    x,
    y,
    net1,
    net2,
    wires,
    labels,
    instances,
    sym_type="C",
    wire_len=1.27,
    project_name="project",
    root_uuid="",
    sheet_uuid="",
):
    """Place a 2-pin passive component and connect both pins via global labels."""
    sym_name = {"C": "C_Small", "R": "R_Small", "L": "L_Small"}[sym_type]
    instances.append(
        place_component(
            sym_name,
            ref,
            value,
            footprint,
            x,
            y,
            project_name=project_name,
            root_uuid=root_uuid,
            sheet_uuid=sheet_uuid,
        )
    )
    cx1 = snap(x) - 3.81
    cy1 = snap(y)
    connect_pin_to_label(cx1, cy1, 0, net1, wires, labels, wire_len=wire_len)
    cx2 = snap(x) + 3.81
    cy2 = snap(y)
    connect_pin_to_label(cx2, cy2, 180, net2, wires, labels, wire_len=wire_len)


# ================================================================
# Sheet assembly
# ================================================================

# Label collision avoidance constants
_COLLISION_CLEARANCE_MM = 3.0  # minimum separation between label bounding boxes
_COLLISION_SHIFT_MM = 2.54  # increment to extend wire stub when avoiding collision
_COLLISION_MAX_SHIFTS = 5  # maximum shifts per label before giving up

# Regex patterns for label and wire parsing
_LABEL_RE = re.compile(
    r'\((global_label|hierarchical_label|label)\s+"([^"]+)"\s+\(shape\s+\w+\)\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)'
)
_WIRE_RE = re.compile(
    r'\(wire\s+\(pts\s+\(xy\s+([-\d.]+)\s+([-\d.]+)\)\s+\(xy\s+([-\d.]+)\s+([-\d.]+)\)\)'
)
_JUNCTION_RE = re.compile(r"\(junction\s+\(at\s+([-\d.]+)\s+([-\d.]+)\)")
_NO_CONNECT_RE = re.compile(r"\(no_connect\s+\(at\s+([-\d.]+)\s+([-\d.]+)\)")


def _boxes_overlap(
    ax1: float, ay1: float, ax2: float, ay2: float,
    bx1: float, by1: float, bx2: float, by2: float,
) -> bool:
    """Return True if axis-aligned bounding boxes overlap."""
    return not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)


def _same_axis(angle_a: int, angle_b: int) -> bool:
    """Return True if two label angles share the same orientation axis."""
    horiz = {0, 180}
    vert = {90, 270}
    a = angle_a % 360
    b = angle_b % 360
    return (a in horiz and b in horiz) or (a in vert and b in vert)


def _shift_direction(angle: int) -> tuple[float, float]:
    """Return (dx, dy) unit vector for extending a label away from its pin."""
    a = angle % 360
    if a == 0:
        return (-1.0, 0.0)  # label is left of pin, shift further left
    elif a == 180:
        return (1.0, 0.0)  # label is right of pin, shift further right
    elif a == 90:
        return (0.0, -1.0)  # label is below pin, shift further down
    elif a == 270:
        return (0.0, 1.0)  # label is above pin, shift further up
    return (0.0, 0.0)


def _orthogonal_segment_hits_box(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    box: tuple[float, float, float, float],
) -> bool:
    """Return whether an orthogonal wire segment enters a body-box interior."""
    left, top, right, bottom = box
    epsilon = 0.01
    if abs(x1 - x2) < epsilon:
        if not (left + epsilon < x1 < right - epsilon):
            return False
        seg_top, seg_bottom = sorted((y1, y2))
        return max(seg_top, top + epsilon) < min(seg_bottom, bottom - epsilon)
    if abs(y1 - y2) < epsilon:
        if not (top + epsilon < y1 < bottom - epsilon):
            return False
        seg_left, seg_right = sorted((x1, x2))
        return max(seg_left, left + epsilon) < min(seg_right, right - epsilon)
    return False


def _resolve_label_collisions(
    labels: list[str],
    wires: list[str],
    obstacle_boxes: list[tuple[float, float, float, float]] | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve label overlaps while preserving every label-to-wire endpoint.

    A label and its associated wire are moved as one atomic operation.  The
    fixed-point pass restarts after every move so later moves cannot silently
    recreate an overlap that was already checked.
    """
    if len(labels) < 2:
        return labels, wires

    parsed_labels: list[dict] = []
    for i, lab in enumerate(labels):
        match = _LABEL_RE.search(lab)
        if not match:
            continue
        text = match.group(2)
        x = float(match.group(3))
        y = float(match.group(4))
        angle = int(match.group(5))
        parsed_labels.append(
            {
                "idx": i,
                "kind": match.group(1),
                "text": text,
                "x": x,
                "y": y,
                "angle": angle,
                "bbox": _label_bbox(x, y, text, angle),
                "shifts": 0,
                "wire_idx": None,
            }
        )

    wire_by_idx: dict[int, dict] = {}
    endpoint_wires: dict[tuple[float, float], list[int]] = {}
    for i, wire in enumerate(wires):
        match = _WIRE_RE.search(wire)
        if not match:
            continue
        parsed = {
            "idx": i,
            "x1": float(match.group(1)),
            "y1": float(match.group(2)),
            "x2": float(match.group(3)),
            "y2": float(match.group(4)),
        }
        wire_by_idx[i] = parsed
        key = (round(parsed["x2"], 2), round(parsed["y2"], 2))
        endpoint_wires.setdefault(key, []).append(i)

    # Label collision resolution extends a wire together with its label. Keep
    # that extension from reaching a different electrical wire component: a
    # visually tidy label must never turn into a schematic short. Components
    # are classified from the existing geometry and label anchors before any
    # moves; safe extensions keep their original component intact.
    wire_parent = {index: index for index in wire_by_idx}

    def wire_find(index: int) -> int:
        while wire_parent[index] != index:
            wire_parent[index] = wire_parent[wire_parent[index]]
            index = wire_parent[index]
        return index

    def wire_union(first: int, second: int) -> None:
        first_root = wire_find(first)
        second_root = wire_find(second)
        if first_root != second_root:
            wire_parent[second_root] = first_root

    def point_on_wire_segment(
        point: tuple[float, float], segment: dict[str, float]
    ) -> bool:
        x, y = point
        epsilon = 0.01
        if abs(segment["x1"] - segment["x2"]) < epsilon:
            return (
                abs(x - segment["x1"]) < epsilon
                and min(segment["y1"], segment["y2"]) - epsilon
                <= y
                <= max(segment["y1"], segment["y2"]) + epsilon
            )
        if abs(segment["y1"] - segment["y2"]) < epsilon:
            return (
                abs(y - segment["y1"]) < epsilon
                and min(segment["x1"], segment["x2"]) - epsilon
                <= x
                <= max(segment["x1"], segment["x2"]) + epsilon
            )
        return False

    def wires_are_electrically_joined(first: dict[str, float], second: dict[str, float]) -> bool:
        return any(
            point_on_wire_segment(point, second)
            for point in ((first["x1"], first["y1"]), (first["x2"], first["y2"]))
        ) or any(
            point_on_wire_segment(point, first)
            for point in ((second["x1"], second["y1"]), (second["x2"], second["y2"]))
        )

    wire_indices = list(wire_by_idx)
    for position, first_index in enumerate(wire_indices):
        first = wire_by_idx[first_index]
        for second_index in wire_indices[position + 1 :]:
            if wires_are_electrically_joined(first, wire_by_idx[second_index]):
                wire_union(first_index, second_index)

    label_wire_components: list[tuple[int, str]] = []
    for label in parsed_labels:
        label_x, label_y = label["x"], label["y"]
        touching = [
            index
            for index, segment in wire_by_idx.items()
            if point_on_wire_segment((label_x, label_y), segment)
        ]
        for index in touching[1:]:
            wire_union(touching[0], index)
        if touching:
            label_wire_components.append((touching[0], label["text"]))

    wire_component_names: dict[int, set[str]] = {}
    for index, name in label_wire_components:
        wire_component_names.setdefault(wire_find(index), set()).add(name)

    def shifted_wire_is_clear(label: dict, new_x: float, new_y: float) -> bool:
        wire_idx = label["wire_idx"]
        if wire_idx is None:
            return False
        current = wire_by_idx[wire_idx]
        candidate = {
            "x1": current["x1"],
            "y1": current["y1"],
            "x2": new_x,
            "y2": new_y,
        }
        own_component = wire_find(wire_idx)
        for other_idx, other in wire_by_idx.items():
            if other_idx == wire_idx or wire_find(other_idx) == own_component:
                continue
            if wires_are_electrically_joined(candidate, other):
                other_names = wire_component_names.get(wire_find(other_idx), set())
                # Unlabeled wires are also foreign until proven otherwise.
                if other_names != {label["text"]}:
                    return False
        return True

    # Claim endpoint-matched wires once.  Multiple labels at one endpoint are
    # paired deterministically with multiple wires in their emission order.
    for label in parsed_labels:
        key = (round(label["x"], 2), round(label["y"], 2))
        candidates = endpoint_wires.get(key, [])
        if candidates:
            label["wire_idx"] = candidates.pop(0)

    max_moves = len(parsed_labels) * _COLLISION_MAX_SHIFTS
    for _ in range(max_moves):
        moved = False
        for i, left in enumerate(parsed_labels):
            for right in parsed_labels[i + 1 :]:
                if left["text"] == right["text"]:
                    continue
                if not _same_axis(left["angle"], right["angle"]):
                    continue
                if not _boxes_overlap(*left["bbox"], *right["bbox"]):
                    continue

                movable = []
                for label in (left, right):
                    if (
                        label["wire_idx"] is None
                        or label["shifts"] >= _COLLISION_MAX_SHIFTS
                    ):
                        continue
                    dx, dy = _shift_direction(label["angle"])
                    new_x = label["x"] + dx * _COLLISION_SHIFT_MM
                    new_y = label["y"] + dy * _COLLISION_SHIFT_MM
                    parsed_wire = wire_by_idx[label["wire_idx"]]
                    if obstacle_boxes and any(
                        _orthogonal_segment_hits_box(
                            parsed_wire["x1"],
                            parsed_wire["y1"],
                            new_x,
                            new_y,
                            box,
                        )
                        for box in obstacle_boxes
                    ):
                        continue
                    if not shifted_wire_is_clear(label, new_x, new_y):
                        continue
                    movable.append(label)
                if not movable:
                    continue

                # Keep moving the earlier-emitted endpoint until this overlap
                # clears (or reaches its cap).  Alternating between the pair
                # moves them in parallel and can preserve the overlap; choosing
                # by projected distance can extend a later stub through nearby
                # symbol bodies even when the earlier stub has a safe escape.
                candidate = min(movable, key=lambda label: label["idx"])
                dx, dy = _shift_direction(candidate["angle"])
                new_x = candidate["x"] + dx * _COLLISION_SHIFT_MM
                new_y = candidate["y"] + dy * _COLLISION_SHIFT_MM

                old_label = labels[candidate["idx"]]
                new_label, label_replacements = re.subn(
                    r"\(at\s+[-\d.]+\s+[-\d.]+\s+\d+\)",
                    f'(at {new_x:.2f} {new_y:.2f} {candidate["angle"]})',
                    old_label,
                    count=1,
                )
                if label_replacements != 1:
                    raise ValueError(
                        f'Could not move label {candidate["text"]!r}: '
                        "its anchor is malformed"
                    )

                wire_idx = candidate["wire_idx"]
                parsed_wire = wire_by_idx[wire_idx]
                if (
                    abs(parsed_wire["x2"] - candidate["x"]) > 0.01
                    or abs(parsed_wire["y2"] - candidate["y"]) > 0.01
                ):
                    raise ValueError(
                        f'Cannot move label {candidate["text"]!r}: '
                        "its wire endpoint is already detached"
                    )

                new_wire, wire_replacements = re.subn(
                    r"(\(pts\s+\(xy\s+[-\d.]+\s+[-\d.]+\)\s+)"
                    r"\(xy\s+[-\d.]+\s+[-\d.]+\)\)",
                    rf"\g<1>(xy {new_x:.2f} {new_y:.2f}))",
                    wires[wire_idx],
                    count=1,
                )
                if wire_replacements != 1:
                    raise ValueError(
                        f'Could not extend wire for label {candidate["text"]!r}'
                    )

                labels[candidate["idx"]] = new_label
                wires[wire_idx] = new_wire
                candidate["x"] = new_x
                candidate["y"] = new_y
                candidate["bbox"] = _label_bbox(
                    new_x,
                    new_y,
                    candidate["text"],
                    candidate["angle"],
                )
                candidate["shifts"] += 1
                parsed_wire["x2"] = new_x
                parsed_wire["y2"] = new_y
                moved = True
                break
            if moved:
                break
        if not moved:
            break

    # Structural postcondition: every endpoint-associated label must still land
    # on the exact second endpoint of the wire it claimed before collision work.
    for label in parsed_labels:
        wire_idx = label["wire_idx"]
        if wire_idx is None:
            continue
        parsed_wire = wire_by_idx[wire_idx]
        if (
            abs(parsed_wire["x2"] - label["x"]) > 0.01
            or abs(parsed_wire["y2"] - label["y"]) > 0.01
        ):
            raise ValueError(
                f'Label {label["text"]!r} is detached from its wire endpoint'
            )

    return labels, wires


def _validate_named_net_components(
    wires: list[str],
    labels: list[str],
    junctions: list[str] | None = None,
    no_connects: list[str] | None = None,
) -> None:
    """Reject conflicting labels or no-connects on a physical wire component.

    KiCad joins coincident endpoints, collinear overlaps, and T-junctions where
    a segment endpoint lands on another segment. A pure interior crossing is
    intentionally not joined unless an explicit junction is present.
    """
    segments: list[tuple[float, float, float, float]] = []
    for wire in wires:
        match = _WIRE_RE.search(wire)
        if match:
            segments.append(tuple(float(match.group(index)) for index in range(1, 5)))

    parent = list(range(len(segments)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    def point_on_segment(
        point: tuple[float, float],
        segment: tuple[float, float, float, float],
    ) -> bool:
        x, y = point
        x1, y1, x2, y2 = segment
        epsilon = 0.01
        if abs(x1 - x2) < epsilon:
            return abs(x - x1) < epsilon and min(y1, y2) - epsilon <= y <= max(y1, y2) + epsilon
        if abs(y1 - y2) < epsilon:
            return abs(y - y1) < epsilon and min(x1, x2) - epsilon <= x <= max(x1, x2) + epsilon
        return False

    def electrically_joined(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> bool:
        first_endpoints = ((first[0], first[1]), (first[2], first[3]))
        second_endpoints = ((second[0], second[1]), (second[2], second[3]))
        return any(point_on_segment(point, second) for point in first_endpoints) or any(
            point_on_segment(point, first) for point in second_endpoints
        )

    for first_index, first in enumerate(segments):
        for second_index in range(first_index + 1, len(segments)):
            if electrically_joined(first, segments[second_index]):
                union(first_index, second_index)

    for junction in junctions or []:
        match = _JUNCTION_RE.search(junction)
        if not match:
            continue
        point = (float(match.group(1)), float(match.group(2)))
        touching = [
            index for index, segment in enumerate(segments) if point_on_segment(point, segment)
        ]
        for index in touching[1:]:
            union(touching[0], index)

    parsed_labels: list[tuple[str, tuple[float, float], list[int]]] = []
    for label in labels:
        match = _LABEL_RE.search(label)
        if not match:
            continue
        name = match.group(2)
        point = (float(match.group(3)), float(match.group(4)))
        touching = [
            index for index, segment in enumerate(segments) if point_on_segment(point, segment)
        ]
        for index in touching[1:]:
            union(touching[0], index)
        parsed_labels.append((name, point, touching))

    names_by_component: dict[tuple, set[str]] = {}
    for name, point, touching in parsed_labels:
        component = (
            ("wire", find(touching[0]))
            if touching
            else ("anchor", round(point[0], 2), round(point[1], 2))
        )
        names_by_component.setdefault(component, set()).add(name)

    collisions = [sorted(names) for names in names_by_component.values() if len(names) > 1]
    if collisions:
        rendered = "; ".join(", ".join(names) for names in sorted(collisions))
        raise ValueError(
            "Schematic generation would attach multiple named nets to one physical wire "
            f"component: {rendered}"
        )

    no_connect_collisions: list[tuple[tuple[float, float], list[str]]] = []
    for no_connect in no_connects or []:
        match = _NO_CONNECT_RE.search(no_connect)
        if not match:
            continue
        point = (float(match.group(1)), float(match.group(2)))
        touching = [
            index for index, segment in enumerate(segments) if point_on_segment(point, segment)
        ]
        components = {("wire", find(index)) for index in touching}
        if not components:
            components.add(("anchor", round(point[0], 2), round(point[1], 2)))
        attached_names = sorted(
            {
                name
                for component in components
                for name in names_by_component.get(component, set())
            }
        )
        if attached_names:
            no_connect_collisions.append((point, attached_names))

    if no_connect_collisions:
        rendered = "; ".join(
            f"({point[0]:.2f}, {point[1]:.2f}): {', '.join(names)}"
            for point, names in no_connect_collisions
        )
        raise ValueError(
            "Schematic generation would place a no-connect marker on a named net "
            f"component: {rendered}"
        )


def _extract_wire_start(wire_str: str) -> str:
    """Extract the start point (x y) from a wire S-expression."""
    m = re.search(r'\(pts\s+\(xy\s+([-\d.]+)\s+([-\d.]+)\)', wire_str)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return "0 0"


def sheet_title_text(title, description="", x=20, y=15):
    """Generate a title banner that keeps centered text inside the left margin."""
    title, description = sexpr_safe(title), sexpr_safe(description)
    # KiCad centers bare text on its ``at`` coordinate.  Keep half the
    # estimated title width to the left of that coordinate so a long project
    # name cannot protrude past the page boundary.
    x = max(snap(x), snap(ANNOTATION_MARGIN_X + text_width_mm(title, 3.0) / 2.0))
    y = snap(y)
    lines = []
    lines.append(f'  (text "{title}" (at {x:.2f} {y:.2f} 0)')
    lines.append("    (effects (font (size 3.0 3.0) bold))")
    lines.append("  )")
    if description:
        dy = snap(y + 5.08)
        lines.append(f'  (text "{description}" (at {x:.2f} {dy:.2f} 0)')
        lines.append("    (effects (font (size 1.5 1.5)))")
        lines.append("  )")
    return "\n".join(lines)


def text_annotation(text, x, y, size=1.27, justify: str | None = None):
    """Generate a text annotation element for design rationale or notes."""
    x = max(snap(ANNOTATION_MARGIN_X), snap(x))
    y = snap(y)
    text = sexpr_safe(text)
    justify_clause = ""
    if justify:
        justify_clause = f" (justify {justify})"
    return (
        f'  (text "{text}" (at {x:.2f} {y:.2f} 0)\n'
        f"    (effects (font (size {size:.1f} {size:.1f})){justify_clause})\n"
        f"  )"
    )


def _dedupe_sheet_elements(
    instances: list[str],
    wires: list[str] | None,
    labels: list[str],
    no_connects: list[str],
    junctions: list[str] | None,
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Drop structurally-identical sheet elements before emission.

    Sprint 40 Task 170 — the strap/support placer and topology dispatchers
    can add a wire, label, or power anchor twice (once via the per-passive
    endpoint renderer, once via the shared anchor loop). The placer code is
    the right place for a full root-cause fix, but the schematic should
    never ship with duplicates regardless of where the duplication was
    introduced, and new generator work is far more likely to regress this
    invariant than the dedup itself. So we fail closed here.

    Dedup keys:
    * instances  — ``(lib_id, ref, at x y rot)``; separately any repeated UUID
    * wires      — sorted endpoint pair
    * labels     — ``(label-kind, text, at x y rot)``
    * no_connects — ``(at x y)``
    * junctions  — ``(at x y)``

    Two distinct refs at the same coordinate are kept — that's an overlap
    bug for the placer to flag, not a reason to silently drop a component.
    """
    import re

    seen_sym_keys: set[tuple[str, str, str]] = set()
    seen_uuids: set[str] = set()
    deduped_instances: list[str] = []
    for inst in instances:
        lib_match = re.search(r'\(symbol\s+\(lib_id\s+"([^"]+)"\)\s+\(at\s+([^)]+)\)', inst)
        ref_match = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', inst)
        key: tuple[str, str, str]
        if lib_match:
            key = (
                lib_match.group(1),
                ref_match.group(1) if ref_match else "",
                lib_match.group(2).strip(),
            )
        else:
            # Fall back to the raw instance text so we at least catch exact
            # duplicate blobs wholesale.
            key = ("_raw_", "", inst)
        if key in seen_sym_keys:
            continue
        seen_sym_keys.add(key)
        # Also strip UUID-level duplicates: KiCad rejects two symbols with
        # the same UUID, and the placer has been seen to emit identical
        # instance blobs wholesale (same UUID, same coords).
        uuid_match = re.search(r'\(uuid\s+"([^"]+)"', inst)
        if uuid_match:
            uuid_val = uuid_match.group(1)
            if uuid_val in seen_uuids:
                continue
            seen_uuids.add(uuid_val)
        deduped_instances.append(inst)

    seen_wire_keys: set[tuple] = set()
    deduped_wires: list[str] = []
    for w in wires or []:
        pts = re.findall(r"\(xy\s+([-0-9.eE]+)\s+([-0-9.eE]+)\)", w)
        if len(pts) >= 2:
            try:
                a = (round(float(pts[0][0]), 4), round(float(pts[0][1]), 4))
                b = (round(float(pts[1][0]), 4), round(float(pts[1][1]), 4))
                key_w = tuple(sorted([a, b]))
            except ValueError:
                key_w = (w,)
        else:
            key_w = (w,)
        if key_w in seen_wire_keys:
            continue
        seen_wire_keys.add(key_w)
        deduped_wires.append(w)

    seen_label_keys: set[tuple[str, str, str]] = set()
    deduped_labels: list[str] = []
    for lab in labels:
        lab_match = re.search(
            r'\((global_label|hierarchical_label|label)\s+"([^"]+)".*?\(at\s+([^)]+)\)',
            lab,
            re.DOTALL,
        )
        if lab_match:
            key_l = (
                lab_match.group(1),
                lab_match.group(2),
                lab_match.group(3).strip(),
            )
        else:
            key_l = ("_raw_", lab, "")
        if key_l in seen_label_keys:
            continue
        seen_label_keys.add(key_l)
        deduped_labels.append(lab)

    seen_nc_keys: set[str] = set()
    deduped_ncs: list[str] = []
    for nc in no_connects:
        at_match = re.search(r"\(at\s+([^)]+)\)", nc)
        key_nc = at_match.group(1).strip() if at_match else nc
        if key_nc in seen_nc_keys:
            continue
        seen_nc_keys.add(key_nc)
        deduped_ncs.append(nc)

    seen_junc_keys: set[str] = set()
    deduped_juncs: list[str] = []
    for j in junctions or []:
        at_match = re.search(r"\(at\s+([^)]+)\)", j)
        key_j = at_match.group(1).strip() if at_match else j
        if key_j in seen_junc_keys:
            continue
        seen_junc_keys.add(key_j)
        deduped_juncs.append(j)

    return deduped_instances, deduped_wires, deduped_labels, deduped_ncs, deduped_juncs


def assemble_sheet(
    header,
    lib_symbols,
    instances,
    labels,
    no_connects,
    extras="",
    wires=None,
    junctions=None,
    bus_elements=None,
    project_name="project",
    root_uuid="",
    sheet_uuid="",
    page_num=1,
    obstacle_boxes=None,
    symbol_library_name=None,
    symbol_scope=None,
    symbol_library_sink=None,
):
    """Assemble a complete .kicad_sch file from parts."""
    import re

    # Sprint 44 T196 — resolve label collisions before dedup so that
    # any exact-position duplicates created by shifting are cleaned up
    # by the dedup pass that follows.
    if wires and labels:
        labels, wires = _resolve_label_collisions(
            list(labels),
            list(wires),
            obstacle_boxes=obstacle_boxes,
        )

    # Sprint 40 Task 170 — guarantee sheet elements are structurally unique
    # before emission. Catches any upstream double-emission in the placer /
    # topology dispatchers / anchor renderer, AND exact duplicates the label
    # collision pass may have created.
    instances, wires, labels, no_connects, junctions = _dedupe_sheet_elements(
        list(instances),
        list(wires) if wires else [],
        list(labels),
        list(no_connects),
        list(junctions) if junctions else [],
    )
    _validate_named_net_components(wires, labels, junctions, no_connects)

    parts = [header]

    # Collect ALL referenced symbols from instances and ensure they're defined
    instances_text = "\n".join(instances)
    existing_syms = "\n".join(lib_symbols)

    # Extract all lib_id references: lib_id "SYMBOL_NAME"
    referenced_symbols = set(re.findall(r'lib_id "([^"]+)"', instances_text))

    # Check which symbols are already defined in lib_symbols
    # Match: (symbol "NAME" or nested (symbol "NAME"
    defined_symbols = set(re.findall(r'\(symbol "([^"]+)"', existing_syms))

    # Add missing symbols
    missing_symbols = referenced_symbols - defined_symbols

    # Auto-inject missing symbols
    _passive_types = {
        "C_Small": "C",
        "R_Small": "R",
        "L_Small": "L",
        "C_Review": "C",
        "R_Review": "R",
        "L_Review": "L",
    }
    _power_symbols = {
        "GND",
        "VCC",
        "VDD",
        "VCCINT",
        "VCCAUX",
        "VDDA",
        "VDDA_1P8",
        "VCCO",
        "MGTAVCC",
        "MGTAVTT",
        "MGT_RREF",
        "VREF_INT",
        "VDD_3P3",
        "VDD_DDR",
    }

    for sym_name in sorted(missing_symbols):
        # Passive symbols
        if sym_name in _passive_types:
            lib_symbols.append(passive_lib_symbol(sym_name, _passive_types[sym_name]))
        # Power symbols (any that look like power nets)
        elif (
            sym_name in _power_symbols
            or sym_name.startswith("VCC")
            or sym_name.startswith("VDD")
            or sym_name.startswith("GND")
            or sym_name.startswith("VREF")
        ):
            lib_symbols.append(sexpr_power_lib_entry(sym_name))
        # For custom ICs and other symbols, generate a minimal valid symbol definition
        else:
            # Generate a minimal placeholder symbol with no pins (they'll be added as needed)
            lib_symbols.append(f'''  (symbol "{sym_name}" (pin_names (offset 1000)) (exclude_from_sim no)
    (in_bom yes) (on_board yes)
    (property "Reference" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "{sym_name}" (at 0 -1.27 0)
      (effects (font (size 1.27 1.27)))
    )
    (symbol "{sym_name}_0_1")
    (symbol "{sym_name}_1_1")
  )''')

    if symbol_library_name:
        safe_library = re.sub(r"[^A-Za-z0-9_]", "_", str(symbol_library_name))
        safe_scope = re.sub(r"[^A-Za-z0-9_]", "_", str(symbol_scope or "sheet"))
        if not safe_library or not safe_scope:
            raise ValueError("Symbol library name and scope must contain a portable identifier")

        qualified_by_original: dict[str, str] = {}
        embedded_symbols: list[str] = []
        for definition in lib_symbols:
            top_match = re.search(r'\(symbol\s+"([^"]+)"', definition)
            if not top_match:
                raise ValueError("Generated library symbol has no top-level symbol name")
            original_name = top_match.group(1)
            scoped_name = f"{safe_scope}__{original_name}"
            qualified_name = f"{safe_library}:{scoped_name}"

            # Rename the library part and all nested unit/body symbols together.
            # Only the embedded top-level name receives the library namespace;
            # KiCad requires nested names to retain the unqualified part prefix.
            renamed_definition = re.sub(
                rf'(\(symbol\s+"){re.escape(original_name)}(?="|_)',
                rf"\g<1>{scoped_name}",
                definition,
            )
            embedded_definition, replacements = re.subn(
                rf'(\(symbol\s+"){re.escape(scoped_name)}"',
                rf'\g<1>{qualified_name}"',
                renamed_definition,
                count=1,
            )
            if replacements != 1:
                raise ValueError(f"Could not namespace generated symbol {original_name!r}")

            if symbol_library_sink is not None:
                existing = symbol_library_sink.get(scoped_name)
                if existing is not None and existing != renamed_definition:
                    raise ValueError(
                        f"Conflicting generated definitions for local symbol {scoped_name!r}"
                    )
                symbol_library_sink[scoped_name] = renamed_definition
            qualified_by_original[original_name] = qualified_name
            embedded_symbols.append(embedded_definition)

        def qualify_instance(match):
            original_name = match.group(1)
            qualified = qualified_by_original.get(original_name)
            if qualified is None:
                raise ValueError(
                    f"Instance references symbol {original_name!r} missing from local library"
                )
            return f'lib_id "{qualified}"'

        instances = [
            re.sub(r'lib_id "([^"]+)"', qualify_instance, instance)
            for instance in instances
        ]
        lib_symbols = embedded_symbols

    parts.append("  (lib_symbols")
    for ls in lib_symbols:
        parts.append(ls)
    parts.append("  )\n")

    for inst in instances:
        parts.append(inst)

    if wires:
        for w in wires:
            parts.append(w)

    if junctions:
        for j in junctions:
            parts.append(j)

    for label in labels:
        parts.append(label)

    if bus_elements:
        for be in bus_elements:
            parts.append(be)

    for nc in no_connects:
        parts.append(nc)

    if extras:
        parts.append(extras)

    if root_uuid and sheet_uuid:
        path = f"/{root_uuid}/{sheet_uuid}"
        parts.append(f'  (sheet_instances\n    (path "{path}" (page "{page_num}"))\n  )\n')
    else:
        parts.append(f'  (sheet_instances\n    (path "/" (page "{page_num}"))\n  )\n')
    parts.append(")")

    return "\n".join(parts)


def _extract_sexpr_block(text: str, start_idx: int) -> str:
    """Return the balanced S-expression block starting at ``start_idx``."""
    depth = 0
    for idx in range(start_idx, len(text)):
        ch = text[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start_idx : idx + 1]
    return text[start_idx:]


def _iter_blocks(text: str, token_pattern: str):
    """Yield balanced S-expression blocks whose opening matches ``token_pattern``."""
    for match in re.finditer(token_pattern, text):
        yield _extract_sexpr_block(text, match.start())


def _text_bbox_centered(x: float, y: float, text: str, size_x: float, size_y: float, angle: int = 0):
    """Estimate bbox for centered KiCad text/property rendering."""
    width = max(size_x, text_width_mm(text, size_x))
    height = max(size_y, size_y * 1.6)
    if angle % 180 != 0:
        width, height = height, width
    half_w = width / 2.0
    half_h = height / 2.0
    return (x - half_w, y - half_h, x + half_w, y + half_h)


def _label_bbox(x: float, y: float, text: str, angle: int = 0, size: float = 1.0):
    """Estimate bbox for a KiCad label anchored at ``(x, y)``."""
    width = max(size, text_width_mm(text, size)) + 2.0
    height = max(size, size * 1.6)
    angle = angle % 360
    if angle == 180:
        return (x - width, y - height / 2.0, x, y + height / 2.0)
    if angle == 90:
        return (x - height / 2.0, y - width, x + height / 2.0, y)
    if angle == 270:
        return (x - height / 2.0, y, x + height / 2.0, y + width)
    return (x, y - height / 2.0, x + width, y + height / 2.0)


def _rotate_point(x: float, y: float, angle: int) -> tuple[float, float]:
    """Rotate a local point around the origin using KiCad's degree convention."""
    angle = angle % 360
    if angle == 0:
        return x, y
    if angle == 90:
        return -y, x
    if angle == 180:
        return -x, -y
    if angle == 270:
        return y, -x

    radians = math.radians(angle)
    sin_a = math.sin(radians)
    cos_a = math.cos(radians)
    return (x * cos_a - y * sin_a, x * sin_a + y * cos_a)


def _rotate_bounds(left: float, right: float, top: float, bottom: float, angle: int):
    """Rotate local bounds around the origin by a KiCad component angle."""
    angle = angle % 360
    if angle == 0:
        return left, right, top, bottom

    corners = [
        (-left, top),
        (right, top),
        (right, -bottom),
        (-left, -bottom),
    ]
    radians = math.radians(angle)
    sin_a = math.sin(radians)
    cos_a = math.cos(radians)
    xs = []
    ys = []
    for x, y in corners:
        xr = x * cos_a - y * sin_a
        yr = x * sin_a + y * cos_a
        xs.append(xr)
        ys.append(yr)
    return (max(0.0, -min(xs)), max(0.0, max(xs)), max(0.0, max(ys)), max(0.0, -min(ys)))


def _symbol_local_bounds(symbol_block: str, symbol_name: str) -> tuple[float, float, float, float]:
    """Estimate local bounds of an embedded lib symbol including pin text."""
    xs = []
    ys = []

    for x1, y1, x2, y2 in re.findall(
        r"\(rectangle\s+\(start\s+([-\d.]+)\s+([-\d.]+)\)\s+\(end\s+([-\d.]+)\s+([-\d.]+)\)",
        symbol_block,
    ):
        xs.extend((float(x1), float(x2)))
        ys.extend((float(y1), float(y2)))

    for x_str, y_str in re.findall(r"\(xy\s+([-\d.]+)\s+([-\d.]+)\)", symbol_block):
        xs.append(float(x_str))
        ys.append(float(y_str))

    for block in _iter_blocks(symbol_block, r"\(property\s+\""):
        if " hide" in block:
            continue
        match = re.search(
            r'\(property\s+"[^"]*"\s+"([^"]*)"\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)',
            block,
        )
        if not match:
            continue
        text = match.group(1)
        x = float(match.group(2))
        y = float(match.group(3))
        angle = int(match.group(4))
        size_match = re.search(r"\(font\s+\(size\s+([-\d.]+)\s+([-\d.]+)\)\)", block)
        size_x = float(size_match.group(1)) if size_match else 1.27
        size_y = float(size_match.group(2)) if size_match else size_x
        min_x, min_y, max_x, max_y = _text_bbox_centered(x, y, text, size_x, size_y, angle)
        xs.extend((min_x, max_x))
        ys.extend((min_y, max_y))

    pins = re.findall(
        r"\(pin\s+(\w+)\s+\w+\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)\s+\(length\s+([-\d.]+)\)"
        r'\s*\(name\s+"([^"]*)"\s.*?\)\s*\(number\s+"([^"]*)"\s',
        symbol_block,
        re.DOTALL,
    )
    for _ptype, x_str, y_str, angle_str, len_str, pname, pnum in pins:
        x = float(x_str)
        y = float(y_str)
        angle = int(angle_str) % 360
        pin_len = float(len_str)
        xs.append(x)
        ys.append(y)
        if angle == 0:
            xs.append(x + pin_len)
        elif angle == 180:
            xs.append(x - pin_len)
        elif angle == 90:
            ys.append(y + pin_len)
        elif angle == 270:
            ys.append(y - pin_len)

        text_w = max(text_width_mm(pname, 1.0), text_width_mm(pnum, 1.0), 2.54) + 1.27
        text_h = 1.8
        if angle in (0, 180):
            xs.extend((x - text_w, x + text_w))
            ys.extend((y - text_h, y + text_h))
        else:
            xs.extend((x - text_h, x + text_h))
            ys.extend((y - text_w, y + text_w))

    if not xs:
        return (10.0, 10.0, 10.0, 10.0)

    return (
        max(0.0, -min(xs)),
        max(0.0, max(xs)),
        max(0.0, max(ys)),
        max(0.0, -min(ys)),
    )


def estimate_content_bounds(content: str) -> tuple[float, float, float, float]:
    """Estimate rendered content bounds for a generated KiCad schematic string."""
    lib_sym_start = content.find("(lib_symbols")
    lib_section = _extract_sexpr_block(content, lib_sym_start) if lib_sym_start >= 0 else ""
    lib_sym_end = lib_sym_start + len(lib_section) if lib_sym_start >= 0 else 0
    after_lib = content[lib_sym_end:] if lib_sym_end > 0 else content

    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    def update(minx: float, miny: float, maxx: float, maxy: float):
        nonlocal min_x, min_y, max_x, max_y
        min_x = min(min_x, minx)
        min_y = min(min_y, miny)
        max_x = max(max_x, maxx)
        max_y = max(max_y, maxy)

    symbol_bounds_cache: dict[str, tuple[float, float, float, float]] = {}

    for match in re.finditer(
        r'\(symbol\s+\(lib_id\s+"([^"]*)"\)\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)',
        after_lib,
    ):
        lib_id = match.group(1)
        x = float(match.group(2))
        y = float(match.group(3))
        angle = int(match.group(4))
        if lib_id not in symbol_bounds_cache:
            block_match = re.search(rf'\(symbol\s+"{re.escape(lib_id)}"(?:\s|\()', lib_section)
            if block_match:
                symbol_block = _extract_sexpr_block(lib_section, block_match.start())
                symbol_bounds_cache[lib_id] = _symbol_local_bounds(symbol_block, lib_id)
            else:
                symbol_bounds_cache[lib_id] = (10.0, 10.0, 10.0, 10.0)
        left, right, top, bottom = _rotate_bounds(*symbol_bounds_cache[lib_id], angle)
        update(x - left, y - top, x + right, y + bottom)

    for match in re.finditer(
        r'\((global_label|hierarchical_label)\s+"([^"]*)"\s+\(shape\s+\w+\)\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)',
        after_lib,
    ):
        name = match.group(2)
        x = float(match.group(3))
        y = float(match.group(4))
        angle = int(match.group(5))
        update(*_label_bbox(x, y, name, angle=angle, size=1.0))

    for block in _iter_blocks(after_lib, r'\(text\s+"'):
        match = re.search(r'\(text\s+"([^"]*)"\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)', block)
        if not match:
            continue
        text = match.group(1)
        x = float(match.group(2))
        y = float(match.group(3))
        angle = int(match.group(4))
        size_match = re.search(r"\(font\s+\(size\s+([-\d.]+)\s+([-\d.]+)\)", block)
        size_x = float(size_match.group(1)) if size_match else 1.27
        size_y = float(size_match.group(2)) if size_match else size_x
        update(*_text_bbox_centered(x, y, text, size_x, size_y, angle))

    for block in _iter_blocks(after_lib, r'\(property\s+"'):
        if " hide" in block:
            continue
        match = re.search(
            r'\(property\s+"[^"]*"\s+"([^"]*)"\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)',
            block,
        )
        if not match:
            continue
        text = match.group(1)
        x = float(match.group(2))
        y = float(match.group(3))
        angle = int(match.group(4))
        size_match = re.search(r"\(font\s+\(size\s+([-\d.]+)\s+([-\d.]+)\)\)", block)
        size_x = float(size_match.group(1)) if size_match else 1.27
        size_y = float(size_match.group(2)) if size_match else size_x
        update(*_text_bbox_centered(x, y, text, size_x, size_y, angle))

    for match in re.finditer(r"\(xy\s+([-\d.]+)\s+([-\d.]+)\)", after_lib):
        x = float(match.group(1))
        y = float(match.group(2))
        update(x, y, x, y)

    for match in re.finditer(r"\(no_connect\s+\(at\s+([-\d.]+)\s+([-\d.]+)\)", after_lib):
        x = float(match.group(1))
        y = float(match.group(2))
        update(x - 1.5, y - 1.5, x + 1.5, y + 1.5)

    if min_x == float("inf"):
        return (0.0, 0.0, 0.0, 0.0)
    return (min_x, min_y, max_x, max_y)


def center_content(content, paper):
    """Shift all coordinates so content is centered on the page."""
    pw, ph = PAPER_SIZES.get(paper, PAPER_SIZES["A3"])
    lib_sym_start = content.find("(lib_symbols")
    lib_section = _extract_sexpr_block(content, lib_sym_start) if lib_sym_start >= 0 else ""
    lib_sym_end = lib_sym_start + len(lib_section) if lib_sym_start >= 0 else 0
    before_lib = content[:lib_sym_start] if lib_sym_start >= 0 else ""
    after_lib = content[lib_sym_end:] if lib_sym_end > 0 else content

    min_x, min_y, max_x, max_y = estimate_content_bounds(content)
    if max_x <= min_x and max_y <= min_y:
        return content

    content_w = max_x - min_x
    content_h = max_y - min_y
    usable_h = ph - TITLE_BLOCK_H

    if content_w > pw or content_h > usable_h:
        dx = snap(max(0, 20 - min_x))
        dy = snap(max(0, 20 - min_y))
    else:
        target_x = (pw - content_w) / 2
        target_y = (usable_h - content_h) / 2
        dx = snap(target_x - min_x)
        dy = snap(target_y - min_y)

    text_xs = []
    for match in re.finditer(
        r'\(text\s+"([^"]*)"\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)(?:.|\n)*?\(font\s+\(size\s+([-\d.]+)\s+([-\d.]+)\)',
        after_lib,
        re.DOTALL,
    ):
        bbox = _text_bbox_centered(
            float(match.group(2)),
            float(match.group(3)),
            match.group(1),
            float(match.group(5)) if match.group(5) else 1.27,
            float(match.group(6)) if match.group(6) else (float(match.group(5)) if match.group(5) else 1.27),
            int(match.group(4)),
        )
        text_xs.append(bbox[0])
    if text_xs:
        dx = max(dx, snap(max(0, snap(ANNOTATION_MARGIN_X) - min(text_xs))))

    render_margin = snap(10.0)
    min_dx = snap(render_margin - min_x)
    max_dx = snap((pw - render_margin) - max_x)
    if min_dx <= max_dx:
        dx = min(max(dx, min_dx), max_dx)

    min_dy = snap(render_margin - min_y)
    max_dy = snap((usable_h - render_margin) - max_y)
    if min_dy <= max_dy:
        dy = min(max(dy, min_dy), max_dy)

    if abs(dx) < 2 and abs(dy) < 2:
        return content

    at_re = re.compile(r"(\(at\s+)([-\d.]+)(\s+)([-\d.]+)")
    xy_re = re.compile(r"(\(xy\s+)([-\d.]+)(\s+)([-\d.]+)")

    def shifter(m):
        x = float(m.group(2)) + dx
        y = float(m.group(4)) + dy
        return f"{m.group(1)}{x:.2f}{m.group(3)}{y:.2f}"

    result = at_re.sub(shifter, after_lib)
    result = xy_re.sub(shifter, result)
    return before_lib + lib_section + result
