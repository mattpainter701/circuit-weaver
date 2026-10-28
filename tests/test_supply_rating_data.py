"""Integrity of the curated supply-rating data.

Every rating in the part library must parse, name real supply pins of its own
part, and agree with any legacy rating fields kept for older consumers.
"""

from __future__ import annotations

import pytest

from circuit_weaver.component_db import BUILTIN_REGISTRY
from circuit_weaver.ic_data import get_all_ics
from circuit_weaver.supply_ratings import normalize_part_supply_ratings, parse_supply_ratings

_IC_DATA = sorted(get_all_ics().items()) if callable(get_all_ics) else []
_BUILTINS = sorted(BUILTIN_REGISTRY.all_mpns())


def _pin_types_from_entry(entry):
    return {
        str(pin["number"]): (str(pin["name"]), str(pin.get("type", pin.get("electrical_type"))))
        for pin in entry["pins"]
    }


@pytest.mark.parametrize(("mpn", "entry"), _IC_DATA, ids=[mpn for mpn, _ in _IC_DATA])
def test_ic_data_ratings_parse_and_name_supply_pins(mpn, entry):
    ratings = normalize_part_supply_ratings(entry, where=mpn)
    pins = _pin_types_from_entry(entry)
    for rating in ratings:
        for pin in rating.pins:
            assert pin in pins, f"{mpn}: rated pin {pin} is not one of the part's pins"
            if rating.kind == "supply":
                assert pins[pin][1] == "power_in", f"{mpn}: supply-rated pin {pin} {pins[pin]} is not power_in"
        if "supply_ratings" in entry and rating.kind == "supply":
            assert rating.source, f"{mpn}: curated rating needs a source"


@pytest.mark.parametrize("mpn", _BUILTINS)
def test_builtin_ratings_parse_and_name_supply_pins(mpn):
    component = BUILTIN_REGISTRY.get(mpn)
    ratings = parse_supply_ratings(component.supply_ratings, where=mpn)
    pins = {pin.number: pin.electrical_type for pin in component.pins}
    for rating in ratings:
        for pin in rating.pins:
            assert pin in pins, f"{mpn}: rated pin {pin} is not one of the part's pins"
            if rating.kind == "supply":
                assert pins[pin] == "power_in", f"{mpn}: supply-rated pin {pin} is not power_in"
            assert rating.source, f"{mpn}: curated rating needs a source"


@pytest.mark.parametrize(("mpn", "entry"), _IC_DATA, ids=[mpn for mpn, _ in _IC_DATA])
def test_legacy_rating_fields_agree_with_supply_ratings(mpn, entry):
    if "supply_ratings" not in entry:
        return
    legacy = {key: entry[key] for key in ("vin_min", "vin_max", "vin_abs_max") if entry.get(key) is not None}
    if not legacy:
        return
    ratings = {pin: rating for rating in parse_supply_ratings(entry["supply_ratings"]) for pin in rating.pins}
    rating = ratings[str(entry["pin_vin"])]
    for key, attribute in (("vin_min", "v_min"), ("vin_max", "v_max"), ("vin_abs_max", "v_abs_max")):
        if key in legacy:
            assert getattr(rating, attribute) == pytest.approx(legacy[key]), (
                f"{mpn}: {key} disagrees with supply_ratings"
            )


def test_parts_shared_by_both_stores_carry_the_same_ratings():
    for mpn in _BUILTINS:
        entry = dict(_IC_DATA).get(mpn)
        builtin = BUILTIN_REGISTRY.get(mpn)
        if not entry or "supply_ratings" not in entry or not builtin.supply_ratings:
            continue
        template_limits = {
            (rating.v_min, rating.v_max, rating.v_abs_max) for rating in normalize_part_supply_ratings(entry)
        }
        builtin_limits = {
            (rating.v_min, rating.v_max, rating.v_abs_max) for rating in parse_supply_ratings(builtin.supply_ratings)
        }
        assert template_limits == builtin_limits, mpn
