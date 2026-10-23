"""CLI integration tests for new Sprint 26-30 commands.

Tests verify that CLI subcommands work end-to-end:
- Exit codes are correct
- stdout/stderr separation is maintained
- JSON output is valid when --json is used
- Error cases produce helpful messages
"""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from circuit_weaver.design_logger import DesignLogger
from circuit_weaver.dispatcher import (
    ValidationMessage,
    ValidationReport,
    _print_validation_report,
)
from circuit_weaver.evidence import EvidenceLedger, EvidenceSource

_PYTHON = sys.executable
_MINIMAL_SPEC = "project: CLITest\nblocks: []\n"


def _run(args: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_PYTHON, "-m", "circuit_weaver", *args],
        capture_output=True, text=True, cwd=cwd,
    )


@pytest.fixture
def project_dir(tmp_path):
    """Create a temp project with minimal design.yaml."""
    proj = tmp_path / "test_project"
    proj.mkdir()
    (proj / "design.yaml").write_text(_MINIMAL_SPEC)
    return proj


@pytest.fixture
def workspace(tmp_path):
    """Create a workspace with discoverable projects."""
    p1 = tmp_path / "sensor_v1"
    p1.mkdir()
    (p1 / "design.yaml").write_text("project: Sensor_v1\nblocks: []\n")

    p2 = tmp_path / "motor_ctrl"
    p2.mkdir()
    (p2 / "motor_ctrl.kicad_pro").write_text("{}")

    empty = tmp_path / "not_a_project"
    empty.mkdir()

    return tmp_path


def test_manufacturing_readiness_cli_reads_canonical_json(tmp_path):
    payload = {
        "state": "needs_review",
        "blockers": ["routing_incomplete"],
        "evidence_ids": [],
        "next_actions": ["Complete routing."],
        "blocked_reason": None,
    }
    path = tmp_path / "manufacturing_readiness.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _run(["manufacturing-readiness", str(path), "--json"])

    assert result.returncode == 0
    assert json.loads(result.stdout) == payload


def test_gerber_export_fails_closed_before_creating_output_without_readiness(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb (version 20240108) (generator pcbnew))", encoding="utf-8")
    output = tmp_path / "gerbers"

    result = _run(["export-gerbers", str(board), "--output", str(output)])

    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "blocked"
    assert not output.exists()


def test_gerber_export_rejects_forged_ready_state_with_empty_evidence_ids(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb (version 20240108) (generator pcbnew))", encoding="utf-8")
    readiness = {
        "state": "fabrication_ready",
        "blockers": [],
        "evidence_ids": [],
        "next_actions": [],
        "blocked_reason": None,
    }
    (tmp_path / "manufacturing_readiness.json").write_text(json.dumps(readiness), encoding="utf-8")
    ledger = EvidenceLedger()
    for subject, kind in (
        ("comp:U1", "datasheet"),
        ("tool:pcb_handoff", "tool_result"),
        ("tool:drc", "tool_result"),
    ):
        ledger.record(
            subject_ref=subject,
            claim=f"verified evidence for {subject}",
            kind=kind,
            source=EvidenceSource(doc_id=subject, extraction_method="test"),
            confidence="verified",
            freshness="current",
        )
    ledger.write(tmp_path)
    output = tmp_path / "gerbers"

    result = _run(["export-gerbers", str(board), "--output", str(output)])

    assert result.returncode == 1
    response = json.loads(result.stdout)
    assert response["status"] == "blocked"
    assert "evidence IDs" in response["message"]
    assert not output.exists()


# ── discover ──────────────────────────────────────────────────────────────

class TestDiscoverCLI:
    def test_discover_json_output(self, workspace):
        result = _run(["discover", "--root", str(workspace), "--json"])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        names = {p["name"] for p in data}
        assert "sensor_v1" in names
        assert "motor_ctrl" in names
        assert "not_a_project" not in names

    def test_discover_table_output(self, workspace):
        result = _run(["discover", "--root", str(workspace)])
        assert result.returncode == 0
        assert "sensor_v1" in result.stdout
        assert "motor_ctrl" in result.stdout

    def test_discover_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        result = _run(["discover", "--root", str(empty)])
        assert result.returncode == 0
        assert "No circuit projects" in result.stdout

    def test_discover_nonexistent_dir(self, tmp_path):
        result = _run(["discover", "--root", str(tmp_path / "nope")])
        assert result.returncode == 0  # empty list, not an error

    def test_discover_depth_flag(self, workspace):
        result = _run(["discover", "--root", str(workspace), "--depth", "1", "--json"])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)


