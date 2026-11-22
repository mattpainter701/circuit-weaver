"""High-level simulation orchestrator for circuit-weaver.

Analyzes a compiled design to determine which simulations are needed,
generates SPICE netlists, runs them via ngspice, and aggregates results
into a DesignSimulationReport with a confidence score.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .component_db import ComponentDef
from .spice_runner import SimulationResult


def _safe_filename_part(ref: str) -> str:
    # A reference designator authored in user YAML can contain path separators
    # or "..". Strip everything that isn't a safe filename character before we
    # use it in output paths.
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", ref or "")
    return cleaned or "unnamed"


@dataclass
class SimulationPlan:
    """Describes which simulations to run for a design."""

    power_sims: list[dict[str, Any]] = field(default_factory=list)
    signal_sims: list[dict[str, Any]] = field(default_factory=list)
    thermal_sims: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.power_sims) + len(self.signal_sims) + len(self.thermal_sims)

    def to_dict(self) -> dict[str, Any]:
        return {
            "power_sims": self.power_sims,
            "signal_sims": self.signal_sims,
            "thermal_sims": self.thermal_sims,
            "total": self.total,
        }


@dataclass
class DesignSimulationReport:
    """Aggregated simulation results for a design."""

    plan: SimulationPlan
    results: list[SimulationResult] = field(default_factory=list)
    confidence_score: float = 0.0
    grade: str = "F"
    summary: str = ""
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "results": [r.to_dict() for r in self.results],
            "confidence_score": round(self.confidence_score, 1),
            "grade": self.grade,
            "summary": self.summary,
            "recommendations": self.recommendations,
        }


_POWER_CATEGORIES = {"buck", "boost", "buck_boost", "ldo", "charge_pump", "power_mux"}
_FILTER_CATEGORIES = {"filter", "rc_filter", "lc_filter", "pll_filter"}


def _evaluate_simulation_targets(result: SimulationResult, targets: dict[str, Any]) -> None:
    """Compare only measurements with a known metric meaning; retain unknowns."""
    checks: dict[str, str] = {}
    for key, limit in targets.items():
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(float(limit)):
            checks[str(key)] = "unverified"
            continue
        normalized = str(key).casefold()
        if normalized == "ripple_mv":
            matches = [value for name, value in result.metrics.items() if name.casefold().endswith("_ripple_mv")]
            if len(matches) != 1 or not math.isfinite(float(matches[0])):
                checks[str(key)] = "unverified"
            else:
                checks[str(key)] = "passed" if matches[0] <= limit else "failed"
        elif normalized == "phase_margin_deg":
            matches = [value for name, value in result.metrics.items() if name.casefold() == normalized]
            if len(matches) != 1 or not math.isfinite(float(matches[0])):
                checks[str(key)] = "unverified"
            else:
                checks[str(key)] = "passed" if matches[0] >= limit else "failed"
        else:
            # In particular, a generic AC point count is not a phase margin.
            checks[str(key)] = "unverified"
    result.target_checks = checks


def _targets_for_analysis(targets: dict[str, Any], analysis: str) -> dict[str, Any]:
    """Keep planned target checks on the analysis that can measure them."""
    selected: dict[str, Any] = {}
    for key, value in targets.items():
        normalized = str(key).casefold()
        if "ripple" in normalized:
            if analysis == "tran":
                selected[key] = value
        elif "phase_margin" in normalized or "bandwidth" in normalized:
            if analysis == "ac":
                selected[key] = value
        else:
            selected[key] = value
    return selected


def plan_simulations(
    components: list[ComponentDef],
    *,
    spec: dict[str, Any] | None = None,
) -> SimulationPlan:
    """Analyze components to determine which simulations are needed.

    Detects:
    - Power regulators -> transient + AC stability analysis
    - Filters (RC/LC) -> AC sweep
    - Op-amps -> AC analysis
    - All power components -> operating point for thermal
    """
    plan = SimulationPlan()

    for comp in components:
        ref = comp.source_ref or ""
        mpn = comp.mpn or ""
        category = (comp.category or "").lower()
        description = (comp.description or "").lower()

        # Detect power regulators
        if category in _POWER_CATEGORIES or any(
            kw in description for kw in ("regulator", "converter", "buck", "boost", "ldo")
        ):
            plan.power_sims.append(
                {
                    "ref": ref,
                    "mpn": mpn,
                    "category": category or "power",
                    "analyses": ["tran", "ac"],
                    "target_metrics": {
                        "ripple_mv": 50.0,  # Max acceptable ripple
                        "phase_margin_deg": 45.0,  # Min phase margin
                    },
                }
            )
            plan.thermal_sims.append(
                {
                    "ref": ref,
                    "mpn": mpn,
                    "analysis": "op",
                    "purpose": "thermal_operating_point",
                }
            )

        # Detect filters
        elif category in _FILTER_CATEGORIES or "filter" in description:
            plan.signal_sims.append(
                {
                    "ref": ref,
                    "mpn": mpn,
                    "category": "filter",
                    "analyses": ["ac"],
                    "target_metrics": {},
                }
            )

        # Detect op-amps
        elif any(kw in description for kw in ("op-amp", "opamp", "operational amplifier")):
            plan.signal_sims.append(
                {
                    "ref": ref,
                    "mpn": mpn,
                    "category": "opamp",
                    "analyses": ["ac", "tran"],
                    "target_metrics": {},
                }
            )

    return plan


def score_simulation_confidence(results: list[SimulationResult]) -> tuple[float, str]:
    """Score 0-100 based on simulation outcomes.

    Factors:
    - % of sims that produced fresh, parsed raw evidence and metrics
    - Penalty for skipped sims (tool not available)
    - Penalty for failed sims (errors)
    """
    if not results:
        return 0.0, "F"

    total = len(results)
    ok_count = sum(
        1 for r in results
        if r.status == "ok"
        and r.evidence_verified
        and bool(r.metrics)
        and bool(r.target_checks)
        and all(state == "passed" for state in r.target_checks.values())
    )
    skipped_count = sum(1 for r in results if r.status == "skipped")
    failed_count = sum(
        1 for r in results
        if r.status in ("failed", "timeout")
        or (
            r.status == "ok"
            and any(state == "failed" for state in r.target_checks.values())
        )
    )

    # Base score from success rate
    if total == skipped_count:
        # All skipped = no data, can't score
        return 0.0, "N/A"

    effective_total = total - skipped_count
    if effective_total == 0:
        return 0.0, "N/A"

    success_rate = ok_count / effective_total
    score = success_rate * 100

    # Penalty for skipped (milder - not the user's fault if tool missing)
    skip_penalty = (skipped_count / total) * 15
    score = max(0, score - skip_penalty)

    # Penalty for failures
    fail_penalty = (failed_count / total) * 25
    score = max(0, score - fail_penalty)

    # Grade
    if score >= 90:
        grade = "A"
    elif score >= 80:
        grade = "B"
    elif score >= 70:
        grade = "C"
    elif score >= 60:
        grade = "D"
    else:
        grade = "F"

    return round(score, 1), grade


def run_design_simulations(
    components: list[ComponentDef],
    plan: SimulationPlan | None = None,
    *,
    output_dir: str | Path = ".",
    model_dir: str | Path | None = None,
    spec: dict[str, Any] | None = None,
) -> DesignSimulationReport:
    """Execute all planned simulations and aggregate results.

    1. Generate SPICE netlists for each simulation target
    2. Link downloaded SPICE models from spice_fetcher manifest
    3. Run ngspice for each
    4. Parse results and compute metrics
    5. Score simulation confidence
    """
    from .spice_fetcher import resolve_spice_models
    from .spice_netlist import export_spice_netlist
    from .spice_runner import run_simulation

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if plan is None:
        plan = plan_simulations(components, spec=spec)

    # Resolve available SPICE models
    model_manifest: dict[str, str] = {}
    if model_dir:
        manifest_path = Path(model_dir) / "manifest.json"
        if manifest_path.exists():
            model_manifest = resolve_spice_models(manifest_path)

    results: list[SimulationResult] = []
    recommendations: list[str] = []

    # Run power simulations
    for sim_def in plan.power_sims:
        ref = sim_def["ref"]
        safe_ref = _safe_filename_part(ref)
        for analysis in sim_def.get("analyses", ["tran"]):
            netlist_path = output_path / f"{safe_ref}_{analysis}.cir"
            export_spice_netlist(
                components,
                netlist_path,
                analysis_type=analysis,
                model_manifest=model_manifest,
                title=f"Power simulation: {ref} ({analysis})",
            )
            result = run_simulation(netlist_path, sim_type=analysis)
            _evaluate_simulation_targets(
                result,
                _targets_for_analysis(sim_def.get("target_metrics", {}), analysis),
            )
            results.append(result)

            if result.status == "ok":
                # Check against targets
                targets = sim_def.get("target_metrics", {})
                for metric_key, target_val in targets.items():
                    for rk, rv in result.metrics.items():
                        if metric_key in rk:
                            if "ripple" in metric_key and rv > target_val:
                                recommendations.append(f"{ref}: Ripple {rv:.1f} mV exceeds target {target_val:.0f} mV")
                            elif "phase_margin" in metric_key and rv < target_val:
                                recommendations.append(
                                    f"{ref}: Phase margin {rv:.1f} deg below minimum {target_val:.0f} deg"
                                )

    # Run signal simulations
    for sim_def in plan.signal_sims:
        ref = sim_def["ref"]
        safe_ref = _safe_filename_part(ref)
        for analysis in sim_def.get("analyses", ["ac"]):
            netlist_path = output_path / f"{safe_ref}_{analysis}.cir"
            export_spice_netlist(
                components,
                netlist_path,
                analysis_type=analysis,
                model_manifest=model_manifest,
                title=f"Signal simulation: {ref} ({analysis})",
            )
            result = run_simulation(netlist_path, sim_type=analysis)
            results.append(result)

    # Run thermal operating point simulations
    for sim_def in plan.thermal_sims:
        ref = sim_def["ref"]
        safe_ref = _safe_filename_part(ref)
        netlist_path = output_path / f"{safe_ref}_op.cir"
        export_spice_netlist(
            components,
            netlist_path,
            analysis_type="op",
            model_manifest=model_manifest,
            title=f"Thermal operating point: {ref}",
        )
        result = run_simulation(netlist_path, sim_type="op")
        results.append(result)

    # Score
    confidence_score, grade = score_simulation_confidence(results)

    # Summary
    ok = sum(1 for r in results if r.status == "ok")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(
        1 for r in results
        if r.status in ("failed", "timeout") or any(state == "failed" for state in r.target_checks.values())
    )
    unverified = sum(
        1 for r in results
        if r.status == "ok"
        and (
            not r.evidence_verified
            or not r.metrics
            or not r.target_checks
            or any(state == "unverified" for state in r.target_checks.values())
        )
    )
    summary = (
        f"{len(results)} simulation(s) planned. "
        f"{ok} ran, {failed} failed, {unverified} unverified, {skipped} skipped. "
        f"Confidence: {confidence_score}/100 ({grade})."
    )

    if skipped == len(results) and len(results) > 0:
        recommendations.append("Install ngspice to enable circuit simulation (apt install ngspice)")

    report = DesignSimulationReport(
        plan=plan,
        results=results,
        confidence_score=confidence_score,
        grade=grade,
        summary=summary,
        recommendations=recommendations,
    )

    # Log summary
    from .logging_bridge import get_design_logger

    dl = get_design_logger()
    if dl:
        dl.log_scoring(
            dimension="simulation",
            score=confidence_score,
            grade=grade,
            gaps=recommendations[:5],
        )

    return report
