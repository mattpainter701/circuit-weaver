from circuit_weaver.component_db import BypassCap, ComponentDef, PinDef, PowerReq
from circuit_weaver.project_spec import _apply_partial_pin_overrides, resolve_project_spec


def test_power_pin_override_retargets_only_pin_addressed_bypass_cap():
    comp = ComponentDef(
        mpn="TEST_IC",
        ref_prefix="U",
        pins=[
            PinDef("A22", "VDDH", "power_in", "L"),
            PinDef("B1", "VDD", "power_in", "L"),
            PinDef("DEC1", "DEC", "power_in", "R"),
        ],
        power_pins={"A22": "VDD_3P3", "B1": "VDD_3P3", "DEC1": "DEC1"},
        power_reqs=[PowerReq("VDD_3P3", 3.3, 80)],
        bypass_caps=[
            BypassCap("A22", "VDD_3P3", "GND", "4.7uF", "Capacitor_SMD:C_0402_1005Metric"),
            BypassCap("DEC1", "DEC1", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
        ],
    )

    _apply_partial_pin_overrides(
        {"power_pins_extra": {"A22": "VDD_1P8", "B1": "VDD_1P8"}}, comp
    )

    assert comp.power_pins["A22"] == "VDD_1P8"
    assert comp.power_reqs[0].net == "VDD_1P8"
    assert comp.power_reqs[0].voltage == 1.8
    assert comp.power_reqs[0].v_nominal == 1.8
    assert comp.bypass_caps[0].net == "VDD_1P8"
    assert comp.bypass_caps[1].net == "DEC1"


def test_partial_supply_pin_override_keeps_component_rail_requirement():
    comp = ComponentDef(
        mpn="TEST_IC",
        power_pins={"A22": "VDD_3P3", "B1": "VDD_3P3"},
        power_reqs=[PowerReq("VDD_3P3", 3.3, 80)],
    )

    _apply_partial_pin_overrides({"power_pins_extra": {"A22": "VDD_1P8"}}, comp)

    assert comp.power_reqs[0].net == "VDD_3P3"


def test_inferred_passive_accepts_explicit_pin_net_assignments():
    components, _ = resolve_project_spec(
        {
            "passives": [
                {
                    "ref": "C1",
                    "value": "10nF",
                    "footprint": "Capacitor_SMD:C_0402_1005Metric",
                    "pin_nets_extra": {"1": "V3_U2", "2": "GND"},
                }
            ]
        },
        use_kicad=False,
    )

    assert components[0].source_ref == "C1"
    assert components[0].pin_nets == {"1": "V3_U2", "2": "GND"}
