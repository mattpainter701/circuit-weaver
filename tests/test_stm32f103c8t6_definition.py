"""Regression coverage for the STM32F103C8T6 LQFP48 pin contract.

Physical numbering and supply guidance are taken from ST DS5319 Rev 20,
Figure 8, Table 5, and Figure 14:
https://www.st.com/resource/en/datasheet/stm32f103c8.pdf
"""

from __future__ import annotations

from pathlib import Path

import yaml

from circuit_weaver.component_db import BUILTIN_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SAMPLE_NCS = {
    "2", "3", "4", "5", "6", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "20",
    "21", "22", "25", "26", "27", "28", "29", "30", "31", "32", "33", "34", "37", "38", "39", "40", "41",
    "42", "43", "45", "46",
}


def test_stm32f103c8t6_builtin_matches_st_lqfp48_pinout_and_supplies() -> None:
    component = BUILTIN_REGISTRY.get("STM32F103C8T6")
    assert component is not None
    pins = {pin.number: pin for pin in component.pins}

    assert set(pins) == {str(number) for number in range(1, 49)}
    assert {
        number for number, pin in pins.items() if pin.name.startswith("VDD") or pin.name in {"VBAT", "VDDA"}
    } == {"1", "9", "24", "36", "48"}
    assert {number for number, pin in pins.items() if pin.name.startswith("VSS") or pin.name == "VSSA"} == {
        "8", "23", "35", "47"
    }
    assert all(pins[number].side == "T" for number in ("1", "9", "24", "36", "48"))
    assert all(pins[number].side == "B" for number in ("8", "23", "35", "47"))
    assert {
        number: pins[number].name
        for number in ("1", "7", "8", "9", "23", "24", "34", "35", "36", "37", "44", "47", "48")
    } == {
        "1": "VBAT",
        "7": "NRST",
        "8": "VSSA",
        "9": "VDDA",
        "23": "VSS_1",
        "24": "VDD_1",
        "34": "PA13-SWDIO",
        "35": "VSS_2",
        "36": "VDD_2",
        "37": "PA14-SWCLK",
        "44": "BOOT0",
        "47": "VSS_3",
        "48": "VDD_3",
    }
    assert component.power_pins == {
        "1": "VDD_3P3",
        "8": "GND",
        "9": "VDD_3P3",
        "23": "GND",
        "24": "VDD_3P3",
        "35": "GND",
        "36": "VDD_3P3",
        "47": "GND",
        "48": "VDD_3P3",
    }
    assert component.pin_nets == {"7": "RESET_N", "34": "SWDIO", "37": "SWCLK", "44": "BOOT0"}
    assert {(cap.pin, cap.value) for cap in component.bypass_caps} == {
        ("24", "100nF"),
        ("36", "100nF"),
        ("48", "100nF"),
        ("48", "4.7uF"),
        ("9", "10nF"),
        ("9", "1uF"),
        ("7", "100nF"),
    }
    assert {(strap.pin, strap.rail, strap.value) for strap in component.straps} == {
        ("44", "GND", "10k"),
        ("7", "VDD_3P3", "10k"),
    }


def test_stm32_samples_leave_only_declared_debug_and_unused_io_unconnected() -> None:
    sample_paths = (
        ROOT / "samples/motor_controller/motor_controller.yaml",
        ROOT / "samples/high_voltage_isolation/high_voltage_isolation.yaml",
        ROOT / "samples/inverter_gate_driver/inverter_gate_driver.yaml",
    )
    for sample_path in sample_paths:
        sample = yaml.safe_load(sample_path.read_text(encoding="utf-8"))
        mcu = next(item for item in sample["digital"] if item["ic"] == "STM32F103C8T6")
        assert set(mcu["no_connects"]) == EXPECTED_SAMPLE_NCS
        assert {"7", "44"}.isdisjoint(mcu["no_connects"])
