"""Focused contracts for explicitly authored load operating cases."""

import math
from decimal import localcontext

import pytest

from circuit_weaver.operating_cases import (
    CurrentLimit,
    Evidence,
    Load,
    OperatingCase,
    check_operating_case,
)


def evidence(subject: str, value: float) -> Evidence:
    return Evidence(
        evidence_id=f"EV-{subject}",
        confidence="verified",
        freshness="current",
        subject=subject,
        value=value,
        unit="mA",
        kind="datasheet",
    )


def case(*, source_ma=1000, connector_ma=800, distribution_ma=750, current_ma=100, count=4):
    source = CurrentLimit("source", source_ma, evidence("source", source_ma)) if source_ma is not None else None
    connectors = (
        (CurrentLimit("J1", connector_ma, evidence("connector:J1", connector_ma)),) if connector_ma is not None else ()
    )
    distribution = (
        (CurrentLimit("copper", distribution_ma, evidence("distribution:copper", distribution_ma)),)
        if distribution_ma is not None
        else ()
    )
    return OperatingCase(
        "relay bank",
        count,
        (Load("relay coil", current_ma, count, evidence("relay coil", current_ma)),),
        source,
        connectors,
        distribution,
    )


def test_nominal_case_checks_each_declared_boundary_and_is_repeatable() -> None:
    result = check_operating_case(case())
    assert result.status == "passed"
    assert result.required_ma == 400
    assert [item.status for item in result.boundary_results] == ["passed"] * 3
    assert result.result_scope == "authored_current_budget_only"
    assert result.functionality_qualification == result.thermal_qualification == "unverified"
    assert result.to_dict() == check_operating_case(case()).to_dict()
    assert result.result_hash == check_operating_case(case()).result_hash


def test_stated_overload_fails_even_when_another_boundary_is_unverified() -> None:
    result = check_operating_case(case(source_ma=300, connector_ma=None))
    assert result.status == "failed"
    assert result.boundary_results[0].status == "failed"
    assert any(item.status == "unverified" for item in result.boundary_results)


def test_missing_limits_remain_explicitly_unverified() -> None:
    result = check_operating_case(case(source_ma=None, connector_ma=None, distribution_ma=None))
    assert result.status == "unverified"
    assert {item.boundary for item in result.boundary_results} == {"source", "connector", "distribution"}
    assert all(item.status == "unverified" for item in result.boundary_results)


def test_unknown_case_concurrency_does_not_assume_all_declared_loads_are_active() -> None:
    authored = case()
    result = check_operating_case(
        OperatingCase(
            authored.name,
            None,
            authored.loads,
            authored.source_limit,
            authored.connector_limits,
            authored.distribution_limits,
        )
    )
    assert result.status == "unverified"
    assert result.required_ma is None
    assert all(item.status == "unverified" and item.required_ma is None for item in result.boundary_results)


def test_unknown_load_current_yields_unverified_without_a_computed_budget() -> None:
    authored = case()
    result = check_operating_case(
        OperatingCase(
            authored.name,
            authored.max_simultaneous,
            (Load("relay coil", None, 4, None),),
            authored.source_limit,
            authored.connector_limits,
            authored.distribution_limits,
        )
    )
    assert result.status == "unverified"
    assert result.required_ma is None
    assert all(item.status == "unverified" for item in result.boundary_results)


def test_untrusted_load_current_cannot_claim_a_pass_or_failure_from_limits() -> None:
    authored = case(source_ma=100, connector_ma=100, distribution_ma=100, current_ma=50, count=4)
    load = Load(
        "relay coil",
        50,
        4,
        Evidence(
            "EV-load",
            "single_source",
            "current",
            "relay coil",
            50,
            "mA",
            "datasheet",
        ),
    )
    result = check_operating_case(
        OperatingCase(
            authored.name,
            4,
            (load,),
            authored.source_limit,
            authored.connector_limits,
            authored.distribution_limits,
        )
    )
    assert result.required_ma == 200  # conditional arithmetic only
    assert result.status == "unverified"
    assert all(item.status == "unverified" for item in result.boundary_results)


def test_unknown_load_evidence_does_not_mask_a_separate_trusted_overload() -> None:
    authored = case(source_ma=100, connector_ma=1000, distribution_ma=1000, current_ma=50, count=4)
    load = Load(
        "relay coil",
        50,
        4,
        Evidence(
            "EV-load",
            "single_source",
            "current",
            "relay coil",
            50,
            "mA",
            "datasheet",
        ),
    )
    result = check_operating_case(
        OperatingCase(
            authored.name,
            4,
            (load,),
            authored.source_limit,
            authored.connector_limits,
            authored.distribution_limits,
        )
    )
    assert result.status == "unverified"
    assert result.boundary_results[0].status == "unverified"


@pytest.mark.parametrize(
    "bad_evidence",
    [
        Evidence("EV-x", "single_source", "current", "source", 1000, "mA", "datasheet"),
        Evidence("EV-x", "verified", "stale", "source", 1000, "mA", "datasheet"),
        Evidence("EV-x", "verified", "current", "wrong-subject", 1000, "mA", "datasheet"),
        Evidence("EV-x", "verified", "current", "source", 999, "mA", "datasheet"),
        Evidence("EV-x", "verified", "current", "source", 1000, "mA", "user", ("EV-conflict",)),
    ],
)
def test_untrusted_or_mismatched_limit_evidence_does_not_pass(bad_evidence) -> None:
    authored = case()
    limits = (CurrentLimit("source", 1000, bad_evidence),)
    result = check_operating_case(
        OperatingCase(
            authored.name,
            authored.max_simultaneous,
            authored.loads,
            limits[0],
            authored.connector_limits,
            authored.distribution_limits,
        )
    )
    assert result.status == "unverified"
    assert result.boundary_results[0].status == "unverified"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "100"])
