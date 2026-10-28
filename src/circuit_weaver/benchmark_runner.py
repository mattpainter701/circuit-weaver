"""Run versioned electrical-validation benchmark fixtures.

Each fixture lives below ``benchmarks/electrical/{positive,negative}/<domain>/``
and contains ``expected-findings.json`` plus ``design.json``.
The expectation file is deliberately small and reviewable::

    {
      "schema_version": "circuit-weaver-electrical-benchmark/v1",
      "fixture_id": "usb-missing-cc",
      "domain": "usb",
      "polarity": "negative",
      "authoring_source": "independent_reference",
      "provenance": {"source_type": "manual", "source_ref": "review", "license": "CC0"},
      "expected_findings": [
        {"rule_id": "CW-USB-014", "expectation": "detected", "rationale": "CC resistors are required"}
      ],
      "expected_absent_rule_ids": []
    }

``RULE_ID_BY_VALIDATOR_CODE`` is the explicit, auditable bridge from current
validator implementation codes to stable benchmark rule IDs. Unknown codes are
reported as unsupported rather than silently treated as false positives.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dispatcher import ValidationReport, validate_design
from .identity import (
    build_human_identity_approval,
    build_identity_record,
    build_identity_source_assertion,
    evaluate_identity_handoff,
    reconcile_identity_assertions,
)
from .validator import _RULE_ID_BY_FINDING_CODE


def _benchmark_validate(spec: dict[str, Any], *, check_determinism: bool = False) -> ValidationReport:
    """Validate without machine-local symbol discovery affecting the corpus."""

    return validate_design(spec, check_determinism=check_determinism, use_kicad=False)


RULE_ID_PATTERN = re.compile(r"^CW-[A-Z0-9]+-[0-9]{3}$")
SCHEMA_VERSION = "circuit-weaver-electrical-benchmark/v1"
"""Frozen benchmark rule-ID namespace."""

# This mapping is intentionally explicit. Add a stable rule ID here when a
# validator code graduates into the benchmark contract; do not infer IDs from
# user-facing messages or category names.
# There is one producer-owned bridge.  Keeping a second benchmark map here
# caused newly emitted validator findings to be reported as unsupported even
# though the validator had already assigned them a stable rule ID.


def _rule_domain(rule_id: str) -> str:
    """Derive the scorecard domain from the frozen stable rule namespace."""

    return {
        "PWR": "power",
        "ID": "identity",
        "PSV": "passives",
        "ANALOG": "analog",
        "CLK": "clock",
        "ERC": "erc",
        "I2C": "i2c",
        "SPI": "spi",
        "UART": "uart",
    }.get(rule_id.split("-")[1], "validation")


# The source of truth is the actual validator finding-contract mapping, not a
# parallel generic check alias.  T245 and T247 have emitted contract findings
# outside that table; T246 owns its explicit fail-closed PSV namespace.
_EXTRA_CONTRACT_RULES: dict[str, str] = {
    "CW-PWR-001": "power-over-voltage",
    "CW-PWR-002": "power-under-voltage",
    "CW-PWR-003": "power-reverse-flow",
    "CW-PWR-004": "power-source-contention",
    "CW-PWR-005": "power-regulator-dropout",
    "CW-PWR-007": "power-sequencing",
    "CW-PWR-014": "supply-rating-missing",
    "CW-PWR-015": "supply-voltage-unknown",
    "CW-ID-001": "identity-selected-mismatch",
    "CW-ID-002": "identity-incomplete-map",
    "CW-ID-003": "identity-unapproved-conflict",
    "CW-ID-004": "identity-insufficient-sources",
    "CW-PSV-001": "passive-missing-basis",
    "CW-PSV-002": "passive-out-of-range",
    "CW-PSV-003": "passive-incompatible-network",
}
# One immutable bridge combines producer validator findings with the small
# set of contract producers that do not pass through validator.py.
RULE_ID_BY_VALIDATOR_CODE: dict[str, str] = {
    **_RULE_ID_BY_FINDING_CODE,
    # Legacy power producers use the more specific code while the stable
    # contract intentionally groups it with the power-budget rule.
    "power-current-budget": "CW-PWR-006",
    **{code: rule_id for rule_id, code in _EXTRA_CONTRACT_RULES.items()},
}
REGISTERED_RULES: dict[str, dict[str, str]] = {
    rule_id: {"code": code, "domain": _rule_domain(rule_id)}
    for code, rule_id in {
        **_RULE_ID_BY_FINDING_CODE,
        **{code: rule for rule, code in _EXTRA_CONTRACT_RULES.items()},
    }.items()
}


def registered_rule_coverage(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return one explicit score/unsupported decision for every registered rule.

    A rule is scored only when both denominators are defined and it has no
    unsupported executions.  All remaining rules deliberately remain visible
    as unsupported; this includes passive-synthesis violations until their
    topology fixtures exercise the producer path rather than a toy oracle.
    """

    required = set(_RULE_ID_BY_FINDING_CODE.values()) | set(_EXTRA_CONTRACT_RULES)
    if set(REGISTERED_RULES) != required:
        raise RuntimeError("benchmark rule inventory does not match emitted finding-contract rule IDs")

    metrics = report["rules"]
    coverage: dict[str, dict[str, Any]] = {}
    for rule_id, registration in sorted(REGISTERED_RULES.items()):
        metric = metrics.get(rule_id)
        scored = (
            isinstance(metric, dict)
            and metric["precision"] is not None
            and metric["recall"] is not None
            and metric["unsupported"] == 0
        )
        coverage[rule_id] = {
            **registration,
            "status": "scored" if scored else "unsupported",
            "reason": None if scored else "no complete labelled executable population",
        }
    return coverage


