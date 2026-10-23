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
    coverage: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if any(result.status in {"failed", "timeout"} for result in self.results):
            return "failed"
        if any(state == "failed" for result in self.results for state in result.target_checks.values()):
            return "failed"
        if not self.results or any(
            result.status != "ok"
            or not result.evidence_verified
            or not result.target_checks
            or any(state != "passed" for state in result.target_checks.values())
            for result in self.results
        ):
            return "unverified"
        return "verified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "plan": self.plan.to_dict(),
            "results": [r.to_dict() for r in self.results],
            "confidence_score": round(self.confidence_score, 1),
            "grade": self.grade,
            "summary": self.summary,
            "recommendations": self.recommendations,
            "coverage": self.coverage,
        }


_POWER_CATEGORIES = {"buck", "boost", "buck_boost", "ldo", "charge_pump", "power_mux"}
_FILTER_CATEGORIES = {"filter", "rc_filter", "lc_filter", "pll_filter"}


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

    simulation_spec = (spec or {}).get("simulation") if isinstance(spec, dict) else None
    if simulation_spec is not None and not isinstance(simulation_spec, dict):
        raise ValueError("spec.simulation must be an object")
    explicit_analyses = simulation_spec.get("analyses") if simulation_spec is not None else None
    if explicit_analyses is not None:
        if not isinstance(explicit_analyses, list):
            raise ValueError("spec.simulation.analyses must be a list")
    if explicit_analyses:
        seen_analysis_ids: set[tuple[str, str]] = set()
        for index, entry in enumerate(explicit_analyses):
            if not isinstance(entry, dict):
                raise ValueError(f"spec.simulation.analyses[{index}] must be an object")
            unknown_fields = set(entry) - {"ref", "kind", "analysis_params", "sources", "target_metrics"}
            if unknown_fields:
                raise ValueError(f"spec.simulation.analyses[{index}] has unsupported fields: {sorted(unknown_fields)}")
            ref = entry.get("ref")
            kind = entry.get("kind")
            analysis_params = entry.get("analysis_params", {})
            sources = entry.get("sources", [])
            targets = entry.get("target_metrics", {})
            if not isinstance(ref, str) or not ref.strip() or not re.fullmatch(r"[A-Za-z0-9_-]+", ref):
                raise ValueError(f"spec.simulation.analyses[{index}].ref must be a safe non-empty label")
            if not isinstance(kind, str) or kind not in {"ac", "tran", "op", "dc"}:
                raise ValueError(f"spec.simulation.analyses[{index}].kind must be ac, tran, op, or dc")
            analysis_id = (ref.casefold(), kind)
            if analysis_id in seen_analysis_ids:
                raise ValueError(f"spec.simulation.analyses[{index}] duplicates ref/kind {ref!r}/{kind}")
            seen_analysis_ids.add(analysis_id)
            if not isinstance(analysis_params, dict) or not isinstance(sources, list) or not isinstance(targets, dict):
                raise ValueError(f"spec.simulation.analyses[{index}] params, sources, and targets have invalid shapes")
            allowed_params = {
                "ac": {"variation", "points", "fstart", "fstop", "input_trace", "output_trace"},
                "tran": {"step", "stop", "start", "output_trace"},
                "dc": {"source", "start", "stop", "step", "output_trace"},
                "op": {"output_trace"},
            }[kind]
            unknown_params = set(analysis_params) - allowed_params
            if unknown_params:
                raise ValueError(
                    f"spec.simulation.analyses[{index}] has unsupported {kind} parameters: {sorted(unknown_params)}"
                )
            if kind == "ac":

                def normalized_node(name: str) -> str:
                    if name.casefold() in {"gnd", "ground", "vss", "0"}:
                        return "0"
                    return re.sub(r"[^a-zA-Z0-9_]", "_", name)

                known_nodes = {
                    normalized_node(net).casefold()
                    for comp in components
                    for net in (comp.pin_nets or {}).values()
                    if isinstance(net, str) and net.strip()
                }
                for trace_key in ("input_trace", "output_trace"):
                    if trace_key not in analysis_params:
                        continue
                    trace = analysis_params[trace_key]
                    match = re.fullmatch(r"v\(([^()\s]+)\)", trace, re.IGNORECASE) if isinstance(trace, str) else None
                    if not match:
                        raise ValueError(
                            f"spec.simulation.analyses[{index}].analysis_params.{trace_key} "
                            "must be a voltage trace v(node)"
                        )
                    node = normalized_node(match.group(1)).casefold()
                    if node == "0" or node not in known_nodes:
                        raise ValueError(
                            f"spec.simulation.analyses[{index}].analysis_params.{trace_key} "
                            "must reference a connected non-ground net"
                        )
                if (
                    analysis_params.get("input_trace", "v(in)").casefold()
                    == analysis_params.get("output_trace", "v(out)").casefold()
                ):
                    raise ValueError(f"spec.simulation.analyses[{index}] input_trace and output_trace must differ")
            if kind in {"op", "dc", "tran"} and "output_trace" in analysis_params:
                trace = analysis_params["output_trace"]
                match = re.fullmatch(r"[vi]\([^()\s]+\)", trace, re.IGNORECASE) if isinstance(trace, str) else None
                if not match:
                    raise ValueError(
                        f"spec.simulation.analyses[{index}].analysis_params.output_trace "
                        "must be v(node) or i(source)"
                    )
            plan.signal_sims.append(
                {
                    "ref": ref,
                    "category": "explicit_testbench",
                    "analyses": [kind],
                    "analysis_params": analysis_params,
                    "sources": sources,
                    "target_metrics": targets,
                }
            )
        return plan

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
                    # Targets must come from project requirements or validated
                    # device constraints; generic ripple/margin values would
                    # make unverified analyses look like design evidence.
                    "target_metrics": {},
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
    - % of analyses with fresh, parsed simulation evidence and explicit
      project targets verified within range
    - Penalty for skipped sims (tool not available)
    - Penalty for failed sims (errors)
    """
    if not results:
        return 0.0, "F"

    total = len(results)
    ok_count = sum(
        1
        for r in results
        if r.status == "ok"
        and r.evidence_verified
        and r.target_checks
        and all(state == "passed" for state in r.target_checks.values())
    )
    skipped_count = sum(1 for r in results if r.status == "skipped")
    failed_count = sum(
        1
        for result in results
        if result.status in ("failed", "timeout") or any(state == "failed" for state in result.target_checks.values())
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
    model_manifest: dict[str, str | dict[str, Any]] = {}
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
                analysis_params=sim_def.get("analysis_params"),
                sources=sim_def.get("sources"),
                model_manifest=model_manifest,
                title=f"Power simulation: {ref} ({analysis})",
            )
            result = run_simulation(netlist_path, sim_type=analysis, analysis_params=sim_def.get("analysis_params"))
            results.append(result)
            _verify_targets(result, sim_def.get("target_metrics", {}), ref, recommendations)

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
                analysis_params=sim_def.get("analysis_params"),
                sources=sim_def.get("sources"),
                model_manifest=model_manifest,
                title=f"Signal simulation: {ref} ({analysis})",
            )
            result = run_simulation(netlist_path, sim_type=analysis, analysis_params=sim_def.get("analysis_params"))
            results.append(result)
            _verify_targets(result, sim_def.get("target_metrics", {}), ref, recommendations)

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
        _verify_targets(result, sim_def.get("target_metrics", {}), ref, recommendations)

    # Score
    confidence_score, grade = score_simulation_confidence(results)

    # Summary
    verified_passed = sum(
        1
        for result in results
        if result.status == "ok"
        and result.evidence_verified
        and result.target_checks
        and all(state == "passed" for state in result.target_checks.values())
    )
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(
        1
        for result in results
        if result.status in ("failed", "timeout") or any(state == "failed" for state in result.target_checks.values())
    )
    unverified = len(results) - verified_passed - failed - skipped
    summary = (
        f"{len(results)} simulation(s) planned. "
        f"{verified_passed} verified, {unverified} unverified, {failed} failed, {skipped} skipped. "
        f"Confidence: {confidence_score}/100 ({grade})."
    )

    unsupported = sorted({ref for result in results for ref in result.unsupported_components})
    missing_models = {
        (model["ref"], model["mpn"], model["reason"]) for result in results for model in result.missing_models
    }
    verified = sum(1 for result in results if result.evidence_verified)
    target_verified = sum(
        1
        for result in results
        if result.target_checks and all(value == "passed" for value in result.target_checks.values())
    )
    coverage = {
        "planned_analyses": len(results),
        "executed_analyses": sum(1 for result in results if result.status != "skipped"),
        "fresh_parsed_analyses": verified,
        "analyses_with_verified_targets": target_verified,
        "unsupported_components": unsupported,
        "missing_models": [{"ref": ref, "mpn": mpn, "reason": reason} for ref, mpn, reason in sorted(missing_models)],
        "analyses": [
            {
                "sim_type": result.sim_type,
                "status": result.status,
                "evidence_verified": result.evidence_verified,
                "target_checks": result.target_checks,
                "unsupported_components": result.unsupported_components,
                "missing_models": result.missing_models,
            }
            for result in results
        ],
        "confidence_basis": "fresh parsed results plus explicit verified project targets",
    }
    if unsupported:
        recommendations.append(
            "Electrical evidence is incomplete; add validated SPICE models for: " + ", ".join(unsupported)
        )
    if verified and not target_verified:
        recommendations.append(
            "Simulation ran, but no explicit project target was verified; electrical confidence remains unscored"
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
        coverage=coverage,
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


def _verify_targets(
    result: SimulationResult,
    targets: dict[str, Any],
    ref: str,
    recommendations: list[str],
) -> None:
    """Record only checks with a recognized metric and explicit comparator."""
    if not targets:
        result.target_checks["project_targets"] = "unverified"
        return
    for key, target in targets.items():
        normalized_key = str(key).strip().lower()
        if isinstance(target, dict):
            _verify_explicit_trace_target(result, str(key), target, ref, recommendations)
            continue
        ripple_targets = {"ripple_mv", "ripple_max_mv"}
        bandwidth_min_targets = {"bandwidth_min_hz", "bandwidth_hz_min"}
        bandwidth_max_targets = {"bandwidth_max_hz", "bandwidth_hz_max"}
        if normalized_key in ripple_targets:
            # The runner reports per-node values such as
            # ``v(out)_ripple_mv``. Accept only this explicit unit-bearing
            # suffix and only when one node matches; choosing the first of
            # several outputs would silently verify the wrong rail.
            matches = [
                (name, value)
                for name, value in result.metrics.items()
                if name.lower() == "ripple_mv" or name.lower().endswith("_ripple_mv")
            ]
        elif normalized_key in bandwidth_min_targets | bandwidth_max_targets:
            # Bandwidth is a single aggregate metric. Substring matching can
            # accidentally accept unrelated names such as ``input_bandwidth``.
            matches = [(name, value) for name, value in result.metrics.items() if name.lower() == "bandwidth_hz"]
        else:
            matches = []
        if not result.evidence_verified or not matches:
            result.target_checks[str(key)] = "unverified"
            recommendations.append(
                f"{ref}: target {key} is unverified because the required simulation metric is missing"
            )
            continue
        if len(matches) != 1:
            result.target_checks[str(key)] = "unverified"
            recommendations.append(f"{ref}: target {key} is unverified because its simulation metric is ambiguous")
            continue
        metric_name, measured = matches[0]
        try:
            if isinstance(target, bool):
                raise TypeError("boolean is not a numeric engineering limit")
            target_value = float(target)
            measured_value = float(measured)
        except (TypeError, ValueError):
            result.target_checks[str(key)] = "unverified"
            recommendations.append(f"{ref}: target {key} is not a numeric engineering limit")
            continue
        if not math.isfinite(target_value) or not math.isfinite(measured_value):
            result.target_checks[str(key)] = "unverified"
            recommendations.append(f"{ref}: target {key} or its measured metric is non-finite")
            continue
        if normalized_key in ripple_targets:
            passed = measured_value <= target_value
        elif normalized_key in bandwidth_min_targets:
            passed = measured_value >= target_value
        elif normalized_key in bandwidth_max_targets:
            passed = measured_value <= target_value
        else:
            result.target_checks[str(key)] = "unverified"
            recommendations.append(f"{ref}: no validated comparator exists for target {key}")
            continue
        result.target_checks[str(key)] = "passed" if passed else "failed"
        if not passed:
            recommendations.append(f"{ref}: {metric_name}={measured_value:g} does not meet target {target_value:g}")


def _verify_explicit_trace_target(
    result: SimulationResult, key: str, target: dict[str, Any], ref: str, recommendations: list[str]
) -> None:
    """Verify a unit-bearing voltage/current bound against one exact raw vector."""
    allowed = {"metric", "trace", "unit", "comparator", "value", "min", "max", "sample"}
    metric, trace, unit, comparator = (target.get(name) for name in ("metric", "trace", "unit", "comparator"))
    expected_units = {"voltage": "V", "current": "A", "ripple": "mV"}
    if (
        set(target) - allowed
        or not isinstance(metric, str)
        or not isinstance(unit, str)
        or not isinstance(comparator, str)
        or metric not in expected_units
        or unit != expected_units.get(metric)
    ):
        result.target_checks[key] = "unverified"
        recommendations.append(f"{ref}: target {key} has an unsupported metric or unit schema")
        return
    # ``bool`` is an ``int`` subclass in Python, but a JSON true/false value
    # is not a meaningful engineering limit. Reject it before float coercion.
    numeric_bounds = ("min", "max") if comparator in {"between", "abs_between"} else ("value",)
    if any(isinstance(target.get(bound), bool) for bound in numeric_bounds if bound in target):
        result.target_checks[key] = "unverified"
        recommendations.append(f"{ref}: target {key} has a boolean engineering limit")
        return
    if not isinstance(trace, str) or not re.fullmatch(r"(?:v\([^()\s]+\)|i\([^()\s]+\))", trace, re.IGNORECASE):
        result.target_checks[key] = "unverified"
        recommendations.append(f"{ref}: target {key} must select one exact voltage or source-current trace")
        return
    if (metric in {"voltage", "ripple"}) != trace.lower().startswith("v("):
        result.target_checks[key] = "unverified"
        recommendations.append(f"{ref}: target {key} trace type does not match its metric")
        return
    allowed_analyses = {"ripple": {"tran"}, "voltage": {"op", "dc"}, "current": {"op", "dc"}}
    if result.sim_type not in allowed_analyses[metric]:
        result.target_checks[key] = "unverified"
        recommendations.append(f"{ref}: target {key} is unsupported for {result.sim_type or 'unknown'} analysis")
        return
    if not result.evidence_verified:
        result.target_checks[key] = "unverified"
        recommendations.append(f"{ref}: target {key} has no fresh parsed simulation evidence")
        return
    if metric == "ripple":
        if "sample" in target:
            result.target_checks[key] = "unverified"
            recommendations.append(f"{ref}: ripple target {key} does not accept a sample selector")
            return
        metric_name = f"{trace}_ripple_mv".casefold()
        matches = [value for name, value in result.metrics.items() if name.casefold() == metric_name]
    else:
        matches = [values for name, values in result.traces.items() if name.casefold() == trace.casefold()]
        if result.sim_type == "dc" and target.get("sample") != "endpoint":
            result.target_checks[key] = "unverified"
            recommendations.append(f"{ref}: DC sweep target {key} must explicitly select sample='endpoint'")
            return
        if result.sim_type != "dc" and "sample" in target:
            result.target_checks[key] = "unverified"
            recommendations.append(f"{ref}: target {key} sample selector is only valid for a DC sweep")
            return
        if result.sim_type == "op" and any(len(values) != 1 for values in matches):
            result.target_checks[key] = "unverified"
            recommendations.append(f"{ref}: operating-point target {key} requires exactly one sample")
            return
        if any(not values for values in matches):
            result.target_checks[key] = "unverified"
            recommendations.append(f"{ref}: target {key} trace has no samples")
            return
        if result.sim_type == "op" and any(complex(values[0]).imag != 0 for values in matches):
            result.target_checks[key] = "unverified"
            recommendations.append(f"{ref}: operating-point target {key} requires a real-valued sample")
            return
        matches = [complex(values[-1]).real for values in matches if values]
    if len(matches) != 1:
        result.target_checks[key] = "unverified"
        recommendations.append(f"{ref}: target {key} trace is missing or ambiguous")
        return
    measured = float(matches[0])
    if not math.isfinite(measured):
        result.target_checks[key] = "unverified"
        recommendations.append(f"{ref}: target {key} measured value is non-finite")
        return
    try:
        expected_target_keys = {"metric", "trace", "unit", "comparator", "min", "max"}
        if result.sim_type == "dc" and metric != "ripple":
            expected_target_keys.add("sample")
        if comparator in {"between", "abs_between"} and set(target) == expected_target_keys:
            low, high = float(target["min"]), float(target["max"])
            observed = abs(measured) if comparator == "abs_between" else measured
            valid_window = low <= high and (not comparator.startswith("abs_") or low >= 0)
            if not math.isfinite(low) or not math.isfinite(high) or not valid_window:
                raise ValueError
            passed = low <= observed <= high
        elif comparator in {"le", "ge", "abs_le", "abs_ge"} and set(target) == expected_target_keys - {
            "min",
            "max",
        } | {"value"}:
            limit = float(target["value"])
            observed = abs(measured) if comparator.startswith("abs_") else measured
            direction = comparator.removeprefix("abs_")
            valid_limit = not comparator.startswith("abs_") or limit >= 0
            if not math.isfinite(limit) or not valid_limit:
                raise ValueError
            passed = observed <= limit if direction == "le" else observed >= limit
        else:
            raise ValueError
    except (TypeError, ValueError):
        result.target_checks[key] = "unverified"
        recommendations.append(f"{ref}: target {key} has an invalid comparator or finite numeric bound")
        return
    result.target_checks[key] = "passed" if passed else "failed"
    if not passed:
        recommendations.append(f"{ref}: {trace}={measured:g} {unit} does not meet target {key}")
