#!/usr/bin/env python3
"""Portable ngspice regression suite for independent passive network oracles.

The suite uses Circuit Weaver's actual SPICE exporter and runner. It defines
every passive network here, compares every parsed AC point against an analytic
transfer function, and reports deliberate IQ/overrange negative controls.
It does not invoke KiCad or claim IC, PCB, RF-layout, or manufacturing closure.
"""

from __future__ import annotations

import argparse
import cmath
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from circuit_weaver.component_db import ComponentDef  # noqa: E402
from circuit_weaver.simulation import run_design_simulations  # noqa: E402
from circuit_weaver.spice_netlist import export_spice_netlist  # noqa: E402
from circuit_weaver.spice_runner import run_simulation  # noqa: E402

ComplexTransfer = Callable[[float], complex]


@dataclass(frozen=True)
class ChannelOracle:
    name: str
    input_trace: str
    output_trace: str
    transfer: ComplexTransfer
    target_metrics: dict | None = None
    expected_bandwidth_hz: float | None = None


@dataclass(frozen=True)
class AcFixture:
    name: str
    components: tuple[ComponentDef, ...]
    sources: tuple[dict, ...]
    channels: tuple[ChannelOracle, ...]
    sweep: dict
    targets: dict


def _part(ref: str, value: str, net1: str, net2: str) -> ComponentDef:
    return ComponentDef(
        mpn="passive-benchmark",
        ref_prefix=ref[0],
        source_ref=ref,
        value=value,
        description="independently defined passive benchmark element",
        category="passive",
        pin_nets={"1": net1, "2": net2},
    )


def _rc_transfer(resistance_ohm: float, capacitance_f: float) -> ComplexTransfer:
    return lambda frequency: 1.0 / (1.0 + 2j * math.pi * frequency * resistance_ohm * capacitance_f)


def _voltage_source(ref: str, node: str, *, ac: float = 1.0, dc: float = 0.0) -> dict:
    return {"ref": ref, "type": "V", "positive": node, "negative": "GND", "dc": dc, "ac": ac}


def _target_window(cutoff_hz: float, tolerance: float = 0.05) -> dict[str, float]:
    return {
        "bandwidth_hz_min": cutoff_hz * (1.0 - tolerance),
        "bandwidth_hz_max": cutoff_hz * (1.0 + tolerance),
    }


def _simple_rc_fixture(name: str, resistance: float, capacitance: float, *, sweep: dict) -> AcFixture:
    cutoff = 1.0 / (2.0 * math.pi * resistance * capacitance)
    components = (
        _part("R1", _ohms(resistance), "VIN", "VOUT"),
        _part("C1", _farads(capacitance), "VOUT", "GND"),
    )
    return AcFixture(
        name=name,
        components=components,
        sources=(_voltage_source("VTEST", "VIN"),),
        channels=(
            ChannelOracle(
                "output", "v(VIN)", "v(VOUT)", _rc_transfer(resistance, capacitance),
                expected_bandwidth_hz=cutoff,
            ),
        ),
        sweep=sweep,
        targets=_target_window(cutoff),
    )


def _ohms(value: float) -> str:
    if value == 1_000_000:
        return "1M"
    if value >= 1000 and value % 1000 == 0:
        return f"{int(value / 1000)}k"
    return f"{value:g}"


def _farads(value: float) -> str:
    if value not in {1e-12, 100e-12, 220e-12, 1e-9, 10e-9, 100e-9}:
        raise ValueError(f"No deterministic passive value encoding for {value:g} F")
    return {
        1e-12: "1pF",
        100e-12: "100pF",
        220e-12: "220pF",
        1e-9: "1nF",
        10e-9: "10nF",
        100e-9: "100nF",
    }[value]


