"""Focused tests for the electrical benchmark runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import circuit_weaver.benchmark_runner as benchmark_runner
from circuit_weaver.benchmark_runner import (
    LEGACY_RULE_ID_ALIASES,
    REGISTERED_RULES,
    canonical_rule_id,
    discover_fixtures,
    render_json,
    render_scorecard_json,
    run_benchmarks,
)
from circuit_weaver.dispatcher import ValidationMessage, ValidationReport
from circuit_weaver.validator import _RULE_ID_BY_FINDING_CODE


def test_benchmark_rule_bridge_is_the_validator_contract() -> None:
    """Benchmark mapping must not drift from producer-owned rule IDs."""

    assert benchmark_runner.RULE_ID_BY_VALIDATOR_CODE.items() >= _RULE_ID_BY_FINDING_CODE.items()
    assert benchmark_runner.RULE_ID_BY_VALIDATOR_CODE["i2c-missing-pullup"] == "CW-I2C-001"
    assert benchmark_runner.RULE_ID_BY_VALIDATOR_CODE["spi-floating-cs"] == "CW-SPI-001"
    assert benchmark_runner.RULE_ID_BY_VALIDATOR_CODE["uart-unpaired"] == "CW-UART-001"
    assert benchmark_runner.RULE_ID_BY_VALIDATOR_CODE["single-pin-net"] == "CW-ERC-001"
    assert benchmark_runner.RULE_ID_BY_VALIDATOR_CODE["power-current-budget"] == "CW-PWR-006"


def test_default_benchmark_validation_disables_machine_local_kicad(monkeypatch) -> None:
    seen: dict[str, object] = {}
    sentinel = object()

    def fake_validate(spec, **kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr(benchmark_runner, "validate_design", fake_validate)

    assert benchmark_runner._benchmark_validate({}) is sentinel
    assert seen == {"check_determinism": False, "use_kicad": False}


def _fixture(
    root: Path,
    polarity: str,
    domain: str,
    fixture_id: str,
    expected: list[str],
    *,
    unsupported: list[str] | None = None,
    absent: list[str] | None = None,
) -> None:
    directory = root / polarity / domain
    directory.mkdir(parents=True)
    (directory / "expected-findings.json").write_text(
        json.dumps(
            {
                "schema_version": "circuit-weaver-electrical-benchmark/v1",
                "fixture_id": fixture_id,
                "domain": domain,
                "polarity": polarity,
                "authoring_source": "independent_reference",
                "provenance": {"source_type": "manual", "source_ref": "review", "license": "CC0"},
                "expected_findings": [
                    {"rule_id": rule_id, "expectation": "detected", "rationale": "reviewed expectation"}
                    for rule_id in expected
                ]
                + [
                    {"rule_id": rule_id, "expectation": "unsupported", "rationale": "not implemented"}
                    for rule_id in (unsupported or [])
                ],
                "expected_absent_rule_ids": absent or [],
            }
        ),
        encoding="utf-8",
    )
    (directory / "design.json").write_text('{"project": "benchmark"}\n', encoding="utf-8")


def _report(*codes: str) -> ValidationReport:
    return ValidationReport(
        profile="standard",
        valid=True,
        categories={
            "electrical": [
                ValidationMessage("electrical", code, "warning", "U1", "benchmark finding") for code in codes
            ]
        },
    )


def test_runner_reports_rule_domain_metrics_unknown_codes_and_explicit_zero_denominators(tmp_path):
    root = tmp_path / "electrical"
    _fixture(root, "negative", "power", "detects", ["CW-PWR-006"])
    _fixture(root, "negative", "usb", "unsupported", [], unsupported=["CW-USB-014"])

    reports = iter([_report("power-budget", "unknown-check"), _report()])
    ticks = iter(float(value) for value in range(20))
    result = run_benchmarks(root, validator=lambda *_args, **_kwargs: next(reports), clock=lambda: next(ticks))

    assert result["rules"]["CW-PWR-006"] == {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "unsupported": 0,
        "precision": 1.0,
        "precision_status": "defined",
        "recall": 1.0,
        "recall_status": "defined",
    }
    assert result["rules"]["CW-USB-014"]["unsupported"] == 1
    assert result["domains"]["power"]["unsupported"] == 1
    assert result["unsupported_actual"] == [{"fixture_id": "detects", "code": "unknown-check"}]
    assert render_json(result) == render_json(result)


def test_discovery_rejects_invalid_rule_fixture(tmp_path):
    root = tmp_path / "electrical"
    _fixture(root, "negative", "power", "bad-rule", ["CW-power-1"])

    with pytest.raises(ValueError, match="invalid rule_id"):
        discover_fixtures(root)


def test_empty_corpus_has_explicit_empty_metrics(tmp_path):
    result = run_benchmarks(tmp_path / "missing", clock=lambda: 1.0)

    assert result["fixture_count"] == 0
    assert result["domains"] == {}
    assert result["rules"] == {}


def test_committed_scorecard_projection_excludes_non_deterministic_timing(tmp_path):
    root = tmp_path / "electrical"
    _fixture(root, "negative", "power", "detects", ["CW-PWR-006"])
    reports = iter([_report("power-budget"), _report("power-budget")])
    first_ticks = iter((0.0, 1.0, 3.0, 6.0))
    second_ticks = iter((0.0, 2.0, 8.0, 16.0))
    first = run_benchmarks(root, validator=lambda *_args, **_kwargs: next(reports), clock=lambda: next(first_ticks))
    second = run_benchmarks(root, validator=lambda *_args, **_kwargs: next(reports), clock=lambda: next(second_ticks))

    assert render_json(first) != render_json(second)
    assert render_scorecard_json(first) == render_scorecard_json(second)


def test_scorecard_explicitly_covers_every_registered_rule_without_crediting_gaps(tmp_path):
    root = tmp_path / "electrical"
    _fixture(root, "negative", "power", "detects", ["CW-PWR-006"])

    result = run_benchmarks(root, validator=lambda *_args, **_kwargs: _report("power-budget"))

    assert set(result["coverage"]) == set(REGISTERED_RULES)
    assert result["coverage"]["CW-PWR-006"]["status"] == "scored"
    assert result["coverage"]["CW-PWR-009"] == {
        "code": "feedback-divider",
        "domain": "power",
        "status": "unsupported",
        "reason": "no complete labelled executable population",
    }
    assert result["aggregate"] == {
        "scored_rule_count": 1,
        "unsupported_rule_count": len(REGISTERED_RULES) - 1,
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "precision": 1.0,
        "recall": 1.0,
        "minimum_precision": 0.95,
        "minimum_recall": 0.90,
        "status": "pass",
    }


def test_scorecard_inventory_matches_actual_emitted_finding_rule_ids():
    extra_contract_rules = {
        "CW-PWR-001",
        "CW-PWR-002",
        "CW-PWR-003",
        "CW-PWR-004",
        "CW-PWR-005",
        "CW-PWR-007",
        "CW-PWR-014",
        "CW-PWR-015",
        "CW-ID-001",
        "CW-ID-002",
        "CW-ID-003",
        "CW-ID-004",
        "CW-PSV-001",
        "CW-PSV-002",
        "CW-PSV-003",
    }

    assert set(REGISTERED_RULES) == set(_RULE_ID_BY_FINDING_CODE.values()) | extra_contract_rules
    assert not any(rule_id.startswith("CW-VAL-") for rule_id in REGISTERED_RULES)


def test_expected_absence_turns_an_unexpected_mapped_rule_into_a_false_positive(tmp_path):
    root = tmp_path / "electrical"
    _fixture(root, "positive", "power", "no-budget-finding", [], absent=["CW-PWR-006"])

    result = run_benchmarks(root, validator=lambda *_args, **_kwargs: _report("power-budget"))

    assert result["rules"]["CW-PWR-006"]["fp"] == 1
    assert result["rules"]["CW-PWR-006"]["recall"] is None


def test_legacy_rule_input_is_canonicalized_before_results_are_emitted(tmp_path):
    root = tmp_path / "electrical"
    legacy_id = next(iter(LEGACY_RULE_ID_ALIASES))
    _fixture(root, "negative", "power", "legacy", [legacy_id])

    result = run_benchmarks(root, validator=lambda *_args, **_kwargs: _report("power-budget"))

    assert canonical_rule_id(legacy_id) == "CW-PWR-006"
    assert legacy_id not in result["rules"]
    assert result["rules"]["CW-PWR-006"]["tp"] == 1


def test_identity_fixture_executes_the_production_handoff_guard(tmp_path):
    root = tmp_path / "electrical"
    directory = root / "negative" / "identity"
    directory.mkdir(parents=True)
    (directory / "expected-findings.json").write_text(
        json.dumps(
            {
                "schema_version": "circuit-weaver-electrical-benchmark/v1",
                "fixture_id": "identity-incomplete-map",
                "domain": "identity",
                "polarity": "negative",
                "authoring_source": "independent_reference",
                "provenance": {"source_type": "manual", "source_ref": "T247 review", "license": "CC0"},
                "expected_findings": [
                    {"rule_id": "CW-ID-002", "expectation": "detected", "rationale": "EP pad is omitted."}
                ],
                "expected_absent_rule_ids": ["CW-ID-001", "CW-ID-003", "CW-ID-004"],
            }
        ),
        encoding="utf-8",
    )
    identity = {
        "status": "resolved",
        "manufacturer": "Acme",
        "mpn": "ABC-QFN-EP",
        "package_suffix": "QFN-EP",
        "symbol_ref": "Acme:ABC",
        "footprint_ref": "Package:QFN-EP",
        "symbol_pins": ["1", "2", "EP"],
        "footprint_pads": ["1", "2", "EP"],
        "pin_pad_map": [
            {"symbol_pin": "1", "footprint_pad": "1"},
            {"symbol_pin": "2", "footprint_pad": "2"},
        ],
    }
    selected = {key: identity[key] for key in ("manufacturer", "mpn", "package_suffix", "symbol_ref", "footprint_ref")}
    assertions = [
        {
            "source_family": "manufacturer",
            "source_uri": "https://example.test/ds",
            "source_doc_id": "ds",
            "identity": identity,
        },
        {
            "source_family": "distributor",
            "source_uri": "https://example.test/listing",
            "source_doc_id": "listing",
            "identity": identity,
        },
    ]
    (directory / "design.json").write_text(
        json.dumps({"project": "identity", "identity_handoff": {"selected": selected, "assertions": assertions}}),
        encoding="utf-8",
    )
    result = run_benchmarks(root)
    assert result["rules"]["CW-ID-002"]["tp"] == 1
    assert result["rules"]["CW-ID-001"]["fp"] == 0


def test_t247_adversarial_identity_corpus_is_provenanced_and_scores_every_guard_rule():
    root = Path(__file__).resolve().parents[1] / "benchmarks" / "electrical"
    identity_fixtures = [fixture for fixture in discover_fixtures(root) if fixture.domain == "identity"]

    assert {fixture.fixture_id for fixture in identity_fixtures} == {
        "identity-distributor-mirror",
        "identity-duplicated-pin-name",
        "identity-lookalike-full-mpn",
        "identity-package-suffix-change",
        "identity-partial-exposed-pad",
        "identity-selected-footprint-mismatch",
        "identity-swapped-differential-pair",
        "identity-targeted-human-approval",
        "identity-valid-two-source",
    }
    assert all(fixture.authoring_source == "independent_reference" for fixture in identity_fixtures)
    assert all(
        fixture.provenance["source_type"].startswith("hand-authored-adversarial") for fixture in identity_fixtures
    )
    assert all(fixture.provenance["license"] == "CC0-1.0" for fixture in identity_fixtures)

    result = run_benchmarks(root)
    identity_rule_ids = ("CW-ID-001", "CW-ID-002", "CW-ID-003", "CW-ID-004")
    assert {rule_id: result["rules"][rule_id]["recall"] for rule_id in identity_rule_ids} == {
        "CW-ID-001": 1.0,
        "CW-ID-002": 1.0,
        "CW-ID-003": 1.0,
        "CW-ID-004": 1.0,
    }
