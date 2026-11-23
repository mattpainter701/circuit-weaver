"""Preparse human-readable YAML specs into engine format.

Handles three cases:
1. Native engine format (blocks at top level)
2. Hybrid format (engine section nested inside detailed spec)
3. Legacy detailed spec (extract blocks from electrical_specifications)
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def extract_engine_spec(spec_dict: dict[str, Any]) -> dict[str, Any]:
    """Extract or convert to engine-native format.

    Returns a spec dict with 'project' and block sections
    (power, digital, storage, audio, interface, etc.)

    Handles:
    - Native format: power, digital, etc. at top level
    - Hybrid format: engine section with blocks nested
    - Detailed format: electrical_specifications with IC details
    """

    # Case 1: Already in engine format (blocks at top level)
    if _has_blocks(spec_dict):
        log.debug("Spec is already in engine format")
        return spec_dict

    # Case 2: Hybrid format (engine section exists)
    if "engine" in spec_dict:
        log.debug("Extracting engine section from hybrid format")
        return spec_dict["engine"]

    # Case 3: Wizard detailed format (extract from electrical_specifications)
    if "electrical_specifications" in spec_dict:
        log.debug("Converting detailed wizard spec to engine format")
        return _extract_from_detailed(spec_dict)

    raise ValueError(
        "Unknown spec format — no recognized block structure "
        "(expected: power/digital blocks OR engine section OR electrical_specifications)"
    )


def _has_blocks(spec_dict: dict[str, Any]) -> bool:
    """Check if spec has native block sections.

    Recognizes:
    - Categorical blocks: power, digital, storage, audio, interface, analog, etc.
    - Generic blocks list: "blocks" key containing list of block dicts
    """
    block_types = [
        "power",
        "digital",
        "storage",
        "audio",
        "interface",
        "analog",
        "connectors",
        "sensors",
        "drivers",
        "misc",
        "protection",
        "passives",
        "blocks",  # Generic blocks list format
    ]
    return any(block in spec_dict for block in block_types)


def _extract_from_detailed(spec_dict: dict[str, Any]) -> dict[str, Any]:
    """Convert detailed wizard spec to engine format.

    Parses electrical_specifications and circuit_blocks sections
    to build native engine blocks.
    """
    metadata = spec_dict.get("metadata", {})
    elec = spec_dict.get("electrical_specifications", {})

    engine_spec: dict[str, Any] = {
        "project": metadata.get("project_name", "Unknown"),
        "power": [],
        "digital": [],
        "storage": [],
        "audio": [],
        "interface": [],
    }

    # Power: LDO regulator
    if "voltage_regulator_ldo" in elec:
        ldo = elec["voltage_regulator_ldo"]
        engine_spec["power"].append(
            {
                "type": "ldo",
                "ic": ldo.get("part"),
                "lcsc": ldo.get("lcsc"),
                "ref": "U4",
                "vin": spec_dict.get("power_budget", {}).get("battery", {}).get("nominal_voltage", 6.0),
                "vout": 3.3,
                "vin_net": "VBAT",
                "rail_name": "VDD_3P3",
            }
        )

    # NOTE: MCU and Flash are NOT template-based subcircuits.
    # They will be added as custom components during circuit resolution.
    # The engine only handles template-based subcircuits (ldo, buck, audio_amplifier, etc.)
    # Skip: mcu, flash_memory, connectors (these are custom components, not templates)

    # Audio: Amplifier (use audio_amplifier template)
    if "audio_amplifier" in elec:
        amp = elec["audio_amplifier"]
        engine_spec["audio"].append(
            {
                "type": "audio_amplifier",
                "ic": amp.get("part"),
                "lcsc": amp.get("lcsc"),
                "ref": "U3",
                "vdd": 3.3,
                "output_power_w": 0.7,
            }
        )

    log.info(
        "Extracted from detailed spec: %d power, %d audio blocks. "
        "MCU, flash, connectors will be added as custom components during resolution.",
        len(engine_spec["power"]),
        len(engine_spec["audio"]),
    )

    return engine_spec