def test_nonfinite_boolean_and_non_numeric_load_currents_are_rejected(value) -> None:
    with pytest.raises(ValueError):
        check_operating_case(
            OperatingCase(
                "bad",
                1,
                (Load("load", value, 1, evidence("load", 1)),),
                CurrentLimit("source", 2, evidence("source", 2)),
                (CurrentLimit("J1", 2, evidence("connector:J1", 2)),),
                (CurrentLimit("trace", 2, evidence("distribution:trace", 2)),),
            )
        )


def test_boolean_or_inconsistent_concurrency_is_rejected() -> None:
    for concurrency in (True, 0, 2):
        with pytest.raises(ValueError):
            check_operating_case(
                OperatingCase(
                    "bad",
                    concurrency,
                    (Load("load", 100, 1, evidence("load", 100)),),
                    CurrentLimit("source", 200, evidence("source", 200)),
                    (CurrentLimit("J1", 200, evidence("connector:J1", 200)),),
                    (CurrentLimit("trace", 200, evidence("distribution:trace", 200)),),
                )
            )


def test_wrong_unit_and_aggregate_overflow_are_rejected() -> None:
    with pytest.raises(ValueError, match="unit"):
        check_operating_case(
            OperatingCase(
                "bad",
                1,
                (Load("load", 100, 1, evidence("load", 100), "A"),),
                CurrentLimit("source", 200, evidence("source", 200)),
                (CurrentLimit("J1", 200, evidence("connector:J1", 200)),),
                (CurrentLimit("trace", 200, evidence("distribution:trace", 200)),),
            )
        )


def test_extremely_large_integer_and_duplicate_names_are_rejected_cleanly() -> None:
    with pytest.raises(ValueError):
        check_operating_case(
            OperatingCase(
                "huge",
                1,
                (Load("load", 10**10000, 1, None),),
                None,
            )
        )
    with pytest.raises(ValueError, match="duplicate load"):
        check_operating_case(
            OperatingCase(
                "duplicates",
                2,
                (Load("load", 1, 1, None), Load("load", 1, 1, None)),
                None,
            )
        )
    with pytest.raises(ValueError, match="duplicate connector"):
        check_operating_case(
            OperatingCase(
                "duplicates",
                1,
                (Load("load", 1, 1, None),),
                None,
                (CurrentLimit("J1", 1, None), CurrentLimit("J1", 1, None)),
            )
        )


def test_aggregate_float_overflow_is_rejected() -> None:
    with pytest.raises(ValueError, match="not finite"):
        check_operating_case(
            OperatingCase(
                "overflow",
                2,
                (Load("a", 1e308, 1, evidence("a", 1e308)), Load("b", 1e308, 1, evidence("b", 1e308))),
                CurrentLimit("source", 1, evidence("source", 1)),
                (CurrentLimit("J1", 1, evidence("connector:J1", 1)),),
                (CurrentLimit("trace", 1, evidence("distribution:trace", 1)),),
            )
        )


def test_decimal_equality_and_true_overload_use_authored_decimal_values() -> None:
    equal = check_operating_case(
        case(
            source_ma=0.3,
            connector_ma=0.4,
            distribution_ma=0.5,
            current_ma=0.1,
            count=3,
        )
    )
    assert equal.required_ma == 0.3
    assert equal.boundary_results[0].status == "passed"

    lower = check_operating_case(
        case(
            source_ma=0.299,
            connector_ma=0.4,
            distribution_ma=0.5,
            current_ma=0.1,
            count=3,
        )
    )
    assert lower.boundary_results[0].status == "failed"


def test_adjacent_decimal_float_inputs_compare_by_authored_spelling() -> None:
    result = check_operating_case(
        case(
            source_ma=0.30000000000000004,
            connector_ma=1.0,
            distribution_ma=1.0,
            current_ma=0.10000000000000002,
            count=3,
        )
    )
    assert result.boundary_results[0].status == "failed"


def test_input_hash_covers_load_evidence_when_result_totals_match() -> None:
    original = case()
    changed = OperatingCase(
        original.name,
        original.max_simultaneous,
        (
            Load(
                "relay coil",
                100,
                4,
                Evidence(
                    "EV-DIFFERENT",
                    "verified",
                    "current",
                    "relay coil",
                    100,
                    "mA",
                    "datasheet",
                ),
            ),
        ),
        original.source_limit,
        original.connector_limits,
        original.distribution_limits,
    )
    first = check_operating_case(original)
    second = check_operating_case(changed)
    assert first.status == second.status
    assert first.required_ma == second.required_ma
    assert first.input_sha256 != second.input_sha256
    assert first.result_hash != second.result_hash


def test_evidence_binding_preserves_integer_differences_beyond_float_precision():
    original = case()
    changed = OperatingCase(
        original.name,
        original.max_simultaneous,
        original.loads,
        CurrentLimit("source", 100000000000000001, evidence("source", 100000000000000000)),
        original.connector_limits,
        original.distribution_limits,
    )
    assert check_operating_case(changed).boundary_results[0].status == "unverified"


def test_decimal_case_is_independent_of_callers_exponent_bounds():
    with localcontext() as context:
        context.Emax = 1
        result = check_operating_case(case(current_ma=100, count=4))
    assert result.status == "passed"
    assert result.required_ma == 400
