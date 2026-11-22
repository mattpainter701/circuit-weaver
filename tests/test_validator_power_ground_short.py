"""Regression coverage for real power-to-ground pin collisions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from circuit_weaver.component_db import ComponentDef, PinDef
from circuit_weaver.dispatcher import validate_design
from circuit_weaver.validator import run_validation_checks


def _net_connectivity_result(components: list[ComponentDef]):
    return next(result for result in run_validation_checks(components) if result.code == "net-connectivity")


def _regulator_with_output_short(net: str = "GND") -> ComponentDef:
    return ComponentDef(
        mpn="TEST_REGULATOR",
        source_ref="U1",
        pins=[
            PinDef("1", "GND", "power_in", "B"),
            PinDef("2", "OUT", "power_out", "R"),
            PinDef("3", "VIN", "power_in", "L"),
        ],
        power_pins={"1": net, "2": net, "3": "VBUS_5V"},
    )


def _short_issues(components: list[ComponentDef]):
    return [issue for issue in _net_connectivity_result(components).issues if issue.code == "vdd-to-gnd-short"]


def test_regulator_output_mapped_to_gnd_is_a_confirmed_blocker():
    result = _net_connectivity_result([_regulator_with_output_short()])

    issue = next(issue for issue in result.issues if issue.code == "vdd-to-gnd-short")
    assert result.status == "FAIL"
    assert issue.is_confirmed_blocker
    assert issue.level == "error"
    assert issue.net == "GND"
    assert "U1:1 (GND)" in issue.message
    assert "U1:2 (OUT)" in issue.message


def test_ground_and_supply_pin_collision_is_detected_on_arbitrary_net_name():
    device = ComponentDef(
        mpn="TEST_DEVICE",
        source_ref="U2",
        pins=[
            PinDef("1", "GND", "power_in", "B"),
            PinDef("2", "VDD", "power_in", "T"),
        ],
        power_pins={"1": "SHARED_NET", "2": "SHARED_NET"},
    )

    assert _short_issues([device])


def test_vin_and_vout_analog_signals_on_ground_net_are_not_supply_evidence():
    converter = ComponentDef(
        mpn="TEST_CONVERTER",
        source_ref="U1",
        pins=[
            PinDef("1", "GND", "power_in", "B"),
            PinDef("2", "VIN", "input", "L"),
            PinDef("3", "VOUT", "output", "R"),
        ],
        power_pins={"1": "SHARED_NET"},
        pin_nets={"2": "SHARED_NET", "3": "SHARED_NET"},
    )

    assert not _short_issues([converter])


def test_explicit_power_assignment_is_supply_evidence_even_if_pin_type_is_generic():
    component = ComponentDef(
        mpn="TEST_COMPONENT",
        source_ref="U1",
        pins=[
            PinDef("1", "GND", "power_in", "B"),
            PinDef("2", "VOUT", "input", "R"),
        ],
        power_pins={"1": "SHARED_NET", "2": "SHARED_NET"},
    )

    assert _short_issues([component])


def test_normal_gnd_power_symbol_and_ground_pins_are_not_reported_as_a_short():
    ground_symbol = ComponentDef(
        mpn="power:GND",
        ref_prefix="#PWR",
        source_ref="#PWR01",
        pins=[PinDef("1", "GND", "power_out", "R")],
        power_pins={"1": "GND"},
    )
    device = ComponentDef(
        mpn="TEST_DEVICE",
        source_ref="U1",
        pins=[
            PinDef("1", "GND", "power_in", "B"),
            PinDef("2", "VDD", "power_in", "T"),
        ],
        power_pins={"1": "GND", "2": "VDD_3P3"},
    )

    assert not _short_issues([ground_symbol, device])


def test_grounded_exposed_pad_negative_rail_and_power_flag_are_not_positive_supplies():
    ground_symbol = ComponentDef(
        mpn="power:GND",
        ref_prefix="#PWR",
        source_ref="#PWR01",
        pins=[PinDef("1", "GND", "power_out", "R")],
        power_pins={"1": "GND"},
    )
    device = ComponentDef(
        mpn="TEST_OPAMP",
        source_ref="U1",
        pins=[
            PinDef("4", "VSS", "power_in", "L"),
            PinDef("5", "EP", "power_in", "B"),
            PinDef("6", "V-", "power_in", "L"),
        ],
        power_pins={"4": "GND", "5": "GND", "6": "GND"},
    )
    power_flag = ComponentDef(
        mpn="PWR_FLAG",
        ref_prefix="#FLG",
        source_ref="#FLG01",
        pins=[PinDef("1", "PWR_FLAG", "power_out", "R")],
        power_pins={"1": "GND"},
    )

    assert not _short_issues([ground_symbol, device, power_flag])


def test_normal_ground_and_positive_rails_are_not_reported_as_a_short():
    regulator = ComponentDef(
        mpn="TEST_REGULATOR",
        source_ref="U1",
        pins=[
            PinDef("1", "GND", "power_in", "B"),
            PinDef("2", "OUT", "power_out", "R"),
            PinDef("3", "VIN", "power_in", "L"),
        ],
        power_pins={"1": "GND", "2": "VDD_3P3", "3": "VBUS_5V"},
    )
    connector = ComponentDef(
        mpn="USB-C-PWR",
        ref_prefix="J",
        source_ref="J1",
        pins=[
            PinDef("A4", "VBUS", "power_out", "R"),
            PinDef("A1", "GND", "power_in", "B"),
        ],
        power_pins={"A4": "VBUS_5V", "A1": "GND"},
    )

    assert not _short_issues([regulator, connector])


def test_led_sample_public_validation_accepts_normal_and_rejects_gnd_shorted_output():
    sample_path = Path(__file__).resolve().parents[1] / "samples" / "led_power_indicator" / "led_power_indicator.yaml"
    sample = yaml.safe_load(sample_path.read_text(encoding="utf-8"))
    shorted_sample = deepcopy(sample)
    shorted_sample["power"][0]["rail_name"] = "GND"

    normal_report = validate_design(sample, check_determinism=False, use_kicad=False)
    short_report = validate_design(shorted_sample, check_determinism=False, use_kicad=False)

    assert normal_report.valid
    assert not short_report.valid
    assert any(
        issue.code == "vdd-to-gnd-short"
        for issue in short_report.categories.get("placement_readiness", [])
    )
