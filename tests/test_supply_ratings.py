"""Supply-pin voltage ratings: schema, rail derivation and the validator check."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from circuit_weaver.component_db import ComponentDef, PinDef, PowerReq
from circuit_weaver.dispatcher import validate_design
from circuit_weaver.supply_ratings import (
    BASIS_BLOCK_PARAMETER,
    BASIS_POWER_DOMAIN,
    check_supply_ratings,
    component_supply_ratings,
    declared_rail_voltages,
    normalize_part_supply_ratings,
    parse_supply_ratings,
    parse_voltage,
    voltage_from_net_name,
)

RATING = {"pins": ["1"], "v_min": 1.45, "v_max": 5.5, "v_abs_max": 6.0, "source": "test datasheet"}


def _part(ref: str = "U1", *, net: str = "VIN", ratings: list[dict] | None = None, **attributes) -> ComponentDef:
    """A small part with one supply pin (1), one ground pin (2) and one output (3)."""
    component = ComponentDef(
        mpn=f"TEST-PART-{ref}",
        source_ref=ref,
        pins=[
            PinDef("1", "VIN", "power_in", "L"),
            PinDef("2", "GND", "power_in", "B"),
            PinDef("3", "OUT", "power_out", "R"),
        ],
        power_pins={"1": net, "2": "GND", "3": "VOUT"},
        supply_ratings=list(ratings or []),
    )
    for key, value in attributes.items():
        setattr(component, key, value)
    return component


def _block(ref: str, template: str = "ldo", **params) -> SimpleNamespace:
    return SimpleNamespace(ref=ref, id=f"power:{ref}", template_type=template, kind="template", params=params)


def _ir(*blocks, domains=()) -> SimpleNamespace:
    return SimpleNamespace(blocks=list(blocks), power_domains=list(domains))


def _codes(findings) -> list[tuple[str, str]]:
    return sorted((finding.code, finding.level) for finding in findings)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def test_parse_supply_ratings_accepts_the_normalized_shape():
    (rating,) = parse_supply_ratings([RATING])
    assert rating.pins == ("1",)
    assert (rating.v_min, rating.v_max, rating.v_abs_max) == (1.45, 5.5, 6.0)
    assert rating.kind == "supply"
    assert rating.upper_limit == 5.5


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"pins": [], "v_max": 5}, "pins"),
        ({"pins": ["1"], "v_min": 6, "v_max": 5}, "v_min"),
        ({"pins": ["1"], "v_max": 7, "v_abs_max": 6}, "v_abs_max"),
        ({"pins": ["1"], "kind": "magic", "v_max": 5}, "kind"),
        ({"pins": ["1"], "v_max": "five"}, "number"),
        ({"pins": ["1"]}, "needs v_max or v_abs_max"),
    ],
)
def test_parse_supply_ratings_rejects_malformed_data(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_supply_ratings([raw])


def test_a_pin_cannot_be_rated_twice():
    with pytest.raises(ValueError, match="rated twice"):
        parse_supply_ratings([RATING, {"pins": ["1"], "v_max": 3.6}])


def test_non_supply_kinds_need_no_numbers():
    ratings = parse_supply_ratings(
        [
            {"pins": ["4"], "kind": "negative_supply"},
            {"pins": ["5"], "kind": "floating", "note": "referenced to VS"},
            {"pins": ["6"], "kind": "reference_input", "note": "VREF <= VDD"},
        ]
    )
    assert [rating.kind for rating in ratings] == ["negative_supply", "floating", "reference_input"]


def test_legacy_rating_fields_normalize_onto_supply_pins():
    pins = [
        {"number": "1", "name": "IN", "type": "power_in"},
        {"number": "2", "name": "GND", "type": "power_in"},
        {"number": "3", "name": "VDD", "type": "power_in"},
    ]
    (vin,) = normalize_part_supply_ratings({"pins": pins, "pin_vin": "1", "vin_min": 3.1, "vin_max": 5.5})
    assert vin.pins == ("1",) and vin.v_max == 5.5

    (vdd,) = normalize_part_supply_ratings({"pins": pins, "vdd_min": 2.0, "vdd_max": 5.5})
    assert vdd.pins == ("1", "3")  # ground excluded

    (span,) = normalize_part_supply_ratings({"pins": pins, "supply_range": [3.0, 32.0]})
    assert span.v_max == 32.0


def test_explicit_supply_ratings_win_over_legacy_fields():
    entry = {"pins": [], "vin_max": 99, "supply_ratings": [RATING]}
    (rating,) = normalize_part_supply_ratings(entry)
    assert rating.v_max == 5.5


def test_instance_power_pin_envelopes_override_part_ratings():
    component = _part(ratings=[RATING])
    component.power_pin_defs = [SimpleNamespace(pin="1", v_min=None, v_max=3.6)]
    (rating,) = component_supply_ratings(component)
    assert rating.v_max == 3.6


# --------------------------------------------------------------------------
# Rail voltages
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(12, (12.0, 12.0)), ("12V", (12.0, 12.0)), ("4.5-5.5 V", (4.5, 5.5)), ("7 to 24", (7.0, 24.0)), ("x", None)],
)
def test_parse_voltage(value, expected):
    assert parse_voltage(value) == expected


@pytest.mark.parametrize(
    ("net", "volts"),
    [("VDD_3P3", 3.3), ("VIN_12V", 12.0), ("3V3", 3.3), ("VDD_1P8_ANALOG", 1.8), ("+5V", 5.0), ("VBAT", None)],
)
def test_voltage_from_net_name(net, volts):
    assert voltage_from_net_name(net) == volts


def test_block_parameters_declare_rail_voltages():
    rails, conflicts = declared_rail_voltages(
        _ir(_block("U1", vin=12, vin_net="VIN_12V", vout=3.3, rail_name="VDD_3P3"))
    )
    assert conflicts == []
    assert rails["VIN_12V"].v_max == 12 and rails["VIN_12V"].basis == BASIS_BLOCK_PARAMETER
    assert rails["VDD_3P3"].v_min == 3.3


def test_power_domains_outrank_block_parameters():
    rails, _ = declared_rail_voltages(
        _ir(
            _block("U1", vout=3.3, rail_name="VDD_3P3"),
            domains=[{"net": "VDD_3P3", "v_nominal": 3.3, "tolerance": 0.05}],
        )
    )
    rail = rails["VDD_3P3"]
    assert rail.basis == BASIS_POWER_DOMAIN
    assert rail.v_max == pytest.approx(3.465)
    assert rail.v_min == pytest.approx(3.135)


def test_vin_max_parameter_widens_the_rail_envelope():
    rails, _ = declared_rail_voltages(_ir(_block("U1", template="buck", vin=12, vin_max=18, vin_net="VIN")))
    assert (rails["VIN"].v_min, rails["VIN"].v_max) == (12, 18)


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------


def test_declared_over_voltage_above_absolute_maximum_is_an_error():
    findings = check_supply_ratings(
        _ir(_block("U1", vin=12, vin_net="VIN_12V")), [_part(net="VIN_12V", ratings=[RATING])]
    )
    assert _codes(findings) == [("power-over-voltage", "error")]
    finding = findings[0]
    assert finding.rule_id == "CW-PWR-001"
    assert "absolute maximum rating of 6 V" in finding.message
    assert finding.calculation["observed"] == {"value": 12, "unit": "V"}
    assert finding.calculation["expected"] == {"maximum": {"value": 5.5, "unit": "V"}}


def test_over_recommended_but_under_absolute_maximum_is_still_an_error():
    findings = check_supply_ratings(_ir(_block("U1", vin=5.8, vin_net="VIN")), [_part(ratings=[RATING])])
    assert _codes(findings) == [("power-over-voltage", "error")]
    assert "recommended maximum of 5.5 V (absolute maximum 6 V)" in findings[0].message


def test_in_range_rail_passes():
    assert check_supply_ratings(_ir(_block("U1", vin=5, vin_net="VIN")), [_part(ratings=[RATING])]) == []


def test_boundary_values_pass():
    rating = {"pins": ["1"], "v_min": 3.3, "v_max": 3.3}
    assert check_supply_ratings(_ir(_block("U1", vin=3.3, vin_net="VIN")), [_part(ratings=[rating])]) == []


def test_declared_under_voltage_is_an_error():
    findings = check_supply_ratings(_ir(_block("U1", vin=1.2, vin_net="VIN")), [_part(ratings=[RATING])])
    assert _codes(findings) == [("power-under-voltage", "error")]
    assert findings[0].rule_id == "CW-PWR-002"


def test_voltage_only_implied_by_the_rail_name_is_a_warning():
    findings = check_supply_ratings(_ir(), [_part(net="VIN_12V", ratings=[RATING])])
    assert _codes(findings) == [("power-over-voltage", "warning")]
    assert "only implied by the rail's name" in findings[0].message


def test_missing_rating_is_reported_not_silently_passed():
    findings = check_supply_ratings(_ir(_block("U1", vin=12, vin_net="VIN_12V")), [_part(net="VIN_12V")])
    assert _codes(findings) == [("supply-rating-missing", "warning")]
    assert findings[0].rule_id == "CW-PWR-014"
    assert "VIN_12V" in findings[0].message and "12 V" in findings[0].message


def test_rated_pin_on_a_rail_with_unknown_voltage_is_reported():
    findings = check_supply_ratings(_ir(), [_part(net="VCC_AUX", ratings=[RATING])])
    assert _codes(findings) == [("supply-voltage-unknown", "warning")]
    assert findings[0].rule_id == "CW-PWR-015"


def test_connectors_and_virtual_containers_need_no_rating():
    connector = _part("J1", net="VIN_12V", ref_prefix="J", category="connector")
    container = _part("RP1", net="VIN_12V", is_virtual_container=True)
    assert check_supply_ratings(_ir(), [connector, container]) == []


def test_non_supply_kinds_are_not_compared_to_rail_voltage():
    rating = {"pins": ["1"], "kind": "floating", "note": "bootstrap"}
    assert check_supply_ratings(_ir(_block("U1", vin=600, vin_net="VIN")), [_part(ratings=[rating])]) == []


def test_conflicting_declarations_on_one_rail_are_an_error_and_use_the_worst_case():
    ir = _ir(_block("U1", vout=3.3, rail_name="VDD"), _block("U2", vin=5, vin_net="VDD"))
    rating = {"pins": ["1"], "v_min": 3.0, "v_max": 3.6}
    findings = check_supply_ratings(ir, [_part("U3", net="VDD", ratings=[rating])])
    assert ("power-domain-voltage-conflict", "error") in _codes(findings)
    assert ("power-over-voltage", "error") in _codes(findings)


def test_typed_power_envelope_findings_are_not_duplicated():
    ir = _ir(domains=[{"net": "VDD", "v_max": 3.8}])
    component = _part(net="VDD", ratings=[{"pins": ["1"], "v_max": 3.6}])
    component.power_reqs = [PowerReq(net="VDD", v_max=3.6)]
    assert check_supply_ratings(ir, [component]) == []


def test_malformed_instance_ratings_fail_closed():
    findings = check_supply_ratings(_ir(), [_part(ratings=[{"pins": ["1"], "v_min": 9, "v_max": 5}])])
    assert _codes(findings) == [("supply-rating-invalid", "error")]


# --------------------------------------------------------------------------
# End to end through validate_design
# --------------------------------------------------------------------------


def _ldo_spec(vin: float, vin_net: str = "VIN_RAIL", **extra) -> dict:
    block = {
        "type": "ldo",
        "ic": "TLV75518",
        "ref": "U1",
        "vin": vin,
        "vout": 1.8,
        "vin_net": vin_net,
        "rail_name": "VDD_1P8",
        "supply_ratings": [{"pins": ["1"], "v_min": 1.45, "v_max": 5.5, "v_abs_max": 6.0, "source": "test"}],
    }
    block.update(extra)
    return {"project": "Supply_Rating_Test", "power": [block]}


def _electrical(report):
    return [message for message in report.categories["electrical"] if message.code.startswith(("power-", "supply-"))]


def test_validate_design_rejects_twelve_volts_into_a_five_volt_ldo():
    report = validate_design(_ldo_spec(12), check_determinism=False, use_kicad=False)
    assert report.valid is False
    (message,) = _electrical(report)
    assert message.code == "power-over-voltage"
    assert message.level == "error"
    assert message.rule_id == "CW-PWR-001"
    assert message.severity == "blocker"
    assert message.observed_value == "12 V"
    assert message.expected_constraint == "rail voltage at most 5.5 V"


def test_validate_design_accepts_the_same_ldo_in_range():
    report = validate_design(_ldo_spec(3.3), check_determinism=False, use_kicad=False)
    assert _electrical(report) == []


def test_template_ratings_apply_only_to_the_primary_component():
    report = validate_design(_ldo_spec(3.3), check_determinism=False, use_kicad=False)
    from circuit_weaver.dispatcher import compile_design_ir

    compiled = compile_design_ir(_ldo_spec(3.3), use_kicad=False)
    rated = [component for component in compiled.components if component.supply_ratings]
    assert [component.source_ref for component in rated] == ["U1"]
    assert report.valid


def test_malformed_spec_ratings_are_rejected_at_load_time():
    spec = _ldo_spec(3.3, supply_ratings=[{"pins": ["1"], "v_min": 9, "v_max": 5}])
    with pytest.raises(ValueError, match="v_min"):
        validate_design(spec, check_determinism=False, use_kicad=False)


# --------------------------------------------------------------------------
# Rating confidence
# --------------------------------------------------------------------------


def test_single_source_rating_turns_a_declared_violation_into_a_warning():
    rating = {**RATING, "confidence": "single_source"}
    findings = check_supply_ratings(_ir(_block("U1", vin=12, vin_net="VIN")), [_part(ratings=[rating])])
    assert _codes(findings) == [("power-over-voltage", "warning")]
    assert "the rating is single-source" in findings[0].message


def test_corroborated_rating_confirms_a_declared_violation():
    rating = {**RATING, "confidence": "corroborated"}
    findings = check_supply_ratings(_ir(_block("U1", vin=12, vin_net="VIN")), [_part(ratings=[rating])])
    assert _codes(findings) == [("power-over-voltage", "error")]


def test_unknown_confidence_is_rejected():
    with pytest.raises(ValueError, match="confidence"):
        parse_supply_ratings([{**RATING, "confidence": "vibes"}])


def test_legacy_fields_are_single_source():
    (rating,) = normalize_part_supply_ratings(
        {"pins": [{"number": "1", "name": "IN", "type": "power_in"}], "pin_vin": "1", "vin_max": 5.5}
    )
    assert rating.confidence == "single_source"
