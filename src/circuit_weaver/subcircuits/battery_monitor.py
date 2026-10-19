"""Battery fuel gauge subcircuit template.

Generates a complete battery monitor/fuel gauge subcircuit from design
parameters: battery net, cell capacity, IC selection.

Supports MAX17048G+T (default, ModelGauge voltage-based, no Rsense) and
BQ27441-G1A (Impedance Track coulomb counting, requires Rsense).
"""

from __future__ import annotations

from typing import Any

from ..component_db import BypassCap, ComponentDef, StrapConfig
from .base import (
    FP_0402R,
    BoundaryPort,
    LegacyDBProxy,
    SubcircuitResult,
    SubcircuitTemplate,
    cap_footprint,
    format_capacitance,
    format_resistance,
    res_footprint,
    snap_cap,
    snap_to_e96,
)

# Known battery fuel gauge ICs and their parameters
BATTERY_MONITOR_IC_DATABASE = LegacyDBProxy("battery_monitor")  # backed by ic_data/*.json (Task 178)


class BatteryMonitorTemplate(SubcircuitTemplate):
    """Battery fuel gauge with I2C interface."""

    template_type = "battery_monitor"
    description = "Battery fuel gauge / state-of-charge monitor with I2C"
    param_schema = [
        {
            "name": "ic",
            "type": "string",
            "required": False,
            "default": "MAX17048G+T",
            "description": "Fuel gauge IC MPN",
        },
        {
            "name": "ref",
            "type": "string",
            "required": False,
            "default": "U",
            "description": "Reference designator for the IC",
        },
        {
            "name": "bat_net",
            "type": "string",
            "required": False,
            "default": "VBAT",
            "description": "Battery positive net name",
        },
        {
            "name": "cell_capacity_mah",
            "type": "number",
            "required": False,
            "default": 2000,
            "description": "Cell capacity in milliamp-hours",
        },
        {
            "name": "rsense",
            "type": "number",
            "required": False,
            "description": "Sense resistor value in ohms (BQ27441 only)",
        },
        {
            "name": "i2c_bus",
            "type": "string",
            "required": False,
            "default": "I2C",
            "description": "I2C bus name prefix for SDA/SCL nets",
        },
        {
            "name": "irq_net",
            "type": "string",
            "required": False,
            "description": "Optional net for the fuel-gauge active-low alert output",
        },
    ]

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = []
        ic_name = params.get("ic", "MAX17048G+T")
        if ic_name not in BATTERY_MONITOR_IC_DATABASE:
            errors.append(
                f"Unknown fuel gauge IC '{ic_name}'. Supported: {', '.join(BATTERY_MONITOR_IC_DATABASE.keys())}"
            )
        cell_cap = params.get("cell_capacity_mah", 2000)
        if cell_cap is not None and cell_cap <= 0:
            errors.append(f"cell_capacity_mah ({cell_cap}) must be positive")
        rsense = params.get("rsense")
        if rsense is not None and rsense <= 0:
            errors.append(f"rsense ({rsense} ohm) must be positive")
        return errors

    def generate(self, params: dict[str, Any]) -> SubcircuitResult:
        """Generate a battery fuel gauge subcircuit.

        Optional params:
            ic: str — IC MPN (default: "MAX17048G+T")
            ref: str — reference designator for IC (default: "U")
            bat_net: str — battery positive net name (default: "VBAT")
            cell_capacity_mah: int — cell capacity in mAh (default: 2000)
            rsense: float — sense resistor override in ohms (BQ27441 only)
            i2c_bus: str — I2C bus name prefix (default: "I2C")
        """
        ic_name = params.get("ic", "MAX17048G+T")
        ref = params.get("ref", "U")
        bat_net = params.get("bat_net", "VBAT")
        cell_capacity_mah = params.get("cell_capacity_mah", 2000)
        i2c_bus = params.get("i2c_bus", "I2C")
        irq_net = str(params.get("irq_net", "")).strip()

        # Look up IC parameters
        ic_db = BATTERY_MONITOR_IC_DATABASE.get(ic_name, BATTERY_MONITOR_IC_DATABASE["MAX17048G+T"])

        # ---- Net names (unique per instance) ----
        sda_net = f"{i2c_bus}_SDA"
        scl_net = f"{i2c_bus}_SCL"
        # ---- Build IC component ----
        power_pins: dict[str, str] = {
            ic_db["pin_vdd"]: bat_net,
            ic_db["pin_gnd"]: "GND",
        }
        # Wire extra GND pins
        for gnd_pin in ic_db.get("extra_gnd_pins", []):
            power_pins[gnd_pin] = "GND"

        pin_nets: dict[str, str] = {
            ic_db["pin_sda"]: sda_net,
            ic_db["pin_scl"]: scl_net,
        }
        if ic_db.get("pin_cell"):
            # MAX17048 CELL is a battery-positive sense input, not an NC pin.
            pin_nets[ic_db["pin_cell"]] = bat_net
        if irq_net and ic_db.get("pin_alrt"):
            pin_nets[ic_db["pin_alrt"]] = irq_net

        bypass_caps: list[BypassCap] = []
        straps: list[StrapConfig] = []
        annotations: list[str] = []

        # MAX17048 requires a 0.1uF bypass at VDD; other gauges retain their
        # catalog recommendation.
        cvdd_val = snap_cap(100e-9 if ic_db.get("pin_alrt") else 1e-6)
        bypass_caps.append(
            BypassCap(
                "CVDD",
                bat_net,
                "GND",
                format_capacitance(cvdd_val),
                cap_footprint(cvdd_val),
                role="decoupling",
                presentation="topology_local",
            ),
        )

        if ic_name == "MAX17048G+T" or not ic_db["has_rsense"]:
            # --- MAX17048 specific ---
            # CTG pin: wire to GND
            power_pins[ic_db["pin_ctg"]] = "GND"

            # CELL and VDD both connect to the positive battery terminal.

            # QSTRT pull-down: 10k to GND (disable quick start)
            qstrt_net = f"QSTRT_{ref}"
            rqstrt_val = snap_to_e96(10e3)
            pin_nets[ic_db["pin_qstrt"]] = qstrt_net
            straps.append(
                StrapConfig(
                    "RQSTRT",
                    qstrt_net,
                    "GND",
                    format_resistance(rqstrt_val),
                    FP_0402R,
                    role="qstrt_pulldown",
                    presentation="topology_local",
                ),
            )

            annotations.extend(
                [
                    f"Fuel gauge {ic_name}: {ic_db['method']}",
                    f"Cell capacity: {cell_capacity_mah}mAh, I2C addr: {ic_db['i2c_addr']}",
                    "CELL and VDD connect to the positive battery terminal",
                    "QSTRT pulled low (quick start disabled)",
                ]
            )

        else:
            # --- BQ27441 specific (coulomb counting with Rsense) ---
            rsense_val = params.get("rsense", ic_db.get("rsense_default", 0.010))
            rsense_snapped = snap_to_e96(rsense_val)

            srp_net = f"SRP_{ref}"
            srn_net = f"SRN_{ref}"

            pin_nets[ic_db["pin_srp"]] = srp_net
            pin_nets[ic_db["pin_srn"]] = srn_net

            # BAT pin connects to bat_net
            power_pins[ic_db["pin_bat"]] = bat_net

            # BIN pin: battery insertion detection, tie to bat_net
            if ic_db.get("pin_bin"):
                pin_nets[ic_db["pin_bin"]] = bat_net

            # Rsense between SRP and SRN
            # SRP = battery positive side, SRN = load side
            # Power rating: assume 2A max for safety
            imax = 2.0
            pdiss_rsense = imax * imax * rsense_snapped
            rsense_fp = res_footprint(rsense_snapped, pdiss_rsense)

            straps.append(
                StrapConfig(
                    "RSENSE",
                    srp_net,
                    srn_net,
                    format_resistance(rsense_snapped),
                    rsense_fp,
                    role="current_sense",
                    presentation="topology_local",
                ),
            )

            # GPOUT: alert/interrupt output
            gpout_net = f"GPOUT_{ref}"
            if ic_db.get("pin_gpout"):
                pin_nets[ic_db["pin_gpout"]] = gpout_net

            annotations.extend(
                [
                    f"Fuel gauge {ic_name}: {ic_db['method']}",
                    f"Cell capacity: {cell_capacity_mah}mAh, I2C addr: {ic_db['i2c_addr']}",
                    f"Rsense: {format_resistance(rsense_snapped)} (Pdiss @ 2A = {pdiss_rsense:.3f}W)",
                    "SRP = battery side, SRN = load side",
                ]
            )

        ic_comp = ComponentDef(
            mpn=ic_name,
            ref_prefix="U",
            value=ic_name,
            footprint=ic_db["footprint"],
            description=ic_db["description"],
            category="power",
            pins=list(ic_db["pins"]),
            power_pins=power_pins,
            pin_nets=pin_nets,
            bypass_caps=bypass_caps,
            straps=straps,
            annotations=annotations,
            explicit_no_connects=(
                {str(pin) for pin in ic_db.get("no_connect_pins", [])}
                | ({ic_db["pin_alrt"]} if ic_db.get("pin_alrt") and not irq_net else set())
            ),
        )
        ic_comp.source_ref = ref

        # ---- Boundary ports ----
        ports = [
            BoundaryPort(bat_net, "input"),
            BoundaryPort("GND", "passive"),
            BoundaryPort(sda_net, "bidirectional"),
            BoundaryPort(scl_net, "input"),
        ]

        # IC-specific output ports
        if ic_db["has_rsense"] and ic_db.get("pin_gpout"):
            gpout_net = f"GPOUT_{ref}"
            ports.append(BoundaryPort(gpout_net, "output"))
        if irq_net and ic_db.get("pin_alrt"):
            ports.append(BoundaryPort(irq_net, "output"))

        return SubcircuitResult(
            components=[ic_comp],
            boundary_ports=ports,
            annotations=[
                f"Battery monitor {ic_name}: {ic_db['method']}, "
                f"{cell_capacity_mah}mAh cell, I2C addr {ic_db['i2c_addr']}",
            ],
            primary_category="power",
        )
