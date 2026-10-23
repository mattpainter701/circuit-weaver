"""Small active-device SPICE fixtures using openly authored generic models.

Run with ``python -m benchmarks.diverse_systems.active_suite --output EMPTY_DIR``.
Models in this suite are generic behavioral examples, not manufacturer-qualified.
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

MODEL_SCOPE = "generic behavioral, not manufacturer qualified"


def _component(ref: str, mpn: str, pins: dict[str, str], value: str = "") -> ComponentDef:
    return ComponentDef(
        mpn=mpn,
        ref_prefix=ref[0],
        source_ref=ref,
        value=value,
        description="generic active-device simulation fixture",
        category="active_fixture",
        pin_nets=pins,
    )


def _run_case(
    output: Path,
    name: str,
    components: list[ComponentDef],
    models: dict,
    sources: list,
    targets: dict,
    *,
    wrong_pin_order: bool = False,
    expected_failure: bool = False,
    expected_analytic_oracle: str = "passed",
    expected_v: float = 2.0,
) -> dict:
    # Export and run through the production netlist and parser contracts.
    from circuit_weaver.spice_netlist import export_spice_netlist
    from circuit_weaver.spice_runner import run_simulation

    case_dir = output / name
    netlist = case_dir / f"{name}.cir"
    export_spice_netlist(
        components, netlist, analysis_type="op", sources=sources, model_manifest=models, title=f"Active fixture {name}"
    )
    result = run_simulation(netlist, sim_type="op")
    checks = {}
    if result.status == "ok" and result.evidence_verified:
        # Evaluate explicit engineering targets using the production validator.
        _verify_targets(result, targets, name, [])
        checks = result.target_checks
    status = result.status
    expected_negative = expected_failure and bool(checks) and all(value == "failed" for value in checks.values())
    if status == "ok" and checks and any(value != "passed" for value in checks.values()) and not expected_negative:
        status = "failed"
    observed_trace = "v(OUT)" if name.startswith("opamp") else "v(ANODE)"
    observed = next(
        (
            float(complex(values[-1]).real)
            for key, values in result.traces.items()
            if key.casefold() == observed_trace.casefold() and values
        ),
        None,
    )
    oracle_target = expected_v
    oracle_pass = observed is not None and math.isclose(observed, oracle_target, rel_tol=0.08, abs_tol=0.08)
    raw_path = Path(result.raw_file) if result.raw_file else None
    raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest() if raw_path and raw_path.is_file() else None
    # The wrong-order control must produce a different result and fail the nominal oracle.
    return {
        "name": name,
        "status": status,
        "evidence_verified": result.evidence_verified,
        "model_scope": MODEL_SCOPE,
        "pin_order": {key: value["pin_order"] for key, value in models.items()},
        "wrong_pin_order_negative_control": wrong_pin_order,
        "expected_failure_control": expected_failure,
        "expected_target_failure": expected_failure,
        "expected_analytic_oracle": expected_analytic_oracle,
        "target_checks": checks,
        "observed_trace": observed_trace,
        "observed_v": observed,
        "analytic_expected_v": oracle_target,
        "analytic_oracle": "passed" if oracle_pass else "failed",
        "raw_file": result.raw_file,
        "raw_sha256": raw_sha256,
        "warnings": result.warnings,
        "skip_reason": result.skip_reason,
    }


def _suite_failures(records: list[dict]) -> list[str]:
    """Compare each recorded physical oracle and acceptance target to its own expectation."""
    failures = []
    for row in records:
        checks = row.get("target_checks", {})
        if row.get("status") in {"skipped", "timeout", "unverified"}:
            continue
        if row.get("status") != "ok" or row.get("analytic_oracle") != row.get("expected_analytic_oracle"):
            failures.append(row.get("name", "unnamed"))
        elif not checks:
            failures.append(row.get("name", "unnamed"))
        elif row.get("expected_target_failure"):
            if any(state != "failed" for state in checks.values()):
                failures.append(row.get("name", "unnamed"))
        elif any(state != "passed" for state in checks.values()):
            failures.append(row.get("name", "unnamed"))
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new or empty output directory")
    parser.add_argument("--ngspice", help="optional ngspice executable; CIRCUIT_WEAVER_NGSPICE also works")
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("CIRCUIT_WEAVER_NGSPICE")
    if args.ngspice:
        os.environ["CIRCUIT_WEAVER_NGSPICE"] = args.ngspice
    try:
        model_dir = output / "models"
        model_dir.mkdir()
        opamp_file = model_dir / "generic_behavioral_opamp.lib"
        opamp_file.write_text(
            "* Generic behavioral op amp; illustrative only, not manufacturer qualified\n"
            ".subckt CW_OPAMP INP INM VPLUS VMINUS OUT\n"
            "RIN INP INM 1G\n"
            "BAMP NRAW VMINUS V=min(max(1e5*(V(INP)-V(INM)),V(VMINUS)+0.05),V(VPLUS)-0.05)\n"
            "ROUT NRAW OUT 10\n.ends CW_OPAMP\n",
            encoding="utf-8",
        )
        diode_file = model_dir / "generic_diode.lib"
        diode_file.write_text(
            "* Generic silicon diode; illustrative only, not manufacturer qualified\n"
            ".model CW_D D(IS=1e-12 N=1 RS=0.5)\n"
            ".subckt CW_DIODE ANODE CATHODE\nD1 ANODE CATHODE CW_D\n.ends CW_DIODE\n",
            encoding="utf-8",
        )

        records = []
        nominal_parts: list[ComponentDef] | None = None
        for supply, load, under_supply in ((5.0, 10_000.0, False), (3.3, 2_000.0, False), (1.5, 10_000.0, True)):
            name = f"opamp_{supply:g}v_{int(load)}ohm"
            model = {"GAMP": {"path": str(opamp_file), "subckt": "CW_OPAMP", "pin_order": ["1", "2", "3", "4", "5"]}}
            parts = [
                _component("U1", "GAMP", {"1": "INP", "2": "FB", "3": "VP", "4": "VN", "5": "OUT"}),
                _component("RFB1", "passive", {"1": "OUT", "2": "FB"}, "10k"),
                _component("RFB2", "passive", {"1": "FB", "2": "GND"}, "10k"),
                _component("RL", "passive", {"1": "OUT", "2": "GND"}, f"{load:g}"),
            ]
            if not under_supply and supply == 5.0:
                nominal_parts = parts
            # Finite open-loop gain and Rout with the feedback divider/load seen as Req.
            effective_load = 1.0 / (1.0 / load + 1.0 / 20_000.0)
            analytic_expected = (
                (supply - 0.05) * effective_load / (effective_load + 10.0)
                if under_supply
                else 1e5 / (1.0 + 10.0 / effective_load + 1e5 / 2.0)
            )
            targets = {
                "output_voltage": {
                    "metric": "voltage",
                    "trace": "v(OUT)",
                    "unit": "V",
                    "comparator": "between",
                    "min": 1.9,
                    "max": 2.1,
                }
            }
            records.append(
                _run_case(
                    output,
                    name,
                    parts,
                    model,
                    [
                        {"ref": "VPLUS", "type": "V", "positive": "VP", "negative": "GND", "dc": supply},
                        {"ref": "VMINUS", "type": "V", "positive": "VN", "negative": "GND", "dc": 0},
                        {"ref": "VIN", "type": "V", "positive": "INP", "negative": "GND", "dc": 1.0},
                    ],
                    targets,
                    expected_failure=under_supply,
                    expected_v=analytic_expected,
                )
            )

        diode_model = {"GDIODE": {"path": str(diode_file), "subckt": "CW_DIODE", "pin_order": ["A", "K"]}}
        diode_parts = [
            _component("D1", "GDIODE", {"A": "ANODE", "K": "GND"}),
            _component("R1", "passive", {"1": "SRC", "2": "ANODE"}, "1k"),
        ]
        diode_target = {
            "drop": {
                "metric": "voltage",
                "trace": "v(ANODE)",
                "unit": "V",
                "comparator": "between",
                "min": 0.45,
                "max": 0.9,
            }
        }
        records.append(
            _run_case(
                output,
                "diode_forward_1ma",
                diode_parts,
                diode_model,
                [
                    {"ref": "VSRC", "type": "V", "positive": "SRC", "negative": "GND", "dc": 1.0},
                ],
                diode_target,
                expected_v=0.54,
            )
        )

        # Deliberately swap op amp inputs in the manifest; the analytic output is expected to disagree.
        wrong_model = {"GAMP": {"path": str(opamp_file), "subckt": "CW_OPAMP", "pin_order": ["2", "1", "3", "4", "5"]}}
        assert nominal_parts is not None
        wrong_parts = nominal_parts
        records.append(
            _run_case(
                output,
                "opamp_wrong_pin_order_negative",
                wrong_parts,
                wrong_model,
                [
                    {"ref": "VPLUS", "type": "V", "positive": "VP", "negative": "GND", "dc": 5},
                    {"ref": "VMINUS", "type": "V", "positive": "VN", "negative": "GND", "dc": 0},
                    {"ref": "VIN", "type": "V", "positive": "INP", "negative": "GND", "dc": 1},
                ],
                {
                    "output_voltage": {
                        "metric": "voltage",
                        "trace": "v(OUT)",
                        "unit": "V",
                        "comparator": "between",
                        "min": 1.8,
                        "max": 2.2,
                    }
                },
                wrong_pin_order=True,
                expected_failure=True,
                expected_analytic_oracle="failed",
                expected_v=2.0,
            )
        )
    finally:
        if args.ngspice:
            if previous is None:
                os.environ.pop("CIRCUIT_WEAVER_NGSPICE", None)
            else:
                os.environ["CIRCUIT_WEAVER_NGSPICE"] = previous
    unverified = [row["name"] for row in records if row["status"] in {"skipped", "timeout"}]
    failures = _suite_failures(records)
    report = {
        "suite": "active-devices-generic-v1",
        "model_scope": MODEL_SCOPE,
        "status": "failed" if failures else "unverified" if unverified else "verified",
        "cases": records,
        "failed_cases": failures,
        "unverified_cases": unverified,
    }
    (output / "active-suite-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(output / "active-suite-report.json")}, indent=2))
    return 1 if failures else 2 if unverified else 0


if __name__ == "__main__":
    raise SystemExit(main())
