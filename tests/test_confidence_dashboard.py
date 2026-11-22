"""Tests for design confidence dashboard."""

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from circuit_weaver.confidence_dashboard import (
    ConfidenceSection,
    _grade,
    _score_from_issues,
    generate_confidence_report,
)
from circuit_weaver.cross_reference_validator import CrossReferenceResult
from circuit_weaver.manufacturing_readiness import (
    ManufacturingReadinessInputs,
    assess_manufacturing_readiness,
)

# Mock objects for testing

@dataclass
class MockValidationReport:
    valid: bool = True
    summary: str = "OK"

    def to_dict(self):
        return {
            "valid": self.valid,
            "summary": self.summary,
            "categories": {
                "structural": [
                    {"level": "error", "message": "test error"} if not self.valid else {},
                ],
            },
        }


@dataclass
class MockErcResult:
    status: str = "ok"
    errors: int = 0
    warnings: int = 0

    def to_dict(self):
        return {"status": self.status, "errors": self.errors, "warnings": self.warnings}


@dataclass
class MockSimReport:
    confidence_score: float = 80.0
    grade: str = "B"
    recommendations: list = field(default_factory=list)

    def to_dict(self):
        return {
            "confidence_score": self.confidence_score,
            "grade": self.grade,
            "recommendations": self.recommendations,
        }


class TestGrade:
    def test_a_grade(self):
        assert _grade(95) == "A"

    def test_b_grade(self):
        assert _grade(85) == "B"

    def test_c_grade(self):
        assert _grade(75) == "C"

    def test_d_grade(self):
        assert _grade(65) == "D"

    def test_f_grade(self):
        assert _grade(50) == "F"


class TestScoreFromIssues:
    def test_no_issues(self):
        assert _score_from_issues(10, 0, 0) == 100.0

    def test_with_errors(self):
        score = _score_from_issues(10, 2, 0)
        assert score == 60.0  # 100 - 2*20

    def test_with_warnings(self):
        score = _score_from_issues(10, 0, 4)
        assert score == 80.0  # 100 - 4*5

    def test_floor_at_zero(self):
        score = _score_from_issues(1, 10, 10)
        assert score == 0.0

    def test_zero_checks(self):
        assert _score_from_issues(0, 0, 0) == 100.0

    def test_zero_checks_with_errors_not_100(self):
        """Bug fix: _score_from_issues(0, 3, 0) must NOT return 100."""
        score = _score_from_issues(0, 3, 0)
        assert score < 100.0
        assert score == 40.0  # 100 - 3*20

    def test_zero_checks_with_warnings_not_100(self):
        score = _score_from_issues(0, 0, 5)
        assert score == 75.0  # 100 - 5*5


