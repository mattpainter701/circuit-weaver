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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SimulationResult:
    """Structured result from a SPICE simulation."""

    status: str  # "ok", "failed", "skipped", "timeout"
    sim_type: str = ""  # "tran", "ac", "dc", "op"
    raw_file: str | None = None
    traces: dict[str, list[float | complex]] = field(default_factory=dict)
    time_axis: list[float] | None = None
    freq_axis: list[float] | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    duration_sec: float = 0.0
    skip_reason: str = ""
    evidence_verified: bool = False
    target_checks: dict[str, str] = field(default_factory=dict)
    unsupported_components: list[str] = field(default_factory=list)
    missing_models: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        safe_metrics = {
            key: value
            for key, value in self.metrics.items()
            if isinstance(value, (int, float)) and math.isfinite(float(value))
        }
        return {
            "status": self.status,
            "sim_type": self.sim_type,
            "raw_file": self.raw_file,
            "trace_names": list(self.traces.keys()),
            "trace_count": len(self.traces),
            "metrics": safe_metrics,
            "warnings": self.warnings,
            "duration_sec": round(self.duration_sec, 2),
            "skip_reason": self.skip_reason,
            "evidence_verified": self.evidence_verified,
            "target_checks": self.target_checks,
            "unsupported_components": self.unsupported_components,
            "missing_models": self.missing_models,
        }


def _find_ngspice() -> str | None:
    """Locate ngspice binary on PATH.

    Returns the path string if found, None otherwise.
    """
    explicit = os.environ.get("CIRCUIT_WEAVER_NGSPICE")
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        return shutil.which(explicit)
    # The Windows GUI executable can wait for an interactive window and hang
    # batch jobs. Prefer the console build when both are installed.
    names = ["ngspice_con", "ngspice"] if os.name == "nt" else ["ngspice"]
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _parse_raw_ascii(raw_path: Path) -> tuple[dict[str, list[float | complex]], list[str]]:
    """Parse ngspice ASCII .raw output file into traces.

    Returns:
        (traces dict, variable names list)
    """
    content = raw_path.read_text(encoding="utf-8", errors="strict")
    traces: dict[str, list[float | complex]] = {}
    var_names: list[str] = []
    expected_vars: int | None = None
    expected_points: int | None = None
    flags = ""
    section = "header"
    samples: list[list[float | complex]] = []
    current: list[float | complex] | None = None
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
            if not match or int(match.group(1)) != len(var_names):
                raise ValueError("Malformed or non-sequential ngspice variable table")
            var_names.append(match.group(2))
            continue

        if not line:
            continue
        # ngspice ASCII format indexes the first value in each point with the
        # point number. The remaining variables in that point are unindexed.
        # Some older/raw producers indexed every vector, so accept that form
        # only when the index matches the next vector position.
        if current is None:
            match = re.match(r"^(\d+)\s+(.+)$", line)
            if not match or int(match.group(1)) != len(samples):
                raise ValueError("Malformed or non-sequential ngspice point index")
            current = []
            value_text = match.group(2).strip()
        elif len(current) >= expected_vars:
            raise ValueError("Raw point contains more values than declared variables")
        else:
            match = re.match(r"^(\d+)\s+(.+)$", line)
            if match and int(match.group(1)) == len(current):
                value_text = match.group(2).strip()
            else:
                value_text = line
        if flags == "complex":
            pair = re.split(r"[\s,]+", value_text)
            if len(pair) != 2:
                raise ValueError("Malformed complex ngspice value")
            value: float | complex = complex(float(pair[0]), float(pair[1]))
        elif flags == "real":
            parts = value_text.split()
            if len(parts) != 1:
                raise ValueError("Malformed real ngspice value")
            value = float(parts[0])
        else:
            raise ValueError(f"Unsupported or missing ngspice raw flags: {flags or 'unknown'}")
        if not math.isfinite(complex(value).real) or not math.isfinite(complex(value).imag):
            raise ValueError("Raw output contains a non-finite value")
        current.append(value)
        if expected_vars is not None and len(current) == expected_vars:
            samples.append(current)
            current = None
    if current is not None:
        raise ValueError("Raw file ends with an incomplete point")
    if not var_names or expected_vars != len(var_names):
        raise ValueError("Raw file variable table is missing or incomplete")
    if len(set(var_names)) != len(var_names):
        raise ValueError("Raw file contains duplicate vector names")
    if expected_points is None or expected_points <= 0 or len(samples) != expected_points:
        raise ValueError("Raw file point count is missing or incomplete")
    if any(len(sample) != expected_vars for sample in samples):
        raise ValueError("Raw file has an incomplete point")
    traces = {name: [] for name in var_names}
    for sample in samples:
        for idx, value in enumerate(sample):
            traces[var_names[idx]].append(value)
    return traces, var_names


