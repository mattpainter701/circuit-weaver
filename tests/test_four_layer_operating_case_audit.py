"""Corpus audit keeps conditional arithmetic distinct from verified load cases."""

import copy

import pytest

from benchmarks.four_layer_acceptance.corpus import build_cases
from benchmarks.four_layer_acceptance.operating_case_audit import audit_cases, synthetic_controls


def test_synthetic_controls_match_independent_expected_outcomes():
    rows = synthetic_controls()
    assert [row["observed_status"] for row in rows] == ["passed", "failed", "unverified", "unverified", "unverified"]
    assert all(row["expected_outcome_observed"] for row in rows)
    assert rows[0]["result"]["required_ma"] == 440
    assert rows[1]["result"]["boundary_results"][0]["limit_ma"] == 300


def test_corpus_never_invents_ratings_or_concurrency_and_keeps_inputs_unchanged():
    cases = build_cases()
    before = copy.deepcopy(cases)
    result = audit_cases(cases)
    assert cases == before
    assert len(result["cases"]) == 10
    assert all(row["operating_case_status"] == "unverified" for row in result["cases"])
    relays = [row for row in result["cases"] if "conditional_all_active" in row]
    assert [row["conditional_all_active"]["channels"] for row in relays] == [8, 48, 288]
    assert [row["conditional_all_active"]["current_ma"] for row in relays] == pytest.approx([640, 3840, 23040])
    for row in relays:
        assert row["result"]["required_ma"] is None
        assert row["result"]["status"] == "unverified"
        assert row["conditional_all_active"]["verified_rating"] is False
