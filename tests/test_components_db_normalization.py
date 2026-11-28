"""A design's project-owned part registry must survive spec normalization."""
import json

from circuit_weaver.design_ir import design_ir_to_engine_spec, design_ir_to_spec, normalize_design_spec
from circuit_weaver.dispatcher import validate_design

PARTS = [
    {
        "mpn": "LOCAL_FOUR_PIN_BUFFER",
        "ref_prefix": "U",
        "value": "LOCAL_FOUR_PIN_BUFFER",
        "category": "digital",
        "footprint": "Package_TO_SOT_SMD:SOT-23-5",
        "description": "Project-owned buffer used only by this test",
        "pins": [
            {"number": "1", "name": "A", "electrical_type": "input", "side": "L"},
            {"number": "2", "name": "GND", "electrical_type": "power_in", "side": "B"},
            {"number": "3", "name": "Y", "electrical_type": "output", "side": "R"},
            {"number": "4", "name": "VCC", "electrical_type": "power_in", "side": "T"},
        ],
    }
]


def _spec(registry=None):
    spec = {
        "project": "Local_Part_Probe",
        "digital": [
            {
                "ic": "LOCAL_FOUR_PIN_BUFFER",
                "ref": "U1",
                "power_pins_extra": {"2": "GND", "4": "VDD_3P3"},
                "no_connects": ["1", "3"],
            }
        ],
    }
    if registry is not None:
        spec["components_db"] = str(registry)
    return spec


def _codes(result):
    return {issue.code for issues in result.categories.values() for issue in issues}


def test_registry_reference_reaches_the_canonical_and_engine_specs(tmp_path):
    registry = tmp_path / "components.json"
    ir = normalize_design_spec(_spec(registry))
    assert design_ir_to_spec(ir)["components_db"] == str(registry)
    assert design_ir_to_engine_spec(ir)["components_db"] == str(registry)


def test_specs_without_a_registry_keep_their_canonical_form():
    ir = normalize_design_spec(_spec())
    assert "components_db" not in design_ir_to_spec(ir)
    assert "components_db" not in design_ir_to_engine_spec(ir)


def test_validation_resolves_a_project_owned_part_and_fails_closed_without_it(tmp_path):
    registry = tmp_path / "components.json"
    registry.write_text(json.dumps(PARTS))
    assert "unresolved-component" not in _codes(validate_design(_spec(registry)))
    # A different, undefined part: resolver results are cached per process, so the
    # fail-closed case must not reuse the name the registry just supplied.
    missing = _spec()
    missing["digital"][0]["ic"] = "LOCAL_PART_NO_REGISTRY_DEFINES"
    assert "unresolved-component" in _codes(validate_design(missing))
