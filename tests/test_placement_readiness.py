"""Dedicated tests for circuit_weaver.placement_readiness."""

from __future__ import annotations

import pytest

from circuit_weaver.component_db import BypassCap, ComponentDef, StrapConfig
from circuit_weaver.design_ir import DesignBlock, DesignInterface, DesignIR
from circuit_weaver.placement_readiness import (
    PlacementReadinessReport,
    categorize_for_placement,
    placement_readiness_issues,
)
from circuit_weaver.validator import ValidationCheckResult, ValidationIssue

PROMOTED_CODES = [
    "single-pin-net",
    "undriven-net",
    "i2c-missing-pullup",
    "spi-floating-cs",
    "uart-unpaired",
    "floating-enable",
    "floating-power-pin",
    "unverified-pinout",
    "vdd-to-gnd-short",
]


def _issue(code: str, suggestion: str = "") -> ValidationIssue:
    return ValidationIssue(
        code=code,
        level="warning",
        ref="U1",
        mpn="TEST",
        message=f"{code} message",
        suggestion=suggestion,
    )


def _result(result_code: str, *issues: ValidationIssue) -> ValidationCheckResult:
    return ValidationCheckResult(
        code=result_code,
        label=result_code,
        status="warning" if issues else "pass",
        issues=tuple(issues),
    )


def _ir(*interfaces: tuple[str, str, str]) -> DesignIR:
    blocks = []
    for ref, net, direction in interfaces:
        blocks.append(
            DesignBlock(
                id=f"block:{ref}",
                section="test",
                kind="component",
                ref=ref,
                interfaces=[DesignInterface(block_id=f"block:{ref}", name=net, direction=direction)],
            )
        )
    return DesignIR(blocks=blocks)


def _comp(ref: str, **kwargs) -> ComponentDef:
    return ComponentDef(mpn=ref, source_ref=ref, **kwargs)


@pytest.mark.parametrize("code", PROMOTED_CODES)
def test_categorize_for_placement_promotes_blocking_codes(code):
    assert categorize_for_placement(code) is True


def test_categorize_for_placement_ignores_soft_electrical_code():
    assert categorize_for_placement("decoupling") is False


def test_promotes_result_level_code_to_error_with_fallback_suggestion():
    out = placement_readiness_issues(
        [_result("single-pin-net", _issue("single-pin-net"))],
        _ir(),
        [],
    )

    assert len(out) == 1
    assert out[0].code == "single-pin-net"
    assert out[0].level == "error"
    assert "dangling" in out[0].suggestion.lower()


def test_promotes_issue_level_code_from_fanout_result():
    out = placement_readiness_issues(
        [_result("bus-completeness", _issue("i2c-missing-pullup"))],
        _ir(),
        [],
    )

    assert [issue.code for issue in out] == ["i2c-missing-pullup"]
    assert out[0].level == "error"


def test_preserves_existing_validator_suggestion():
    out = placement_readiness_issues(
        [_result("single-pin-net", _issue("single-pin-net", suggestion="Wire TP1 to J1"))],
        _ir(),
        [],
    )

    assert out[0].suggestion == "Wire TP1 to J1"


def test_ignores_non_promoted_validator_issue():
    out = placement_readiness_issues(
        [_result("decoupling", _issue("decoupling"))],
        _ir(),
        [],
    )

    assert out == []


def test_orphan_interface_detected_when_no_other_block_uses_net():
    out = placement_readiness_issues([], _ir(("U1", "SENSOR_IRQ", "output")), [_comp("U1")])

    assert len(out) == 1
    assert out[0].code == "orphan-interface"
    assert out[0].level == "error"
    assert "SENSOR_IRQ" in out[0].message


def test_orphan_interface_ignored_when_other_block_uses_pin_net():
    out = placement_readiness_issues(
        [],
        _ir(("U1", "I2C_SDA", "bidirectional")),
        [_comp("U1", pin_nets={"1": "I2C_SDA"}), _comp("U2", pin_nets={"2": "I2C_SDA"})],
    )

    assert out == []


def test_orphan_interface_ignored_when_other_block_uses_power_pin():
    out = placement_readiness_issues(
        [],
        _ir(("U1", "VREF", "output")),
        [_comp("U1", power_pins={"1": "VREF"}), _comp("U2", power_pins={"2": "VREF"})],
    )

    assert out == []


def test_orphan_interface_not_satisfied_by_other_block_bypass_cap():
    out = placement_readiness_issues(
        [],
        _ir(("U1", "ANALOG_BIAS", "output")),
        [
            _comp("U1", pin_nets={"1": "ANALOG_BIAS"}),
            _comp("U2", bypass_caps=[BypassCap("1", "ANALOG_BIAS", "GND", "100nF", "C_0402")]),
        ],
    )

    assert len(out) == 1
    assert out[0].code == "orphan-interface"
    assert "ANALOG_BIAS" in out[0].message


def test_orphan_interface_not_satisfied_by_other_block_strap():
    out = placement_readiness_issues(
        [],
        _ir(("U1", "BOOT", "bidirectional")),
        [
            _comp("U1", pin_nets={"1": "BOOT"}),
            _comp("U2", straps=[StrapConfig("2", "BOOT", "VDD_3P3", "10k", "R_0402")]),
            _comp("U3", power_pins={"3": "VDD_3P3"}),
        ],
    )

    assert len(out) == 1
    assert out[0].code == "orphan-interface"
    assert "BOOT" in out[0].message


