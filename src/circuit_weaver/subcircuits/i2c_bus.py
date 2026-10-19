"""I2C bus pull-up and level-shifter subcircuit template.

Generates I2C bus conditioning: pull-up resistors for a simple bus, or
a PCA9306 level shifter for multi-voltage domains.

Auto-calculates pull-up resistance from I2C speed and bus capacitance
using the t_rise / (0.8473 * C_bus) formula.  All values snapped to E24.

Supports PULLUPS_ONLY (default) and PCA9306 topologies.
"""

from __future__ import annotations

from typing import Any

from ..component_db import BypassCap, ComponentDef, StrapConfig
from .base import (
    FP_0402C,
    FP_0402R,
    BoundaryPort,
    LegacyDBProxy,
    SubcircuitResult,
    SubcircuitTemplate,
    format_resistance,
    snap_to_e24,
)

# Maximum rise-time (seconds) per I2C speed mode (NXP UM10204)
_I2C_TRISE_MAX = {
    100_000: 1000e-9,  # Standard-mode: 1000 ns
    400_000: 300e-9,  # Fast-mode: 300 ns
    1_000_000: 120e-9,  # Fast-mode Plus: 120 ns
}

# Known I2C bus ICs and their parameters
I2C_BUS_IC_DATABASE = LegacyDBProxy("i2c_bus")  # backed by ic_data/*.json (Task 178)


def _calc_pullup_resistance(speed_hz: float, c_bus_f: float) -> float:
    """Calculate I2C pull-up resistance from bus speed and capacitance.

    R = t_rise_max / (0.8473 * C_bus)
    The 0.8473 factor comes from the RC charge curve to reach 0.7*VDD.
    """
    # Find the closest matching speed tier
    trise = _I2C_TRISE_MAX.get(speed_hz)
    if trise is None:
        # Pick the tier whose speed is >= requested, or fastest available
        for spd in sorted(_I2C_TRISE_MAX):
            if spd >= speed_hz:
                trise = _I2C_TRISE_MAX[spd]
                break
        if trise is None:
            trise = _I2C_TRISE_MAX[1_000_000]  # fastest tier

    if c_bus_f <= 0:
        c_bus_f = 100e-12  # default 100 pF

    r_raw = trise / (0.8473 * c_bus_f)
    return r_raw


