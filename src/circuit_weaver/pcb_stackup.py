"""Small helpers for explicitly selecting supported copper layer counts.

This describes only copper layer names. It does not define dielectric materials,
thicknesses, impedance, or a manufacturing stackup.
"""

from __future__ import annotations

_COPPER_LAYER_NAMES = {
    2: ("F.Cu", "B.Cu"),
    4: ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
}


def validate_copper_layers(value: object) -> int:
    """Accept exactly the supported integer copper counts; reject bools."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in _COPPER_LAYER_NAMES:
        raise ValueError("copper_layers must be the integer 2 or 4")
    return value


def expected_copper_layer_names(value: object) -> tuple[str, ...]:
    """Return the documented outer-to-inner-to-outer copper layer order."""
    return _COPPER_LAYER_NAMES[validate_copper_layers(value)]