class TestGenerateConfidenceReport:
    def test_all_data_sources(self):
        report = generate_confidence_report(
            project="TestProject",
            validation_report=MockValidationReport(valid=True),
            sim_report=MockSimReport(confidence_score=85),
            thermal_result={"components": [], "recommendations": []},
            dfm_violations=[],
            erc_result=MockErcResult(errors=0, warnings=0),
            xref_results=[
                CrossReferenceResult(pass_name="test", status="pass", checked_items=5),
            ],
        )
        assert report.overall_score > 0
        assert report.overall_grade in ("A", "B", "C", "D", "F")
        assert report.readiness == "not_ready"
        assert len(report.sections) == 7

    def test_validation_only(self):
        report = generate_confidence_report(
            validation_report=MockValidationReport(valid=True),
        )
        assert report.sections["electrical"].status == "complete"
        assert report.sections["simulation"].status == "skipped"
        assert report.overall_score > 0  # electrical contributes

    def test_no_data(self):
        report = generate_confidence_report()
        assert report.overall_score == 0.0
        assert report.readiness == "not_ready"
        for section in report.sections.values():
            assert section.status == "skipped"

    def test_blockers_from_errors(self):
        report = generate_confidence_report(
            validation_report=MockValidationReport(valid=False),
            erc_result=MockErcResult(errors=3, warnings=1),
        )
        assert len(report.blockers) >= 1
        assert any("ERC" in b for b in report.blockers)

    def test_high_score_does_not_compute_fabrication_readiness(self):
        report = generate_confidence_report(
            validation_report=MockValidationReport(valid=True),
            sim_report=MockSimReport(confidence_score=95),
            thermal_result={"components": [], "recommendations": []},
            dfm_violations=[],
            erc_result=MockErcResult(errors=0),
            xref_results=[CrossReferenceResult("test", "pass", [], 5)],
        )
        assert report.readiness == "not_ready"

    def test_confidence_reads_supplied_fabrication_ready_state(self):
        records = (
            {
                "id": "EV-DATASHEET-000000000001",
                "subject_ref": "comp:U1",
                "kind": "datasheet",
                "confidence": "verified",
                "conflicts": [],
            },
            {
                "id": "EV-TOOL_RESULT-000000000002",
                "subject_ref": "tool:pcb_handoff",
                "kind": "tool_result",
                "confidence": "verified",
                "conflicts": [],
            },
            {
                "id": "EV-TOOL_RESULT-000000000003",
                "subject_ref": "tool:drc",
                "kind": "tool_result",
                "confidence": "verified",
                "conflicts": [],
            },
        )
        readiness = assess_manufacturing_readiness(
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

        report = generate_confidence_report(manufacturing_readiness=readiness)

        assert report.readiness == "fabrication_ready"
        assert report.to_dict()["readiness"] == readiness.to_dict()

    def test_weight_redistribution(self):
        # Only electrical available -- should still produce a meaningful score
        report = generate_confidence_report(
            validation_report=MockValidationReport(valid=True),
        )
        # Electrical score should be high (no errors), overall should reflect it
        assert report.sections["electrical"].score > 80
        assert report.overall_score > 50  # redistributed weight from electrical

    def test_thermal_critical_is_blocker(self):
        report = generate_confidence_report(
            thermal_result={
                "components": [
                    {"ref": "U1", "status": "critical", "tj_calculated": 130, "tj_max": 125},
                ],
                "recommendations": ["Add heatsink"],
            },
        )
        assert any("Thermal" in b for b in report.blockers)

    def test_to_dict_serializable(self):
        report = generate_confidence_report(project="Test")
        d = report.to_dict()
        assert d["project"] == "Test"
        assert isinstance(d["overall_score"], float)
        assert isinstance(d["sections"], dict)
        # Should be JSON-serializable
        import json
        json.dumps(d)

    def test_collects_validation_evidence_for_electrical_section_and_report(self):
        class EvidenceValidationReport:
            def to_dict(self):
                return {
                    "categories": {"electrical": [{"level": "warning", "evidence_ids": ["EV-USER-1"]}]},
                    "evidence_ids": ["EV-CALC-2"],
                    "evidence_manifest": "evidence_manifest.json",
                }

        report = generate_confidence_report(validation_report=EvidenceValidationReport())

        assert report.evidence_ids == ["EV-CALC-2", "EV-USER-1"]
        assert report.evidence_manifest == "evidence_manifest.json"
        assert report.sections["electrical"].evidence_ids == ["EV-CALC-2", "EV-USER-1"]

    def test_to_terminal(self):
        report = generate_confidence_report(
            project="TestProject",
            validation_report=MockValidationReport(valid=True),
        )
        text = report.to_terminal()
        assert "TestProject" in text
        assert "Confidence" in text or "confidence" in text.lower()
        assert "/100" in text

    def test_to_html(self):
        report = generate_confidence_report(
            project="TestProject",
            validation_report=MockValidationReport(valid=True),
        )
        html = report.to_html()
        assert "<html>" in html
        assert "TestProject" in html
        assert "/100" in html

    def test_action_items_from_recommendations(self):
        report = generate_confidence_report(
            sim_report=MockSimReport(
                confidence_score=50,
                recommendations=["Fix ripple", "Check phase margin"],
            ),
        )
        assert len(report.action_items) >= 2
        assert any("ripple" in a["description"].lower() for a in report.action_items)


class TestConfidenceSection:
    def test_to_dict(self):
        section = ConfidenceSection(
            name="Test", score=85.0, grade="B", status="complete",
            issues=[{"type": "test"}], recommendations=["fix it"],
        )
        d = section.to_dict()
        assert d["name"] == "Test"
        assert d["score"] == 85.0
        assert d["issue_count"] == 1


class TestConfidenceCLI:
    def test_missing_board_and_thermal_data_are_unassessed(self, tmp_path):
        source = Path(__file__).parents[1] / "samples/led_power_indicator/led_power_indicator.yaml"
        spec = tmp_path / "design.yaml"
        shutil.copyfile(source, spec)
        cmd = [sys.executable, "-m", "circuit_weaver", "confidence", str(spec), "--json",
               "--pcb", str(tmp_path / "missing.kicad_pcb")]
        report = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
        for name in ("thermal", "manufacturing"):
            section = report["sections"][name]
            assert (section["status"], section["grade"], section["score"]) == ("skipped", "N/A", 0)
        assert report["sections"]["thermal"]["assessed_components"] == 0
        assert any("DFM" in message for message in report["blockers"])

        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        data = {"AMS1117-3.3": {"theta_ja": 100, "pdiss_max_w": 2, "tj_max": 125}}
        (specs_dir / "ic_thermal.json").write_text(json.dumps(data), encoding="utf-8")
        with_data = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
        thermal = with_data["sections"]["thermal"]
        assert thermal["assessed_components"] == 1
        assert thermal["status"] == "partial"
        assert thermal["score"] < 100
        assert any("Thermal" in message for message in with_data["blockers"])

    def test_failed_erc_and_empty_audit_contribute_no_grade(self):
        report = generate_confidence_report(erc_result=MockErcResult(status="failed"), xref_results=[])
        assert report.sections["erc_drc"].grade == "N/A"
        assert report.sections["cross_reference"].grade == "N/A"
        assert report.overall_score == 0

    def test_confidence_cli_json(self, tmp_path):
        # Create a minimal spec
        spec = tmp_path / "design.yaml"
        spec.write_text("project: CLITest\nblocks: []")

        result = subprocess.run(
            [sys.executable, "-m", "circuit_weaver", "confidence", str(spec), "--json"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "overall_score" in data
        assert "readiness" in data
