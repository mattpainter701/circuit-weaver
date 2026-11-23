"""Generic RC transient fixtures with independent analytic checks.

Run with ``python -m benchmarks.diverse_systems.transient_suite --output EMPTY_DIR``.
These are behavioral circuit examples, not product, RF, or qualification evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path

from circuit_weaver.component_db import ComponentDef
from circuit_weaver.simulation import _verify_targets
from circuit_weaver.spice_netlist import export_spice_netlist
from circuit_weaver.spice_runner import run_simulation

MODEL_SCOPE = "generic passive RC behavior, not product or RF qualified"


def _part(ref: str, value: str, first: str, second: str) -> ComponentDef:
    return ComponentDef(
        mpn="generic-passive",
        ref_prefix=ref[0],
        source_ref=ref,
        value=value,
        description="generic transient fixture",
        category="passive",
        pin_nets={"1": first, "2": second},
    )


def _run_case(
    output: Path,
    name: str,
    *,
    topology: str,
    stop: float,
    target_mean: tuple[float, float],
    final_v: float,
    tau: float,
    source: list[dict],
    components: list[ComponentDef],
    expected_failure: bool = False,
    expected_analytic_oracle: str = "passed",
    actual_final_v: float | None = None,
    oracle_tau_s: float | None = None,
) -> dict:
    actual_final = final_v if actual_final_v is None else actual_final_v
    oracle_tau = tau if oracle_tau_s is None else oracle_tau_s
    window_start, window_stop = 4 * oracle_tau, min(8 * oracle_tau, stop)
    low, upper = target_mean
    if expected_failure:
        low, upper = actual_final * 1.1, actual_final * 1.2
    target = {"window_mean": {"metric": "voltage", "trace": "v(VOUT)", "unit": "V",
                              "comparator": "between", "statistic": "mean",
                              "window": {"start_s": window_start, "stop_s": window_stop},
                              "min": low, "max": upper}}
    case_dir = output / name
    netlist = case_dir / f"{name}.cir"
    export_spice_netlist(components, netlist, analysis_type="tran",
                         analysis_params={"step": tau / 100, "stop": stop}, sources=source,
                         title=f"Generic RC transient {name}")
    result = run_simulation(netlist, sim_type="tran")
    if result.status == "ok" and result.evidence_verified:
        _verify_targets(result, target, name, [])
    times = result.time_axis or []
    values = next((v for k, v in result.traces.items() if k.casefold() == "v(vout)"), [])
    samples = [(float(t), float(complex(v).real)) for t, v in zip(times, values)
               if window_start <= t <= window_stop]
    # The independent step-response oracle is evaluated at each solver time.
    errors = [abs(value - final_v * (1 - math.exp(-time / oracle_tau))) for time, value in samples]
    tolerance = max(final_v * 0.015, 1e-4)
    oracle_pass = bool(samples) and max(errors) <= tolerance
    oracle_label = "passed" if oracle_pass else "failed"
    checks = result.target_checks
    check_state = checks.get("window_mean", "unverified")
    expected_state = "failed" if expected_failure else "passed"
    if result.status == "ok" and result.evidence_verified and check_state != expected_state:
        status = "failed"
    else:
        status = result.status
    raw_path = Path(result.raw_file) if result.raw_file else None
    raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest() if raw_path and raw_path.is_file() else None
    return {"name": name, "status": status, "model_scope": MODEL_SCOPE, "topology": topology,
            "evidence_verified": result.evidence_verified, "target_checks": checks,
            "expected_target_failure": expected_failure,
            "analytic_oracle": oracle_label,
            "expected_analytic_oracle": expected_analytic_oracle, "nominal_final_v": final_v,
            "analytic_final_v": actual_final, "actual_tau_s": tau, "oracle_tau_s": oracle_tau,
            "max_analytic_error_v": max(errors) if errors else None,
            "analytic_tolerance_v": tolerance, "sample_count": len(samples),
            "raw_file": result.raw_file, "raw_sha256": raw_sha256,
            "warnings": result.warnings, "skip_reason": result.skip_reason}


def _failures(cases: list[dict]) -> list[str]:
    failures = []
    for case in cases:
        if case["status"] in {"skipped", "timeout"}:
            continue
        expected_check = "failed" if case["expected_target_failure"] else "passed"
        if (case["status"] != "ok" or case["analytic_oracle"] != case["expected_analytic_oracle"]
                or case["target_checks"].get("window_mean") != expected_check
                or (case.get("evidence_verified") and not case.get("raw_sha256"))):
            failures.append(case["name"])
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new or empty output directory")
    parser.add_argument("--ngspice", help="optional ngspice executable")
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("CIRCUIT_WEAVER_NGSPICE")
    if args.ngspice:
        os.environ["CIRCUIT_WEAVER_NGSPICE"] = args.ngspice
    try:
        cases = [
            _run_case(
                output, "iot_battery_input_rc", topology="voltage_rc", stop=0.08,
                target_mean=(3.20, 3.31), final_v=3.3, tau=0.01,
                source=[{"ref": "VSTEP", "type": "V", "positive": "VIN", "negative": "GND",
                         "pulse": {"v1": 0.0, "v2": 3.3, "delay": 0.0, "rise": 1e-12, "fall": 1e-12,
                                   "width": 0.16, "period": 0.32}}],
                components=[_part("R1", "1k", "VIN", "VOUT"), _part("C1", "10u", "VOUT", "GND")],
            ),
            _run_case(
                output, "ot_input_filter_current_burden", topology="current_burden", stop=0.02,
                target_mean=(4.80, 5.01), final_v=5.0, tau=0.00047,
                source=[{"ref": "ILOAD", "type": "I", "positive": "GND", "negative": "VOUT",
                         "pulse": {"v1": 0.0, "v2": 0.01, "delay": 0.0, "rise": 1e-12, "fall": 1e-12,
                                   "width": 0.04, "period": 0.08}}],
                components=[_part("R1", "500", "VOUT", "GND"), _part("C1", "940n", "VOUT", "GND")],
            ),
            _run_case(
                output, "transmitter_bias_loaded_divider", topology="loaded_divider", stop=0.008,
                target_mean=(2.35, 2.51), final_v=2.5, tau=0.0005,
                source=[{"ref": "VSTEP", "type": "V", "positive": "VIN", "negative": "GND",
                         "pulse": {"v1": 0.0, "v2": 5.0, "delay": 0.0, "rise": 1e-12, "fall": 1e-12,
                                   "width": 0.016, "period": 0.032}}],
                components=[_part("R1", "10k", "VIN", "VOUT"), _part("R2", "10k", "VOUT", "GND"),
                            _part("C1", "100n", "VOUT", "GND")],
            ),
            _run_case(
                output, "transmitter_wrong_shunt_value_control", topology="loaded_divider_fault", stop=0.008,
                target_mean=(2.35, 2.51), final_v=2.5, actual_final_v=5 * 20_000 / 30_000,
                tau=(10_000 * 20_000 / 30_000) * 100e-9, expected_failure=True,
                expected_analytic_oracle="failed", oracle_tau_s=0.0005,
                source=[{"ref": "VSTEP", "type": "V", "positive": "VIN", "negative": "GND",
                         "pulse": {"v1": 0.0, "v2": 5.0, "delay": 0.0, "rise": 1e-12, "fall": 1e-12,
                                   "width": 0.016, "period": 0.032}}],
                components=[_part("R1", "10k", "VIN", "VOUT"), _part("R2", "20k", "VOUT", "GND"),
                            _part("C1", "100n", "VOUT", "GND")],
            ),
            _run_case(
                output, "iot_wrong_target_limit_control", topology="voltage_rc_target_fault", stop=0.08,
                target_mean=(3.20, 3.31), final_v=3.3, tau=0.01, expected_failure=True,
                source=[{"ref": "VSTEP", "type": "V", "positive": "VIN", "negative": "GND",
                         "pulse": {"v1": 0.0, "v2": 3.3, "delay": 0.0, "rise": 1e-12, "fall": 1e-12,
                                   "width": 0.16, "period": 0.32}}],
                components=[_part("R1", "1k", "VIN", "VOUT"), _part("C1", "10u", "VOUT", "GND")],
            ),
        ]
    finally:
        if args.ngspice:
            if previous is None:
                os.environ.pop("CIRCUIT_WEAVER_NGSPICE", None)
            else:
                os.environ["CIRCUIT_WEAVER_NGSPICE"] = previous
    unverified = [case["name"] for case in cases if case["status"] in {"skipped", "timeout"}]
    failures = _failures(cases)
    report = {"suite": "generic-transient-systems-v1", "model_scope": MODEL_SCOPE,
              "status": "failed" if failures else "unverified" if unverified else "verified",
              "cases": cases, "failed_cases": failures, "unverified_cases": unverified}
    path = output / "transient-suite-report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(path)}, indent=2))
    return 1 if failures else 2 if unverified else 0


if __name__ == "__main__":
    raise SystemExit(main())