def _make_fixtures() -> list[AcFixture]:
    sweep_standard = {"variation": "dec", "points": 60, "fstart": "1", "fstop": "10Meg"}
    fixtures = [
        _simple_rc_fixture("iot_rc", 4700.0, 100e-9, sweep=sweep_standard),
        _simple_rc_fixture("megohm_picofarad", 1_000_000.0, 1e-12, sweep=sweep_standard),
    ]

    for mismatch in (False, True):
        channels = []
        components = []
        sources = []
        targets = {}
        for channel, resistance in (("i", 1000.0), ("q", 1200.0 if mismatch else 1000.0)):
            vin, vout = f"VIN_{channel.upper()}", f"VOUT_{channel.upper()}"
            components.extend(
                (_part(f"R_{channel.upper()}", _ohms(resistance), vin, vout),
                 _part(f"C_{channel.upper()}", "220pF", vout, "GND"))
            )
            sources.append(_voltage_source(f"V{channel.upper()}", vin))
            cutoff = 1.0 / (2.0 * math.pi * resistance * 220e-12)
            channels.append(
                ChannelOracle(
                    channel,
                    f"v({vin})",
                    f"v({vout})",
                    _rc_transfer(resistance, 220e-12),
                    _target_window(cutoff),
                    cutoff,
                )
            )
        fixtures.append(
            AcFixture(
                name="sdr_iq_mismatched" if mismatch else "sdr_iq_matched",
                components=tuple(components),
                sources=tuple(sources),
                channels=tuple(channels),
                sweep={"variation": "dec", "points": 60, "fstart": "1k", "fstop": "5Meg"},
                targets=targets,
            )
        )

    # Normalized third-order Butterworth ladder: g=[1,2,1], 50 ohm,
    # 100 MHz cutoff, 50 ohm source and termination.
    impedance, cutoff = 50.0, 100e6
    l_value = impedance / (2.0 * math.pi * cutoff)
    c_value = 2.0 / (impedance * 2.0 * math.pi * cutoff)
    fixtures.append(
        AcFixture(
            name="butterworth_50ohm_100mhz",
            components=(
                _part("RS", "50", "VSRC", "N1"),
                _part("L1", f"{l_value * 1e9:.9g}nH", "N1", "N2"),
                _part("C2", f"{c_value * 1e12:.9g}pF", "N2", "GND"),
                _part("L3", f"{l_value * 1e9:.9g}nH", "N2", "VOUT"),
                _part("RL", "50", "VOUT", "GND"),
            ),
            sources=(_voltage_source("VTEST", "VSRC"),),
            channels=(
                ChannelOracle(
                    "output", "v(VSRC)", "v(VOUT)", _butterworth_transfer(cutoff, impedance),
                    expected_bandwidth_hz=cutoff,
                ),
            ),
            sweep={"variation": "dec", "points": 60, "fstart": "1Meg", "fstop": "1G"},
            targets=_target_window(cutoff, 0.06),
        )
    )

    for count in (32, 256, 640):
        components: list[ComponentDef] = []
        channels = []
        for index in range(count):
            suffix = f"{index:03d}"
            output = f"IF_OUT_{suffix}"
            components.extend(
                (_part(f"R{index + 1}", "1k", "IF_IN", output),
                 _part(f"C{index + 1}", "10nF", output, "GND"))
            )
            cutoff = 1.0 / (2.0 * math.pi * 1000.0 * 10e-9)
            channels.append(
                ChannelOracle(
                    suffix, "v(IF_IN)", f"v({output})", _rc_transfer(1000.0, 10e-9),
                    expected_bandwidth_hz=cutoff,
                )
            )
        fixtures.append(
            AcFixture(
                name=f"radar_if_bank_{count}",
                components=tuple(components),
                sources=(_voltage_source("VIF", "IF_IN"),),
                channels=tuple(channels),
                sweep={"variation": "dec", "points": 60, "fstart": "100", "fstop": "1Meg"},
                targets=_target_window(1.0 / (2.0 * math.pi * 1000.0 * 10e-9), 0.06),
            )
        )
    return fixtures


def _butterworth_transfer(cutoff_hz: float, impedance: float) -> ComplexTransfer:
    inductance = impedance / (2.0 * math.pi * cutoff_hz)
    capacitance = 2.0 / (impedance * 2.0 * math.pi * cutoff_hz)

    def transfer(frequency: float) -> complex:
        omega = 2.0 * math.pi * frequency
        # ABCD matrix for source resistance, L, shunt C, L, and load.
        a, b, c, d = 1 + 0j, 0j, 0j, 1 + 0j
        for ea, eb, ec, ed in (
            (1, 1j * omega * inductance, 0, 1),
            (1, 0, 1j * omega * capacitance, 1),
            (1, 1j * omega * inductance, 0, 1),
        ):
            a, b, c, d = a * ea + b * ec, a * eb + b * ed, c * ea + d * ec, c * eb + d * ed
        denominator = a + b / impedance + impedance * (c + d / impedance)
        return 1.0 / denominator

    return transfer


def _find_trace(traces: dict, selector: str):
    return next((values for name, values in traces.items() if name.casefold() == selector.casefold()), None)


