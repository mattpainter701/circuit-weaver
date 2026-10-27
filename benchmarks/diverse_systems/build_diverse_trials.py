"""Build a deterministic connected-system benchmark corpus.

This script writes YAML/JSON fixtures only. It never compiles designs, invokes
KiCad, starts native layout, or runs simulation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

OUTPUT_ROOT = Path(__file__).resolve().parent
DEFAULT_REPO_ROOT = OUTPUT_ROOT.parents[1]


def _read_yaml(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.write_text(yaml.safe_dump(data, sort_keys=False, width=100), encoding="utf-8", newline="\n")


def _load_scale_fixtures(repo_root: Path):
    """Load existing pure fixture builders without invoking compile/build calls."""
    fixture_path = repo_root / "benchmarks" / "unseen_scale" / "build_stress_fixtures.py"
    if not fixture_path.is_file():
        raise FileNotFoundError(f"Existing unseen-scale fixture module not found: {fixture_path}")
    source_root = str(repo_root / "src")
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, source_root)
    try:
        module_spec = importlib.util.spec_from_file_location("cw_unseen_scale_fixture_builders", fixture_path)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"Cannot load existing fixture builders from {fixture_path}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        return module
    finally:
        try:
            sys.path.remove(source_root)
        except ValueError:
            pass
        sys.dont_write_bytecode = previous_dont_write_bytecode


def _suite_case(repo_root: Path, suite_rel: str, case_id: str) -> dict[str, Any]:
    suite = _read_yaml(repo_root / suite_rel)
    for entry in suite.get("cases", []):
        if entry.get("id") == case_id:
            return entry
    raise KeyError(f"Case {case_id!r} was not found in {suite_rel}")


def _signal_path(path_id: str, source: tuple[str, str], loads: list[tuple[str, str]], net: str) -> dict[str, Any]:
    endpoints = [{"ref": ref, "pad": pad} for ref, pad in (source, *loads)]
    return {
        "id": path_id,
        "source": {"ref": source[0], "pad": source[1]},
        "loads": [{"ref": ref, "pad": pad} for ref, pad in loads],
        "segments": [{"net": net, "endpoints": endpoints}],
        "through": [],
    }


def _architecture(
    case_id: str,
    title: str,
    spec: dict[str, Any],
    contract: dict[str, Any],
    *,
    minimum: int | None,
    target: int | None,
    blocks: list[str],
    assertions: list[dict[str, Any]],
    assumptions: list[str],
    exclusions: list[str],
    comparison_reuse: bool = False,
) -> dict[str, Any]:
    board = next(
        (item for item in spec.get("pcb_constraints", []) if item.get("kind") == "placement"),
        {},
    )
    return {
        "case_id": case_id,
        "architecture": title,
        "comparison_fixture_reuse": comparison_reuse,
        "counts": {"minimum": minimum, "target": target, "measured": None},
        "board_mm": {"width": board.get("width_mm"), "height": board.get("height_mm")},
        "functional_blocks": blocks,
        "pin_net_assertions": assertions,
        "assumptions": assumptions,
        "excluded_claims": exclusions,
        "contract": contract,
    }


def build_relay_768(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    builders = _load_scale_fixtures(repo_root)
    spec = builders.build_serial_relay_output_card(768)
    spec["project"] = "Diverse_Relay_Output_Card_768"
    spec["description"] = (
        "Prototype 12 V relay output card with 768 individually addressed coil-return channels. "
        "A 96-device cascaded 74HC595 chain drives 110 ULN2003A sink arrays; each output has its own "
        "two-pin field connector. This is a connectivity/layout stress fixture, not a safety-rated product."
    )
    spec["pcb_constraints"] = [{"kind": "placement", "target": "board", "width_mm": 520, "height_mm": 420}]
    # The first generated connector is SWD. J162 is a genuine channel connector.
    spec["connectors"][0]["ref"] = "J9000"
    contract = {"required_paths": builders._relay_paths(768)}
    architecture = _architecture(
        "relay_768",
        "768-channel serialized relay output card",
        spec,
        contract,
        minimum=1000,
        target=1250,
        blocks=[
            "STM32 control and reset-safe output enable",
            "96 x 74HC595 serial register chain",
            "110 x ULN2003A coil sinks",
            "768 dedicated 12 V coil connectors",
        ],
        assertions=[
            {
                "kind": "per_channel_path",
                "count": 768,
                "source": "74HC595 Q0-Q7",
                "through": "ULN2003A input/output channel",
                "sink": "dedicated connector pin 2",
            },
            {"kind": "serial_chain", "count": 96, "signals": ["SHIFT_DATA", "SHIFT_CLOCK", "SHIFT_LATCH"]},
        ],
        assumptions=[
            "External relay coils are assumed to draw at most 80 mA each; "
            "aggregate power and thermal limits are unverified.",
            "The 12 V coil source and field returns are external.",
            "The shared data, clock, and latch nets drive 96 register inputs without added buffering; "
            "timing/fanout needs review.",
            "Board dimensions are authored stress constraints, not a placement result.",
        ],
        exclusions=["No safety certification, thermal validation, field wiring approval, or manufacturing release."],
    )
    return spec, architecture


def build_ble_sensor(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = _read_yaml(repo_root / "benchmarks/general_scale/cases/sensor_control_hub.yaml")
    spec["project"] = "Diverse_nRF52840_BLE_Sensor_Node"
    spec["description"] = (
        "BLE sensor-node prototype: nRF52840-QIAA with 32 MHz crystal, connected eight-channel MCP3208 "
        "acquisition and SPI DAC/control channels. SPI uses Nordic P0.13/P0.14/P0.15/P0.16. "
        "A direct ANT test port is present; RF behavior and firmware are outside scope."
    )
    spec["pcb_constraints"] = [{"kind": "placement", "target": "board", "width_mm": 180, "height_mm": 135}]
    spec["digital"] = [
        {
            "ic": "nRF52840-QIAA",
            "ref": "U4",
            "pin_nets_extra": {
                "AD8": "SPI_SCK",  # P0.13
                "AC9": "SPI_MOSI",  # P0.14
                "AD10": "SPI_MISO",  # P0.15
                "AC11": "ADC_CS_N",  # P0.16
                "G1": "I2C_SCL",  # P0.26
                "J1": "I2C_SDA",  # P0.04
                "A14": "DAC_SYNC_N",  # P0.19
                "AD12": "ACTUATOR_ENABLE",  # P0.17
                "H23": "RF_IN",  # ANT
            },
            "no_connects": ["AA24", "AC24", "AC13", "B3", "AB2"],
        },
        {"ic": "32MHz-XTAL-CL8", "ref": "Y1"},
    ]
    spec["connectors"].append({"ic": "SMA-EDGE", "ref": "J90"})
    entry = _suite_case(repo_root, "benchmarks/general_scale/cases.yaml", "sensor_control_hub")
    contract = {key: entry[key] for key in ("required_channels", "required_paths") if key in entry}
    # This case replaces the source fixture's STM32 with an nRF52840, which
    # changes auto-allocated support references. Its two earlier power blocks
    # own feedback straps R1-R4; the ADC template then allocates RFILT_CH0..7
    # in channel order as R5-R12. Keep that deterministic logical mapping
    # explicit here instead of inheriting stale STM32-era filter references.
    for channel in contract.get("required_channels", []):
        channel_index = int(channel["adc_channel"])
        channel["adc_filter_ref"] = f"R{5 + channel_index}"
    pad_map = {
        "25": "AC11",
        "26": "AD8",
        "27": "AD10",
        "28": "AC9",
        "21": "G1",
        "22": "J1",
        "15": "A14",
        "14": "AD12",
    }

    def remap_u4_pads(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("ref") == "U4" and str(value.get("pad")) in pad_map:
                value["pad"] = pad_map[str(value["pad"])]
            for child in value.values():
                remap_u4_pads(child)
        elif isinstance(value, list):
            for child in value:
                remap_u4_pads(child)

    for path in contract.get("required_paths", []):
        remap_u4_pads(path)
    contract.setdefault("required_paths", []).append(
        {
            "id": "ble_antenna_test_port",
            "source": {"ref": "U4", "pad": "H23"},
            "loads": [{"ref": "J90", "pad": "1"}],
            "segments": [
                {
                    "net": "RF_IN",
                    "endpoints": [{"ref": "U4", "pad": "H23"}, {"ref": "J90", "pad": "1"}],
                }
            ],
            "through": [],
        }
    )
    contract["required_paths"].extend(
        [
            _signal_path("ble_spi_clock", ("U4", "AD8"), [("U7", "13"), ("U8", "6")], "SPI_SCK"),
            _signal_path("ble_spi_mosi", ("U4", "AC9"), [("U7", "11"), ("U8", "7")], "SPI_MOSI"),
            _signal_path("ble_spi_miso", ("U7", "12"), [("U4", "AD10")], "SPI_MISO"),
            _signal_path("ble_adc_chip_select", ("U4", "AC11"), [("U7", "10")], "ADC_CS_N"),
            _signal_path("ble_dac_chip_select", ("U4", "A14"), [("U8", "5")], "DAC_SYNC_N"),
            _signal_path("ble_i2c_scl", ("U4", "G1"), [("U5", "4")], "I2C_SCL"),
            _signal_path("ble_i2c_sda", ("U4", "J1"), [("U5", "3")], "I2C_SDA"),
        ]
    )
    architecture = _architecture(
        "ble_sensor_8",
        "nRF52840 BLE sensor node with connected multichannel acquisition",
        spec,
        contract,
        minimum=140,
        target=160,
        blocks=[
            "nRF52840-QIAA BLE MCU",
            "32 MHz crystal Y1",
            "Eight-channel MCP3208 acquisition",
            "SPI DAC and analog setpoint outputs",
            "I2C environmental sensor",
            "Direct ANT test port to SMA J90",
        ],
        assertions=[
            {"signal": "SPI_SCK", "mcu": "U4", "pad": "AD8", "function": "P0.13"},
            {"signal": "SPI_MOSI", "mcu": "U4", "pad": "AC9", "function": "P0.14"},
            {"signal": "SPI_MISO", "mcu": "U4", "pad": "AD10", "function": "P0.15"},
            {"signal": "ADC_CS_N", "mcu": "U4", "pad": "AC11", "function": "P0.16"},
            {"signal": "RF_IN", "mcu": "U4", "pad": "H23", "function": "ANT to SMA-EDGE J90"},
        ],
        assumptions=[
            "Nordic P0.13-P0.16 are firmware-configured as SPI signals.",
            "Crystal load network follows the existing Nordic sample assumptions; "
            "production tolerance needs separate validation.",
            "The ANT-to-SMA connection is a test port, not an antenna match or radio-performance claim.",
        ],
        exclusions=["No BLE range, antenna, RF compliance, firmware, or wireless-security claim."],
    )
    return spec, architecture


def build_sdr_clock_carrier(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = _read_yaml(repo_root / "benchmarks/unseen_scale/cases/distributed_facility_gateway.yaml")
    spec["project"] = "Diverse_AD9528_SDR_Clock_Carrier"
    spec["description"] = (
        "External SDR clock/control carrier prototype built around one AD9528 clock synthesizer. Four module "
        "slots each receive three differential clock pairs; an STM32 controls the clock IC over SPI. RF "
        "transceivers, ADC data lanes, jitter/phase noise, and high-speed SI are external and unqualified."
    )
    spec["pcb_constraints"] = [{"kind": "placement", "target": "board", "width_mm": 260, "height_mm": 190}]
    spec["power"].append(
        {
            "type": "ldo",
            "ic": "MCP1700-1802E",
            "ref": "U80",
            "vin": 3.3,
            "vout": 1.8,
            "vin_net": "VDD_3P3",
            "rail_name": "VDD_1P8",
        }
    )
    mcu = next(item for item in spec["digital"] if item.get("ref") == "U3")
    mcu["pin_nets_extra"].update(
        {"43": "SCLK_U90", "2": "SDIO_U90", "3": "SDO_U90", "4": "CSB_U90", "45": "RESETB_U90"}
    )
    mcu["no_connects"] = [
        pin for pin in mcu.get("no_connects", []) if str(pin) not in {"2", "3", "4", "43", "45"}
    ]
    spec["analog"].append(
        {
            "type": "clock_synth",
            "ic": "AD9528",
            "params": {
                "ic": "AD9528",
                "ref": "U90",
                "ref_freq": 10000000,
                "pll_bw": 20000,
                "vdd_net": "VDD_1P8",
                "vddo_net": "VDD_1P8",
                "ref_net": "EXT_REF",
            },
            "ref": "U90",
            "ref_freq": 10000000,
            "pll_bw": 20000,
            "vdd_net": "VDD_1P8",
            "vddo_net": "VDD_1P8",
            "ref_net": "EXT_REF",
            "pin_nets_extra": {},
        }
    )
    spec["connectors"].append(
        {"type": "connector", "ic": "PIN_HEADER_2P", "ref": "J90", "signal_nets": "EXT_REF_P,EXT_REF_N"}
    )
    output_pins = [
        ("22", "23"),
        ("25", "26"),
        ("28", "29"),
        ("31", "32"),
        ("34", "35"),
        ("37", "38"),
        ("40", "41"),
        ("43", "44"),
        ("46", "47"),
        ("49", "50"),
        ("52", "53"),
        ("55", "56"),
    ]
    output_pin_nets: dict[str, str] = {}
    clock_paths: list[dict[str, Any]] = []
    differential_pairs: list[dict[str, str]] = []
    for index, (positive_pad, negative_pad) in enumerate(output_pins):
        slot, lane = divmod(index, 3)
        positive_net = f"SLOT{slot}_CLK{lane}_P"
        negative_net = f"SLOT{slot}_CLK{lane}_N"
        connector_ref = f"J{91 + index}"
        differential_pairs.append(
            {
                "id": f"slot{slot}_clock{lane}",
                "positive_net": positive_net,
                "negative_net": negative_net,
                "connector_ref": connector_ref,
            }
        )
        output_pin_nets[positive_pad] = positive_net
        output_pin_nets[negative_pad] = negative_net
        spec["connectors"].append(
            {
                "type": "connector",
                "ic": "PIN_HEADER_2P",
                "ref": connector_ref,
                "signal_nets": f"{positive_net},{negative_net}",
            }
        )
        for polarity, source_pad, connector_pad, net in (
            ("p", positive_pad, "1", positive_net),
            ("n", negative_pad, "2", negative_net),
        ):
            source = {"ref": "U90", "pad": source_pad}
            load = {"ref": connector_ref, "pad": connector_pad}
            clock_paths.append(
                {
                    "id": f"clock_pair_{index}_{polarity}",
                    "source": source,
                    "loads": [load],
                    "segments": [{"net": net, "endpoints": [source, load]}],
                    "through": [],
                }
            )
    spec["analog"][-1]["pin_nets_extra"] = output_pin_nets

    entry = _suite_case(repo_root, "benchmarks/unseen_scale/cases.yaml", "distributed_facility_gateway")
    contract = {key: entry[key] for key in ("required_channels", "required_paths") if key in entry}
    for signal, mcu_pad, clock_pad in (
        ("SCLK_U90", "43", "14"),
        ("SDIO_U90", "2", "15"),
        ("SDO_U90", "3", "16"),
        ("CSB_U90", "4", "17"),
        ("RESETB_U90", "45", "13"),
    ):
        source = ("U90", clock_pad) if signal == "SDO_U90" else ("U3", mcu_pad)
        load = ("U3", mcu_pad) if signal == "SDO_U90" else ("U90", clock_pad)
        contract.setdefault("required_paths", []).append(
            {
                "id": f"clock_control_{signal.lower()}",
                "source": {"ref": source[0], "pad": source[1]},
                "loads": [{"ref": load[0], "pad": load[1]}],
                "segments": [
                    {
                        "net": signal,
                        "endpoints": [{"ref": source[0], "pad": source[1]}, {"ref": load[0], "pad": load[1]}],
                    }
                ],
                "through": [],
            }
        )
    contract["required_paths"].extend(clock_paths)
    for polarity, connector_pad, clock_pad, net in (
        ("p", "1", "1", "EXT_REF_P"),
        ("n", "2", "2", "EXT_REF_N"),
    ):
        source = {"ref": "J90", "pad": connector_pad}
        load = {"ref": "U90", "pad": clock_pad}
        contract["required_paths"].append(
            {
                "id": f"external_reference_{polarity}",
                "source": source,
                "loads": [load],
                "segments": [{"net": net, "endpoints": [source, load]}],
                "through": [],
            }
        )
    architecture = _architecture(
        "sdr_clock_carrier",
        "AD9528 external SDR clock/control carrier",
        spec,
        contract,
        minimum=180,
        target=240,
        blocks=[
            "AD9528 JESD clock synthesizer",
            "External 10 MHz differential reference",
            "STM32 SPI/reset control",
            "Four SDR module slots with three differential clock pairs each",
        ],
        assertions=[
            {
                "kind": "differential_output_pairs",
                "count": 12,
                "pairs": differential_pairs,
                "source": "AD9528 OUT0-OUT11 pads 22/23 through 55/56",
                "loads": "one 2-pin header per output pair",
            },
            {"kind": "reference", "source": "J90 EXT_REF_P/N", "destination": "AD9528 REFA pads 1/2"},
            {"kind": "spi_control", "controller": "STM32 U3", "target": "AD9528 U90"},
        ],
        assumptions=[
            "VDD and VDDO use 1.8 V; exact power sequencing and operating limits need datasheet review.",
            "Outputs are assigned to external module clock inputs; no frequency plan or "
            "jitter/phase-noise guarantee is made.",
            "The external reference source, terminations, routing, and high-speed integrity "
            "require separate design review.",
        ],
        exclusions=["Not a complete SDR, RF transceiver, ADC capture board, or qualified clock design."],
    )
    return spec, architecture


def build_comparison(
    repo_root: Path,
    *,
    source_spec: str,
    source_suite: str,
    source_case: str,
    case_id: str,
    title: str,
    assumptions: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = _read_yaml(repo_root / source_spec)
    entry = _suite_case(repo_root, source_suite, source_case)
    spec["project"] = f"Diverse_{case_id}"
    spec["description"] = f"Comparison fixture reuse: {title}"
    contract = {key: entry[key] for key in ("required_channels", "required_paths") if key in entry}
    architecture = _architecture(
        case_id,
        f"Comparison fixture reuse: {title}",
        spec,
        contract,
        minimum=entry.get("minimum_physical_parts") or 100,
        target=None,
        blocks=entry.get("required_sections", []),
        assertions=[],
        assumptions=assumptions,
        exclusions=["Reused comparison baseline; it is not evidence of a new architecture or qualification."],
        comparison_reuse=True,
    )
    return spec, architecture


def write_case(output_root: Path, case_id: str, spec: dict[str, Any], architecture: dict[str, Any]) -> None:
    cases_dir = output_root / "cases"
    architecture_dir = output_root / "architecture"
    contracts_dir = output_root / "contracts"
    for directory in (cases_dir, architecture_dir, contracts_dir):
        directory.mkdir(parents=True, exist_ok=True)
    _write_yaml(cases_dir / f"{case_id}.yaml", spec)
    architecture_record = {key: value for key, value in architecture.items() if key != "contract"}
    architecture_record["native_contract"] = f"contracts/{case_id}.json"
    (architecture_dir / f"{case_id}.json").write_text(
        json.dumps(architecture_record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    (contracts_dir / f"{case_id}.json").write_text(
        json.dumps(architecture["contract"], indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def build_corpus(repo_root: Path, output_root: Path) -> None:
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError("Generated fixture output must be new or empty")
    generated = [
        ("relay_768", build_relay_768(repo_root)),
        ("ble_sensor_8", build_ble_sensor(repo_root)),
        ("sdr_clock_carrier", build_sdr_clock_carrier(repo_root)),
        (
            "ot_mixed_io_comparison",
            build_comparison(
                repo_root,
                source_spec="benchmarks/general_scale/cases/field_io_gateway.yaml",
                source_suite="benchmarks/general_scale/cases.yaml",
                source_case="field_io_gateway",
                case_id="ot_mixed_io_comparison",
                title="existing connected field gateway with analog inputs, RS-485, and CAN",
                assumptions=["This existing fixture is included as a baseline comparison only."],
            ),
        ),
        (
            "dual_dac_72_comparison",
            build_comparison(
                repo_root,
                source_spec="benchmarks/unseen_scale/cases/multichannel_analog_setpoint_card.yaml",
                source_suite="benchmarks/unseen_scale/cases.yaml",
                source_case="multichannel_analog_setpoint_card",
                case_id="dual_dac_72_comparison",
                title="existing 72-channel dual-DAC card with SPI selection and per-output filters",
                assumptions=[
                    "This existing fixture is included as a baseline comparison only.",
                    "Output accuracy and load drive require bench validation.",
                ],
            ),
        ),
    ]
    suite_cases = []
    for case_id, (spec, architecture) in generated:
        write_case(output_root, case_id, spec, architecture)
        suite_cases.append(
            {
                "id": case_id,
                "spec": f"cases/{case_id}.yaml",
                "architecture": f"architecture/{case_id}.json",
                "contract": f"contracts/{case_id}.json",
                "family": architecture["architecture"],
                "minimum_physical_parts": architecture["counts"]["minimum"],
                "board_mm": [architecture["board_mm"]["width"], architecture["board_mm"]["height"]],
                "required_path_count": len(architecture["contract"].get("required_paths", [])),
                "required_channel_count": len(architecture["contract"].get("required_channels", [])),
                "comparison_fixture_reuse": architecture["comparison_fixture_reuse"],
            }
        )
    manifest = {
        "schema_version": 1,
        "description": "Diverse connected-system stress cases; measured counts and native results are pending.",
        "minimum_physical_parts": min(item["minimum_physical_parts"] for item in suite_cases),
        "cases": suite_cases,
    }
    _write_yaml(output_root / "cases.yaml", manifest)
    verify_corpus(output_root)


def verify_corpus(output_root: Path) -> None:
    """Require the frozen pre-trim circuit/contract data, not self-updated expectations."""
    inventory = json.loads((OUTPUT_ROOT / "fixture_inventory.json").read_text(encoding="utf-8"))
    expected = inventory["files"]
    actual = {path.relative_to(output_root).as_posix() for folder in ("cases", "contracts", "architecture")
              for path in (output_root / folder).glob("*") if path.is_file()}
    if actual != set(expected):
        raise ValueError("Generated fixture coverage differs from the frozen inventory")
    for relative, digest in expected.items():
        path = output_root / relative
        value = _read_yaml(path) if path.suffix == ".yaml" else json.loads(path.read_text(encoding="utf-8"))
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if hashlib.sha256(canonical).hexdigest() != digest:
            raise ValueError(f"Generated fixture differs from frozen input: {relative}")
    suite = _read_yaml(output_root / "cases.yaml")
    baseline = _read_yaml(OUTPUT_ROOT / "cases.yaml")
    if suite != baseline:
        raise ValueError("Generated suite metadata differs from its reviewed index")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--output", type=Path, required=True, help="new or empty generated-input directory")
    args = parser.parse_args()
    build_corpus(args.repo_root.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
