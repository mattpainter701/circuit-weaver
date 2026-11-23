from __future__ import annotations

from benchmarks.diverse_systems.active_suite import _suite_failures


def _row(name: str, *, oracle: str, expected_oracle: str, target: str, expected_target_failure: bool) -> dict:
    return {
        "name": name,
        "status": "ok",
        "analytic_oracle": oracle,
        "expected_analytic_oracle": expected_oracle,
        "target_checks": {"output": target},
        "expected_target_failure": expected_target_failure,
    }


def test_suite_summary_separates_physical_oracle_from_acceptance_target():
    cases = [
        _row("nominal", oracle="passed", expected_oracle="passed", target="passed", expected_target_failure=False),
        _row("under_supply", oracle="passed", expected_oracle="passed", target="failed", expected_target_failure=True),
        _row("wrong_pin", oracle="failed", expected_oracle="failed", target="failed", expected_target_failure=True),
    ]
    assert _suite_failures(cases) == []


def test_suite_summary_catches_wrong_expected_oracle_or_target_result():
    cases = [
        _row(
            "unexpected_physics",
            oracle="failed",
            expected_oracle="passed",
            target="passed",
            expected_target_failure=False,
        ),
        _row(
            "missed_under_supply",
            oracle="passed",
            expected_oracle="passed",
            target="passed",
            expected_target_failure=True,
        ),
        _row(
            "missed_pin_swap", oracle="passed", expected_oracle="failed", target="passed", expected_target_failure=True
        ),
    ]
    assert _suite_failures(cases) == ["unexpected_physics", "missed_under_supply", "missed_pin_swap"]
