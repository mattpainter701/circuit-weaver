"""T251 exact-byte DRC and shared-finding contracts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import circuit_weaver.drc_runner as drc_runner
from circuit_weaver.evidence import EvidenceLedger
from circuit_weaver.pcb_contracts import PcbConstraint
from circuit_weaver.validator import ValidationIssue


def _constraint() -> PcbConstraint:
    return PcbConstraint.create(
        klass="clearance",
        target="net:VBUS",
        params={"minimum": {"value": 0.2, "unit": "mm"}},
        origin="fab_profile",
        evidence_ids=("EV-DATASHEET-0123456789ab",),
    )


def _raw(*, severity="error"):
    return {
        "kicad_version": "10.0.4",
        "violations": [
            {
                "type": "clearance",
                "severity": severity,
                "description": "Clearance 0.10 mm below 0.20 mm on net VBUS",
                "items": [{"description": "Pad U1 1"}],
            }
        ],
        "unconnected_items": [],
        "schematic_parity": [],
    }


def test_parser_reuses_validation_issue_with_stable_rule_and_object_refs() -> None:
    first = drc_runner._parse_drc_json(
        _raw(),
        evidence_id="EV-TOOL_RESULT-0123456789ab",
        constraints=(_constraint(),),
    )
    second = drc_runner._parse_drc_json(
        _raw(),
        evidence_id="EV-TOOL_RESULT-0123456789ab",
        constraints=(_constraint(),),
    )

    assert first == second
    assert type(first[0]) is ValidationIssue
    assert first[0].rule_id == "CW-DRC-001"
    assert first[0].ref == "U1" and first[0].net == "VBUS"
    assert first[0].detection_confidence == "verified"
    assert _constraint().id in first[0].expected_constraint


def test_approved_override_is_explicit_suppression_not_deleted_finding() -> None:
    findings = drc_runner._parse_drc_json(
        _raw(),
        evidence_id="EV-TOOL_RESULT-0123456789ab",
        approved_overrides={"CW-DRC-001": "OVR-001"},
    )

    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert findings[0].suppression_id == "OVR-001"


def test_run_drc_hashes_exact_bytes_and_records_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = tmp_path / "board.kicad_pcb"
    payload = b"(kicad_pcb exact staged bytes)\r\n"
    board.write_bytes(payload)
    seen: dict[str, bytes] = {}

    def fake_run(command, **_kwargs):
        seen["bytes"] = Path(command[-1]).read_bytes()
        report = Path(command[command.index("--output") + 1])
        report.write_text(json.dumps(_raw(severity="warning")), encoding="utf-8")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(drc_runner, "_kicad_cli_path", lambda: Path("kicad-cli"))
    monkeypatch.setattr(drc_runner.subprocess, "run", fake_run)
    ledger = EvidenceLedger()
    result = drc_runner.run_drc(board, evidence_ledger=ledger, constraints=(_constraint(),))

    assert seen["bytes"] == payload == board.read_bytes()
    assert result.board_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.passed and result.blocker_count == 0
    assert type(result.findings[0]) is ValidationIssue
    record = ledger.get(result.evidence_id)
    assert record is not None and record.subject_ref == "tool:drc" and record.kind == "tool_result"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (subprocess.TimeoutExpired(cmd="kicad-cli", timeout=120), "timed out"),
        (None, "exited 2"),
    ],
)
def test_operational_failure_never_reports_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure,
    reason: str,
) -> None:
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    monkeypatch.setattr(drc_runner, "_kicad_cli_path", lambda: Path("kicad-cli"))
    if failure is not None:
        monkeypatch.setattr(drc_runner.subprocess, "run", MagicMock(side_effect=failure))
    else:
        monkeypatch.setattr(
            drc_runner.subprocess,
            "run",
            MagicMock(return_value=MagicMock(returncode=2, stdout="", stderr="load failed")),
        )

    result = drc_runner.run_drc(board, evidence_ledger=EvidenceLedger())

    assert not result.passed and result.status == "failed"
    assert reason in result.failure_reason


def test_parser_drift_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    def fake_run(command, **_kwargs):
        if command[1:] == ["version"]:
            return MagicMock(returncode=0, stdout="9.0.2\n", stderr="")
        Path(command[command.index("--output") + 1]).write_text(
            '{"violations": "not-a-list"}',
            encoding="utf-8",
        )
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(drc_runner, "_kicad_cli_path", lambda: Path("kicad-cli"))
    monkeypatch.setattr(drc_runner.subprocess, "run", fake_run)

    result = drc_runner.run_drc(board, evidence_ledger=EvidenceLedger())

    assert not result.passed
    assert "parse error" in result.failure_reason


@pytest.mark.parametrize("version", ["8.0.9", "9.0.6", "10.0.4"])
def test_kicad_version_variants_keep_one_finding_shape(version: str) -> None:
    raw = _raw()
    raw["kicad_version"] = version

    findings = drc_runner._parse_drc_json(
        raw,
        evidence_id="EV-TOOL_RESULT-0123456789ab",
    )

    assert len(findings) == 1 and type(findings[0]) is ValidationIssue


def test_missing_kicad_is_skipped_and_never_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    monkeypatch.setattr(drc_runner, "_kicad_cli_path", lambda: None)

    result = drc_runner.run_drc(board, evidence_ledger=EvidenceLedger())

    assert result.status == "skipped"
    assert not result.passed
    assert "not available" in result.failure_reason
