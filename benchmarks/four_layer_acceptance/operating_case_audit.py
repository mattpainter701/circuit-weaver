"""Exercise authored current budgets and expose absent corpus operating cases.

This pure audit does not infer ratings from part names or run native EDA tools.
Its synthetic controls test the checker; they do not qualify any corpus device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.four_layer_acceptance.corpus import build_cases  # noqa: E402 - explicit benchmark bootstrap
from circuit_weaver.operating_cases import (  # noqa: E402 - installed or source product selected by caller
    CurrentLimit,
    Evidence,
    Load,
    OperatingCase,
    check_operating_case,
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _evidence(subject: str, current_ma: float) -> Evidence:
    return Evidence(
        evidence_id=f"SYNTHETIC-{_digest([subject, current_ma])[:16]}",
        confidence="verified",
        freshness="current",
        subject=subject,
        value=current_ma,
        unit="mA",
        kind="calculation",
    )


def synthetic_controls() -> list[dict[str, Any]]:
    """Independent 120 + 4*80 = 440 mA oracle; every number is synthetic."""
    nominal = OperatingCase(
        "Synthetic series feed: controller and four loads",
        5,
        (Load("controller", 120, 1, _evidence("controller", 120)), Load("coil", 80, 4, _evidence("coil", 80))),
        CurrentLimit("source", 600, _evidence("source", 600)),
        (CurrentLimit("J1", 500, _evidence("connector:J1", 500)),),
        (CurrentLimit("feed", 480, _evidence("distribution:feed", 480)),),
    )
    controls = [
        ("nominal", nominal, "passed"),
        (
            "undersized_feed",
            replace(nominal, source_limit=CurrentLimit("source", 300, _evidence("source", 300))),
            "failed",
        ),
        (
            "missing_feed_ratings",
            replace(nominal, source_limit=None, connector_limits=(), distribution_limits=()),
            "unverified",
        ),
        ("unknown_concurrency", replace(nominal, max_simultaneous=None), "unverified"),
        (
            "value_provenance_mismatch",
            replace(nominal, loads=(nominal.loads[0], replace(nominal.loads[1], current_ma=400))),
            "unverified",
        ),
    ]
    rows = []
    for name, case, expected in controls:
        result = check_operating_case(case)
        rows.append(
            {
                "control": name,
                "scope": "synthetic_checker_contract_only",
                "input": asdict(case),
                "input_sha256": _digest(asdict(case)),
                "expected_status": expected,
                "observed_status": result.status,
                "expected_outcome_observed": result.status == expected,
                "result": result.to_dict(),
                "result_sha256": result.result_hash,
            }
        )
    assert rows[0]["result"]["required_ma"] == 440
    return rows


def audit_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Report missing authoring as unknown; keep all-active arithmetic conditional."""
    before = _digest(cases)
    rows = []
    for case in cases:
        drivers = [item for item in case["spec"].get("power", []) if item.get("type") == "relay_driver"]
        row: dict[str, Any] = {
            "case_id": case["case_id"],
            "input_sha256": _digest(case),
            "operating_case_status": "unverified",
            "reason": "No evidence-bound simultaneous-load/source/connector/distribution contract is authored.",
            "device_function": "unverified",
            "thermal": "unverified",
        }
        if drivers:
            loads = tuple(Load(item["ref"], item["icoil"] * 1000, None, None) for item in drivers)
            declared = OperatingCase(case["case_id"], None, loads, None)
            result = check_operating_case(declared)
            row.update(
                {
                    "conditional_all_active": {
                        "scope": "all_declared_channels_active_assumption_only",
                        "channels": sum(item["channels_used"] for item in drivers),
                        "current_ma": sum(item["channels_used"] * item["icoil"] * 1000 for item in drivers),
                        "source_values": [
                            {key: item[key] for key in ("ref", "channels_used", "icoil")} for item in drivers
                        ],
                        "current_value_unit": "icoil is authored in A; output current_ma is mA",
                        "maximum_simultaneous_contract": None,
                        "verified_rating": False,
                    },
                    "input": asdict(declared),
                    "result": result.to_dict(),
                    "result_sha256": result.result_hash,
                }
            )
        rows.append(row)
    assert _digest(cases) == before, "audit mutated the acceptance inputs"
    controls = synthetic_controls()
    return {
        "scope": "authored_current_budget_only; no native CAD or device qualification",
        "input_sha256": before,
        "inputs_preserved": True,
        "cases": rows,
        "synthetic_controls": controls,
        "synthetic_expected_outcomes_observed": all(item["expected_outcome_observed"] for item in controls),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path; must not exist")
    args = parser.parse_args()
    report = audit_cases(build_cases())
    report["checker_source_sha256"] = hashlib.sha256(
        Path(sys.modules[check_operating_case.__module__].__file__).read_bytes()
    ).hexdigest()
    report["checker_import_path"] = sys.modules[check_operating_case.__module__].__file__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {"cases": len(report["cases"]), "synthetic_controls_passed": report["synthetic_expected_outcomes_observed"]}
        )
    )
    return 0 if report["synthetic_expected_outcomes_observed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
