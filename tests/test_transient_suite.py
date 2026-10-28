from benchmarks.diverse_systems.transient_suite import _failures


def _case(
    name: str,
    *,
    state: str = "passed",
    oracle: str = "passed",
    expected_oracle: str = "passed",
    negative: bool = False,
) -> dict:
    return {
        "name": name,
        "status": "ok",
        "target_checks": {"window_mean": state},
        "analytic_oracle": oracle,
        "expected_analytic_oracle": expected_oracle,
        "expected_target_failure": negative,
        "evidence_verified": True,
        "raw_sha256": "a" * 64,
    }


def test_transient_suite_accepts_expected_nominal_and_negative_control():
    assert _failures([
        _case("iot"),
        _case("wrong_target", state="failed", negative=True),
    ]) == []


def test_transient_suite_detects_missed_negative_control_and_wrong_oracle():
    assert _failures([
        _case("missed_negative", negative=True),
        _case("bad_physics", oracle="failed"),
        _case("wrong_fault_expectation", oracle="passed", expected_oracle="failed", negative=True, state="failed"),
    ]) == ["missed_negative", "bad_physics", "wrong_fault_expectation"]


def test_transient_suite_leaves_unavailable_runner_unverified():
    assert _failures([{"name": "no_ngspice", "status": "skipped"}]) == []
