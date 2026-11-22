"""Design confidence dashboard for circuit-weaver.

Aggregates validation, simulation, thermal, signal integrity, DFM,
cross-reference, and ERC/DRC results into a unified confidence report
with actionable recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .manufacturing_readiness import (
    ManufacturingReadiness,
    ManufacturingReadinessState,
    read_manufacturing_readiness,
)


@dataclass
class ConfidenceSection:
    """A single section of the confidence report."""

    name: str
    score: float  # 0-100
    grade: str  # A-F
    status: str  # "complete", "partial", "skipped"
    issues: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    assessed_components: int | None = None

    def display_result(self) -> str:
        score = "N/A" if self.status == "skipped" else f"{self.score:.0f}/100 ({self.grade})"
        if self.assessed_components is None:
            return score
        unit = "component" if self.assessed_components == 1 else "components"
        return f"{score} · {self.assessed_components} {unit} assessed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 1),
            "grade": self.grade,
            "status": self.status,
            "issue_count": len(self.issues),
            "recommendations": self.recommendations,
            "evidence_ids": sorted(set(self.evidence_ids)),
            **({"assessed_components": self.assessed_components} if self.assessed_components is not None else {}),
        }


@dataclass
class DesignConfidenceReport:
    """Unified confidence report aggregating all data sources."""

    project: str = ""
    timestamp: str = ""
    overall_score: float = 0.0
    overall_grade: str = "F"
    manufacturing_readiness: ManufacturingReadiness = field(default_factory=ManufacturingReadiness)
    sections: dict[str, ConfidenceSection] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    action_items: list[dict[str, str]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    evidence_manifest: str | None = None

    @property
    def readiness(self) -> str:
        """Compatibility view of the canonical manufacturing-readiness state."""

        return self.manufacturing_readiness.state.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "timestamp": self.timestamp,
            "overall_score": round(self.overall_score, 1),
            "overall_grade": self.overall_grade,
            "readiness": self.manufacturing_readiness.to_dict(),
            "sections": {k: v.to_dict() for k, v in self.sections.items()},
            "blockers": self.blockers,
            "action_items": self.action_items,
            "evidence_ids": sorted(set(self.evidence_ids)),
            "evidence_manifest": self.evidence_manifest,
        }

    def to_terminal(self) -> str:
        """Generate terminal-friendly output."""
        lines = []
        lines.append("")
        lines.append("=" * 60)
        lines.append("Design Confidence Report")
        lines.append("=" * 60)
        lines.append(f"Project:    {self.project}")
        lines.append(f"Score:      {self.overall_score:.0f}/100 ({self.overall_grade})")
        lines.append(f"Readiness:  {self.readiness.replace('_', ' ').upper()}")
        lines.append("")

        lines.append("Sections:")
        skipped_hints: list[str] = []
        for name, section in self.sections.items():
            status_icon = {
                "complete": "[OK]",
                "partial": "[!!]",
                "skipped": "[--]",
            }.get(section.status, "[??]")
            lines.append(
                f"  {status_icon} {section.name:<25} {section.display_result()}"
            )
            if section.status == "skipped" and section.recommendations:
                skipped_hints.append(f"  {section.name}: {section.recommendations[0]}")

        if skipped_hints:
            lines.append("")
            lines.append("To improve your score, enable skipped sections:")
            lines.extend(skipped_hints)
            lines.append("")
            lines.append("Run 'circuit-weaver doctor' to check what's installed.")

        if self.blockers:
            lines.append("")
            lines.append(f"BLOCKERS ({len(self.blockers)}):")
            for b in self.blockers:
                lines.append(f"  !! {b}")

        if self.action_items:
            lines.append("")
            lines.append(f"Action Items ({len(self.action_items)}):")
            for item in self.action_items[:10]:
                lines.append(f"  - [{item.get('priority', '?')}] {item.get('description', '')}")

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)

    def to_html(self) -> str:
        """Generate self-contained HTML dashboard."""
        sections_html = ""
        for name, section in self.sections.items():
            color = (
                "#22c55e" if section.score >= 80
                else "#eab308" if section.score >= 60
                else "#ef4444"
            )
            sections_html += f"""
            <div style="margin:8px 0;padding:8px 12px;background:#f8f9fa;border-radius:6px;
                        border-left:4px solid {color}">
                <strong>{section.name}</strong>
                <span style="float:right;color:{color};font-weight:bold">
                    {section.display_result()}
                </span>
                <div style="font-size:0.85em;color:#666">{section.status}</div>
            </div>"""

        blockers_html = ""
        if self.blockers:
            items = "".join(f"<li style='color:#ef4444'>{b}</li>" for b in self.blockers)
            blockers_html = f"<h3>Blockers</h3><ul>{items}</ul>"

        actions_html = ""
        if self.action_items:
            items = "".join(
                f"<li>[{a.get('priority', '?')}] {a.get('description', '')}</li>"
                for a in self.action_items[:10]
            )
            actions_html = f"<h3>Action Items</h3><ul>{items}</ul>"

        readiness_color = {
            "fabrication_ready": "#22c55e",
            "drc_clean": "#3b82f6",
            "drc_pending": "#eab308",
            "needs_review": "#eab308",
            "not_ready": "#ef4444",
            "blocked": "#7f1d1d",
        }.get(self.readiness, "#666")

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Design Confidence: {self.project}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; }}
h1 {{ border-bottom: 2px solid #333; padding-bottom: 8px; }}
.score {{ font-size: 2.5em; font-weight: bold; text-align: center; margin: 20px 0; }}
.readiness {{ text-align: center; font-size: 1.2em; padding: 8px 16px;
             border-radius: 8px; display: inline-block; color: white;
             background: {readiness_color}; }}
.center {{ text-align: center; margin: 16px 0; }}
</style></head>
<body>
<h1>Design Confidence: {self.project}</h1>
<div class="score">{self.overall_score:.0f}/100 ({self.overall_grade})</div>
<div class="center"><span class="readiness">{self.readiness.replace('_', ' ').upper()}</span></div>
<h3>Sections</h3>
{sections_html}
{blockers_html}
{actions_html}
<p style="color:#999;font-size:0.8em">Generated {self.timestamp} by circuit-weaver</p>
</body></html>"""


