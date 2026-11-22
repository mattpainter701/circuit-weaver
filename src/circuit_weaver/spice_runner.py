"""ngspice simulation runner for circuit-weaver.

Runs ngspice on generated .cir netlists, parses results, and extracts
domain-specific metrics. Follows the erc_runner.py pattern for graceful
degradation when ngspice is not installed.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SimulationResult:
    """Structured result from a SPICE simulation."""

    status: str  # "ok", "failed", "skipped", "timeout"
    sim_type: str = ""  # "tran", "ac", "dc", "op"
    raw_file: str | None = None
    traces: dict[str, list[float]] = field(default_factory=dict)
    time_axis: list[float] | None = None
    freq_axis: list[float] | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    duration_sec: float = 0.0
    skip_reason: str = ""
    evidence_verified: bool = False
    target_checks: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "sim_type": self.sim_type,
            "raw_file": self.raw_file,
            "trace_names": list(self.traces.keys()),
            "trace_count": len(self.traces),
            "metrics": self.metrics,
            "warnings": self.warnings,
            "duration_sec": round(self.duration_sec, 2),
            "skip_reason": self.skip_reason,
            "evidence_verified": self.evidence_verified,
            "target_checks": self.target_checks,
        }


def _find_ngspice() -> str | None:
    """Locate ngspice binary on PATH.

    Returns the path string if found, None otherwise.
    """
    path = shutil.which("ngspice")
    return path


def _parse_raw_ascii(raw_path: Path) -> tuple[dict[str, list[float]], list[str]]:
    """Parse ngspice ASCII .raw output file into traces.

    Returns:
        (traces dict, variable names list)
    """
    content = raw_path.read_text(encoding="utf-8", errors="strict")
    names: list[str] = []
    expected_vars: int | None = None
    expected_points: int | None = None
    flags = ""
    section = "header"
    points: list[list[float]] = []
    current: list[float] = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if section == "header":
            if line.startswith("Flags:"):
                flags = line.split(":", 1)[1].strip().lower()
            elif line.startswith("No. Variables:"):
                expected_vars = int(line.split(":", 1)[1])
            elif line.startswith("No. Points:"):
                expected_points = int(line.split(":", 1)[1])
            elif line == "Variables:":
                section = "variables"
            continue
        if section == "variables":
            if line == "Values:":
                section = "values"
                continue
            match = re.match(r"^(\d+)\s+([^\s]+)\s+([^\s]+)(?:\s+.*)?$", line)
            if not match or int(match.group(1)) != len(names):
                raise ValueError("Malformed or non-sequential raw variable table")
            names.append(match.group(2))
            continue
        if not line:
            continue

        # Each point starts with its sequential point index. Remaining variables
        # may be on continuation lines, optionally prefixed by their variable index.
        match = re.match(r"^(\d+)\s+(.+)$", line)
        if not current:
            if not match or int(match.group(1)) != len(points):
                raise ValueError("Malformed or non-sequential raw point index")
            value_text = match.group(2).strip()
        elif match and int(match.group(1)) == len(current):
            value_text = match.group(2).strip()
        else:
            value_text = line

        if flags == "complex":
            pair = re.split(r"[\s,]+", value_text.strip("()"))
            if len(pair) != 2:
                raise ValueError("Malformed complex raw value")
            value = complex(float(pair[0]), float(pair[1]))
            if not math.isfinite(value.real) or not math.isfinite(value.imag):
                raise ValueError("Raw output contains non-finite values")
            # Existing trace consumers are real-valued; retain magnitude for AC
            # response vectors while preserving the real frequency axis.
            parsed_value = abs(value)
        elif flags == "real":
            try:
                parsed_value = float(value_text)
            except ValueError as exc:
                raise ValueError("Malformed real raw value") from exc
            if not math.isfinite(parsed_value):
                raise ValueError("Raw output contains non-finite values")
        else:
            raise ValueError(f"Unsupported or missing raw flags: {flags or 'unknown'}")
        current.append(parsed_value)
        if expected_vars is not None and len(current) == expected_vars:
            points.append(current)
            current = []
        elif expected_vars is not None and len(current) > expected_vars:
            raise ValueError("Raw point has more values than declared variables")

    if current:
        raise ValueError("Raw output ends with an incomplete point")
    if not names or expected_vars != len(names):
        raise ValueError("Raw variable table is missing or incomplete")
    if expected_points is None or expected_points <= 0 or len(points) != expected_points:
        raise ValueError("Raw output point count is missing or incomplete")
    return {name: [point[index] for point in points] for index, name in enumerate(names)}, names


def _validate_raw_evidence(traces: dict[str, list[float]], sim_type: str) -> None:
    """Reject empty, malformed, or incomplete raw output before calling it evidence."""
    if not traces or any(not values for values in traces.values()):
        raise ValueError("raw output contains no complete vectors")
    if any(not math.isfinite(value) for values in traces.values() for value in values):
        raise ValueError("raw output contains non-finite values")
    if len({len(values) for values in traces.values()}) != 1:
        raise ValueError("raw output vectors have inconsistent point counts")
    axis_name = {"tran": "time", "ac": "frequency"}.get(sim_type)
    if axis_name:
        axis = traces.get(axis_name)
        if axis is None or len(axis) < 2 or any(b <= a for a, b in zip(axis, axis[1:])):
            raise ValueError(f"raw output lacks a strictly increasing {axis_name} axis")
    if sim_type in {"tran", "ac"}:
        axis_name = "time" if sim_type == "tran" else "frequency"
        if not any(name.casefold() != axis_name for name in traces):
            raise ValueError("raw output contains no measured signal vectors")


def _extract_metrics(
    traces: dict[str, list[float]],
    sim_type: str,
    params: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Extract domain-specific metrics from simulation traces.

    For transient: ripple_mv, settling_time_us, overshoot_pct, avg_voltage
    For AC: bandwidth_hz (placeholder)
    For DC: output voltage at operating point
    """
    metrics: dict[str, float] = {}
    params = params or {}

    if sim_type == "tran":
        # Look for output voltage traces (V(vout), V(out), V(3v3), etc.)
        for name, values in traces.items():
            name_lower = name.lower()
            if not values:
                continue

            if any(kw in name_lower for kw in ("vout", "v_out", "3v3", "3p3", "1v8", "5v")):
                # Compute ripple: skip first 20% (startup transient)
                start_idx = max(1, len(values) // 5)
                steady = values[start_idx:]
                if steady:
                    vmin = min(steady)
                    vmax = max(steady)
                    ripple_mv = (vmax - vmin) * 1000
                    avg = sum(steady) / len(steady)
                    metrics[f"{name}_ripple_mv"] = round(ripple_mv, 2)
                    metrics[f"{name}_avg_v"] = round(avg, 4)
                break

        # Time axis info
        time_trace = traces.get("time", [])
        if time_trace:
            metrics["sim_duration_s"] = round(time_trace[-1] - time_trace[0], 6) if len(time_trace) > 1 else 0.0

    elif sim_type == "ac":
        # Placeholder for AC metrics
        for name, values in traces.items():
            if values:
                metrics[f"{name}_points"] = len(values)

    elif sim_type == "op" or sim_type == "dc":
        # Report all node voltages/currents
        for name, values in traces.items():
            if values:
                metrics[name] = round(values[-1], 6)

    return metrics


def run_simulation(
    netlist: str | Path,
    *,
    sim_type: str = "tran",
    timeout: int = 120,
    working_dir: str | Path | None = None,
) -> SimulationResult:
    """Run ngspice on a netlist and parse results.

    Degrades gracefully:
    - Returns status="skipped" if ngspice not installed
    - Returns status="failed" on timeout or parse error
    - Returns status="ok" with parsed traces on success

    Args:
        netlist: Path to .cir netlist file.
        sim_type: Simulation type hint for metric extraction.
        timeout: Maximum seconds to wait for ngspice.
        working_dir: Working directory for ngspice (default: netlist parent).

    Returns:
        SimulationResult with parsed traces and metrics.
    """
    netlist = Path(netlist)
    t0 = time.time()

    ngspice = _find_ngspice()
    if ngspice is None:
        return SimulationResult(
            status="skipped",
            sim_type=sim_type,
            skip_reason="ngspice not installed. Install ngspice to enable circuit simulation.",
        )

    if not netlist.exists():
        return SimulationResult(
            status="failed",
            sim_type=sim_type,
            skip_reason=f"Netlist not found: {netlist}",
        )

    original_netlist = netlist.resolve()
    wdir = (Path(working_dir) if working_dir else original_netlist.parent).resolve()
    try:
        source = original_netlist.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        return SimulationResult(status="failed", sim_type=sim_type, skip_reason=f"Cannot read netlist: {exc}")

    # Each process gets a private input path and raw output. Keep cwd unchanged
    # so relative .include and .lib paths resolve as they did before isolation.
    run_id = uuid.uuid4().hex
    run_netlist = wdir / f".{original_netlist.stem}-{run_id}.cir"
    raw_file = wdir / f".{original_netlist.stem}-{run_id}.raw"
    source = re.sub(
        r"(?im)^(\s*write\s+)(?:\"[^\"]*\"|'[^']*'|\S+)(.*)$",
        lambda match: f'{match.group(1)}"{raw_file}"{match.group(2)}',
        source,
    )
    try:
        run_netlist.write_text(source, encoding="utf-8", errors="strict")
    except OSError as exc:
        return SimulationResult(status="failed", sim_type=sim_type, skip_reason=f"Cannot stage isolated netlist: {exc}")

    try:
        # Run ngspice in batch mode
        proc = subprocess.run(
            [ngspice, "-b", "-r", str(raw_file), str(run_netlist)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(wdir),
            env={**os.environ, "SPICE_ASCIIRAWFILE": "1"},
        )

        duration = time.time() - t0

        warnings = []
        diagnostics = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if diagnostics:
            for line in diagnostics.splitlines():
                if "warning" in line.lower() or "error" in line.lower():
                    warnings.append(line.strip())

        if proc.returncode != 0 or re.search(
            r"\b(?:fatal\s+)?error\s*:|singular matrix|timestep too small|failed to converge|no convergence",
            diagnostics,
            re.IGNORECASE,
        ):
            result = SimulationResult(
                status="failed", sim_type=sim_type,
                skip_reason=f"ngspice reported failure (exit {proc.returncode})",
                warnings=warnings[:10], duration_sec=duration,
            )
        elif raw_file.exists() and raw_file.stat().st_size > 0:
            try:
                traces, var_names = _parse_raw_ascii(raw_file)
                _validate_raw_evidence(traces, sim_type)
                metrics = _extract_metrics(traces, sim_type)

                result = SimulationResult(
                    status="ok",
                    sim_type=sim_type,
                    raw_file=str(raw_file),
                    traces=traces,
                    time_axis=traces.get("time"),
                    freq_axis=traces.get("frequency"),
                    metrics=metrics,
                    warnings=warnings[:10],
                    duration_sec=duration,
                    evidence_verified=True,
                )
            except Exception as exc:
                result = SimulationResult(
                    status="failed",
                    sim_type=sim_type,
                    skip_reason=f"Failed to parse simulation output: {exc}",
                    warnings=warnings[:10],
                    duration_sec=duration,
                )
        else:
            result = SimulationResult(
                status="failed",
                sim_type=sim_type,
                skip_reason="ngspice produced no output file",
                warnings=warnings[:10],
                duration_sec=duration,
            )

    except subprocess.TimeoutExpired:
        result = SimulationResult(
            status="timeout",
            sim_type=sim_type,
            skip_reason=f"ngspice timed out after {timeout}s",
            duration_sec=time.time() - t0,
        )
    except OSError as exc:
        result = SimulationResult(
            status="failed",
            sim_type=sim_type,
            skip_reason=f"Failed to run ngspice: {exc}",
            duration_sec=time.time() - t0,
        )
    finally:
        try:
            run_netlist.unlink(missing_ok=True)
        except OSError as exc:
            if "result" in locals():
                result.warnings.append(f"Could not remove staged netlist: {exc}")

    return _log_result(result, original_netlist, sim_type)


def _log_result(result: SimulationResult, netlist: Path, sim_type: str) -> SimulationResult:
    # Log to design.log
    from .logging_bridge import get_design_logger

    dl = get_design_logger()
    if dl:
        dl.log_simulation(
            sim_type=sim_type,
            target=str(netlist.stem),
            status=result.status,
            metrics=result.metrics,
            duration_sec=result.duration_sec,
        )

    return result
