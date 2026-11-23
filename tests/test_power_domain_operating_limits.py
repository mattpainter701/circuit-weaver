"""Reject invalid electrical envelopes before they become PCB constraints."""

import math

import pytest

from circuit_weaver.design_ir import PowerDomain, normalize_design_spec


@pytest.mark.parametrize("field", ["v_min", "v_nominal", "v_max", "i_peak_ma", "i_steady_ma", "tolerance"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "3.3", 10**400])
def test_nonfinite_or_non_numeric_power_values_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        PowerDomain(net="RAIL", **{field: value}).normalized()


@pytest.mark.parametrize("value", [True, 1.5, math.nan, math.inf, "1", -1])
def test_invalid_sequence_orders_are_rejected(value):
    with pytest.raises(ValueError, match="sequence_order"):
        PowerDomain(net="RAIL", sequence_order=value).normalized()


def test_signed_rail_and_disabled_load_preserve_known_values():
    rail = PowerDomain(
        net="VEE",
        v_min=-12.6,
        v_nominal=-12,
        v_max=-11.4,
        i_steady_ma=0,
        i_peak_ma=0,
        tolerance=0,
        sequence_order=0,
    ).normalized()
    assert (rail.v_min, rail.v_nominal, rail.v_max) == (-12.6, -12, -11.4)
    assert rail.i_steady_ma == rail.i_peak_ma == rail.sequence_order == 0


def test_peak_below_declared_steady_load_is_rejected():
    with pytest.raises(ValueError, match="i_peak_ma"):
        PowerDomain(net="VBUS", i_steady_ma=800, i_peak_ma=300).normalized()


def test_unknown_limits_stay_unknown():
    rail = PowerDomain(net="VBUS").normalized()
    assert rail.v_min is rail.v_nominal is rail.v_max is None
    assert rail.i_steady_ma is rail.i_peak_ma is rail.tolerance is None


@pytest.mark.parametrize("field,value", [("v_nominal", True), ("i_peak_ma", math.nan), ("sequence_order", 1.5)])
def test_canonical_specs_reject_invalid_envelopes_before_compilation(field, value):
    with pytest.raises(ValueError, match=field):
        normalize_design_spec({"project": "limits", "blocks": [], "power_domains": [{"net": "VBUS", field: value}]})
