"""Current budgets must follow ledger evidence across normal validation surfaces."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from circuit_weaver import dispatcher, mcp_server, project_workflow
from circuit_weaver.evidence import EvidenceLedger, EvidenceSource
from circuit_weaver.operating_case_spec import evaluate_spec_operating_cases
from circuit_weaver.project_spec import _parse_yaml


def authored_spec(source_ma=500):
    ledger = EvidenceLedger()

    def value(subject, amount):
        return ledger.record(
            subject_ref=subject, claim=json.dumps({"value": amount, "unit": "mA"}, sort_keys=True),
            kind="calculation", source=EvidenceSource(
                doc_id="synthetic-test-control", content_hash="a" * 64, extraction_method="authored-test-fixture",
            ), confidence="verified", freshness="current",
        )

    row = {
        "name": "Relay", "max_simultaneous": 4,
        "loads": [{"name": "coil", "current_ma": 100, "max_concurrent": 4,
                   "evidence_id": value("param:Relay.current.coil", 100)}],
        "source_limit": {"name": "feed", "current_ma": source_ma,
                         "evidence_id": value("param:Relay.source.source", source_ma)},
        "connector_limits": [{"name": "J1", "current_ma": 500,
                              "evidence_id": value("param:Relay.connector.J1", 500)}],
        "distribution_limits": [{"name": "branch", "current_ma": 500,
                                 "evidence_id": value("param:Relay.distribution.branch", 500)}],
    }
    spec = _parse_yaml(Path(__file__).resolve().parents[1] / "samples/led_power_indicator/led_power_indicator.yaml")
    spec["operating_cases"] = {"cases": [row], "evidence_manifest": ledger.to_manifest()}
    return spec


def validate(spec, **kwargs):
    return dispatcher.validate_design(
        spec, use_kicad=False, check_native_export=False, check_determinism=False, **kwargs,
    )


def test_nominal_spec_resolves_real_ledger_ids_and_preserves_qualification_limits():
    spec = authored_spec()
    result, ledger = evaluate_spec_operating_cases(spec)
    assert result["status"] == "passed"
    case = result["cases"][0]
    assert case["required_ma"] == 400
    assert case["functionality_qualification"] == case["thermal_qualification"] == "unverified"
    report = validate(spec)
    assert report.metadata["operating_cases"] == result
    known = {row["id"] for row in report.metadata["evidence_manifest"]["records"]}
    assert set(case["evidence_ids"]) <= known
    message = next(message for message in report.categories["electrical"] if message.code == "operating-current-budget")
    assert set(case["evidence_ids"]) <= set(message.evidence_ids)
    assert ledger.to_manifest()["records"]
    assert evaluate_spec_operating_cases(spec)[0] == result


def test_supported_overload_blocks_validation_before_artifact_generation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("An overloaded authored feed must not reach artifact generation")

    monkeypatch.setattr(dispatcher, "_generate_compiled_artifacts", forbidden)
    report = validate(authored_spec(399))
    assert not report.valid
    assert report.metadata["operating_cases"]["status"] == "failed"
    assert report.metadata["native_export"]["status"] == "not_run"


@pytest.mark.parametrize("change", ["unknown_id", "mismatch", "wrong_subject", "stale", "single_source", "no_hash"])
def test_missing_or_inapplicable_load_provenance_cannot_establish_pass_or_overload(change):
    spec = authored_spec(1)
    document = spec["operating_cases"]
    load = document["cases"][0]["loads"][0]
    record = next(row for row in document["evidence_manifest"]["records"] if row["id"] == load["evidence_id"])
    if change == "unknown_id":
        load["evidence_id"] = "EV-UNKNOWN-123456789abc"
    elif change == "mismatch":
        load["current_ma"] = 101
    elif change == "wrong_subject":
        load["evidence_id"] = document["cases"][0]["source_limit"]["evidence_id"]
    elif change in {"stale", "single_source"}:
        record["freshness" if change == "stale" else "confidence"] = change
    else:
        record["source"]["content_hash"] = None
    result, _ = evaluate_spec_operating_cases(spec)
    assert result["status"] == "unverified"
    assert all(row["status"] == "unverified" for row in result["cases"][0]["boundary_results"])


@pytest.mark.parametrize("document", [None, [], {}, {"cases": []}, {"cases": [], "unknown": True}])
def test_malformed_documents_are_visible_validation_errors(document):
    spec = authored_spec()
    spec["operating_cases"] = document
    report = validate(spec)
    assert not report.valid
    assert any(row.code == "invalid-operating-cases" for row in report.categories["electrical"])


def test_changed_declared_values_and_source_metadata_change_document_hash():
    spec = authored_spec()
    before = evaluate_spec_operating_cases(spec)[0]
    changed = copy.deepcopy(spec)
    changed["operating_cases"]["evidence_manifest"]["records"][0]["source"]["content_hash"] = "b" * 64
    assert evaluate_spec_operating_cases(changed)[0]["input_sha256"] != before["input_sha256"]
    changed = copy.deepcopy(spec)
    changed["operating_cases"]["cases"][0]["loads"][0]["max_concurrent"] = 3
    changed["operating_cases"]["cases"][0]["max_simultaneous"] = 3
    assert evaluate_spec_operating_cases(changed)[0]["input_sha256"] != before["input_sha256"]


def test_no_authored_case_is_not_implicitly_qualified():
    spec = authored_spec()
    del spec["operating_cases"]
    assert evaluate_spec_operating_cases(spec)[0] == {"status": "not_requested", "cases": []}


def test_unknown_rating_is_visible_and_strict_validation_rejects_it():
    spec = authored_spec()
    spec["operating_cases"]["cases"][0]["source_limit"] = None
    report = validate(spec)
    assert report.valid
    assert report.metadata["operating_cases"]["status"] == "unverified"
    assert not validate(spec, strict=True).valid


def test_superseded_load_rating_is_not_reused_as_current():
    spec = authored_spec(1)
    document = spec["operating_cases"]
    ledger = EvidenceLedger.from_manifest(document["evidence_manifest"])
    old_id = document["cases"][0]["loads"][0]["evidence_id"]
    old = ledger.get(old_id)
    ledger.record(
        subject_ref=old.subject_ref, claim=json.dumps({"value": 200, "unit": "mA"}),
        kind="calculation", source=EvidenceSource(
            doc_id="revised-synthetic-control", content_hash="b" * 64, extraction_method="authored-test-fixture",
        ), confidence="verified", freshness="current", supersedes=old_id,
    )
    document["evidence_manifest"] = ledger.to_manifest()
    assert evaluate_spec_operating_cases(spec)[0]["status"] == "unverified"


@pytest.mark.parametrize("mutation", ["id", "claim", "duplicate", "empty_loads", "unknown_field", "boolean_current"])
def test_invalid_case_or_tampered_ledger_cannot_pass(mutation):
    spec = authored_spec()
    document = spec["operating_cases"]
    if mutation in {"id", "claim"}:
        document["evidence_manifest"]["records"][0][mutation] = "tampered"
    elif mutation == "duplicate":
        document["cases"].append(copy.deepcopy(document["cases"][0]))
    elif mutation == "empty_loads":
        document["cases"][0]["loads"] = []
    elif mutation == "unknown_field":
        document["cases"][0]["loads"][0]["confidence"] = "verified"
    else:
        document["cases"][0]["loads"][0]["current_ma"] = True
    report = validate(spec)
    assert not report.valid
    assert report.metadata["operating_cases"]["status"] == "failed"


def test_mcp_summary_preserves_current_case_results_and_failure(monkeypatch):
    original = dispatcher.validate_design
    monkeypatch.setattr(dispatcher, "validate_design", lambda spec: original(
        spec, use_kicad=False, check_native_export=False, check_determinism=False,
    ))
    payload = json.loads(mcp_server._validate_design_tool(json.dumps(authored_spec(399))))
    assert payload["valid"] is False
    assert payload["metadata"]["operating_cases"]["status"] == "failed"


def test_shared_workflow_stops_on_overload_and_retains_successful_budget(tmp_path, monkeypatch):
    spec = authored_spec(399)
    spec_path = tmp_path / "design.yaml"
    spec_path.write_text(json.dumps(spec))
    monkeypatch.setattr(project_workflow, "_spec_path", lambda root: spec_path)
    original = dispatcher.validate_design
    monkeypatch.setattr(dispatcher, "validate_design", lambda spec, **kwargs: original(
        spec, use_kicad=False, check_native_export=False, **kwargs,
    ))
    with pytest.raises(project_workflow.WorkflowError) as error:
        project_workflow._execute_stage(tmp_path, "validate", {}, [])
    assert error.value.code == "validation_failed"
    spec_path.write_text(json.dumps(authored_spec()))
    result, _ = project_workflow._execute_stage(tmp_path, "validate", {}, [])
    assert result["operating_cases"]["status"] == "passed"