_SECTION_WEIGHTS = {
    "electrical": 0.20,
    "simulation": 0.15,
    "thermal": 0.10,
    "signal_integrity": 0.10,
    "manufacturing": 0.15,
    "cross_reference": 0.15,
    "erc_drc": 0.15,
}


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def _score_from_issues(
    total_checks: int,
    errors: int,
    warnings: int,
) -> float:
    """Compute a 0-100 score from check/issue counts."""
    if total_checks == 0 and errors == 0 and warnings == 0:
        return 100.0
    # Errors cost 20 points each, warnings cost 5
    penalty = errors * 20 + warnings * 5
    return max(0.0, min(100.0, 100.0 - penalty))


def _evidence_ids(report: dict[str, Any]) -> list[str]:
    """Collect report- and finding-level evidence IDs without inventing any."""

    raw_ids = report.get("evidence_ids", [])
    ids = {value for value in raw_ids if isinstance(value, str)} if isinstance(raw_ids, list) else set()
    for findings in report.get("categories", {}).values():
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if isinstance(finding, dict) and isinstance(finding.get("evidence_ids"), list):
                ids.update(value for value in finding["evidence_ids"] if isinstance(value, str))
    return sorted(value for value in ids if isinstance(value, str))


def generate_confidence_report(
    components: list | None = None,
    *,
    project: str = "",
    validation_report: Any = None,
    sim_report: Any = None,
    thermal_result: dict | None = None,
    dfm_violations: list | None = None,
    erc_result: Any = None,
    xref_results: list | None = None,
    spec: dict | None = None,
    manufacturing_readiness: ManufacturingReadiness | dict[str, Any] | str | Path | None = None,
) -> DesignConfidenceReport:
    """Aggregate all available data sources into a unified confidence report.

    Each parameter is optional. Missing data sources are marked as "skipped"
    and their weights redistribute proportionally.
    """
    sections: dict[str, ConfidenceSection] = {}
    blockers: list[str] = []
    action_items: list[dict[str, str]] = []
    evidence_ids: list[str] = []
    evidence_manifest: str | None = None

    # 1. Electrical Validation
    if validation_report is not None:
        report_dict = validation_report.to_dict() if hasattr(validation_report, "to_dict") else {}
        evidence_ids = _evidence_ids(report_dict)
        manifest_value = report_dict.get("evidence_manifest")
        evidence_manifest = manifest_value if isinstance(manifest_value, str) else None
        errors = sum(
            1 for cat in report_dict.get("categories", {}).values()
            for msg in cat
            if msg.get("level") == "error"
        )
        warnings = sum(
            1 for cat in report_dict.get("categories", {}).values()
            for msg in cat
            if msg.get("level") == "warning"
        )
        total = sum(len(cat) for cat in report_dict.get("categories", {}).values()) + 1
        score = _score_from_issues(total, errors, warnings)
        sections["electrical"] = ConfidenceSection(
            name="Electrical Validation",
            score=score,
            grade=_grade(score),
            status="complete",
            issues=[{"level": "error", "count": errors}, {"level": "warning", "count": warnings}],
            evidence_ids=evidence_ids,
        )
        if errors:
            blockers.append(f"Electrical validation: {errors} error(s)")
    else:
        sections["electrical"] = ConfidenceSection(
            name="Electrical Validation", score=0, grade="N/A", status="skipped",
            recommendations=["Run: circuit-weaver validate design.yaml"],
        )

    # 2. Simulation
    if sim_report is not None:
        sim_dict = sim_report.to_dict() if hasattr(sim_report, "to_dict") else sim_report
        sim_score = sim_dict.get("confidence_score", 0)
        sections["simulation"] = ConfidenceSection(
            name="Simulation",
            score=sim_score,
            grade=_grade(sim_score),
            status="complete" if sim_score > 0 else "partial",
            recommendations=sim_dict.get("recommendations", []),
        )
    else:
        sections["simulation"] = ConfidenceSection(
            name="Simulation", score=0, grade="N/A", status="skipped",
            recommendations=[
                "Run: circuit-weaver confidence design.yaml --run-sims",
                "Requires ngspice — check with: circuit-weaver doctor",
            ],
        )

    # 3. Thermal
    thermal_components = (thermal_result or {}).get("components", [])
    if thermal_components:
        critical = sum(1 for c in thermal_components if c.get("status") == "critical")
        warns = sum(1 for c in thermal_components if c.get("status") == "warning")
        total_comps = len(thermal_components)
        score = _score_from_issues(total_comps, critical, warns)
        sections["thermal"] = ConfidenceSection(
            name="Thermal Analysis",
            score=score,
            grade=_grade(score),
            status="partial" if components and total_comps < len(components) else "complete",
            recommendations=thermal_result.get("recommendations", []),
            assessed_components=total_comps,
        )
        if critical:
            blockers.append(f"Thermal: {critical} component(s) exceed Tj_max")
    else:
        sections["thermal"] = ConfidenceSection(
            name="Thermal Analysis", score=0, grade="N/A", status="skipped",
            recommendations=["Supply thermal data including pdiss_max_w in the specs directory."],
            assessed_components=0,
        )

    # 4. Signal Integrity
    # Placeholder: use SI constraints if available
    sections["signal_integrity"] = ConfidenceSection(
        name="Signal Integrity", score=0, grade="N/A", status="skipped",
        recommendations=["Auto-detected from high-speed buses — add USB/DDR/LVDS nets to enable"],
    )

    # 5. Manufacturing/DFM
    input_error = next((v for v in dfm_violations or () if v.type == "dfm_input_unknown"), None)
    if input_error is not None:
        blockers.append(input_error.message)
        sections["manufacturing"] = ConfidenceSection(
            name="Manufacturing (DFM)", score=0, grade="N/A", status="skipped",
            recommendations=[input_error.suggestion],
        )
    elif dfm_violations is not None:
        critical = sum(1 for v in dfm_violations if getattr(v, "severity", "") == "critical")
        warns = sum(1 for v in dfm_violations if getattr(v, "severity", "") == "warning")
        score = _score_from_issues(len(dfm_violations) + 1, critical, warns)
        sections["manufacturing"] = ConfidenceSection(
            name="Manufacturing (DFM)",
            score=score,
            grade=_grade(score),
            status="complete",
        )
        if critical:
            blockers.append(f"DFM: {critical} critical violation(s)")
    else:
        sections["manufacturing"] = ConfidenceSection(
            name="Manufacturing (DFM)", score=0, grade="N/A", status="skipped",
            recommendations=["Run: circuit-weaver confidence design.yaml --pcb board.kicad_pcb"],
        )

    # 6. Cross-Reference
    if any(xr.checked_items or xr.issues for xr in xref_results or ()):
        xr_errors = sum(
            sum(1 for i in xr.issues if i.level == "error")
            for xr in xref_results
        )
        xr_warnings = sum(
            sum(1 for i in xr.issues if i.level == "warning")
            for xr in xref_results
        )
        xr_checked = sum(xr.checked_items for xr in xref_results)
        score = _score_from_issues(xr_checked, xr_errors, xr_warnings)
        sections["cross_reference"] = ConfidenceSection(
            name="Cross-Reference Audit",
            score=score,
            grade=_grade(score),
            status="complete",
        )
        if xr_errors:
            blockers.append(f"Cross-reference: {xr_errors} error(s)")
    else:
        sections["cross_reference"] = ConfidenceSection(
            name="Cross-Reference Audit", score=0, grade="N/A", status="skipped",
            recommendations=["Run: circuit-weaver validate design.yaml --enhanced"],
        )

    # 7. ERC/DRC
    erc_dict = erc_result.to_dict() if hasattr(erc_result, "to_dict") else erc_result or {}
    if erc_dict.get("status") == "ok":
        erc_errs = erc_dict.get("errors", 0)
        erc_warns = erc_dict.get("warnings", 0)
        score = _score_from_issues(erc_errs + erc_warns + 1, erc_errs, erc_warns)
        sections["erc_drc"] = ConfidenceSection(
            name="ERC/DRC",
            score=score,
            grade=_grade(score),
            status="complete",
        )
        if erc_errs:
            blockers.append(f"ERC: {erc_errs} error(s)")
    else:
        if erc_dict.get("status") == "failed":
            blockers.append(f"ERC failed: {erc_dict.get('skip_reason') or 'no usable result'}")
        sections["erc_drc"] = ConfidenceSection(
            name="ERC/DRC", score=0, grade="N/A", status="skipped",
            recommendations=[
                "Run: circuit-weaver erc output/main.kicad_sch",
                "Requires KiCad CLI — check with: circuit-weaver doctor",
            ],
        )

    # Compute weighted overall score
    active_weights: dict[str, float] = {}
    active_total = 0.0
    for key, weight in _SECTION_WEIGHTS.items():
        if key in sections and sections[key].status != "skipped":
            active_weights[key] = weight
            active_total += weight

    if active_total > 0:
        # Redistribute skipped weights proportionally
        scale = 1.0 / active_total
        overall = sum(
            sections[key].score * weight * scale
            for key, weight in active_weights.items()
        )
    else:
        overall = 0.0

    overall_grade = _grade(overall)

    if isinstance(manufacturing_readiness, ManufacturingReadiness):
        readiness = manufacturing_readiness
    elif isinstance(manufacturing_readiness, dict):
        readiness = ManufacturingReadiness.from_dict(manufacturing_readiness)
    elif isinstance(manufacturing_readiness, (str, Path)):
        readiness = read_manufacturing_readiness(manufacturing_readiness)
    elif manufacturing_readiness is None:
        readiness = ManufacturingReadiness(
            state=ManufacturingReadinessState.NOT_READY,
            blockers=("manufacturing_readiness_not_supplied",),
            next_actions=("Generate or supply manufacturing_readiness.json.",),
        )
    else:
        raise TypeError("manufacturing_readiness must be a readiness payload or artifact path")
    blockers.extend(
        f"Manufacturing readiness: {blocker}"
        for blocker in readiness.blockers
        if f"Manufacturing readiness: {blocker}" not in blockers
    )
    evidence_ids = sorted(set(evidence_ids) | set(readiness.evidence_ids))

    # Action items from sections
    for section in sections.values():
        for rec in section.recommendations:
            action_items.append({
                "priority": "high" if section.score < 60 else "medium",
                "description": rec,
                "section": section.name,
            })
    action_items.extend(
        {"priority": "high", "description": action, "section": "Manufacturing Readiness"}
        for action in readiness.next_actions
    )

    report = DesignConfidenceReport(
        project=project,
        timestamp=datetime.now(timezone.utc).isoformat(),
        overall_score=overall,
        overall_grade=overall_grade,
        manufacturing_readiness=readiness,
        sections=sections,
        blockers=blockers,
        action_items=action_items,
        evidence_ids=evidence_ids,
        evidence_manifest=evidence_manifest,
    )

    # Log
    from .logging_bridge import get_design_logger

    dl = get_design_logger()
    if dl:
        dl.log_scoring(
            dimension="confidence",
            score=overall,
            grade=overall_grade,
            gaps=blockers[:5],
        )

    return report
