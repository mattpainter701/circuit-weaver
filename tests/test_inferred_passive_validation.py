from pathlib import Path
from unittest.mock import patch

import yaml

from circuit_weaver.design_loader import compile_design_ir
from circuit_weaver.dispatcher import _validate_block_definitions


def _compile(spec_sections):
    spec = {"project": "passive-validation", "auto_repair": False, "digital": [], **spec_sections}
    return compile_design_ir(spec, use_kicad=False)


def test_explicit_inferred_c_r_l_passives_need_no_external_symbol_resolver():
    passives = [
        {"ref": "C1", "value": "10nF", "footprint": "Capacitor_SMD:C_0402_1005Metric",
         "pin_nets_extra": {"1": "V3_U2", "2": "GND"}},
        {"ref": "R1", "value": "10k", "footprint": "Resistor_SMD:R_0402_1005Metric"},
        {"ref": "L1", "value": "4.7uH", "footprint": "Inductor_SMD:L_0805_2012Metric"},
    ]
    with patch(
        "circuit_weaver.symbol_resolver.SymbolResolver.resolve",
        side_effect=AssertionError("passive inference must not consult a symbol library"),
    ):
        compiled = _compile({"passives": passives})
    assert not _validate_block_definitions(compiled.ir)[0]
    components = {component.source_ref: component for component in compiled.components}
    assert components["C1"].value == "10nF"
    assert components["C1"].footprint == "Capacitor_SMD:C_0402_1005Metric"
    assert components["C1"].pin_nets == {"1": "V3_U2", "2": "GND"}
    assert components["R1"].value == "10k"
    assert components["R1"].footprint == "Resistor_SMD:R_0402_1005Metric"
    assert components["L1"].value == "4.7uH"
    assert components["L1"].footprint == "Inductor_SMD:L_0805_2012Metric"


def test_missing_ic_exemption_does_not_accept_ordinary_or_unknown_components():
    ordinary = _compile({"digital": [{"ref": "U1", "value": "10nF",
                                       "footprint": "Capacitor_SMD:C_0402_1005Metric"}]})
    unknown_passive = _compile({"passives": [{"ref": "X1", "value": "not-a-passive",
                                              "footprint": "Unknown:Part"}]})
    incomplete_passive = _compile({"passives": [{"ref": "C2", "footprint": "Capacitor_SMD:C_0402_1005Metric"}]})
    for compiled in (ordinary, unknown_passive, incomplete_passive):
        errors, _ = _validate_block_definitions(compiled.ir)
        assert any(message.code == "missing-ic" for message in errors)


def test_zigbee_v3_inferred_passive_preserves_value_footprint_and_pin_nets():
    sample = Path(__file__).parents[1] / "samples" / "zigbee_humidistat" / "zigbee_humidistat.yaml"
    spec = yaml.safe_load(sample.read_text(encoding="utf-8"))
    capacitor_spec = next(item for item in spec["passives"] if item["ref"] == "C_V3")
    assert "ic" not in capacitor_spec
    with patch(
        "circuit_weaver.symbol_resolver.SymbolResolver.resolve",
        side_effect=AssertionError("inferred passives must not consult a symbol library"),
    ):
        compiled = _compile({"passives": [capacitor_spec]})
    capacitor = next(component for component in compiled.components if component.source_ref == "C_V3")
    assert capacitor.value == "100nF"
    assert capacitor.footprint == "Capacitor_SMD:C_0402_1005Metric"
    assert capacitor.pin_nets == {"1": "V3_U2", "2": "GND"}
    assert not _validate_block_definitions(compiled.ir)[0]
