"""Focused checks for package IDs and manufacturer pad bindings."""

import json
import os
import re
from importlib.resources import files
from pathlib import Path

import pytest

from circuit_weaver.component_db import BUILTIN_REGISTRY
from circuit_weaver.footprint_lib import KiCadFootprintLibrary
from circuit_weaver.ic_data import get_ic_data
from circuit_weaver.project_spec import _parse_yaml, resolve_project_spec
from circuit_weaver.subcircuits.battery_monitor import BATTERY_MONITOR_IC_DATABASE, BatteryMonitorTemplate


def test_nrf52840_uses_native_aqfn73_pad_ids_and_supply_pads():
    comp = BUILTIN_REGISTRY.get("nRF52840-QIAA")
    assert comp is not None
    assert comp.footprint == "Package_DFN_QFN:Nordic_AQFN-73-1EP_7x7mm_P0.5mm"

    pads = {pin.number: pin.name for pin in comp.pins}
    assert len(pads) == len(comp.pins)
    assert len(pads) == 74  # 73 named footprint pads plus the exposed ground pad.
    assert pads["AA24"] == "SWDCLK"
    assert pads["AC24"] == "SWDIO"
    assert pads["AC13"] == "P0.18/nRESET"
    assert pads["H23"] == "ANT"
    assert pads["B3"] == "DCC"
    assert pads["EP"] == "VSS"
    assert {"A22", "B1", "W1", "AD14", "AD23", "Y2"} <= set(comp.power_pins)
    assert comp.power_pins["Y2"] == "VDD_3P3"
    assert comp.power_pins["EP"] == "GND"
    assert comp.pin_nets["B5"] == comp.pin_nets["E24"] == "DEC4_1P3"


def test_nrf52840_has_an_explicit_hfxo_prototype_and_load_caps():
    crystal = BUILTIN_REGISTRY.get("32MHz-XTAL-CL8")
    assert crystal is not None
    assert crystal.ref_prefix == "Y"
    assert crystal.footprint == "Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm"
    assert "CL=8 pF" in crystal.value
    assert crystal.pin_nets == {"1": "HFXO_XC1", "2": "HFXO_XC2"}
    assert {cap.net: cap.value for cap in crystal.bypass_caps} == {
        "HFXO_XC1": "12pF",
        "HFXO_XC2": "12pF",
    }


def test_wearable_uses_real_outer_row_gpio_for_twim_sda_escape():
    sample = Path(__file__).parents[1] / "samples" / "wearable_bms" / "wearable_bms.yaml"
    components, _ = resolve_project_spec(_parse_yaml(sample), use_kicad=False)
    radio = next(component for component in components if component.source_ref == "U4")
    assert {pin.number: pin.name for pin in radio.pins}["J1"] == "AIN2/P0.04"
    assert radio.pin_nets["J1"] == "I2C_SDA"
    assert radio.pin_nets["G1"] == "I2C_SCL"
    assert "H2" in radio.explicit_no_connects
    assert "J1" not in radio.explicit_no_connects


