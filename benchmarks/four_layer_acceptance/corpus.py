"""Deterministic connected four-layer acceptance fixtures.

The builders return ordinary design-spec dictionaries and contracts only. They
do not compile a design, invoke KiCad, create output files, or run placement.
Counts in this corpus are planning targets; the harness records the actual
assembly-manifest count, including generated support components.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _yaml(relative: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


def _limits(
    *, rails: list[float], max_current_a: float, signals: list[str], input_v: tuple[float, float] = (9.0, 15.0)
) -> dict[str, Any]:
    return {
        "declared": {
            "input_voltage_v": list(input_v),
            "ambient_temperature_c": [20, 25],
            "max_board_temperature_c": 60,
            "regulated_rails_v": rails,
            "max_load_current_a": max_current_a,
            "signal_interfaces": signals,
        },
        "unverified": [
            "Electrical limits are design assumptions, not simulation or measurement results.",
            "No firmware, EMC, thermal-rise, RF, or product-safety validation is claimed.",
        ],
    }


def _case(
    case_id: str,
    title: str,
    family: str,
    spec: dict[str, Any],
    *,
    target: int,
    nets: list[str],
    endpoints: list[dict[str, str]],
    components: list[dict[str, str]],
    library: list[str],
    limits: dict[str, Any],
    board_mm: tuple[int, int] | None = None,
    plane_layers: list[str] | None = None,
    reused: bool = False,
    controls: list[dict[str, Any]] | None = None,
    supports: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    board = next((item for item in spec.get("pcb_constraints", []) if item.get("kind") == "placement"), {})
    return {
        "case_id": case_id,
        "title": title,
        "family": family,
        "comparison_fixture_reuse": reused,
        "component_count_target": target,
        "copper_layers": 4,
        "prototype_profile": {"clearance_mm": 0.127, "minimum_drill_mm": 0.2},
        "ground_plane": {"net": "GND", "layers": plane_layers or ["In1.Cu"]},
        "spec": spec,
        "expectations": {
            "required_nets": nets,
            "required_endpoints": endpoints,
            "required_components": components,
            "required_library_parts": library,
            "required_supports": supports or [],
            "board_constraints": {
                "width_mm": board_mm[0] if board_mm else board.get("width_mm"),
                "height_mm": board_mm[1] if board_mm else board.get("height_mm"),
            },
            "mechanical_requirements": spec.get("mechanical_requirements", {}),
        },
        "declared_limits": limits,
        "controls": controls or [],
    }


def _negative_controls(*, src: tuple[str, str], dst: tuple[str, str], net: str) -> list[dict[str, Any]]:
    return [
        {
            "control_id": "swapped_endpoint_net",
            "kind": "connectivity",
            "expected_result": "reject",
            "mutation": {
                "source": {"ref": src[0], "pad": src[1]},
                "endpoint": {"ref": dst[0], "pad": dst[1]},
                "replace_net": f"{net}_WRONG",
            },
        },
        {
            "control_id": "missing_library_part",
            "kind": "library",
            "expected_result": "reject_or_unverified",
            "mutation": {"remove_library_entry": "one required component footprint or symbol"},
        },
        {
            "control_id": "wrong_stackup",
            "kind": "stackup",
            "expected_result": "reject",
            "mutation": {"copper_layers": 2, "ground_plane_layers": ["In1.Cu"]},
        },
        {
            "control_id": "weak_electrical_limits",
            "kind": "limits",
            "expected_result": "report_as_unverified",
            "mutation": {"remove_declared": ["input_voltage_v", "max_load_current_a", "ambient_temperature_c"]},
        },
    ]


def _small_iot_node() -> tuple[dict[str, Any], list[dict[str, str]]]:
    spec = {
        "project": "FourLayer_Connected_IoT_Environment_Node",
        "company": "Circuit Weaver Acceptance Corpus",
        "version": "1.0",
        "description": (
            "Compact wired IoT environmental node with a regulated rail, STM32 controller, BME280 sensor, "
            "and service connectors for I2C and SWD. The board is a functional connected prototype; "
            "firmware and environmental calibration are outside this fixture."
        ),
        "pcb_constraints": [{"kind": "placement", "target": "board", "width_mm": 55, "height_mm": 42}],
        "power": [
            {
                "type": "ldo",
                "ic": "AP2112K-3.3",
                "ref": "U1",
                "vin": 5,
                "vout": 3.3,
                "iout": 0.5,
                "vin_net": "VBUS_5V",
                "rail_name": "VDD_3P3",
            },
        ],
        "digital": [
            {
                "ic": "STM32F103C8T6",
                "ref": "U2",
                "pin_nets_extra": {"10": "I2C_SCL", "11": "I2C_SDA", "34": "SWDIO", "37": "SWCLK"},
                "no_connects": [
                    "2",
                    "3",
                    "4",
                    "5",
                    "6",
                    "12",
                    "13",
                    "14",
                    "15",
                    "16",
                    "17",
                    "18",
                    "19",
                    "20",
                    "21",
                    "22",
                    "25",
                    "26",
                    "27",
                    "28",
                    "29",
                    "30",
                    "31",
                    "32",
                    "33",
                    "38",
                    "39",
                    "40",
                    "41",
                    "42",
                    "43",
                    "45",
                    "46",
                ],
            }
        ],
        "sensors": [
            {
                "ic": "BME280",
                "ref": "U3",
                "vdd_net": "VDD_3P3",
                "pin_nets_extra": {"3": "I2C_SDA", "4": "I2C_SCL", "5": "VDD_3P3"},
                "power_pins_extra": {"1": "GND", "6": "VDD_3P3", "7": "GND", "8": "VDD_3P3"},
            }
        ],
        "connectors": [
            {"ic": "USB-C-PWR", "ref": "J1"},
            {"type": "connector", "ic": "PIN_HEADER_4P", "ref": "J2", "signal_nets": "I2C_SCL,I2C_SDA,VDD_3P3,GND"},
            {"type": "connector", "ic": "PIN_HEADER_4P", "ref": "J3", "signal_nets": "SWDIO,SWCLK,VDD_3P3,GND"},
        ],
        "communication": [
            {
                "type": "i2c_bus",
                "ic": "PULLUPS_ONLY",
                "ref": "RP1",
                "vdd": 3.3,
                "speed": 100000,
                "c_bus": 5.0e-11,
                "vdd_net": "VDD_3P3",
                "sda_net": "I2C_SDA",
                "scl_net": "I2C_SCL",
            }
        ],
    }
    return spec, [
        {"ref": "U2", "pad": "10", "net": "I2C_SCL"},
        {"ref": "U2", "pad": "11", "net": "I2C_SDA"},
        {"ref": "U3", "pad": "4", "net": "I2C_SCL"},
        {"ref": "U3", "pad": "3", "net": "I2C_SDA"},
        {"ref": "J2", "pad": "1", "net": "I2C_SCL"},
        {"ref": "J2", "pad": "2", "net": "I2C_SDA"},
        {"ref": "U2", "pad": "34", "net": "SWDIO"},
        {"ref": "U2", "pad": "37", "net": "SWCLK"},
        {"ref": "J3", "pad": "1", "net": "SWDIO"},
        {"ref": "J3", "pad": "2", "net": "SWCLK"},
    ]


def build_cases() -> list[dict[str, Any]]:
    """Return a deterministic set of connected, four-layer acceptance cases."""
    from benchmarks.unseen_scale import build_stress_fixtures as scale

    cases: list[dict[str, Any]] = []

    tiny, tiny_eps = _small_iot_node()
    cases.append(
        _case(
            "iot_environment_node",
            "Connected IoT environment node",
            "iot_sensor_controller",
            tiny,
            target=55,
            nets=["VBUS_5V", "VDD_3P3", "GND", "I2C_SCL", "I2C_SDA", "SWDIO", "SWCLK"],
            endpoints=tiny_eps,
            components=[
                {"ref": "U2", "ic": "STM32F103C8T6"},
                {"ref": "U3", "ic": "BME280"},
                {"ref": "J2", "ic": "PIN_HEADER_4P"},
            ],
            library=["STM32F103C8T6", "BME280", "AP2112K-3.3", "USB-C-PWR", "PIN_HEADER_4P"],
            limits=_limits(
                rails=[3.3],
                max_current_a=0.35,
                signals=["I2C standard mode", "SWD service header"],
                input_v=(4.75, 5.25),
            ),
            controls=_negative_controls(src=("U2", "10"), dst=("U3", "4"), net="I2C_SCL"),
        )
    )

    # Bootstrap case with no plated through-hole component anchor for the
    # internal GND plane. The harness can distinguish this condition before
    # route-added ground vias exist.
    smd_node = copy.deepcopy(tiny)
    smd_node["project"] = "FourLayer_Ground_Anchorless_IoT_Bootstrap_Node"
    smd_node["description"] = (
        "USB-C powered I2C environment node with no plated through-hole pad directly on GND, "
        "used to expose inner-plane fill bootstrap behavior."
    )
    smd_node["pcb_constraints"] = [{"kind": "placement", "target": "board", "width_mm": 55, "height_mm": 42}]
    smd_node["connectors"] = [{"ic": "USB-C-PWR", "ref": "J1"}]
    smd_node["power"] = [
        {
            "type": "ldo",
            "ic": "AP2112K-3.3",
            "ref": "U1",
            "vin": 5,
            "vout": 3.3,
            "iout": 0.5,
            "vin_net": "VBUS_5V",
            "rail_name": "VDD_3P3",
        }
    ]
    # KiCad's installed GCT_USB4085 footprint has plated pads, so this is
    # explicitly a ground-anchorless mutation, not an all-SMD claim. Its USB-C
    # GND pins are separated as POWER_RETURN and joined to GND via surface-mount
    # zero-ohm link R5; no plated pad itself is assigned to the plane net.
    smd_node["connectors"] = [
        {
            "ic": "USB-C-PWR",
            "ref": "J1",
            "power_pins_extra": {
                "A1": "POWER_RETURN",
                "A12": "POWER_RETURN",
                "B1": "POWER_RETURN",
                "B12": "POWER_RETURN",
            },
        },
    ]
    smd_mcu = next(item for item in smd_node["digital"] if item.get("ref") == "U2")
    smd_mcu["pin_nets_extra"] = {pin: net for pin, net in smd_mcu["pin_nets_extra"].items() if pin not in {"34", "37"}}
    smd_mcu["no_connects"] = sorted({str(pin) for pin in smd_mcu.get("no_connects", [])} | {"34", "37"}, key=int)
    smd_node.setdefault("passives", []).append(
        {
            "ref": "R5",
            "value": "0R",
            "footprint": "Resistor_SMD:R_0402_1005Metric",
            "pin_nets_extra": {"1": "POWER_RETURN", "2": "GND"},
        }
    )
    smd_node["mechanical_requirements"] = {
        "all_smd": False,
        "plated_through_hole_gnd_anchors": 0,
        "note": (
            "The connector has plated pads, but none is directly assigned to GND; "
            "POWER_RETURN joins GND through SMD link R5."
        ),
    }
    cases.append(
        _case(
            "iot_environment_node_ground_anchorless",
            "IoT node with no plated GND anchor",
            "iot_sensor_controller",
            smd_node,
            target=50,
            nets=["VBUS_5V", "VDD_3P3", "GND", "POWER_RETURN", "I2C_SCL", "I2C_SDA"],
            endpoints=[
                {"ref": "U2", "pad": "10", "net": "I2C_SCL"},
                {"ref": "U3", "pad": "4", "net": "I2C_SCL"},
                {"ref": "U2", "pad": "11", "net": "I2C_SDA"},
                {"ref": "U3", "pad": "3", "net": "I2C_SDA"},
                {"ref": "J1", "pad": "A1", "net": "POWER_RETURN"},
                {"ref": "R5", "pad": "1", "net": "POWER_RETURN"},
                {"ref": "R5", "pad": "2", "net": "GND"},
            ],
            components=[
                {"ref": "J1", "ic": "USB-C-PWR"},
                {"ref": "U2", "ic": "STM32F103C8T6"},
                {"ref": "U3", "ic": "BME280"},
            ],
            library=[
                "USB-C-PWR",
                "STM32F103C8T6",
                "BME280",
                "AP2112K-3.3",
                "PULLUPS_ONLY",
                "Resistor_SMD:R_0402_1005Metric",
            ],
            limits=_limits(rails=[3.3], max_current_a=0.35, signals=["I2C standard mode"], input_v=(4.75, 5.25)),
            reused=True,
            controls=_negative_controls(src=("U2", "10"), dst=("U3", "4"), net="I2C_SCL"),
        )
    )

    # Scaled relay channels use the proven 74HC595 -> ULN2003 -> coil connector
    # builder. The circuit topology is a comparison control; channel counts and
    # resulting assemblies are new corpus variants.
    for channels, target, dual_plane in [(8, 36, False), (48, 150, False), (288, 466, True)]:
        spec = scale.build_serial_relay_output_card(channels)
        spec["project"] = f"FourLayer_Relay_Output_{channels}"
        spec["description"] = (
            f"Four-layer acceptance variant of the connected {channels}-channel serialized relay output card."
        )
        spec["pcb_constraints"] = [
            {"kind": "placement", "target": "board", "width_mm": max(70, channels * 1.6), "height_mm": 75}
        ]
        if channels >= 161:
            # The stock fixture reserves J162 for SWD; high-channel variants
            # would collide with that reference, so move this service header.
            spec["connectors"][0]["ref"] = "J9000"
        case_id = f"relay_output_{channels}"
        src = ("U100", "15")
        dst = ("U200", "1")
        cases.append(
            _case(
                case_id,
                f"Serialized relay output card ({channels} channels)",
                "serialized_relay_output",
                spec,
                target=target,
                nets=[
                    "VIN_12V",
                    "VDD_3P3",
                    "GND",
                    "SHIFT_DATA",
                    "SHIFT_CLOCK",
                    "SHIFT_LATCH",
                    "COIL_DRV_U200_1",
                    "LOAD_U200_1",
                ],
                endpoints=[
                    {"ref": "U100", "pad": "15", "net": "COIL_DRV_U200_1"},
                    {"ref": "U200", "pad": "1", "net": "COIL_DRV_U200_1"},
                    {"ref": "U200", "pad": "16", "net": "LOAD_U200_1"},
                    {"ref": "J2", "pad": "2", "net": "LOAD_U200_1"},
                ],
                components=[
                    {"ref": "U100", "ic": "74HC595D"},
                    {"ref": "U200", "ic": "ULN2003A"},
                    {"ref": "J2", "ic": "PIN_HEADER_2P"},
                ],
                library=["74HC595D", "ULN2003A", "AP62300", "PIN_HEADER_2P"],
                limits=_limits(
                    rails=[3.3], max_current_a=0.08, signals=["serial shift control", "switched 12 V coil return"]
                ),
                plane_layers=["In1.Cu", "In2.Cu"] if dual_plane else ["In1.Cu"],
                reused=True,
                controls=_negative_controls(src=src, dst=dst, net="COIL_DRV_U200_1"),
            )
        )

    # Multichannel setpoint outputs exercise analog routing and generated
    # filter support. Keep even channel counts so every DAC output is used.
    for channels, target in [(24, 150), (80, 480)]:
        spec = scale.build_multichannel_analog_setpoint_card(channels)
        spec["project"] = f"FourLayer_Analog_Setpoint_{channels}"
        spec["description"] = (
            f"Four-layer acceptance variant of the connected {channels}-channel filtered DAC setpoint card."
        )
        spec["pcb_constraints"] = [
            {"kind": "placement", "target": "board", "width_mm": max(100, channels * 2), "height_mm": 95}
        ]
        if channels >= 74:
            # J74 is the stock SWD header and overlaps output connector refs
            # once the channel ladder grows beyond 72 channels.
            spec["connectors"][-1]["ref"] = "J9000"
        n = "SETPOINT_01"
        cases.append(
            _case(
                f"analog_setpoint_{channels}",
                f"Filtered analog setpoint card ({channels} channels)",
                "analog_setpoint_output",
                spec,
                target=target,
                nets=["VDD_3P3", "DAC_REF_3V3", "SPI_MOSI", "SPI_SCK", "DAC_SYNC_01", "VOUTA_RAW_U30", n, "GND"],
                endpoints=[
                    {"ref": "U10", "pad": "28", "net": "SPI_MOSI"},
                    {"ref": "U30", "pad": "7", "net": "SPI_MOSI"},
                    {"ref": "U14", "pad": "15", "net": "DAC_SYNC_01"},
                    {"ref": "U30", "pad": "4", "net": "VOUTA_RAW_U30"},
                    {"ref": "J2", "pad": "1", "net": "SETPOINT_01"},
                ],
                components=[
                    {"ref": "U10", "ic": "STM32F103C8T6"},
                    {"ref": "U30", "ic": "DAC8552IDGK"},
                    {"ref": "U13", "ic": "REF3033"},
                ],
                library=["STM32F103C8T6", "DAC8552IDGK", "REF3033", "74HC595D", "AP62300"],
                limits=_limits(
                    rails=[5.0, 3.3], max_current_a=0.02, signals=["SPI", "0-3.3 V high-impedance setpoint"]
                ),
                reused=True,
                controls=_negative_controls(src=("U30", "4"), dst=("J2", "1"), net="SETPOINT_01"),
                supports=[
                    {
                        "owner_ref": "U30",
                        "role": "output_filter",
                        "source_kind": "strap",
                        "net1": "VOUTA_RAW_U30",
                        "net2": "SETPOINT_01",
                    },
                    {
                        "owner_ref": "U30",
                        "role": "output_filter",
                        "source_kind": "bypass",
                        "net1": "SETPOINT_01",
                        "net2": "GND",
                    },
                ],
            )
        )

    # Two independent connected controls broaden family coverage beyond the
    # parameterized relay/DAC ladder. Their inherited behaviors are marked.
    gateway = _yaml("benchmarks/unseen_scale/cases/distributed_facility_gateway.yaml")
    gateway["project"] = "FourLayer_Facility_Fieldbus_Gateway"
    gateway["pcb_constraints"] = [{"kind": "placement", "target": "board", "width_mm": 180, "height_mm": 135}]
    cases.append(
        _case(
            "facility_fieldbus_gateway",
            "Facility sensor and fieldbus gateway",
            "fieldbus_gateway",
            gateway,
            target=236,
            nets=["VIN_12V", "VDD_5V", "VDD_3P3", "SPI_SCK", "FIELD_A_TX", "FIELD_A_RX", "CAN_TXD", "CAN_RXD"],
            endpoints=[
                {"ref": "U3", "pad": "26", "net": "SPI_SCK"},
                {"ref": "U8", "pad": "13", "net": "SPI_SCK"},
                {"ref": "U3", "pad": "30", "net": "FIELD_A_TX"},
                {"ref": "U4", "pad": "4", "net": "FIELD_A_TX"},
                {"ref": "U3", "pad": "33", "net": "CAN_TXD"},
                {"ref": "U7", "pad": "1", "net": "CAN_TXD"},
            ],
            components=[
                {"ref": "U3", "ic": "STM32F103C8T6"},
                {"ref": "U8", "ic": "MCP3208-CI/SL"},
                {"ref": "U4", "ic": "SP3485EN-L/TR"},
            ],
            library=["STM32F103C8T6", "MCP3208-CI/SL", "SP3485EN-L/TR", "SN65HVD230", "AP62300"],
            limits=_limits(rails=[5.0, 3.3], max_current_a=0.5, signals=["SPI", "RS-485", "CAN"]),
            reused=True,
            controls=_negative_controls(src=("U3", "30"), dst=("U4", "4"), net="FIELD_A_TX"),
        )
    )

    daq = _yaml("benchmarks/general_scale/cases/precision_control_daq.yaml")
    daq["project"] = "FourLayer_Precision_Acquisition_Control"
    daq["pcb_constraints"] = [{"kind": "placement", "target": "board", "width_mm": 150, "height_mm": 110}]
    cases.append(
        _case(
            "precision_acquisition_control",
            "Bridge and load precision acquisition",
            "precision_acquisition",
            daq,
            target=180,
            nets=[
                "VDD_5V",
                "VDD_3P3",
                "ADC_CS_N",
                "SPI_SCK",
                "SPI_MOSI",
                "SPI_MISO",
                "CH0_U5",
                "FILT_CH0_U5",
                "BRIDGE0_P",
                "BRIDGE0_N",
            ],
            endpoints=[
                {"ref": "U4", "pad": "25", "net": "ADC_CS_N"},
                {"ref": "U5", "pad": "10", "net": "ADC_CS_N"},
                {"ref": "U7", "pad": "3", "net": "BRIDGE0_P"},
                {"ref": "J2", "pad": "1", "net": "BRIDGE0_P"},
                {"ref": "U7", "pad": "6", "net": "CH0_U5"},
                {"ref": "U5", "pad": "1", "net": "FILT_CH0_U5"},
            ],
            components=[
                {"ref": "U4", "ic": "STM32F103C8T6"},
                {"ref": "U5", "ic": "MCP3208-CI/SL"},
                {"ref": "U7", "ic": "INA128PA"},
            ],
            library=["STM32F103C8T6", "MCP3208-CI/SL", "INA128PA", "REF3033", "DAC8552IDGK"],
            limits=_limits(
                rails=[5.0, 3.3, 1.8],
                max_current_a=0.3,
                signals=["SPI", "bridge differential input", "buffered setpoint"],
            ),
            reused=True,
            controls=_negative_controls(src=("U7", "3"), dst=("J2", "1"), net="BRIDGE0_P"),
            supports=[
                {
                    "owner_ref": "U5",
                    "role": "input_filter",
                    "source_kind": "strap",
                    "net1": "CH0_U5",
                    "net2": "FILT_CH0_U5",
                },
                {
                    "owner_ref": "U5",
                    "role": "input_filter",
                    "source_kind": "bypass",
                    "net1": "FILT_CH0_U5",
                    "net2": "GND",
                },
            ],
        )
    )

    from benchmarks.diverse_systems.build_diverse_trials import build_sdr_clock_carrier

    sdr, _architecture = build_sdr_clock_carrier(ROOT)
    sdr["project"] = "FourLayer_SDR_Clock_Carrier_Control"
    cases.append(
        _case(
            "sdr_clock_carrier_control",
            "SDR clock and control carrier (reused comparison)",
            "clock_distribution_control",
            sdr,
            target=240,
            nets=[
                "VDD_5V",
                "VDD_3P3",
                "VDD_1P8",
                "SCLK_U90",
                "SDIO_U90",
                "EXT_REF_P",
                "EXT_REF_N",
                "SLOT0_CLK0_P",
                "SLOT0_CLK0_N",
            ],
            endpoints=[
                {"ref": "U3", "pad": "43", "net": "SCLK_U90"},
                {"ref": "U90", "pad": "14", "net": "SCLK_U90"},
                {"ref": "J90", "pad": "1", "net": "EXT_REF_P"},
                {"ref": "U90", "pad": "1", "net": "EXT_REF_P"},
                {"ref": "U90", "pad": "22", "net": "SLOT0_CLK0_P"},
                {"ref": "J91", "pad": "1", "net": "SLOT0_CLK0_P"},
            ],
            components=[
                {"ref": "U3", "ic": "STM32F103C8T6"},
                {"ref": "U90", "ic": "AD9528"},
                {"ref": "J91", "ic": "PIN_HEADER_2P"},
            ],
            library=["STM32F103C8T6", "AD9528", "AP62300", "MCP1700-1802E", "PIN_HEADER_2P"],
            limits=_limits(
                rails=[5.0, 3.3, 1.8],
                max_current_a=0.4,
                signals=["SPI control", "external differential clock pairs"],
                input_v=(9.0, 15.0),
            ),
            board_mm=(260, 190),
            reused=True,
            controls=_negative_controls(src=("U3", "43"), dst=("U90", "14"), net="SCLK_U90"),
        )
    )

    # Avoid callers accidentally sharing nested mutable structures between runs.
    return copy.deepcopy(cases)