def scorecard_aggregate(report: dict[str, Any], coverage: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compute the supported-only release threshold without inventing metrics."""

    scored = [rule_id for rule_id, item in coverage.items() if item["status"] == "scored"]
    rules = report["rules"]
    tp = sum(rules[rule_id]["tp"] for rule_id in scored)
    fp = sum(rules[rule_id]["fp"] for rule_id in scored)
    fn = sum(rules[rule_id]["fn"] for rule_id in scored)
    return {
        "scored_rule_count": len(scored),
        "unsupported_rule_count": len(coverage) - len(scored),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "minimum_precision": 0.95,
        "minimum_recall": 0.90,
        "status": "pass" if tp + fp and tp + fn and tp / (tp + fp) >= 0.95 and tp / (tp + fn) >= 0.90 else "not_met",
    }


# Legacy fixture/baseline inputs remain readable during the namespace
# migration. Results are always emitted under the canonical ID.
LEGACY_RULE_ID_ALIASES: dict[str, str] = {
    "CW-POWER-001": "CW-PWR-006",
    "CW-POWER-002": "CW-PWR-008",
}


def canonical_rule_id(rule_id: str) -> str:
    """Return the output namespace for a current or legacy rule ID."""
    return LEGACY_RULE_ID_ALIASES.get(rule_id, rule_id)


@dataclass(frozen=True)
class BenchmarkFixture:
    """A versioned, labelled design fixture."""

    fixture_id: str
    polarity: str
    domain: str
    authoring_source: str
    provenance: dict[str, str]
    directory: Path
    design_path: Path
    expected_detected_rule_ids: tuple[str, ...]
    expected_unsupported_rule_ids: tuple[str, ...]
    expected_absent_rule_ids: tuple[str, ...]


def _expectation_file_paths(root: Path) -> list[Path]:
    # T243 fixtures remain at <polarity>/<domain>; later epics may add
    # reviewable cases beneath a domain without forking the corpus schema.
    return sorted(root.glob("*/*/**/expected-findings.json"), key=lambda path: path.as_posix())


def _load_fixture(root: Path, expected_path: Path) -> BenchmarkFixture:
    relative = expected_path.relative_to(root)
    if len(relative.parts) < 3:
        raise ValueError(f"{expected_path}: fixture must be beneath <polarity>/<domain>/")
    polarity, domain = relative.parts[:2]
    if polarity not in {"positive", "negative"}:
        raise ValueError(f"{expected_path}: polarity must be positive or negative")
    try:
        payload = json.loads(expected_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{expected_path}: invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{expected_path}: expectation must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{expected_path}: unsupported schema_version")
    fixture_id = payload.get("fixture_id")
    authoring_source = payload.get("authoring_source")
    provenance = payload.get("provenance")
    expected = payload.get("expected_findings")
    absent = payload.get("expected_absent_rule_ids")
    if not isinstance(fixture_id, str) or not fixture_id:
        raise ValueError(f"{expected_path}: fixture_id is required")
    if payload.get("domain") != domain or payload.get("polarity") != polarity:
        raise ValueError(f"{expected_path}: domain and polarity must match its path")
    if authoring_source not in {"generator_authored", "independent_reference"}:
        raise ValueError(f"{expected_path}: invalid authoring_source")
    if not isinstance(provenance, dict) or set(provenance) != {"source_type", "source_ref", "license"}:
        raise ValueError(f"{expected_path}: provenance must contain source_type, source_ref, and license")
    if not all(isinstance(value, str) and value for value in provenance.values()):
        raise ValueError(f"{expected_path}: provenance values must be non-empty strings")
    if not isinstance(expected, list):
        raise ValueError(f"{expected_path}: expected_findings must be a list")
    if not isinstance(absent, list):
        raise ValueError(f"{expected_path}: expected_absent_rule_ids must be a list")
    detected_rule_ids: list[str] = []
    unsupported_rule_ids: list[str] = []
    for finding in expected:
        if not isinstance(finding, dict):
            raise ValueError(f"{expected_path}: expected finding must be an object")
        rule_id = finding.get("rule_id")
        rationale = finding.get("rationale")
        if not isinstance(rule_id, str) or not RULE_ID_PATTERN.fullmatch(rule_id):
            raise ValueError(f"{expected_path}: invalid rule_id {rule_id!r}")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"{expected_path}: {rule_id} requires a rationale")
        expectation = finding.get("expectation")
        if expectation == "detected":
            detected_rule_ids.append(canonical_rule_id(rule_id))
        elif expectation == "unsupported":
            unsupported_rule_ids.append(canonical_rule_id(rule_id))
        else:
            raise ValueError(f"{expected_path}: {rule_id} expectation must be detected or unsupported")
    if not all(isinstance(rule_id, str) and RULE_ID_PATTERN.fullmatch(rule_id) for rule_id in absent):
        raise ValueError(f"{expected_path}: invalid expected_absent_rule_ids")
    absent = [canonical_rule_id(rule_id) for rule_id in absent]
    all_rule_ids = detected_rule_ids + unsupported_rule_ids + list(absent)
    if len(all_rule_ids) != len(set(all_rule_ids)):
        raise ValueError(f"{expected_path}: expected rule IDs must be unique per fixture")
    design_path = expected_path.parent / "design.json"
    if not design_path.is_file():
        raise ValueError(f"{expected_path}: design.json is required")
    return BenchmarkFixture(
        fixture_id=fixture_id,
        polarity=polarity,
        domain=domain,
        authoring_source=authoring_source,
        provenance=provenance,
        directory=expected_path.parent,
        design_path=design_path,
        expected_detected_rule_ids=tuple(sorted(detected_rule_ids)),
        expected_unsupported_rule_ids=tuple(sorted(unsupported_rule_ids)),
        expected_absent_rule_ids=tuple(sorted(absent)),
    )


def discover_fixtures(root: str | Path) -> list[BenchmarkFixture]:
    """Discover validated fixture manifests in deterministic path order."""

    fixture_root = Path(root)
    if not fixture_root.is_dir():
        return []
    fixtures = [_load_fixture(fixture_root, path) for path in _expectation_file_paths(fixture_root)]
    ids = [fixture.fixture_id for fixture in fixtures]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark fixture_id values must be globally unique")
    return fixtures


def _actual_codes(report: ValidationReport) -> Iterable[str]:
    for category in report.categories.values():
        for finding in category:
            yield finding.code


def _identity_actual_rule_ids(spec: dict[str, Any]) -> set[str]:
    """Execute an identity fixture through the production reconciliation guard.

    The fixture format deliberately supplies exact assertions rather than a
    precomputed handoff result.  This keeps the corpus on the same path Epic C
    will call and makes a forged ``ready`` flag impossible.
    """

    handoff = spec.get("identity_handoff")
    if not isinstance(handoff, dict):
        raise ValueError("identity fixture requires identity_handoff")
    selected = handoff.get("selected")
    raw_assertions = handoff.get("assertions")
    if not isinstance(selected, dict) or not isinstance(raw_assertions, list) or not raw_assertions:
        raise ValueError("identity fixture requires selected identity and assertions")

    assertions = []
    for raw in raw_assertions:
        if not isinstance(raw, dict):
            raise ValueError("identity assertion must be an object")
        raw_identity = raw.get("identity")
        if not isinstance(raw_identity, dict):
            raise ValueError("identity assertion requires an identity")
        identity = build_identity_record(**raw_identity)
        assertions.append(
            build_identity_source_assertion(
                source_family=raw["source_family"],
                source_uri=raw.get("source_uri"),
                source_doc_id=raw.get("source_doc_id"),
                identity=identity,
                evidence_ids=raw.get("evidence_ids", ()),
            )
        )
    raw_approval = handoff.get("approval")
    approval = None
    if raw_approval is not None:
        if not isinstance(raw_approval, dict) or not isinstance(raw_approval.get("assertion_index"), int):
            raise ValueError("identity approval requires assertion_index")
        index = raw_approval["assertion_index"]
        if index < 0 or index >= len(assertions):
            raise ValueError("identity approval assertion_index is out of range")
        approval = build_human_identity_approval(
            owner=raw_approval["owner"],
            reason=raw_approval["reason"],
            approved_identity_id=assertions[index].identity.id,
            evidence_ids=raw_approval.get("evidence_ids", ()),
        )
    reconciliation = reconcile_identity_assertions(assertions, approval=approval)
    result = evaluate_identity_handoff(
        assertions,
        reconciliation,
        manufacturer=selected["manufacturer"],
        mpn=selected["mpn"],
        package_suffix=selected["package_suffix"],
        symbol_ref=selected["symbol_ref"],
        footprint_ref=selected["footprint_ref"],
    )
    return set(result.blocker_codes)


def _passive_actual_rule_ids(spec: dict[str, Any]) -> set[str]:
    """Execute the production fail-closed passive calculation path.

    These small fixtures exercise the same calculation records and withholding
    constructor that topology producers call.  They score a synthesis finding
    without pretending a generated board is a validator input.
    """

    from . import calc

    payload = spec.get("passive_synthesis")
    if not isinstance(payload, dict):
        raise ValueError("passive fixture requires passive_synthesis")
    operation = payload.get("operation")
    target = payload.get("target", "param:U1.passive.value")
    if not isinstance(target, str):
        raise ValueError("passive synthesis target must be a string")
    if operation == "bounded_fallback":
        decision = calc.bounded_fallback_scalar(target=target, **payload["inputs"])
        return {decision.finding.rule_id} if decision.finding is not None else set()
    if operation == "withhold":
        inputs = payload.get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError("withhold passive fixture requires inputs")
        calculation = calc.termination_resistor_match(target=target, **inputs)
        _withheld, finding = calc.withhold_calculation(
            calculation,
            reason=payload["reason"],
            expected_min=payload.get("expected_min"),
            expected_max=payload.get("expected_max"),
            expected_unit=payload.get("expected_unit"),
        )
        return {finding.rule_id}
    if operation == "valid_termination":
        inputs = payload.get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError("valid passive fixture requires inputs")
        calculation = calc.termination_resistor_match(target=target, **inputs)
        return set() if calc.is_selection_eligible(calculation) else {"CW-PSV-001"}
    raise ValueError(f"unsupported passive synthesis operation: {operation!r}")


def _metric(counts: dict[str, int]) -> dict[str, Any]:
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    return {
        **counts,
        "precision": (tp / precision_denominator) if precision_denominator else None,
        "precision_status": "defined" if precision_denominator else "undefined_no_predictions",
        "recall": (tp / recall_denominator) if recall_denominator else None,
        "recall_status": "defined" if recall_denominator else "undefined_no_expected",
    }


def _empty_counts() -> dict[str, int]:
    return {"tp": 0, "fp": 0, "fn": 0, "unsupported": 0}


def run_benchmarks(
    root: str | Path,
    *,
    validator: Callable[..., ValidationReport] = _benchmark_validate,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Run supported fixtures and return deterministic, JSON-ready benchmark metrics.

    A malformed fixture is rejected during discovery. A fixture rejected by
    the validator is classified as an execution error and its expected rules
    increment ``unsupported`` rather than ``fn``. Unmapped actual validator
    codes are retained in ``unsupported_actual`` and added to the domain's
    unsupported count.
    """

    fixtures = discover_fixtures(root)
    by_rule: dict[str, dict[str, int]] = defaultdict(_empty_counts)
    by_domain: dict[str, dict[str, int]] = defaultdict(_empty_counts)
    fixture_results: list[dict[str, Any]] = []
    unsupported_actual: list[dict[str, str]] = []
    total_started = clock()

    for fixture in fixtures:
        started = clock()
        expected = set(fixture.expected_detected_rule_ids)
        expected_unsupported = set(fixture.expected_unsupported_rule_ids)
        expected_absent = set(fixture.expected_absent_rule_ids)
        # Ensure a zero-count domain/rule still appears in the report. This is
        # important for explicit zero-denominator semantics on positive cases.
        _ = by_domain[fixture.domain]
        for rule_id in expected | expected_unsupported | expected_absent:
            _ = by_rule[rule_id]
        result: dict[str, Any] = {
            "fixture_id": fixture.fixture_id,
            "polarity": fixture.polarity,
            "domain": fixture.domain,
            "authoring_source": fixture.authoring_source,
            "provenance": fixture.provenance,
            "expected_detected_rule_ids": sorted(expected),
            "expected_unsupported_rule_ids": sorted(expected_unsupported),
            "expected_absent_rule_ids": sorted(expected_absent),
        }
        try:
            spec = json.loads(fixture.design_path.read_text(encoding="utf-8"))
            if not isinstance(spec, dict):
                raise ValueError("design.json must contain an object")
            # Validation currently emits progress diagnostics on some paths.
            # The benchmark CLI's stdout is a JSON contract, so retain only the
            # structured report here and prevent those diagnostics corrupting it.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                if fixture.domain == "identity":
                    actual = _identity_actual_rule_ids(spec)
                    codes = []
                    mapped = actual
                    unmapped = []
                elif fixture.domain == "passives":
                    actual = _passive_actual_rule_ids(spec)
                    codes = []
                    mapped = actual
                    unmapped = []
                else:
                    report = validator(spec, check_determinism=False)
                    codes = sorted(set(_actual_codes(report)))
                    mapped = {RULE_ID_BY_VALIDATOR_CODE[code] for code in codes if code in RULE_ID_BY_VALIDATOR_CODE}
                    unmapped = [code for code in codes if code not in RULE_ID_BY_VALIDATOR_CODE]
            result["status"] = "executed"
            result["actual_rule_ids"] = sorted(mapped)
            actual = mapped
            for code in unmapped:
                unsupported_actual.append({"fixture_id": fixture.fixture_id, "code": code})
                by_domain[fixture.domain]["unsupported"] += 1
        except Exception as exc:
            result["status"] = "error"
            result["reason"] = type(exc).__name__
            actual = set()
            for rule_id in expected:
                by_rule[rule_id]["unsupported"] += 1
                by_domain[fixture.domain]["unsupported"] += 1

        if result["status"] == "executed":
            for rule_id in expected & actual:
                by_rule[rule_id]["tp"] += 1
                by_domain[fixture.domain]["tp"] += 1
            for rule_id in expected - actual:
                by_rule[rule_id]["fn"] += 1
                by_domain[fixture.domain]["fn"] += 1
            for rule_id in actual - expected:
                by_rule[rule_id]["fp"] += 1
                by_domain[fixture.domain]["fp"] += 1
        for rule_id in expected_unsupported:
            by_rule[rule_id]["unsupported"] += 1
            by_domain[fixture.domain]["unsupported"] += 1
        result["runtime_seconds"] = clock() - started
        fixture_results.append(result)

    report = {
        "schema_version": 1,
        "fixture_count": len(fixtures),
        "runtime_seconds": clock() - total_started,
        "fixtures": fixture_results,
        "domains": {domain: _metric(by_domain[domain]) for domain in sorted(by_domain)},
        "rules": {rule_id: _metric(by_rule[rule_id]) for rule_id in sorted(by_rule)},
        "unsupported_actual": sorted(unsupported_actual, key=lambda item: (item["fixture_id"], item["code"])),
    }
    coverage = registered_rule_coverage(report)
    report["coverage"] = coverage
    report["aggregate"] = scorecard_aggregate(report, coverage)
    return report


def render_json(report: dict[str, Any]) -> str:
    """Serialize a report with stable key and collection ordering."""

    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def render_scorecard_json(report: dict[str, Any]) -> str:
    """Serialize the deterministic, reviewable scorecard artifact.

    The raw run report retains fixture timings for diagnostics.  Timings do
    not belong in a committed quality artifact, so this projection deliberately
    excludes both total and per-fixture runtime fields.
    """

    artifact = {
        "schema_version": 1,
        "fixture_count": report["fixture_count"],
        "suppression_count": report.get("suppression_count", 0),
        "aggregate": report["aggregate"],
        "coverage": report["coverage"],
        "domains": report["domains"],
        "rules": report["rules"],
        "unsupported_actual": report["unsupported_actual"],
    }
    return json.dumps(artifact, indent=2, sort_keys=True) + "\n"


def render_scorecard_summary(report: dict[str, Any]) -> str:
    """Render the committed, human-reviewable companion to ``scorecard.json``."""

    aggregate = report["aggregate"]
    lines = [
        "# Electrical benchmark scorecard",
        "",
        f"Fixtures: {report['fixture_count']}",
        f"Validated suppressions: {report.get('suppression_count', 0)} (marked findings remain scored)",
        (
            f"Supported rules: {aggregate['scored_rule_count']}; "
            f"explicit unsupported rules: {aggregate['unsupported_rule_count']}"
        ),
        (
            "Supported aggregate: "
            f"precision {aggregate['precision']:.3f}, recall {aggregate['recall']:.3f} "
            f"(gate >={aggregate['minimum_precision']:.2f} / "
            f">={aggregate['minimum_recall']:.2f}) - {aggregate['status'].upper()}"
        ),
        "",
        "| Rule | Domain | Status |",
        "| --- | --- | --- |",
    ]
    for rule_id, item in report["coverage"].items():
        label = item["status"]
        if item["reason"]:
            label = f"{label}: {item['reason']}"
        lines.append(f"| {rule_id} | {item['domain']} | {label} |")
    lines.append("")
    return "\n".join(lines)