# ── simulate ──────────────────────────────────────────────────────────────

class TestSimulateCLI:
    def test_simulate_json_output(self, project_dir):
        result = _run(["simulate", str(project_dir / "design.yaml"), "-o", str(project_dir / "sims"), "--json"])
        assert result.returncode == 2
        data = json.loads(result.stdout)
        assert data['status'] == 'unverified'
        assert "confidence_score" in data
        assert "plan" in data

    def test_simulate_text_output(self, project_dir):
        result = _run(["simulate", str(project_dir / "design.yaml"), "-o", str(project_dir / "sims")])
        assert result.returncode == 2
        assert "simulation" in result.stdout.lower()

    def test_simulate_type_filter(self, project_dir):
        result = _run([
            "simulate", str(project_dir / "design.yaml"),
            "-o", str(project_dir / "sims"), "--type", "power", "--json",
        ])
        assert result.returncode == 2

    def test_simulate_bad_spec(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: valid: spec: [")
        result = _run(["simulate", str(bad), "-o", str(tmp_path / "sims")])
        assert result.returncode != 0
        assert "error" in result.stderr.lower() or "Error" in result.stderr

    @pytest.mark.parametrize(('status', 'exit_code'), [('verified', 0), ('failed', 1), ('unverified', 2), (None, 2)])
    def test_simulate_exit_code_requires_verified_evidence(self, tmp_path, monkeypatch, capsys, status, exit_code):
        from types import SimpleNamespace

        from circuit_weaver import dispatcher, simulation

        spec = tmp_path / 'design.yaml'
        spec.write_text(_MINIMAL_SPEC, encoding='utf-8')
        payload = {'status': status or 'unverified'}
        attributes = {'to_dict': lambda: payload}
        if status is not None:
            attributes['status'] = status
        monkeypatch.setattr(simulation, 'run_design_simulations', lambda *a, **kw: SimpleNamespace(**attributes))
        monkeypatch.setattr(sys, 'argv', ['circuit-weaver', 'simulate', str(spec), '--json'])
        with pytest.raises(SystemExit) as exc:
            dispatcher.main()
        assert exc.value.code == exit_code
        assert json.loads(capsys.readouterr().out) == payload


# ── confidence ────────────────────────────────────────────────────────────

class TestConfidenceCLI:
    def test_confidence_json_output(self, project_dir):
        result = _run(["confidence", str(project_dir / "design.yaml"), "--json"])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "overall_score" in data
        assert "readiness" in data
        assert "sections" in data

    def test_confidence_terminal_output(self, project_dir):
        result = _run(["confidence", str(project_dir / "design.yaml")])
        assert result.returncode == 0
        assert "Score" in result.stdout or "score" in result.stdout.lower()

    def test_confidence_html_output(self, project_dir):
        html_path = project_dir / "report.html"
        result = _run(["confidence", str(project_dir / "design.yaml"), "-o", str(html_path)])
        assert result.returncode == 0
        assert html_path.exists()
        content = html_path.read_text()
        assert "<html>" in content

    def test_confidence_with_run_sims(self, project_dir):
        result = _run(["confidence", str(project_dir / "design.yaml"), "--run-sims", "--json"])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "overall_score" in data

    def test_confidence_bad_spec(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("not valid yaml [[[")
        result = _run(["confidence", str(bad), "--json"])
        assert result.returncode != 0


# ── log-event ─────────────────────────────────────────────────────────────

class TestLogEventCLI:
    def test_log_wizard_step(self, project_dir):
        result = _run([
            "log-event", str(project_dir), "--type", "wizard_step",
            "--message", "Step 1: Setup",
            "--data", '{"step": 1, "user_input": {"name": "Test"}}',
        ])
        assert result.returncode == 0
        # Verify entry was written
        log = project_dir / "design.log"
        assert log.exists()
        entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert any(e.get("type") == "wizard_step" for e in entries)

    def test_log_error_event(self, project_dir):
        result = _run([
            "log-event", str(project_dir), "--type", "error",
            "--message", "Something broke",
            "--data", '{"operation": "generate"}',
        ])
        assert result.returncode == 0

    def test_log_scoring_event(self, project_dir):
        result = _run([
            "log-event", str(project_dir), "--type", "scoring",
            "--message", "Design scored",
            "--data", '{"dimension": "overall", "score": 85.0, "grade": "B"}',
        ])
        assert result.returncode == 0

    def test_log_invalid_json(self, project_dir):
        result = _run([
            "log-event", str(project_dir), "--type", "scoring",
            "--message", "test", "--data", "not-json",
        ])
        assert result.returncode == 1
        assert "JSON" in result.stderr

    def test_log_creates_dir(self, tmp_path):
        new_dir = tmp_path / "new_project"
        result = _run([
            "log-event", str(new_dir), "--type", "wizard_step",
            "--message", "Created project",
        ])
        assert result.returncode == 0
        assert new_dir.exists()

    def test_log_all_event_types(self, project_dir):
        """Smoke test: every event type should succeed."""
        types_and_data = [
            ("wizard_step", '{"step": 1}'),
            ("cli_call", '{"command": "validate", "args": ["x"], "return_code": 0}'),
            ("validation", '{"spec_file": "x", "passed": true}'),
            ("research", '{"phase": "test", "status": "ok"}'),
            ("part_lookup", '{"mpn": "X", "source": "y", "status": "ok"}'),
            ("symbol_resolution", '{"ref": "U1", "mpn": "X", "status": "ok"}'),
            ("simulation", '{"sim_type": "tran", "target": "U1", "status": "ok"}'),
            ("thermal", '{"ref": "U1", "tj_calc": 85, "tj_max": 125, "status": "ok"}'),
            ("erc_drc", '{"check_type": "erc", "file": "x", "errors": 0, "warnings": 0}'),
            ("scoring", '{"dimension": "overall", "score": 80, "grade": "B"}'),
            ("sourcing", '{"mpn": "X", "supplier": "lcsc", "status": "ok"}'),
            ("generation", '{"artifact_type": "sch", "path": "x", "status": "ok"}'),
            ("error", '{"operation": "test"}'),
        ]
        for event_type, data in types_and_data:
            result = _run([
                "log-event", str(project_dir), "--type", event_type,
                "--message", f"test {event_type}", "--data", data,
            ])
            assert result.returncode == 0, f"Failed for type {event_type}: {result.stderr}"


# ── log-status / log-view ─────────────────────────────────────────────────

class TestLogStatusCLI:
    def test_log_status_empty(self, project_dir):
        result = _run(["log-status", str(project_dir)])
        assert result.returncode == 0

    def test_log_status_with_entries(self, project_dir):
        # Write some log entries first
        _run(["log-event", str(project_dir), "--type", "wizard_step", "--message", "setup"])
        result = _run(["log-status", str(project_dir)])
        assert result.returncode == 0

    def test_log_status_shows_validation_only_session(self, project_dir):
        logger = DesignLogger(project_dir)
        logger.log_validation(
            spec_file="design.yaml",
            passed=False,
            errors=["[placement_readiness:single-pin-net] dangling net"],
            warnings=["[electrical:crystal-load] missing load caps"],
            scope="final_report",
            error_count=9,
            warning_count=2,
        )
        result = _run(["log-status", str(project_dir)])
        assert result.returncode == 0
        assert "No design workflow recorded yet." not in result.stdout
        assert "Validation:  FAILED (9 errors, 2 warnings)" in result.stdout


class TestLogViewCLI:
    def test_log_view_empty(self, project_dir):
        result = _run(["log-view", str(project_dir)])
        # log-view exits 1 when no entries found (expected behavior)
        assert result.returncode in (0, 1)
        assert "No log entries" in result.stdout or result.returncode == 0

    def test_log_view_with_entries(self, project_dir):
        _run(["log-event", str(project_dir), "--type", "wizard_step", "--message", "setup"])
        result = _run(["log-view", str(project_dir)])
        assert result.returncode == 0


class TestValidationReportOutput:
    def test_print_validation_report_falls_back_to_ascii_on_cp1252(self, monkeypatch):
        report = ValidationReport(
            profile="standard",
            valid=False,
            categories={
                "placement_readiness": [
                    ValidationMessage(
                        category="placement_readiness",
                        code="single-pin-net",
                        level="error",
                        subject="U1",
                        message="dangling net",
                    )
                ]
            },
            metadata={"project": "CP1252Probe"},
        )
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        monkeypatch.setattr(sys, "stdout", stream)
        _print_validation_report(report, use_color=False, verbose=False)
        stream.flush()
        rendered = raw.getvalue().decode("cp1252")
        assert "FAIL CP1252Probe" in rendered
        assert "dangling net" in rendered
