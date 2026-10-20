from __future__ import annotations

import json
from importlib.resources import files

from circuit_weaver.subcircuits.voltage_reference import VoltageReferenceTemplate

CATALOG = files("circuit_weaver").joinpath("ic_data", "linear_regulator.json")


def test_lm4040_shunt_variants_preserve_real_sot23_three_pin_contract() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    template = VoltageReferenceTemplate()
    for mpn in ("LM4040-2.5", "LM4040-4.1"):
        entry = catalog[mpn]
        result = template.generate({"ic": mpn, "vin": 5.0, "iload": 0.001})
        component = result.components[0]
        pin_numbers = {pin.number for pin in component.pins}
        assert pin_numbers == {"1", "2", "3"}
        expected_vref = f"VREF_{entry['vout']}V".replace(".", "P")
        assert component.pin_nets == {"1": expected_vref, "2": "GND"}
        assert component.explicit_no_connects == {"3"}
        assert {pin["number"] for pin in entry["pins"]} == pin_numbers
        assert entry["pin_cathode"] == "1"
        assert entry["pin_anode"] == "2"
        assert entry["explicit_no_connects"] == ["3"]


def test_ref3030_family_operating_and_absolute_input_limits_are_distinct() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    for mpn in ("REF3030", "REF3033"):
        reference = catalog[mpn]
        assert reference["vin_max"] == 5.5
        assert reference["vin_abs_max"] == 7.0
        assert reference["vin_abs_max"] > reference["vin_max"]
        assert reference["datasheet_url"] == "https://www.ti.com/lit/ds/symlink/ref30.pdf"

    template = VoltageReferenceTemplate()
    assert not template.validate_params({"ic": "REF3030", "vin": 5.5})
    assert any("above maximum 5.5V" in error for error in
               template.validate_params({"ic": "REF3030", "vin": 5.6}))