class I2CBusTemplate(SubcircuitTemplate):
    """I2C bus pull-up / level-shifter subcircuit."""

    template_type = "i2c_bus"
    description = "I2C bus pull-ups or level shifter with auto-calculated resistance"
    param_schema = [
        {
            "name": "vdd",
            "type": "number",
            "required": False,
            "default": 3.3,
            "description": "I2C bus supply voltage in volts",
        },
        {
            "name": "speed",
            "type": "number",
            "required": False,
            "default": 400000,
            "description": "I2C bus speed in Hz (100000, 400000, or 1000000)",
        },
        {
            "name": "c_bus",
            "type": "number",
            "required": False,
            "default": 100e-12,
            "description": "Total I2C bus capacitance in farads (default 100pF)",
        },
        {
            "name": "ic",
            "type": "string",
            "required": False,
            "default": "PULLUPS_ONLY",
            "description": "I2C bus IC MPN (PULLUPS_ONLY or PCA9306)",
        },
        {
            "name": "ref",
            "type": "string",
            "required": False,
            "default": "RP",
            "description": "Reference designator",
        },
        {
            "name": "vdd_net",
            "type": "string",
            "required": False,
            "default": "VDD_3P3",
            "description": "Power supply net name for pull-ups",
        },
        {
            "name": "sda_net",
            "type": "string",
            "required": False,
            "default": "I2C_SDA",
            "description": "I2C SDA bus net name",
        },
        {
            "name": "scl_net",
            "type": "string",
            "required": False,
            "default": "I2C_SCL",
            "description": "I2C SCL bus net name",
        },
        {
            "name": "vdd_low_net",
            "type": "string",
            "required": False,
            "description": "Low-side supply net for PCA9306 (VREF1)",
        },
        {
            "name": "vdd_high_net",
            "type": "string",
            "required": False,
            "description": "High-side supply net for PCA9306 (VREF2)",
        },
        {
            "name": "sda_low_net",
            "type": "string",
            "required": False,
            "description": "Low-side SDA net for PCA9306",
        },
        {
            "name": "scl_low_net",
            "type": "string",
            "required": False,
            "description": "Low-side SCL net for PCA9306",
        },
        {
            "name": "sda_high_net",
            "type": "string",
            "required": False,
            "description": "High-side SDA net for PCA9306",
        },
        {
            "name": "scl_high_net",
            "type": "string",
            "required": False,
            "description": "High-side SCL net for PCA9306",
        },
    ]

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = []
        ic_name = params.get("ic", "PULLUPS_ONLY")
        if ic_name not in I2C_BUS_IC_DATABASE:
            errors.append(f"Unknown I2C bus IC '{ic_name}'. Available: {', '.join(I2C_BUS_IC_DATABASE)}")

        speed = params.get("speed", 400_000)
        if speed <= 0:
            errors.append(f"speed ({speed}Hz) must be positive")

        c_bus = params.get("c_bus", 100e-12)
        if c_bus <= 0:
            errors.append(f"c_bus ({c_bus}F) must be positive")

        return errors

    def generate(self, params: dict[str, Any]) -> SubcircuitResult:
        """Generate an I2C bus conditioning subcircuit.

        Optional params:
            vdd: float -- bus supply voltage (default: 3.3V)
            speed: float -- I2C speed in Hz (default: 400000)
            c_bus: float -- bus capacitance in farads (default: 100pF)
            ic: str -- IC MPN (default: "PULLUPS_ONLY")
            ref: str -- reference designator (default: "RP")
            vdd_net: str -- supply net (default: "VDD_3P3")
            sda_net: str -- SDA net (default: "I2C_SDA")
            scl_net: str -- SCL net (default: "I2C_SCL")
            vdd_low_net: str -- low-side supply for PCA9306
            vdd_high_net: str -- high-side supply for PCA9306
            sda_low_net / scl_low_net: str -- low-side bus nets for PCA9306
            sda_high_net / scl_high_net: str -- high-side bus nets for PCA9306
        """
        ic_name = params.get("ic", "PULLUPS_ONLY")
        ic_db = I2C_BUS_IC_DATABASE.get(ic_name, I2C_BUS_IC_DATABASE["PULLUPS_ONLY"])

        if ic_db["has_level_shift"]:
            return self._generate_level_shifter(ic_name, ic_db, params)
        return self._generate_pullups(ic_name, ic_db, params)

    # ----------------------------------------------------------------
    # PULLUPS_ONLY
    # ----------------------------------------------------------------
    def _generate_pullups(
        self,
        ic_name: str,
        ic_db: dict,
        params: dict[str, Any],
    ) -> SubcircuitResult:
        """Generate I2C pull-up resistor network."""
        vdd = params.get("vdd", 3.3)
        speed = params.get("speed", 400_000)
        c_bus = params.get("c_bus", 100e-12)
        ref = params.get("ref", "RP")
        vdd_net = params.get("vdd_net", "VDD_3P3")
        sda_net = params.get("sda_net", "I2C_SDA")
        scl_net = params.get("scl_net", "I2C_SCL")

        # ---- Calculate pull-up resistance ----
        r_pullup_raw = _calc_pullup_resistance(speed, c_bus)
        r_pullup = snap_to_e24(r_pullup_raw)

        # ---- Power / signal pins ----
        power_pins = {
            ic_db["pin_vdd"]: vdd_net,
            ic_db["pin_gnd"]: "GND",
        }
        pin_nets = {
            ic_db["pin_sda"]: sda_net,
            ic_db["pin_scl"]: scl_net,
        }

        # ---- Pull-up straps (VDD to SDA/SCL) ----
        straps = [
            StrapConfig(
                "R_SDA",
                sda_net,
                vdd_net,
                format_resistance(r_pullup),
                FP_0402R,
                role="i2c_pullup",
                presentation="topology_local",
            ),
            StrapConfig(
                "R_SCL",
                scl_net,
                vdd_net,
                format_resistance(r_pullup),
                FP_0402R,
                role="i2c_pullup",
                presentation="topology_local",
            ),
        ]

        # ---- Speed description for annotations ----
        if speed >= 1_000_000:
            speed_str = f"{speed / 1e6:g}MHz"
        else:
            speed_str = f"{speed / 1e3:g}kHz"

        annotations = [
            f"I2C pull-ups: {format_resistance(r_pullup)} to {vdd_net} ({vdd}V)",
            f"Speed: {speed_str}, C_bus={c_bus * 1e12:g}pF",
            f"R = t_rise / (0.8473 * C_bus) = {r_pullup_raw:.0f} -> {format_resistance(r_pullup)}",
        ]

        ic_comp = ComponentDef(
            mpn=ic_name,
            ref_prefix=ic_db["ref_prefix"],
            value="I2C_PULLUPS",
            footprint=ic_db["footprint"],
            description=ic_db["description"],
            category="communication",
            pins=list(ic_db["pins"]),
            power_pins=power_pins,
            pin_nets=pin_nets,
            straps=straps,
            annotations=annotations,
        )
        ic_comp.source_ref = ref
        ic_comp.is_virtual_container = True

        # ---- Boundary ports ----
        ports = [
            BoundaryPort(vdd_net, "input"),
            BoundaryPort("GND", "passive"),
            BoundaryPort(sda_net, "bidirectional"),
            BoundaryPort(scl_net, "bidirectional"),
        ]

        return SubcircuitResult(
            components=[ic_comp],
            boundary_ports=ports,
            annotations=[
                f"I2C pull-ups: {format_resistance(r_pullup)} to {vdd_net}, {speed_str}, C_bus={c_bus * 1e12:g}pF",
            ],
            primary_category="communication",
        )

    # ----------------------------------------------------------------
    # PCA9306 level shifter
    # ----------------------------------------------------------------
    def _generate_level_shifter(
        self,
        ic_name: str,
        ic_db: dict,
        params: dict[str, Any],
    ) -> SubcircuitResult:
        """Generate PCA9306 I2C level-shifter subcircuit."""
        ref = params.get("ref", "U")
        vdd_low_net = params.get("vdd_low_net") or params.get("vdd_net", "VDD_3P3")
        vdd_high_net = params.get("vdd_high_net", "VDD_5V")
        sda_low_net = params.get("sda_low_net") or params.get("sda_net", "I2C_SDA")
        scl_low_net = params.get("scl_low_net") or params.get("scl_net", "I2C_SCL")
        sda_high_net = params.get("sda_high_net", f"SDA_HV_{ref}")
        scl_high_net = params.get("scl_high_net", f"SCL_HV_{ref}")

        # ---- Power pins (PCA9306 has no GND — VREF1 is low-side reference) ----
        power_pins = {
            ic_db["pin_vref1"]: vdd_low_net,
            ic_db["pin_vref2"]: vdd_high_net,
        }

        # ---- Signal pin nets ----
        pin_nets = {
            ic_db["pin_sda1"]: sda_low_net,
            ic_db["pin_scl1"]: scl_low_net,
            ic_db["pin_sda2"]: sda_high_net,
            ic_db["pin_scl2"]: scl_high_net,
        }

        # ---- Bypass caps: 100nF on each VREF ----
        bypass_caps = [
            BypassCap(
                "C_VREF1",
                vdd_low_net,
                "GND",
                "100nF",
                FP_0402C,
                role="decoupling",
                presentation="topology_local",
            ),
            BypassCap(
                "C_VREF2",
                vdd_high_net,
                "GND",
                "100nF",
                FP_0402C,
                role="decoupling",
                presentation="topology_local",
            ),
        ]

        annotations = [
            f"I2C level shifter: {vdd_low_net} <-> {vdd_high_net}",
            f"Low side: {sda_low_net}/{scl_low_net}, High side: {sda_high_net}/{scl_high_net}",
            "PCA9306: internal pull-ups, no external pull-ups needed",
        ]

        ic_comp = ComponentDef(
            mpn=ic_name,
            ref_prefix=ic_db["ref_prefix"],
            value=ic_name,
            footprint=ic_db["footprint"],
            description=ic_db["description"],
            category="communication",
            pins=list(ic_db["pins"]),
            power_pins=power_pins,
            pin_nets=pin_nets,
            bypass_caps=bypass_caps,
            annotations=annotations,
        )
        ic_comp.source_ref = ref

        # ---- Boundary ports ----
        ports = [
            BoundaryPort(vdd_low_net, "input"),
            BoundaryPort(vdd_high_net, "input"),
            BoundaryPort("GND", "passive"),
            BoundaryPort(sda_low_net, "bidirectional"),
            BoundaryPort(scl_low_net, "bidirectional"),
            BoundaryPort(sda_high_net, "bidirectional"),
            BoundaryPort(scl_high_net, "bidirectional"),
        ]

        return SubcircuitResult(
            components=[ic_comp],
            boundary_ports=ports,
            annotations=[
                f"I2C level shifter {ic_name}: {vdd_low_net} <-> {vdd_high_net}",
            ],
            primary_category="communication",
        )
