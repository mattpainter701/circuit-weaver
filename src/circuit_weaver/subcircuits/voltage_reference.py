"""Precision voltage reference subcircuit template.

Generates a complete voltage reference subcircuit with input/output
decoupling and optional trim network.

Supports REF3030 (3.0V, default), REF3033 (3.3V), LM4040-2.5 (shunt),
and LM4040-4.1 (shunt) topologies.

Auto-selects: input decoupling, output filter cap, and (for shunt refs)
the series resistor from supply and load current.
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

# Known voltage reference ICs and their parameters
VREF_IC_DATABASE = LegacyDBProxy("voltage_reference")  # backed by ic_data/*.json (Task 178)


class VoltageReferenceTemplate(SubcircuitTemplate):
    """Precision voltage reference with decoupling and optional shunt resistor."""

    template_type = "voltage_reference"
    description = "Precision voltage reference (series or shunt topology)"
    param_schema = [
        {
            "name": "ic",
            "type": "string",
            "required": False,
            "default": "REF3030",
            "description": "Voltage reference IC MPN",
        },
        {
            "name": "ref",
            "type": "string",
            "required": False,
            "default": "U",
            "description": "Reference designator for the IC",
        },
        {
            "name": "vin_net",
            "type": "string",
            "required": False,
            "default": "VDD_3P3",
            "description": "Input supply net name",
        },
        {
            "name": "vref_net",
            "type": "string",
            "required": False,
            "description": "Output reference net name; defaults to VREF_{vout}V",
        },
        {
            "name": "iload",
            "type": "number",
            "required": False,
            "default": 0.001,
            "description": "Expected load current in amps (used for shunt resistor sizing)",
        },
        {
            "name": "vin",
            "type": "number",
            "required": False,
            "description": "Input voltage in volts (used for shunt resistor calculation)",
        },
    ]

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = []
        ic_name = params.get("ic", "REF3030")
        if ic_name not in VREF_IC_DATABASE:
            errors.append(
                f"Unknown voltage reference IC '{ic_name}'. "
                f"Available: {', '.join(VREF_IC_DATABASE)}"
            )
            return errors

        ic_db = VREF_IC_DATABASE[ic_name]
        if ic_db.get("topology_subtype", ic_db.get("topology", "")) == "series":
            vin = params.get("vin")
            if vin is not None and vin < ic_db.get("vin_min", 0):
                errors.append(
                    f"vin ({vin}V) below minimum {ic_db['vin_min']}V for {ic_name}"
                )
            if vin is not None and vin > ic_db.get("vin_max", float("inf")):
                errors.append(
                    f"vin ({vin}V) above maximum {ic_db['vin_max']}V for {ic_name}"
                )
        return errors

    def generate(self, params: dict[str, Any]) -> SubcircuitResult:
        """Generate a voltage reference subcircuit.

        Optional params:
            ic: str -- IC MPN (default: "REF3030")
            ref: str -- reference designator (default: "U")
            vin_net: str -- input supply net (default: "VDD_3P3")
            vref_net: str -- output reference net (default: derived from vout)
            iload: float -- expected load current in A (default: 0.001)
            vin: float -- input voltage in V (for shunt sizing)
        """
        ic_name = params.get("ic", "REF3030")
        ic_db = VREF_IC_DATABASE.get(ic_name, VREF_IC_DATABASE["REF3030"])
        ref = params.get("ref", "U")
        vin_net = params.get("vin_net", "VDD_3P3")
        vout = ic_db["vout"]
        vref_net = params.get("vref_net") or f"VREF_{vout}V".replace(".", "P")
        iload = params.get("iload", 0.001)
        vin = params.get("vin", 5.0)

        topology = ic_db.get("topology_subtype", ic_db.get("topology", ""))

        if topology == "series":
            return self._generate_series(ic_name, ic_db, ref, vin_net, vref_net, vout)
        else:
            return self._generate_shunt(
                ic_name, ic_db, ref, vin_net, vref_net, vout, vin, iload
            )

    def _generate_series(
        self,
        ic_name: str,
        ic_db: dict,
        ref: str,
        vin_net: str,
        vref_net: str,
        vout: float,
    ) -> SubcircuitResult:
        """Generate a series voltage reference (REF30xx style)."""
        power_pins = {
            ic_db["pin_vin"]: vin_net,
            ic_db["pin_gnd"]: "GND",
            ic_db["pin_vout"]: vref_net,
        }

        bypass_caps = [
            BypassCap(
                "CIN",
                vin_net,
                "GND",
                "100nF",
                FP_0402C,
                role="decoupling",
                presentation="topology_local",
            ),
            BypassCap(
                "COUT",
                vref_net,
                "GND",
                "100nF",
                FP_0402C,
                role="output_filter",
                presentation="topology_local",
            ),
        ]

        annotations = [
            f"Voltage reference {ic_name}: {vout}V series, "
            f"{ic_db['accuracy_pct']}%, {ic_db['tempco_ppm']}ppm/C",
            f"Iq={ic_db['iq_ua']}uA, Iout_max={ic_db['iout_max'] * 1e3:.0f}mA",
        ]

        ic_comp = ComponentDef(
            mpn=ic_name,
            ref_prefix="U",
            value=ic_name,
            footprint=ic_db["footprint"],
            description=ic_db["description"],
            category="power",
            pins=list(ic_db["pins"]),
            power_pins=power_pins,
            pin_nets={},
            bypass_caps=bypass_caps,
            annotations=annotations,
        )
        ic_comp.source_ref = ref

        ports = [
            BoundaryPort(vin_net, "input"),
            BoundaryPort(vref_net, "output"),
            BoundaryPort("GND", "passive"),
        ]

        return SubcircuitResult(
            components=[ic_comp],
            boundary_ports=ports,
            annotations=[
                f"Vref {ic_name}: {vout}V series reference from {vin_net}",
            ],
            primary_category="power",
        )

    def _generate_shunt(
        self,
        ic_name: str,
        ic_db: dict,
        ref: str,
        vin_net: str,
        vref_net: str,
        vout: float,
        vin: float,
        iload: float,
    ) -> SubcircuitResult:
        """Generate a shunt voltage reference (LM4040 style)."""
        # Shunt ref needs a series resistor: R = (Vin - Vref) / (Iz + Iload)
        # Iz must exceed iz_min for regulation; use 1mA or 10x iz_min
        iz_min = ic_db.get("iz_min", 60e-6)
        iz_target = max(iz_min * 10, iz_min + 0.5e-3)
        i_total = iz_target + iload

        r_series_raw = (vin - vout) / i_total if vin > vout else 1e3
        r_series = snap_to_e24(max(r_series_raw, 10.0))

        # Actual cathode current with snapped resistor
        actual_iz = (vin - vout) / r_series - iload if r_series > 0 else iz_target

        pin_nets = {
            ic_db["pin_cathode"]: vref_net,
            ic_db["pin_anode"]: "GND",
        }

        # Series resistor from supply to cathode
        straps = [
            StrapConfig(
                "RS",
                vin_net,
                vref_net,
                format_resistance(r_series),
                FP_0402R,
                role="shunt_bias",
                presentation="topology_local",
            ),
        ]

        bypass_caps = [
            BypassCap(
                "COUT",
                vref_net,
                "GND",
                "100nF",
                FP_0402C,
                role="output_filter",
                presentation="topology_local",
            ),
        ]

        annotations = [
            f"Voltage reference {ic_name}: {vout}V shunt, "
            f"{ic_db['accuracy_pct']}%, {ic_db['tempco_ppm']}ppm/C",
            f"Rs={format_resistance(r_series)}: "
            f"Iz={actual_iz * 1e3:.2f}mA, Iload={iload * 1e3:.1f}mA",
            f"R = ({vin}V - {vout}V) / ({actual_iz * 1e3:.2f}mA + {iload * 1e3:.1f}mA)",
        ]

        ic_comp = ComponentDef(
            mpn=ic_name,
            ref_prefix="D",
            value=ic_name,
            footprint=ic_db["footprint"],
            description=ic_db["description"],
            category="power",
            pins=list(ic_db["pins"]),
            pin_nets=pin_nets,
            explicit_no_connects={str(pin) for pin in ic_db.get("explicit_no_connects", [])},
            straps=straps,
            bypass_caps=bypass_caps,
            annotations=annotations,
        )
        ic_comp.source_ref = ref

        ports = [
            BoundaryPort(vin_net, "input"),
            BoundaryPort(vref_net, "output"),
            BoundaryPort("GND", "passive"),
        ]

        return SubcircuitResult(
            components=[ic_comp],
            boundary_ports=ports,
            annotations=[
                f"Vref {ic_name}: {vout}V shunt reference, "
                f"Rs={format_resistance(r_series)}",
            ],
            primary_category="power",
        )