def test_output_filter_series_resistor_publishes_template_output_interface():
    out = placement_readiness_issues(
        [],
        _ir(("U1", "DAC_OUT", "output"), ("J1", "DAC_OUT", "input")),
        [
            _comp(
                "U1",
                pin_nets={"4": "DAC_RAW"},
                straps=[
                    StrapConfig(
                        "RFILT_A",
                        "DAC_RAW",
                        "DAC_OUT",
                        "1k",
                        "Resistor_SMD:R_0402",
                        role="output_filter",
                    )
                ],
            ),
            _comp("J1", category="connector", pin_nets={"1": "DAC_OUT"}),
        ],
    )

    assert out == []


def test_orphan_interface_excludes_power_and_ground_nets():
    out = placement_readiness_issues(
        [],
        _ir(("U1", "VDD_3P3", "output"), ("U2", "GND", "passive")),
        [_comp("U1"), _comp("U2")],
    )

    assert out == []


def test_empty_interface_name_is_ignored():
    out = placement_readiness_issues([], _ir(("U1", "", "output")), [_comp("U1")])

    assert out == []


def test_self_connection_only_still_counts_as_orphan():
    out = placement_readiness_issues(
        [],
        _ir(("U1", "SELF_ONLY", "output")),
        [_comp("U1", pin_nets={"1": "SELF_ONLY"})],
    )

    assert len(out) == 1
    assert out[0].code == "orphan-interface"


def test_single_consumer_non_interface_net_is_promoted_to_error():
    out = placement_readiness_issues(
        [],
        _ir(),
        [_comp("U1", pin_nets={"1": "PHANTOM_BUS"})],
    )

    assert len(out) == 1
    assert out[0].code == "orphan-net"
    assert "PHANTOM_BUS" in out[0].message


def test_single_consumer_net_allowlists_debug_block():
    out = placement_readiness_issues(
        [],
        _ir(),
        [_comp("J1", category="debug", pin_nets={"1": "SWD_IO"})],
    )

    assert out == []


def test_single_consumer_net_allowlists_connector_local_control_nets():
    out = placement_readiness_issues(
        [],
        _ir(),
        [_comp("J1", category="connector", pin_nets={"A5": "USB_CC1", "B5": "USB_CC2"})],
    )

    assert out == []


def test_single_consumer_net_allowlists_test_point_ref():
    out = placement_readiness_issues(
        [],
        _ir(),
        [_comp("TP1", ref_prefix="TP", pin_nets={"1": "MEAS_NODE"})],
    )

    assert out == []


def test_single_consumer_net_excludes_internal_power_rails():
    out = placement_readiness_issues(
        [],
        _ir(),
        [_comp("U5", pin_nets={"45": "DVDD_1V1"})],
    )

    assert out == []


def test_single_consumer_net_exempts_nets_terminated_by_own_straps():
    """A regulator FB node ending at its feedback divider is two-ended."""
    comp = _comp(
        "U1",
        pin_nets={"4": "FB_U1"},
        straps=[
            StrapConfig("FBT", "FB_U1", "VDD_3P3", "100k", "R_0402"),
            StrapConfig("FBB", "FB_U1", "GND", "33k", "R_0402"),
        ],
    )
    out = placement_readiness_issues([], _ir(), [comp])

    assert out == []


def test_single_consumer_net_exempts_nets_terminated_by_own_bypass_caps():
    """A buck SW node ending at its inductor/bootstrap cap is two-ended."""
    comp = _comp(
        "U1",
        pin_nets={"2": "SW_U1", "5": "BST_U1"},
        bypass_caps=[
            BypassCap("L", "SW_U1", "VDD_5V", "10uH", "L_0805"),
            BypassCap("CBST", "BST_U1", "SW_U1", "100nF", "C_0402"),
        ],
    )
    out = placement_readiness_issues([], _ir(), [comp])

    assert out == []


def test_single_consumer_net_still_fires_without_support_passives():
    """The support-passive exemption must not swallow genuinely dangling nets."""
    comp = _comp(
        "U1",
        pin_nets={"1": "PHANTOM_BUS", "4": "FB_U1"},
        straps=[StrapConfig("FBT", "FB_U1", "VDD_3P3", "100k", "R_0402")],
    )
    out = placement_readiness_issues([], _ir(), [comp])

    assert [issue.code for issue in out] == ["orphan-net"]
    assert "PHANTOM_BUS" in out[0].message


def test_single_consumer_net_does_not_duplicate_validator_single_pin_issue():
    validator_issue = ValidationIssue(
        code="single-pin-net",
        level="warning",
        ref="U1",
        mpn="TEST",
        message="Net 'FLOATING_IRQ' has only one connection (pin 1 on U1) — likely dangling",
        suggestion="",
    )
    out = placement_readiness_issues(
        [_result("single-pin-net", validator_issue)],
        _ir(),
        [_comp("U1", pin_nets={"1": "FLOATING_IRQ"})],
    )

    assert [issue.code for issue in out] == ["single-pin-net"]


def test_placement_readiness_report_to_dict_copies_payload():
    report = PlacementReadinessReport(
        ready=False,
        blocking=[{"code": "single-pin-net"}],
        auto_repaired=[{"kind": "i2c_pullups"}],
        summary={"errors": 1},
    )

    payload = report.to_dict()
    payload["blocking"].append({"code": "extra"})
    payload["summary"]["errors"] = 99

    assert report.blocking == [{"code": "single-pin-net"}]
    assert report.summary == {"errors": 1}


def test_multiple_promoted_results_preserve_order():
    out = placement_readiness_issues(
        [
            _result("single-pin-net", _issue("single-pin-net")),
            _result("enable-pins", _issue("floating-enable")),
        ],
        _ir(),
        [],
    )

    assert [issue.code for issue in out] == ["single-pin-net", "floating-enable"]
