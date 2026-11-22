"""T252 one manufacturing-readiness state across gates and surfaces."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from circuit_weaver.manufacturing_readiness import (
    ManufacturingReadiness,
    ManufacturingReadinessInputs,
    ManufacturingReadinessOverride,
    ManufacturingReadinessState,
    ReadinessContractError,
    assess_manufacturing_readiness,
    read_manufacturing_readiness,
    require_export_authorized,
)


def _record(record_id: str, subject: str, kind: str = "tool_result") -> dict:
    return {
        "id": record_id,
        "subject_ref": subject,
        "claim": json.dumps({"board_sha256": "a" * 64}) if subject == "tool:drc" else "verified",
        "source": {"content_hash": "a" * 64} if subject == "tool:drc" else {},
        "kind": kind,
        "confidence": "verified",
        "conflicts": [],
    }


def _fabrication_evidence() -> tuple[dict, ...]:
    return (
        _record("EV-DATASHEET-000000000001", "comp:U1", "datasheet"),
        _record("EV-TOOL_RESULT-000000000002", "tool:pcb_handoff"),
        _record("EV-TOOL_RESULT-000000000003", "tool:drc"),
    )


def test_fabrication_export_requires_the_exact_drc_board_hash_unless_overridden() -> None:
    records = _fabrication_evidence()
    readiness = ManufacturingReadiness.from_dict(
        {
            "state": "fabrication_ready",
            "blockers": [],
            "evidence_ids": sorted(record["id"] for record in records),
            "next_actions": [],
            "blocked_reason": None,
        }
    )

    require_export_authorized(readiness, evidence_records=records, board_sha256="a" * 64)
    with pytest.raises(ReadinessContractError, match="does not match the exact board bytes"):
        require_export_authorized(readiness, evidence_records=records, board_sha256="b" * 64)

    explicit_override = ManufacturingReadinessOverride(
        id="OVR-TEST-001",
        reason="User approved export of the changed board",
        expires_at="2030-01-01T00:00:00+00:00",
    )
    require_export_authorized(
        readiness,
        evidence_records=records,
        board_sha256="b" * 64,
        override=explicit_override,
        now=datetime(2029, 1, 1, tzinfo=timezone.utc),
    )


def test_fabrication_export_requires_drc_source_hash_to_match_the_board() -> None:
    records = [dict(record) for record in _fabrication_evidence()]
    drc = next(record for record in records if record["subject_ref"] == "tool:drc")
    drc["source"] = {"content_hash": "b" * 64}
    readiness = ManufacturingReadiness.from_dict(
        {
            "state": "fabrication_ready",
            "blockers": [],
            "evidence_ids": sorted(record["id"] for record in records),
            "next_actions": [],
            "blocked_reason": None,
        }
    )

    with pytest.raises(ReadinessContractError, match="does not bind its source hash"):
        require_export_authorized(readiness, evidence_records=records, board_sha256="a" * 64)


def test_central_assessor_walks_the_one_ordered_vocabulary() -> None:
    assert assess_manufacturing_readiness(ManufacturingReadinessInputs()).state is ManufacturingReadinessState.NOT_READY
    assert assess_manufacturing_readiness(
        ManufacturingReadinessInputs(identity_complete=True, placement_approved=True)
    ).state is ManufacturingReadinessState.NEEDS_REVIEW
    assert assess_manufacturing_readiness(
        ManufacturingReadinessInputs(
            identity_complete=True,
            placement_approved=True,
            routing_complete=True,
        )
    ).state is ManufacturingReadinessState.DRC_PENDING
    assert assess_manufacturing_readiness(
        ManufacturingReadinessInputs(
            identity_complete=True,
            placement_approved=True,
            routing_complete=True,
            drc_completed=True,
            drc_passed=True,
        ),
        evidence_records=_fabrication_evidence(),
    ).state is ManufacturingReadinessState.DRC_CLEAN
    assert assess_manufacturing_readiness(
        ManufacturingReadinessInputs(
            identity_complete=True,
            placement_approved=True,
            routing_complete=True,
            erc_passed=True,
            drc_completed=True,
            drc_passed=True,
            bom_cpl_reconciled=True,
            fabrication_artifacts_valid=True,
        ),
        evidence_records=_fabrication_evidence(),
    ).state is ManufacturingReadinessState.FABRICATION_READY


def test_fabrication_ready_cannot_be_constructed_or_assessed_without_identity_pads_drc_gate() -> None:
    with pytest.raises(ReadinessContractError, match="evidence gate"):
        ManufacturingReadiness(state=ManufacturingReadinessState.FABRICATION_READY)
    with pytest.raises(ReadinessContractError, match="identity/pads/DRC"):
        assess_manufacturing_readiness(
            ManufacturingReadinessInputs(
                identity_complete=True,
                placement_approved=True,
                routing_complete=True,
                erc_passed=True,
                drc_completed=True,
                drc_passed=True,
                bom_cpl_reconciled=True,
                fabrication_artifacts_valid=True,
            ),
            evidence_records=(_record("EV-TOOL_RESULT-000000000003", "tool:drc"),),
        )


def test_readiness_artifact_round_trip_is_exact(tmp_path) -> None:
    readiness = assess_manufacturing_readiness(
        ManufacturingReadinessInputs(identity_complete=True, placement_approved=True),
        evidence_records=_fabrication_evidence(),
    )
    path = readiness.write(tmp_path)

    assert read_manufacturing_readiness(path) == readiness
    assert json.loads(path.read_text(encoding="utf-8")) == readiness.to_dict()

    project = tmp_path / "project"
    readiness.write(project / "output")
    assert read_manufacturing_readiness(project) == readiness


def test_readiness_reader_rejects_payload_normalization() -> None:
    with pytest.raises(ReadinessContractError, match="sorted and unique"):
        ManufacturingReadiness.from_dict(
            {
                "state": "not_ready",
                "blockers": ["z", "a", "a"],
                "evidence_ids": [],
                "next_actions": [],
                "blocked_reason": None,
            }
        )


def test_export_requires_ready_or_explicit_unexpired_override() -> None:
    readiness = ManufacturingReadiness()
    with pytest.raises(ReadinessContractError, match="explicit override"):
        require_export_authorized(readiness)
    expired = ManufacturingReadinessOverride(
        id="OVR-1",
        reason="engineering disposition",
        expires_at="2025-01-01T00:00:00Z",
    )
    with pytest.raises(ReadinessContractError, match="expired"):
        require_export_authorized(
            readiness,
            override=expired,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    malformed_expiry = ManufacturingReadinessOverride(
        id="OVR-BAD",
        reason="engineering disposition",
        expires_at=None,  # type: ignore[arg-type] - runtime boundary regression
    )
    with pytest.raises(ReadinessContractError, match="ISO-8601"):
        require_export_authorized(readiness, override=malformed_expiry)
    current = ManufacturingReadinessOverride(
        id="OVR-2",
        reason="engineering disposition",
        expires_at="2027-01-01T00:00:00Z",
    )
    require_export_authorized(
        readiness,
        override=current,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_read_back_fabrication_ready_revalidates_linked_evidence() -> None:
    records = _fabrication_evidence()
    ready = assess_manufacturing_readiness(
        ManufacturingReadinessInputs(
            identity_complete=True,
            placement_approved=True,
            routing_complete=True,
            erc_passed=True,
            drc_completed=True,
            drc_passed=True,
            bom_cpl_reconciled=True,
            fabrication_artifacts_valid=True,
        ),
        evidence_records=records,
    )
    loaded = ManufacturingReadiness.from_dict(ready.to_dict())

    require_export_authorized(loaded, evidence_records=records)
    with pytest.raises(ReadinessContractError, match="evidence IDs"):
        require_export_authorized(
            ManufacturingReadiness.from_dict({**ready.to_dict(), "evidence_ids": []}),
            evidence_records=records,
        )


def test_failed_drc_is_terminal_blocked_with_reason() -> None:
    readiness = assess_manufacturing_readiness(
        ManufacturingReadinessInputs(
            identity_complete=True,
            placement_approved=True,
            routing_complete=True,
            drc_completed=True,
            drc_passed=False,
        )
    )

    assert readiness.state is ManufacturingReadinessState.BLOCKED
    assert readiness.blocked_reason