def test_max17048_bindings_match_all_tdfn_pads_and_datasheet_nets():
    footprint = "Package_DFN_QFN:TDFN-8-1EP_2x2mm_P0.5mm_EP0.8x1.2mm"
    catalog = BATTERY_MONITOR_IC_DATABASE["MAX17048G+T"]
    assert catalog["footprint"] == footprint
    assert catalog["pins"][-1].number == "9"
    assert catalog["pins"][-1].name == "EP"
    assert catalog["pins"][-1].electrical_type == "power_in"
    catalog_pins = {pin.number: (pin.name, pin.electrical_type) for pin in catalog["pins"]}
    assert catalog_pins == {
        "1": ("CTG", "input"),
        "2": ("CELL", "input"),
        "3": ("VDD", "power_in"),
        "4": ("GND", "power_in"),
        "5": ("ALRT", "output"),
        "6": ("QSTRT", "input"),
        "7": ("SCL", "input"),
        "8": ("SDA", "bidirectional"),
        "9": ("EP", "power_in"),
    }
    assert catalog["pin_scl"] == "7"
    assert catalog["pin_sda"] == "8"
    assert catalog["pin_alrt"] == "5"
    assert catalog["no_connect_pins"] == []
    assert catalog["extra_gnd_pins"] == ["9"]
    assert catalog["datasheet_url"].endswith("max17048-max17049.pdf")
    built_in = BUILTIN_REGISTRY.get("MAX17048G+T10")
    assert built_in is not None
    assert built_in.footprint == footprint
    assert {pin.number for pin in built_in.pins} == set("123456789")
    assert built_in.pin_nets == {
        "2": "VBAT", "6": "QSTRT_U2", "7": "I2C_SCL", "8": "I2C_SDA",
    }
    assert built_in.power_pins == {"1": "GND", "3": "VBAT", "4": "GND", "9": "GND"}
    assert built_in.explicit_no_connects == {"5"}
    assert built_in.datasheet_url.endswith("max17048-max17049.pdf")
    assert [(strap.net, strap.rail) for strap in built_in.straps] == [("QSTRT_U2", "GND")]
    assert [(cap.net, cap.value) for cap in built_in.bypass_caps] == [("VBAT", "100nF")]

    generated = BatteryMonitorTemplate().generate(
        {"ic": "MAX17048G+T", "ref": "U2", "bat_net": "VBAT"}
    ).components[0]
    assert generated.pin_nets == built_in.pin_nets
    assert generated.power_pins == built_in.power_pins
    assert generated.explicit_no_connects == {"5"}
    assert generated.pin_nets["2"] == "VBAT"
    assert [(cap.net, cap.value) for cap in generated.bypass_caps] == [("VBAT", "100nF")]
    assert [(strap.net, strap.rail) for strap in generated.straps] == [("QSTRT_U2", "GND")]
    assert {pin.number for pin in generated.pins} >= {"1", "2", "3", "4", "5", "6", "7", "8", "9"}

    alert = BatteryMonitorTemplate().generate(
        {"ic": "MAX17048G+T", "ref": "U3", "bat_net": "VBAT", "irq_net": "FUEL_ALERT_N"}
    ).components[0]
    assert alert.pin_nets["5"] == "FUEL_ALERT_N"
    assert "5" not in alert.explicit_no_connects

    from circuit_weaver.subcircuits.topology_builders import build_battery_monitor

    raw_catalog = json.loads(
        files("circuit_weaver").joinpath("ic_data", "misc.json").read_text(
            encoding="utf-8"
        )
    )["MAX17048G+T"]
    topology = build_battery_monitor(
        raw_catalog,
        {"ic": "MAX17048G+T", "ref": "U2", "bat_net": "VBAT"},
    )
    topology_component = topology.components[0]
    assert topology_component.pin_nets == generated.pin_nets
    assert topology_component.power_pins == generated.power_pins
    assert topology_component.explicit_no_connects == {"5"}
    assert topology_component.pin_nets["2"] == "VBAT"
    assert [(cap.net, cap.value) for cap in topology_component.bypass_caps] == [("VBAT", "100nF")]
    assert [(strap.net, strap.rail) for strap in topology_component.straps] == [("QSTRT_U2", "GND")]


def test_sma_edge_binding_uses_an_installed_edge_launch_package_and_center_ground_pads():
    connector = BUILTIN_REGISTRY.get("SMA-EDGE")
    assert connector is not None
    assert connector.footprint == "Connector_Coaxial:SMA_Amphenol_132289_EdgeMount"
    assert connector.pin_nets == {"1": "RF_IN"}
    assert connector.power_pins == {"2": "GND"}
    assert connector.value == "RP-SMA"


def test_oled_sample_binds_real_adafruit_module_pads_not_bare_controller_pins():
    module = BUILTIN_REGISTRY.get("ADAFRUIT-938")
    assert module is not None
    assert module.footprint == "Display:Adafruit_SSD1306"
    assert {pin.number for pin in module.pins} == set("12345678")
    assert module.pin_nets == {
        "1": "I2C_SDA",
        "2": "I2C_SCL",
        "3": "VDD_3P3",
        "4": "OLED_RESET_N",
    }
    assert module.power_pins == {"5": "GND", "6": "VDD_3P3", "8": "GND"}
    assert module.explicit_no_connects == {"7"}
    assert len(module.official_references) == 2

    sample = Path(__file__).parents[1] / "samples" / "oled_display_module" / "oled_display_module.yaml"
    components, _ = resolve_project_spec(_parse_yaml(sample), use_kicad=False)
    resolved = next(component for component in components if component.source_ref == "U2")
    assert resolved.mpn == "ADAFRUIT-938"
    assert resolved.footprint == "Display:Adafruit_SSD1306"
    assert {pin.number for pin in resolved.pins} == set("12345678")
    assert resolved.pin_nets["1"] == "I2C_SDA"
    assert resolved.pin_nets["2"] == "I2C_SCL"
    assert resolved.pin_nets["3"] == "VDD_3P3"
    assert resolved.power_pins["5"] == "GND"
    assert resolved.power_pins["6"] == "VDD_3P3"
    assert resolved.power_pins["8"] == "GND"
    assert resolved.explicit_no_connects == {"7"}


