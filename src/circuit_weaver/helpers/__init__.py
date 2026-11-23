"""Reusable PCB and schematic helper utilities for Circuit Weaver."""

from .impedance import (
    GENERIC_6L_FR4_STACKUP,
    differential_microstrip_z0,
    find_width_for_z0,
    microstrip_z0,
    stripline_z0,
    symmetric_stripline_z0_ideal,
)
from .placement import (
    find_matching_capacitor,
    footprint_has_net,
    footprint_matches_keyword,
    footprint_position_mm,
    footprint_ref,
    footprint_value,
    is_locked,
    iter_board_footprints,
    values_match,
)
from .silkscreen import get_state_path, load_state, save_state, sync_managed_silkscreen

__all__ = [
    "GENERIC_6L_FR4_STACKUP",
    "differential_microstrip_z0",
    "find_matching_capacitor",
    "find_width_for_z0",
    "footprint_has_net",
    "footprint_matches_keyword",
    "footprint_position_mm",
    "footprint_ref",
    "footprint_value",
    "get_state_path",
    "is_locked",
    "iter_board_footprints",
    "load_state",
    "microstrip_z0",
    "save_state",
    "stripline_z0",
    "symmetric_stripline_z0_ideal",
    "sync_managed_silkscreen",
    "values_match",
]
