"""Deterministically materialize the high-count unseen scale fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from circuit_weaver.assembly_manifest import build_assembly_manifest
from circuit_weaver.design_loader import compile_design_ir

ROOT = Path(__file__).resolve().parent


def _base(project: str, description: str, width: int, height: int) -> dict[str, Any]:
    return {
        "project": project,
        "company": "Circuit Weaver Benchmark",
        "version": "1.0",
        "description": description,
        "pcb_constraints": [{"kind": "placement", "target": "board", "width_mm": width, "height_mm": height}],
        "power": [
            {
                "type": "connector",
                "ic": "BARREL_JACK_2.1MM",
                "ref": "J1",
                "positive_net": "VIN_12V",
                "negative_net": "GND",
                "decoupling": True,
            },
            {
                "type": "buck",
                "ic": "AP62300",
                "ref": "U1",
                "vin": 12,
                "vout": 3.3,
                "iout": 2,
                "vin_net": "VIN_12V",
                "rail_name": "VDD_3P3",
            },
        ],
    }


def _stm32_io(pin_nets: dict[str, str]) -> dict[str, Any]:
    all_gpio = {
        "10",
        "11",
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
    }
    used = set(pin_nets)
    return {
        "ic": "STM32F103C8T6",
        "ref": "U2",
        "pin_nets_extra": pin_nets,
        "no_connects": sorted((all_gpio - used) | {"2", "3", "4", "5", "6"}),
    }


def _shift_register(ref: str, serial_in: str, output_nets: list[str], *, serial_out: str | None) -> dict[str, Any]:
    output_pads = ["15", "1", "2", "3", "4", "5", "6", "7"]
    extra = {"10": "VDD_3P3", "11": "SHIFT_CLOCK", "12": "SHIFT_LATCH", "13": "GND", "14": serial_in}
    no_connects = []
    for pad, net in zip(output_pads, output_nets, strict=False):
        if net:
            extra[pad] = net
        else:
            no_connects.append(pad)
    if serial_out:
        extra["9"] = serial_out
    else:
        no_connects.append("9")
    return {
        "ic": "74HC595D",
        "ref": ref,
        "vdd_net": "VDD_3P3",
        "pin_nets_extra": extra,
        "power_pins_extra": {"8": "GND", "16": "VDD_3P3"},
        "no_connects": no_connects,
    }


def build_serial_relay_output_card(channels: int = 160) -> dict[str, Any]:
    """160 switched 12 V coil returns, serialized to MCU and transistor arrays."""
    spec = _base(
        "UnseenScale_Serial_Relay_Output_Card",
        (
            "12 V industrial relay output card. An STM32 shifts channel states through cascaded 74HC595 "
            "registers; each output drives one ULN2003A sink channel and terminates at a dedicated keyed "
            "relay-coil connector. The ULN2003A includes inductive flyback diodes. OUTPUT_ENABLE_N is "
            "pulled high during reset so all sinks start disabled. This card has no fieldbus and uses the "
            "shift chain for output control."
        ),
        340,
        260,
    )
    sr_count = (channels + 7) // 8
    driver_count = (channels + 6) // 7
    spec["digital"] = [_stm32_io({"10": "SHIFT_DATA", "11": "SHIFT_CLOCK", "12": "SHIFT_LATCH"})]
    for sr in range(sr_count):
        output_nets = []
        for bit in range(8):
            idx = sr * 8 + bit
            if idx >= channels:
                output_nets.append("")
            else:
                output_nets.append(f"COIL_DRV_U{200 + idx // 7}_{idx % 7 + 1}")
        spec["digital"].append(
            _shift_register(
                f"U{100 + sr}",
                "SHIFT_DATA" if sr == 0 else f"SHIFT_QH_{sr}",
                output_nets,
                serial_out=f"SHIFT_QH_{sr + 1}" if sr + 1 < sr_count else None,
            )
        )
    for drv in range(driver_count):
        used = min(7, channels - 7 * drv)
        spec["power"].append(
            {
                "type": "relay_driver",
                "ic": "ULN2003A",
                "ref": f"U{200 + drv}",
                "vcoil": 12,
                "icoil": 0.08,
                "vcoil_net": "VIN_12V",
                "drive_net": f"COIL_DRV_U{200 + drv}",
                "channels_used": used,
                "vdrive": 3.3,
            }
        )
    spec["passives"] = [
        {
            "ref": "R1",
            "value": "10k",
            "footprint": "Resistor_SMD:R_0402_1005Metric",
            "pin_nets_extra": {"1": "VDD_3P3", "2": "OUTPUT_ENABLE_N"},
        }
    ]
    spec["digital"][0]["pin_nets_extra"]["13"] = "OUTPUT_ENABLE_N"
    spec["digital"][0]["no_connects"].remove("13")
    spec["connectors"] = []
    spec["connectors"].append(
        {"type": "connector", "ic": "PIN_HEADER_4P", "ref": "J162", "signal_nets": "VDD_3P3,GND,SWDIO,SWCLK"}
    )
    for idx in range(channels):
        drv, channel = divmod(idx, 7)
        spec["connectors"].append(
            {
                "type": "connector",
                "ic": "PIN_HEADER_2P",
                "ref": f"J{idx + 2}",
                "signal_nets": f"VIN_12V,LOAD_U{200 + drv}_{channel + 1}",
            }
        )
    return spec


def build_multichannel_analog_setpoint_card(channels: int = 72) -> dict[str, Any]:
    """72 filtered voltage outputs from dual SPI DACs selected by a shift chain."""
    dac_count = (channels + 1) // 2
    sr_count = (dac_count + 7) // 8
    spec = _base(
        "UnseenScale_Multichannel_Analog_Setpoint_Card",
        (
            "72-channel analog setpoint card for a process controller. Thirty-six dual 16-bit SPI DACs "
            "share data and clock; a cascaded 74HC595 bank selects each DAC SYNC line. Every DAC output "
            "has its generated RC filter and a dedicated connector for a high-impedance 0–3.3 V setpoint "
            "input. A shared 3.3 V precision reference drives DAC VREF pins. Firmware updates one selected "
            "converter at a time; output accuracy and load-drive capability are outside this workflow fixture."
        ),
        300,
        220,
    )
    spec["power"][1].update({"vout": 5, "rail_name": "VDD_5V"})
    spec["power"].append(
        {
            "type": "buck",
            "ic": "AP62300",
            "ref": "U2",
            "vin": 5,
            "vout": 3.3,
            "iout": 1,
            "vin_net": "VDD_5V",
            "rail_name": "VDD_3P3",
        }
    )
    io = {"10": "SHIFT_DATA", "11": "SHIFT_CLOCK", "12": "SHIFT_LATCH", "26": "SPI_SCK", "28": "SPI_MOSI"}
    mcu = _stm32_io(io)
    mcu["ref"] = "U10"
    spec["digital"] = [mcu]
    spec["analog"] = [
        {
            "type": "voltage_reference",
            "ic": "REF3033",
            "params": {"ic": "REF3033"},
            "ref": "U13",
            "vin": 5,
            "vin_net": "VDD_5V",
            "vref_net": "DAC_REF_3V3",
            "iload": 0.02,
            "interfaces": [{"name": "DAC_REF_3V3", "direction": "output"}],
        }
    ]
    for sr in range(sr_count):
        output_nets = [f"DAC_SYNC_{sr * 8 + bit + 1:02d}" if sr * 8 + bit < dac_count else "" for bit in range(8)]
        spec["digital"].append(
            _shift_register(
                f"U{14 + sr}",
                "SHIFT_DATA" if sr == 0 else f"SHIFT_QH_{sr}",
                output_nets,
                serial_out=f"SHIFT_QH_{sr + 1}" if sr + 1 < sr_count else None,
            )
        )
    spec["connectors"] = []
    for dac_idx in range(dac_count):
        ref = f"U{30 + dac_idx}"
        a_idx = 2 * dac_idx
        output_a = f"SETPOINT_{a_idx + 1:02d}"
        output_b = f"SETPOINT_{a_idx + 2:02d}" if a_idx + 1 < channels else f"NC_{ref}_B"
        spec["analog"].append(
            {
                "type": "dac",
                "ic": "DAC8552IDGK",
                "params": {"ic": "DAC8552IDGK"},
                "ref": ref,
                "vdd_net": "VDD_3P3",
                "vref": "DAC_REF_3V3",
                "din_net": "SPI_MOSI",
                "sclk_net": "SPI_SCK",
                "sync_net": f"DAC_SYNC_{dac_idx + 1:02d}",
                "output_net": output_a,
                "output_b_net": output_b,
                "interfaces": [{"name": "DAC_REF_3V3", "direction": "input"}],
            }
        )
        spec["connectors"].append(
            {"type": "connector", "ic": "PIN_HEADER_2P", "ref": f"J{2 + a_idx}", "signal_nets": f"{output_a},GND"}
        )
        if a_idx + 1 < channels:
            spec["connectors"].append(
                {"type": "connector", "ic": "PIN_HEADER_2P", "ref": f"J{3 + a_idx}", "signal_nets": f"{output_b},GND"}
            )
    spec["connectors"].append(
        {"type": "connector", "ic": "PIN_HEADER_4P", "ref": "J74", "signal_nets": "VDD_3P3,GND,SWDIO,SWCLK"}
    )
    return spec


def _path(path_id: str, net: str, source: tuple[str, str], *loads: tuple[str, str]) -> dict[str, Any]:
    endpoints = [{"ref": ref, "pad": pad} for ref, pad in (source, *loads)]
    return {
        "id": path_id,
        "source": {"ref": source[0], "pad": source[1]},
        "loads": [{"ref": ref, "pad": pad} for ref, pad in loads],
        "segments": [{"net": net, "endpoints": endpoints}],
        "through": [],
    }


def _field_gateway_paths() -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    for channel, u, j, pins, diode_a, diode_b in [
        ("A", "U4", "J2", ("30", "31", "29"), "D2", "D3"),
        ("B", "U5", "J3", ("12", "13", "11"), "D4", "D5"),
        ("C", "U6", "J4", ("21", "22", "20"), "D6", "D7"),
        ("CAN", "U7", "J5", ("33", "32"), "D8", "D9"),
        ("D", "U25", "J14", ("10", "14", "15"), "D10", "D11"),
        ("E", "U26", "J15", ("16", "17", "18"), "D12", "D13"),
        ("F", "U27", "J16", ("19", "38", "39"), "D14", "D15"),
        ("G", "U28", "J17", ("40", "41", "42"), "D16", "D17"),
    ]:
        if channel == "CAN":
            paths.extend(
                [
                    _path("can_controller_tx", "CAN_TXD", ("U3", pins[0]), (u, "1")),
                    _path("can_controller_rx", "CAN_RXD", (u, "4"), ("U3", pins[1])),
                    _path("can_bus_h", "FIELD_CAN_H_U7", (u, "7"), (j, "1"), (diode_a, "1")),
                    _path("can_bus_l", "FIELD_CAN_L_U7", (u, "6"), (j, "2"), (diode_b, "1")),
                ]
            )
            continue
        prefix = f"FIELD_{channel}"
        paths.extend(
            [
                _path(f"{channel.lower()}_tx", f"{prefix}_TX", ("U3", pins[0]), (u, "4")),
                _path(f"{channel.lower()}_rx", f"{prefix}_RX", (u, "1"), ("U3", pins[1])),
                _path(f"{channel.lower()}_de", f"{prefix}_DE", ("U3", pins[2]), (u, "3")),
                _path(f"{channel.lower()}_bus_a", f"{prefix}_RS485_A_{u}", (u, "6"), (j, "1"), (diode_a, "1")),
                _path(f"{channel.lower()}_bus_b", f"{prefix}_RS485_B_{u}", (u, "7"), (j, "2"), (diode_b, "1")),
            ]
        )
    paths.extend(
        [
            _path("adc_chip_select", "SPI_CS_N", ("U3", "25"), ("U8", "10")),
            _path("adc_spi_clock", "SPI_SCK", ("U3", "26"), ("U8", "13")),
            _path("adc_spi_mosi", "SPI_MOSI", ("U3", "28"), ("U8", "11")),
            _path("adc_spi_miso", "SPI_MISO", ("U8", "12"), ("U3", "27")),
        ]
    )
    return paths


def _relay_paths(channels: int = 160) -> list[dict[str, Any]]:
    paths = []
    output_pads = ["15", "1", "2", "3", "4", "5", "6", "7"]
    for idx in range(channels):
        sr_idx, bit = divmod(idx, 8)
        driver_idx, channel = divmod(idx, 7)
        sr, driver, connector = f"U{100 + sr_idx}", f"U{200 + driver_idx}", f"J{2 + idx}"
        input_pad, output_pad = str(channel + 1), str(16 - channel)
        drive_net, load_net = f"COIL_DRV_{driver}_{channel + 1}", f"LOAD_{driver}_{channel + 1}"
        sr_pad = output_pads[bit]
        connector_pad = "2"
        paths.append(
            {
                "id": f"relay_output_{idx + 1:03d}",
                "source": {"ref": sr, "pad": sr_pad},
                "loads": [{"ref": connector, "pad": connector_pad}],
                "segments": [
                    {"net": drive_net, "endpoints": [{"ref": sr, "pad": sr_pad}, {"ref": driver, "pad": input_pad}]},
                    {
                        "net": load_net,
                        "endpoints": [{"ref": driver, "pad": output_pad}, {"ref": connector, "pad": connector_pad}],
                    },
                ],
                "through": [{"ref": driver, "input_pad": input_pad, "output_pad": output_pad}],
            }
        )
    return paths


def _analog_paths(channels: int = 72) -> list[dict[str, Any]]:
    dac_count = (channels + 1) // 2
    paths = [
        _path("analog_spi_data", "SPI_MOSI", ("U10", "28"), *((f"U{30 + n}", "7") for n in range(dac_count))),
        _path("analog_spi_clock", "SPI_SCK", ("U10", "26"), *((f"U{30 + n}", "6") for n in range(dac_count))),
    ]
    for dac_idx in range(dac_count):
        register_idx, bit = divmod(dac_idx, 8)
        output_pad = ["15", "1", "2", "3", "4", "5", "6", "7"][bit]
        paths.append(
            _path(
                f"dac_sync_{dac_idx + 1:02d}",
                f"DAC_SYNC_{dac_idx + 1:02d}",
                (f"U{14 + register_idx}", output_pad),
                (f"U{30 + dac_idx}", "5"),
            )
        )
    for output_idx in range(channels):
        dac_idx, output = divmod(output_idx, 2)
        dac_ref = f"U{30 + dac_idx}"
        resistor_ref = f"R{7 + output_idx}"
        capacitor_ref = f"C{28 + 6 * dac_idx + output}"
        connector_ref = f"J{2 + output_idx}"
        raw_net = f"VOUT{'A' if output == 0 else 'B'}_RAW_{dac_ref}"
        setpoint_net = f"SETPOINT_{output_idx + 1:02d}"
        dac_pad = "4" if output == 0 else "3"
        paths.append(
            {
                "id": f"setpoint_output_{output_idx + 1:02d}",
                "source": {"ref": dac_ref, "pad": dac_pad},
                "loads": [{"ref": connector_ref, "pad": "1"}],
                "segments": [
                    {
                        "net": raw_net,
                        "endpoints": [{"ref": dac_ref, "pad": dac_pad}, {"ref": resistor_ref, "pad": "1"}],
                    },
                    {
                        "net": setpoint_net,
                        "endpoints": [
                            {"ref": resistor_ref, "pad": "2"},
                            {"ref": capacitor_ref, "pad": "1"},
                            {"ref": connector_ref, "pad": "1"},
                        ],
                    },
                ],
                "through": [{"ref": resistor_ref, "input_pad": "1", "output_pad": "2"}],
            }
        )
    return paths


def _gateway_channel_contracts() -> list[dict[str, Any]]:
    old_suite = yaml.safe_load((ROOT.parent / "general_scale" / "cases.yaml").read_text(encoding="utf-8"))
    gateway = next(case for case in old_suite["cases"] if case["id"] == "field_io_gateway")
    channels = [dict(channel) for channel in gateway["required_channels"]]
    spec = yaml.safe_load((ROOT / "cases" / "distributed_facility_gateway.yaml").read_text(encoding="utf-8"))
    manifest = build_assembly_manifest(compile_design_ir(spec).components)
    filters = {
        item.owner_pin: item
        for item in manifest.items
        if item.source_kind == "strap" and item.owner_ref == "U8" and item.owner_pin.startswith("RFILT_CH")
    }
    for channel in channels:
        channel_index = channel["adc_channel"]
        item = filters.get(f"RFILT_CH{channel_index}")
        if item is None or item.net1 != channel["frontend_out_net"] or item.net2 != channel["adc_net"]:
            raise ValueError(f"gateway ADC channel {channel_index} has no matching generated input-filter strap")
        channel["adc_filter_ref"] = item.reference
    return channels


def build_suite() -> dict[str, Any]:
    channel_contracts = _gateway_channel_contracts()
    return {
        "schema_version": 1,
        "description": (
            "Three independently connected, high-count functional circuits spanning fieldbus acquisition, "
            "serial relay outputs, and precision analog setpoints."
        ),
        "minimum_physical_parts": 156,
        "cases": [
            {
                "id": "distributed_facility_gateway",
                "spec": "cases/distributed_facility_gateway.yaml",
                "family": "distributed_facility_control",
                "minimum_physical_parts": 180,
                "required_sections": ["power", "digital", "communication", "connectors", "protection"],
                "board_mm": [180, 135],
                "required_channels": channel_contracts,
                "required_paths": _field_gateway_paths(),
            },
            {
                "id": "serial_relay_output_card",
                "spec": "cases/serial_relay_output_card.yaml",
                "family": "serial_expanded_relay_control",
                "minimum_physical_parts": 250,
                "required_sections": ["power", "digital", "connectors", "passives"],
                "board_mm": [340, 260],
                "required_paths": _relay_paths(),
            },
            {
                "id": "multichannel_analog_setpoint_card",
                "spec": "cases/multichannel_analog_setpoint_card.yaml",
                "family": "precision_analog_setpoint_output",
                "minimum_physical_parts": 350,
                "required_sections": ["power", "digital", "analog", "connectors"],
                "board_mm": [300, 220],
                "required_paths": _analog_paths(),
            },
        ],
    }


def materialize() -> None:
    cases = {
        "serial_relay_output_card.yaml": build_serial_relay_output_card(),
        "multichannel_analog_setpoint_card.yaml": build_multichannel_analog_setpoint_card(),
    }
    (ROOT / "cases").mkdir(parents=True, exist_ok=True)
    for name, spec in cases.items():
        (ROOT / "cases" / name).write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    (ROOT / "cases.yaml").write_text(yaml.safe_dump(build_suite(), sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    materialize()