def _run_ac_fixture(fixture: AcFixture, output: Path) -> dict:
    primary = fixture.channels[0]
    analysis = {
        "ref": fixture.name,
        "kind": "ac",
        "analysis_params": {
            **fixture.sweep,
            "input_trace": primary.input_trace,
            "output_trace": primary.output_trace,
        },
        "sources": list(fixture.sources),
        "target_metrics": primary.target_metrics or fixture.targets,
    }
    report = run_design_simulations(
        list(fixture.components),
        output_dir=output / fixture.name,
        spec={"simulation": {"analyses": [analysis]}},
    )
    result = report.results[0] if report.results else None
    if result is None:
        return {
            "name": fixture.name,
            "physical_passives": len(fixture.components),
            "analysis_runs": 1,
            "status": "failed",
            "error": "product runner returned no result",
            "channels": [],
        }

    raw_path = Path(result.raw_file) if result.raw_file else None
    raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest() if raw_path and raw_path.is_file() else None
    channels_out = []
    failed = result.status == "failed"
    for channel_index, channel in enumerate(fixture.channels):
        row = {
            "channel": channel.name,
            "product_analysis_run": fixture.name,
            "evidence_verified": result.evidence_verified,
            "status": result.status,
        }
        if result.status != "ok" or not result.evidence_verified:
            channels_out.append(row)
            continue
        frequencies = result.traces.get("frequency", [])
        input_values = _find_trace(result.traces, channel.input_trace)
        output_values = _find_trace(result.traces, channel.output_trace)
        if (
            not frequencies
            or input_values is None
            or output_values is None
            or len(input_values) != len(frequencies)
            or len(output_values) != len(frequencies)
        ):
            row.update({"oracle_status": "failed", "error": "required parsed vectors are missing"})
            failed = True
            channels_out.append(row)
            continue
        errors = []
        magnitude_errors = []
        for frequency, vin, vout in zip(frequencies, input_values, output_values):
            f_hz = complex(frequency).real
            if abs(complex(vin)) == 0:
                errors.append(math.inf)
                magnitude_errors.append(math.inf)
                continue
            actual = complex(vout) / complex(vin)
            expected = channel.transfer(f_hz)
            errors.append(abs(actual - expected) / max(abs(expected), 1e-10))
            magnitude_errors.append(abs(abs(actual) - abs(expected)) / max(abs(expected), 1e-10))
        max_error = max(errors, default=math.inf)
        max_magnitude_error = max(magnitude_errors, default=math.inf)
        row.update(
            {
                "oracle_status": "passed" if max_error <= 0.025 and max_magnitude_error <= 0.025 else "failed",
                "points_compared": len(errors),
                "max_complex_relative_error": max_error if math.isfinite(max_error) else None,
                "max_magnitude_relative_error": max_magnitude_error if math.isfinite(max_magnitude_error) else None,
                "target_check_source": "product_metrics" if channel_index == 0 else "analytic_transfer_oracle",
            }
        )
        if frequencies and all(abs(complex(vin)) for vin in input_values):
            sample_index = min(range(len(frequencies)), key=lambda idx: abs(complex(frequencies[idx]).real - 100_000.0))
            sample_frequency = complex(frequencies[sample_index]).real
            observed = complex(output_values[sample_index]) / complex(input_values[sample_index])
            row["observed_transfer_at_100khz"] = {
                "frequency_hz": sample_frequency,
                "real": observed.real,
                "imag": observed.imag,
                "magnitude": abs(observed),
                "phase_deg": math.degrees(cmath.phase(observed)),
            }
        if channel_index == 0:
            row["target_checks"] = result.target_checks
            row["metrics"] = result.metrics
            if any(state != "passed" for state in result.target_checks.values()):
                failed = True
        elif channel.expected_bandwidth_hz is not None:
            target_window = channel.target_metrics or fixture.targets
            lower = target_window.get("bandwidth_hz_min", target_window.get("bandwidth_min_hz", -math.inf))
            upper = target_window.get("bandwidth_hz_max", target_window.get("bandwidth_max_hz", math.inf))
            passed = lower <= channel.expected_bandwidth_hz <= upper
            row["analytic_bandwidth_hz"] = channel.expected_bandwidth_hz
            row["analytic_target_status"] = "passed" if passed else "failed"
            failed = failed or not passed
        if max_error > 0.025 or max_magnitude_error > 0.025:
            failed = True
        channels_out.append(row)

    return {
        "name": fixture.name,
        "physical_passives": len(fixture.components),
        "analysis_runs": 1,
        "status": "failed" if failed else report.status,
        "summary": report.summary,
        "raw_file": str(raw_path) if raw_path else None,
        "raw_sha256": raw_sha256,
        "channels": channels_out,
    }


