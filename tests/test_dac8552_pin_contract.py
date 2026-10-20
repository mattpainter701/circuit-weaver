from __future__ import annotations

import json
from importlib.resources import files

from circuit_weaver.subcircuits.dac import DACTemplate


def test_dac8552idgk_dgk_pin_roles_and_active_nets_match_ti_ordering() -> None:
    catalog = json.loads(
        files("circuit_weaver").joinpath("ic_data", "converter.json").read_text(encoding="utf-8")
    )
    entry = catalog["DAC8552IDGK"]
    assert entry["footprint"] == "Package_SO:MSOP-8_3x3mm_P0.65mm"
    assert {pin["number"]: pin["name"] for pin in entry["pins"]} == {
        "1": "VDD", "2": "VREF", "3": "VOUTB", "4": "VOUTA",
        "5": "SYNC", "6": "SCLK", "7": "DIN", "8": "GND",
    }
    assert {
        "pin_vdd": "1", "pin_vref": "2", "pin_voutb": "3", "pin_vouta": "4",
        "pin_sync": "5", "pin_sclk": "6", "pin_din": "7", "pin_gnd": "8",
    } == {key: entry[key] for key in (
        "pin_vdd", "pin_vref", "pin_voutb", "pin_vouta", "pin_sync", "pin_sclk", "pin_din", "pin_gnd",
    )}

    result = DACTemplate().generate({
        "ic": "DAC8552IDGK", "ref": "U15", "vdd_net": "VDD_3P3",
        "vref": "DAC_VREF", "din_net": "SPI_MOSI", "sclk_net": "SPI_SCK",
        "sync_net": "DAC_CS_N", "output_net": "DAC_SETPOINT_A",
        "output_b_net": "DAC_SETPOINT_B",
    })
    component = result.components[0]
    assert component.power_pins == {"1": "VDD_3P3", "8": "GND", "2": "DAC_VREF"}
    assert component.pin_nets == {
        "4": "VOUTA_RAW_U15", "3": "VOUTB_RAW_U15", "7": "SPI_MOSI",
        "6": "SPI_SCK", "5": "DAC_CS_N",
    }
    assert {(strap.net, strap.rail) for strap in component.straps} == {
        ("VOUTA_RAW_U15", "DAC_SETPOINT_A"), ("VOUTB_RAW_U15", "DAC_SETPOINT_B"),
    }
