"""Bounded, read-only placement diagnostics from native footprint envelopes.

An empty rectangle is a review prompt, not an electrical acceptance rule.
Grid cells touched by rotated envelope boxes are occupied conservatively.
"""

from __future__ import annotations

import math
from typing import Any


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if positive and result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def _largest_rectangle(available: list[list[bool]]) -> tuple[int, tuple[int, int, int, int] | None]:
    """Largest all-free axis-aligned grid rectangle in O(rows * columns)."""
    heights = [0] * len(available[0])
    best_area = 0
    best = None
    for row_index, row in enumerate(available):
        heights = [height + 1 if free else 0 for height, free in zip(heights, row)]
        stack: list[tuple[int, int]] = []
        for column, height in enumerate([*heights, 0]):
            start = column
            while stack and stack[-1][1] > height:
                left, previous_height = stack.pop()
                area = previous_height * (column - left)
                if area > best_area:
                    best_area = area
                    best = (left, row_index + 1 - previous_height, column, row_index + 1)
                start = left
            if not stack or stack[-1][1] < height:
                stack.append((start, height))
    return best_area, best


def review_placement_inventory(
    inventory: dict[str, Any], *, reserved_regions: list[dict[str, Any]] | None = None,
    cell_size_mm: float = 5.0, max_empty_fraction: float = 0.2,
) -> dict[str, Any]:
    """Review native rectangular-board placement without changing its geometry.

    Reservations use absolute native-board x/y coordinates. They must be
    explicitly authored rectangles with a name and are excluded from usable
    cells. This is a conservative grid diagnostic of footprint placement only;
    it does not measure copper fill, thermal clearance or electrical quality.
    """
    cell_size = _number(cell_size_mm, "cell_size_mm", positive=True)
    threshold = _number(max_empty_fraction, "max_empty_fraction")
    if not 0 <= threshold <= 1:
        raise ValueError("max_empty_fraction must be between 0 and 1")
    result: dict[str, Any] = {
        "status": "unverified", "scope": "placement geometry diagnostic only",
        "method": "largest empty axis-aligned rectangle on a conservative envelope grid",
        "electrical_or_manufacturing_approval": False,
        "requested_cell_size_mm": cell_size, "max_empty_fraction": threshold,
    }
    if not isinstance(inventory, dict) or inventory.get("status") != "ok":
        return {**result, "reason": "Native inspection did not succeed"}
    if inventory.get("rectangular_outline") is not True:
        return {**result, "reason": "A verified rectangular outline is required"}
    bounds = inventory.get("bounds")
    if not isinstance(bounds, dict):
        return {**result, "reason": "Board bounds are missing"}
    try:
        origin_x, origin_y = (_number(bounds[key], f"bounds.{key}") for key in ("x", "y"))
        width, height = (_number(bounds[key], f"bounds.{key}", positive=True) for key in ("width", "height"))
    except (KeyError, ValueError) as exc:
        return {**result, "reason": f"Invalid board bounds: {exc}"}
    columns, rows = math.ceil(width / cell_size), math.ceil(height / cell_size)
    if columns > 512 or rows > 512 or columns * rows > 65536:
        raise ValueError("Placement review grid exceeds 65,536 cells or 512 cells per axis; increase cell_size_mm")
    dx, dy = width / columns, height / rows
    occupied = [[False] * columns for _ in range(rows)]
    reserved = [[False] * columns for _ in range(rows)]

    def paint(grid: list[list[bool]], box: tuple[float, float, float, float]) -> None:
        left, top, right, bottom = box
        if right <= origin_x or bottom <= origin_y or left >= origin_x + width or top >= origin_y + height:
            return
        first_x = max(0, math.floor((left - origin_x) / dx))
        last_x = min(columns, math.ceil((right - origin_x) / dx))
        first_y = max(0, math.floor((top - origin_y) / dy))
        last_y = min(rows, math.ceil((bottom - origin_y) / dy))
        for row in range(first_y, last_y):
            for column in range(first_x, last_x):
                grid[row][column] = True

    footprints = inventory.get("footprints")
    if not isinstance(footprints, list) or not footprints:
        return {**result, "reason": "No native footprint envelopes are available"}
    refs: set[str] = set()
    centers = []
    invalid = []
    overhangs = []
    for item in footprints:
        ref = item.get("ref") if isinstance(item, dict) else None
        try:
            if not isinstance(ref, str) or not ref.strip() or ref in refs:
                raise ValueError("Missing or duplicate footprint reference")
            refs.add(ref)
            x, y = (_number(item[key], f"{ref}.{key}") for key in ("x", "y"))
            body_width = _number(item.get("courtyard_width", item.get("width")), f"{ref}.width", positive=True)
            body_height = _number(item.get("courtyard_height", item.get("height")), f"{ref}.height", positive=True)
            angle = math.radians(_number(item["rotation"], f"{ref}.rotation"))
            offset_x = _number(item.get("courtyard_center_x", 0), f"{ref}.courtyard_center_x")
            offset_y = _number(item.get("courtyard_center_y", 0), f"{ref}.courtyard_center_y")
            cosine, sine = math.cos(angle), math.sin(angle)
            center_x = x + offset_x * cosine + offset_y * sine
            center_y = y + offset_y * cosine - offset_x * sine
            extent_x = (abs(body_width * cosine) + abs(body_height * sine)) / 2
            extent_y = (abs(body_height * cosine) + abs(body_width * sine)) / 2
            box = (center_x - extent_x, center_y - extent_y, center_x + extent_x, center_y + extent_y)
            if (box[0] < origin_x - 1e-6 or box[1] < origin_y - 1e-6
                    or box[2] > origin_x + width + 1e-6 or box[3] > origin_y + height + 1e-6):
                overhangs.append(ref)
            paint(occupied, box)
            centers.append((center_x, center_y))
        except (KeyError, TypeError, ValueError) as exc:
            invalid.append({"ref": ref, "reason": str(exc)})
    if invalid:
        return {**result, "reason": "Footprint geometry is incomplete", "invalid_footprints": invalid}
    if reserved_regions is not None and not isinstance(reserved_regions, list):
        raise ValueError("reserved_regions must be a list")
    names = set()
    for region in reserved_regions or []:
        if not isinstance(region, dict) or set(region) != {"name", "x_mm", "y_mm", "width_mm", "height_mm"}:
            raise ValueError("Each reservation requires only name, x_mm, y_mm, width_mm, height_mm")
        name = region["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Reservation names must be nonempty and unique")
        names.add(name)
        x, y = (_number(region[key], f"reservation.{key}") for key in ("x_mm", "y_mm"))
        region_width, region_height = (
            _number(region[key], f"reservation.{key}", positive=True) for key in ("width_mm", "height_mm")
        )
        if x < origin_x or y < origin_y or x + region_width > origin_x + width or y + region_height > origin_y + height:
            raise ValueError("Reservations must lie within the board bounds")
        paint(reserved, (x, y, x + region_width, y + region_height))
    usable = sum(not cell for row in reserved for cell in row)
    if not usable:
        return {**result, "reason": "Reservations cover the entire usable grid"}
    available = [[not occupied[y][x] and not reserved[y][x] for x in range(columns)] for y in range(rows)]
    area_cells, rectangle = _largest_rectangle(available)
    occupied_cells = sum(occupied[y][x] and not reserved[y][x] for y in range(rows) for x in range(columns))
    fraction = area_cells / usable
    empty_box = None
    if rectangle:
        left, top, right, bottom = rectangle
        empty_box = {
            "x_mm": origin_x + left * dx, "y_mm": origin_y + top * dy,
            "width_mm": (right - left) * dx, "height_mm": (bottom - top) * dy,
            "area_mm2": area_cells * dx * dy, "fraction_of_usable_grid": fraction,
        }
    reasons = ["Large unused placement rectangle has no authored reservation"] if fraction > threshold else []
    if overhangs:
        reasons.append("Native footprint envelopes extend outside the board bounds")
    return {
        **result, "status": "review_required" if reasons else "verified",
        "footprint_count": len(footprints), "reservations": reserved_regions or [],
        "grid": {"columns": columns, "rows": rows, "cell_width_mm": dx, "cell_height_mm": dy},
        "usable_grid_area_mm2": usable * dx * dy,
        "occupied_grid_fraction": occupied_cells / usable,
        "footprint_centroid_mm": {"x": sum(x for x, _ in centers) / len(centers),
                                  "y": sum(y for _, y in centers) / len(centers)},
        "largest_empty_rectangle": empty_box,
        "review_reasons": reasons, "overhanging_footprints": overhangs,
        "limitations": [
            "Rotated envelope bounding boxes and cells overestimate occupied/reserved area; narrow gaps may be hidden",
            "Copper fill, keepout legality, grouping, thermal and electrical performance are not evaluated",
            "A verified diagnostic does not establish design acceptance",
        ],
    }