def test_catalog_data_remains_valid_json():
    data_dir = files("circuit_weaver").joinpath("ic_data")
    json.loads((data_dir / "misc.json").read_text(encoding="utf-8"))
    regulators = json.loads((data_dir / "linear_regulator.json").read_text(encoding="utf-8"))
    assert regulators["AP2112K-3.3"]["footprint"] == "Package_TO_SOT_SMD:SOT-23-5"
    assert regulators["MCP1700-1802E"]["footprint"] == "Package_TO_SOT_SMD:SOT-23"


def test_ina128pa_binds_pdip_package_for_the_ti_orderable_variant():
    data = get_ic_data("INA128PA")
    assert data["description"] == "Precision Instrumentation Amplifier PDIP-8"
    assert data["footprint"] == "Package_DIP:DIP-8_W7.62mm"
    assert data["datasheet_url"] == "https://www.ti.com/lit/ds/symlink/ina128.pdf"


@pytest.mark.skip_category("optional-tool")
@pytest.mark.parametrize("footprint,pads,model", [
    ("Package_DFN_QFN:TDFN-8-1EP_2x2mm_P0.5mm_EP0.8x1.2mm", set("123456789"),
     "TDFN-8-1EP_2x2mm_P0.5mm_EP0.8x1.2mm"),
    ("Package_DIP:DIP-8_W7.62mm", set("12345678"), "DIP-8_W7.62mm"),
    ("Display:Adafruit_SSD1306", set("12345678"), "Adafruit_SSD1306"),
])
def test_corrected_packages_match_native_pad_numbers_and_declare_real_models(footprint, pads, model):
    library = KiCadFootprintLibrary()
    if not library.roots:
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail("Native package checks require KiCad standard footprint libraries")
        pytest.skip("Native package checks require KiCad standard footprint libraries")
    assert library.footprint_exists(footprint)
    footprint_text = library.read(footprint)
    assert re.search(r'\(model\s+"[^"\n]*/' + re.escape(model) + r'\.(?:step|wrl)"', footprint_text)
    assert pads == {
        (match.group(1) or match.group(2)).strip('"')
        for match in re.finditer(r'\(pad\s+(?:"([^"]+)"|([^\s]+))', footprint_text)
        if (match.group(1) or match.group(2)).strip('"')
    }


def test_ap2112k_sot23_5_has_input_on_pin_1_and_output_on_pin_5():
    """Diodes DS39724 SOT-23-5 (K) pinout: 1 VIN, 2 GND, 3 EN, 4 NC, 5 VOUT.

    The entry once had IN and OUT swapped, which wired the 5 V input rail to
    the regulator output in every sample that used it.
    """
    data = get_ic_data("AP2112K-3.3")
    pins = {pin["number"]: (pin["name"], pin["type"]) for pin in data["pins"]}
    assert pins["1"] == ("IN", "power_in")
    assert pins["5"] == ("OUT", "power_out")
    assert (data["pin_vin"], data["pin_out"], data["pin_gnd"], data["pin_en"]) == ("1", "5", "2", "3")

    sample = Path(__file__).parents[1] / "samples" / "oled_display_module" / "oled_display_module.yaml"
    components, _ = resolve_project_spec(_parse_yaml(sample), use_kicad=False)
    regulator = next(component for component in components if component.mpn == "AP2112K-3.3")
    assert regulator.power_pins["1"] == "VBUS_5V"
    assert regulator.power_pins["5"] == "VDD_3P3"