def _extract_metrics(
    traces: dict[str, list[float | complex]],
    sim_type: str,
    params: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Extract domain-specific metrics from simulation traces.

    For transient: ripple_mv, settling_time_us, overshoot_pct, avg_voltage
    For AC: low-frequency gain and interpolated -3 dB bandwidth when the
    input and output complex vectors are available.
    For DC: output voltage at operating point
    """
    metrics: dict[str, float] = {}
    params = params or {}

    if sim_type == "tran":
        selector = params.get("output_trace")
        if selector is None:
            # Backward-compatible convenience for legacy callers only. Explicit
            # selectors always use exact equality below.
            selected = [
                (name, values)
                for name, values in traces.items()
                if any(token in name.casefold() for token in ("vout", "v_out", "3v3", "3p3", "1v8", "5v"))
            ][:1]
        else:
            selected = [
                (name, values) for name, values in traces.items() if name.casefold() == str(selector).casefold()
            ]
        if (selector is None or selector) and len(selected) == 1 and selected[0][0].casefold().startswith("v("):
            name, values = selected[0]
            if len(values) >= 5 and all(math.isfinite(complex(value).real) for value in values):
                # Ignore startup only when the caller supplied a selected output.
                steady = values[max(1, len(values) // 5) :]
                if steady:
                    metrics[f"{name}_ripple_mv"] = round((max(steady) - min(steady)) * 1000, 2)
                    metrics[f"{name}_avg_v"] = round(sum(steady) / len(steady), 6)

        # Time axis info
        time_trace = traces.get("time", [])
        if time_trace:
            metrics["sim_duration_s"] = round(time_trace[-1] - time_trace[0], 6) if len(time_trace) > 1 else 0.0

    elif sim_type == "ac":
        frequency = traces.get("frequency", [])
        output_name = str(params.get("output_trace", "v(out)")).lower()
        input_name = str(params.get("input_trace", "v(in)")).lower()
        output = next((v for k, v in traces.items() if k.lower() == output_name), None)
        input_values = next((v for k, v in traces.items() if k.lower() == input_name), None)
        if "output_trace" not in params:
            output = output or next(
                (v for alias in ("v(vout)", "v(output)", "v(load)") for k, v in traces.items() if k.lower() == alias),
                None,
            )
        if "input_trace" not in params:
            input_values = input_values or next(
                (
                    v
                    for alias in ("v(vin)", "v(input)", "v(source)", "v(src)")
                    for k, v in traces.items()
                    if k.lower() == alias
                ),
                None,
            )
        if frequency and output and input_values and len(frequency) == len(output) == len(input_values):
            gains = [
                abs(complex(out)) / abs(complex(inp)) if abs(complex(inp)) > 0 else math.nan
                for out, inp in zip(output, input_values)
            ]
            if all(math.isfinite(g) for g in gains) and gains[0] > 0:
                metrics["low_frequency_gain"] = round(gains[0], 8)
                threshold = gains[0] / math.sqrt(2)
                crossing = next((i for i, gain in enumerate(gains) if gain <= threshold), None)
                if crossing is not None:
                    if crossing == 0:
                        crossing_frequency = complex(frequency[0]).real
                    else:
                        f1 = complex(frequency[crossing - 1]).real
                        f2 = complex(frequency[crossing]).real
                        g1 = 20 * math.log10(gains[crossing - 1])
                        g2 = 20 * math.log10(gains[crossing])
                        target_db = 20 * math.log10(threshold)
                        if f1 <= 0 or f2 <= f1 or g2 == g1 or gains[crossing] <= 0:
                            crossing_frequency = f2
                        else:
                            fraction = min(1.0, max(0.0, (target_db - g1) / (g2 - g1)))
                            crossing_frequency = 10 ** (math.log10(f1) + fraction * (math.log10(f2) - math.log10(f1)))
                    metrics["bandwidth_hz"] = round(crossing_frequency, 6)

    elif sim_type == "op" or sim_type == "dc":
        # Report all node voltages/currents
        for name, values in traces.items():
            if values:
                value = complex(values[-1])
                if math.isfinite(value.real) and math.isfinite(value.imag):
                    metrics[name] = round(value.real, 9)

    return {
        name: value
        for name, value in metrics.items()
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    }


def run_simulation(
    netlist: str | Path,
    *,
    sim_type: str = "tran",
    timeout: int = 120,
    working_dir: str | Path | None = None,
    analysis_params: dict[str, Any] | None = None,
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

    if not netlist.exists():
        return SimulationResult(
            status="failed",
            sim_type=sim_type,
            skip_reason=f"Netlist not found: {netlist}",
        )

    wdir = Path(working_dir) if working_dir else netlist.parent
    netlist = netlist.resolve()
    expected_outputs = [wdir / "results.raw", netlist.with_suffix(".raw")]
    try:
        source = netlist.read_text(encoding="utf-8", errors="strict")
    except OSError as exc:
        return SimulationResult(status="failed", sim_type=sim_type, skip_reason=f"Cannot read netlist: {exc}")
    unsupported_lines = re.findall(
        r"^\*\s*Skipped\s+([^\s(]+)\s+\(([^)]*)\)\s*-\s*(.*)$",
        source,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    unsupported_refs = re.findall(r"^\*\s*Skipped\s+([^\s(]+)", source, flags=re.MULTILINE | re.IGNORECASE)
    if unsupported_refs:
        details_by_ref = {ref: (model, reason) for ref, model, reason in unsupported_lines}
        return SimulationResult(
            status="failed",
            sim_type=sim_type,
            skip_reason="Netlist contains unsupported components without verified models",
            unsupported_components=unsupported_refs,
            missing_models=[
                {
                    "ref": ref,
                    "mpn": ""
                    if details_by_ref.get(ref, ("no MPN", ""))[0] == "no MPN"
                    else details_by_ref.get(ref, ("", ""))[0],
                    "reason": details_by_ref.get(ref, ("", "unspecified unsupported component"))[1],
                }
                for ref in unsupported_refs
            ],
        )
    active_lines = []
    in_subckt = False
    for line in source.splitlines():
        stripped = line.strip()
        if re.match(r"^\.subckt\b", stripped, re.I):
            in_subckt = True
            continue
        if re.match(r"^\.ends\b", stripped, re.I):
            in_subckt = False
            continue
        if stripped and not in_subckt and not stripped.startswith(("*", ".")):
            active_lines.append(stripped)
    if not active_lines:
        return SimulationResult(status="failed", sim_type=sim_type, skip_reason="Netlist has no circuit elements")
    if (
        sim_type in {"tran", "ac", "dc"}
        and not any(line[0].upper() in {"V", "I", "B"} and len(line.split()) >= 4 for line in active_lines)
        and not (sim_type == "tran" and re.search(r"^\s*\.ic\b", source, re.I | re.M))
    ):
        return SimulationResult(
            status="failed", sim_type=sim_type, skip_reason=f"{sim_type} analysis has no explicit independent source"
        )
    analysis_cards = {
        "tran": r"^\s*\.tran\b",
        "ac": r"^\s*\.ac\b",
        "dc": r"^\s*\.dc\b",
        "op": r"^\s*\.op\b",
    }
    if sim_type in analysis_cards and not re.search(analysis_cards[sim_type], source, re.I | re.M):
        return SimulationResult(status="failed", sim_type=sim_type, skip_reason=f"{sim_type} analysis card is missing")
    # Includes and subcircuit pin order must be explicit. Bare legacy MPN-to-path
    # entries are not enough to establish electrical connectivity.
    included_text = ""
    include_pattern = re.compile(r'^\s*\.include\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))', re.I | re.M)
    for match in include_pattern.finditer(source):
        include = next(value for value in match.groups() if value is not None)
        include_path = Path(include)
        if not include_path.is_absolute():
            include_path = netlist.parent / include_path
        if not include_path.is_file():
            return SimulationResult(
                status="failed", sim_type=sim_type, skip_reason=f"Included model file not found: {include_path}"
            )
        try:
            included_text += "\n" + include_path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            return SimulationResult(
                status="failed", sim_type=sim_type, skip_reason=f"Included model is not readable SPICE text: {exc}"
            )
    subckt_pin_counts: dict[str, int] = {}
    for line in (source + "\n" + included_text).splitlines():
        declaration = re.match(r"^\s*\.subckt\s+(\S+)(?:\s+(.*?))?\s*$", line, re.I)
        if declaration:
            pins = (declaration.group(2) or "").split()
            if "params:" in [token.lower() for token in pins]:
                pins = pins[: [token.lower() for token in pins].index("params:")]
            subckt_pin_counts[declaration.group(1).lower()] = len(pins)
    for instance in re.findall(r"^\s*X\S+\s+(.+)$", source, flags=re.MULTILINE | re.IGNORECASE):
        tokens = instance.split()
        if (
            len(tokens) < 2
            or tokens[-1].lower() not in subckt_pin_counts
            or len(tokens) - 1 != subckt_pin_counts[tokens[-1].lower()]
        ):
            return SimulationResult(
                status="failed",
                sim_type=sim_type,
                skip_reason="Subcircuit instance has no matching pin-compatible explicit .subckt model",
            )
    ngspice = _find_ngspice()
    if ngspice is None:
        return SimulationResult(
            status="skipped",
            sim_type=sim_type,
            skip_reason="ngspice not installed. Install ngspice to enable circuit simulation.",
        )
    for output in expected_outputs:
        try:
            output.unlink(missing_ok=True)
        except OSError as exc:
            return SimulationResult(
                status="failed", sim_type=sim_type, skip_reason=f"Cannot clear previous raw output {output}: {exc}"
            )

    try:
        # Run ngspice in batch mode
        run_kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "cwd": str(wdir),
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run([ngspice, "-b", str(netlist)], **run_kwargs)

        duration = time.time() - t0

        # Check for fresh raw file output (previous files were removed above).
        raw_file = wdir / "results.raw"
        if not raw_file.exists():
            # Try default output name
            raw_file = netlist.with_suffix(".raw")

        warnings = []
        diagnostics = f"{proc.stdout}\n{proc.stderr}"
        for line in diagnostics.splitlines():
            if "warning" in line.lower() or "error" in line.lower():
                warnings.append(line.strip())

        solver_failure = re.search(
            r"\b(?:fatal\s+)?error\s*:|singular matrix|timestep too small|failed to converge|no convergence",
            diagnostics,
            re.I,
        )
        if proc.returncode != 0 or solver_failure:
            result = SimulationResult(
                status="failed",
                sim_type=sim_type,
                skip_reason=f"ngspice reported failure (exit {proc.returncode})",
                warnings=warnings[:10],
                duration_sec=duration,
            )
            return _log_result(result, netlist, sim_type)

        if raw_file.exists() and raw_file.stat().st_size > 0:
            try:
                traces, _ = _parse_raw_ascii(raw_file)
                metrics = _extract_metrics(traces, sim_type, analysis_params)
                required = {"tran": {"time"}, "ac": {"frequency"}, "dc": set(), "op": set()}.get(sim_type, set())
                if not traces or not required.issubset(traces):
                    raise ValueError(f"Raw output lacks required vectors: {sorted(required - set(traces))}")
                axis_name = "time" if sim_type == "tran" else "frequency" if sim_type == "ac" else ""
                if axis_name:
                    axis = [complex(value).real for value in traces[axis_name]]
                    if len(axis) < 2 or any(right <= left for left, right in zip(axis, axis[1:])):
                        raise ValueError(f"Raw {axis_name} axis is not strictly increasing")
                if sim_type == "ac" and not metrics:
                    raise ValueError("AC vectors parsed, but no input/output transfer function was verified")

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

    return _log_result(result, netlist, sim_type)


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