def _run_ot_overrange(output: Path) -> list[dict]:
    records = []
    for resistance, expected_v in ((150.0, 3.0), (250.0, 5.0)):
        ref = f"OT_{int(resistance)}R"
        case_dir = output / ref
        case_dir.mkdir(parents=True, exist_ok=True)
        netlist = case_dir / f"{ref}.cir"
        export_spice_netlist(
            [_part("R1", f"{int(resistance)}", "LOOP", "GND")],
            netlist,
            analysis_type="op",
            sources=[{"ref": "ITEST", "type": "I", "positive": "GND", "negative": "LOOP", "dc": 0.02}],
            title=f"Illustrative 4-20mA burden {int(resistance)} ohm",
        )
        result = run_simulation(netlist, sim_type="op")
        measured = next(
            (values[-1] for name, values in result.traces.items() if name.casefold() == "v(loop)" and values),
            None,
        )
        row = {
            "name": ref,
            "resistance_ohm": resistance,
            "current_a": 0.02,
            "analytic_voltage_v": expected_v,
            "status": result.status,
            "evidence_verified": result.evidence_verified,
            "measured_voltage_v": measured,
            "target_status": "unverified_no_validated_comparator",
            "negative_control": "unverified",
        }
        if result.status == "ok" and measured is not None:
            row["oracle_status"] = (
                "passed" if math.isclose(float(measured), expected_v, rel_tol=0.01, abs_tol=0.02) else "failed"
            )
            if row["oracle_status"] == "failed":
                row["status"] = "failed"
            elif expected_v > 3.3:
                row["negative_control"] = "overrange_detected"
            else:
                row["negative_control"] = "within_illustrative_3v3_headroom"
        else:
            row["oracle_status"] = "unverified" if result.status == "skipped" else "failed"
        records.append(row)
    return records


def _json_default(value):
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, type=Path, help="new or empty directory for netlists/raw files/report"
    )
    parser.add_argument(
        "--ngspice", help="optional explicit ngspice executable; CIRCUIT_WEAVER_NGSPICE is also honored"
    )
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    old_ngspice = os.environ.get("CIRCUIT_WEAVER_NGSPICE")
    used_ngspice = args.ngspice or old_ngspice or "PATH discovery"
    if args.ngspice:
        os.environ["CIRCUIT_WEAVER_NGSPICE"] = args.ngspice
    try:
        fixtures = _make_fixtures()
        ac_results = [_run_ac_fixture(fixture, output) for fixture in fixtures]
        ot_results = _run_ot_overrange(output)
    finally:
        if args.ngspice:
            if old_ngspice is None:
                os.environ.pop("CIRCUIT_WEAVER_NGSPICE", None)
            else:
                os.environ["CIRCUIT_WEAVER_NGSPICE"] = old_ngspice

    iq_matched = next(row for row in ac_results if row["name"] == "sdr_iq_matched")
    iq_mismatch = next(row for row in ac_results if row["name"] == "sdr_iq_mismatched")
    iq_control = "unverified"
    if all(ch.get("oracle_status") == "passed" for ch in iq_matched["channels"] + iq_mismatch["channels"]):
        def measured_delta(case: dict) -> tuple[float, float] | None:
            by_name = {channel["channel"]: channel for channel in case["channels"]}
            i_data = by_name.get("i", {}).get("observed_transfer_at_100khz")
            q_data = by_name.get("q", {}).get("observed_transfer_at_100khz")
            if not i_data or not q_data:
                return None
            i_value = complex(i_data["real"], i_data["imag"])
            q_value = complex(q_data["real"], q_data["imag"])
            relative = abs(i_value - q_value) / max(abs(i_value), 1e-12)
            phase_delta = abs(math.degrees(cmath.phase(q_value / i_value)))
            return relative, phase_delta

        matched_delta = measured_delta(iq_matched)
        mismatched_delta = measured_delta(iq_mismatch)
        if matched_delta is not None and mismatched_delta is not None:
            iq_control = "detected" if matched_delta[0] < 0.01 and mismatched_delta[0] > 0.01 else "failed"
            results_iq = {
                "matched_complex_relative_delta_at_100khz": matched_delta[0],
                "matched_phase_delta_deg_at_100khz": matched_delta[1],
                "mismatched_complex_relative_delta_at_100khz": mismatched_delta[0],
                "mismatched_phase_delta_deg_at_100khz": mismatched_delta[1],
            }
        else:
            iq_control = "unverified"
            results_iq = {}
    else:
        results_iq = {}

    results = {
        "suite": "diverse-passive-spice-v1",
        "ngspice": used_ngspice,
        "ac_fixtures": ac_results,
        "ot_dc_overrange": ot_results,
        "negative_controls": {
            "iq_channel_mismatch": iq_control,
            "iq_measured_deltas": results_iq,
            "250_ohm_20ma_3v3_headroom": next(
                (row["negative_control"] for row in ot_results if row["resistance_ohm"] == 250), "unverified"
            ),
        },
    }
    statuses = [row["status"] for row in ac_results] + [row["status"] for row in ot_results]
    if any(status == "failed" for status in statuses) or iq_control == "failed":
        results["status"] = "failed"
        exit_code = 1
    elif any(status in {"skipped", "unverified"} for status in statuses) or iq_control == "unverified":
        results["status"] = "unverified"
        exit_code = 2
    else:
        results["status"] = "verified"
        exit_code = 0
    report_path = output / "suite-report.json"
    report_path.write_text(json.dumps(results, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(json.dumps({"status": results["status"], "report": str(report_path)}, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
