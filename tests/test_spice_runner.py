"""Tests for SPICE simulation runner."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from circuit_weaver.spice_runner import (
    SimulationResult,
    _extract_metrics,
    _find_ngspice,
    _parse_raw_ascii,
    run_simulation,
)


class TestFindNgspice:
    def test_returns_none_when_not_installed(self):
        with patch("shutil.which", return_value=None):
            assert _find_ngspice() is None

    def test_returns_path_when_installed(self):
        with patch("shutil.which", return_value="/usr/bin/ngspice"):
            assert _find_ngspice() == "/usr/bin/ngspice"


class TestRunSimulation:
    def test_skipped_when_ngspice_missing(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text(".end")
        with patch("circuit_weaver.spice_runner._find_ngspice", return_value=None):
            result = run_simulation(netlist)
        assert result.status == "skipped"
        assert "ngspice not installed" in result.skip_reason

    def test_failed_when_netlist_missing(self, tmp_path):
        with patch("circuit_weaver.spice_runner._find_ngspice", return_value="/usr/bin/ngspice"):
            result = run_simulation(tmp_path / "nonexistent.cir")
        assert result.status == "failed"
        assert "not found" in result.skip_reason

    @staticmethod
    def _raw(path, contents=None):
        path.write_text(contents or """Title: test
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 3
Variables:
0\ttime\ttime
1\tv(out)\tvoltage
Values:
0 0
 1 1.0
1 1e-6
 1 1.1
2 2e-6
 1 1.0
""")

    def test_success_requires_fresh_verified_raw_evidence(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text(".tran 1u 2u\n.end")

        def run(*args, **kwargs):
            command = args[0]
            assert "-a" not in command
            assert kwargs["env"]["SPICE_ASCIIRAWFILE"] == "1"
            self._raw(Path(command[command.index("-r") + 1]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"), patch(
            "circuit_weaver.spice_runner.subprocess.run", side_effect=run
        ):
            result = run_simulation(netlist, sim_type="tran")
        assert result.status == "ok"
        assert result.evidence_verified
        assert result.metrics

    def test_stale_raw_file_cannot_satisfy_run(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text(".tran 1u 2u\n.end")
        self._raw(tmp_path / "results.raw")
        with patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"), patch(
            "circuit_weaver.spice_runner.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            result = run_simulation(netlist, sim_type="tran")
        assert result.status == "failed"
        assert not result.evidence_verified
        assert "no output" in result.skip_reason
        assert (tmp_path / "results.raw").exists()

    def test_nonzero_process_fails_even_if_raw_output_exists(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text(".tran 1u 2u\n.end")

        def run(*args, **kwargs):
            command = args[0]
            self._raw(Path(command[command.index("-r") + 1]))
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        with patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"), patch(
            "circuit_weaver.spice_runner.subprocess.run", side_effect=run
        ):
            result = run_simulation(netlist, sim_type="tran")
        assert result.status == "failed"
        assert not result.evidence_verified
        assert "exit 1" in result.skip_reason

    def test_missing_required_axis_fails_closed(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text(".tran 1u 2u\n.end")

        def run(*args, **kwargs):
            command = args[0]
            self._raw(Path(command[command.index("-r") + 1]), """Title: test
Flags: real
No. Variables: 2
No. Points: 2
Variables:
0\tindex\tindex
1\tv(out)\tvoltage
Values:
0 0
 1 1.0
1 1
 1 1.1
""")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"), patch(
            "circuit_weaver.spice_runner.subprocess.run", side_effect=run
        ):
            result = run_simulation(netlist, sim_type="tran")
        assert result.status == "failed"
        assert not result.evidence_verified
        assert "time axis" in result.skip_reason

    def test_concurrent_runs_use_isolated_raw_files_and_preserve_include_cwd(self, tmp_path):
        netlist = tmp_path / "same.cir"
        netlist.write_text('.include "models/device.lib"\n.tran 1u 2u\n.end')
        (tmp_path / "results.raw").write_text("user's existing raw data")
        barrier = Barrier(2)

        def run(*args, **kwargs):
            command = args[0]
            raw_path = Path(command[command.index("-r") + 1])
            staged_netlist = Path(command[-1])
            assert kwargs["cwd"] == str(tmp_path.resolve())
            assert '.include "models/device.lib"' in staged_netlist.read_text()
            barrier.wait(timeout=5)
            self._raw(raw_path)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"), patch(
            "circuit_weaver.spice_runner.subprocess.run", side_effect=run
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run_simulation, netlist, sim_type="tran") for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]

        assert all(result.status == "ok" for result in results)
        assert len({result.raw_file for result in results}) == 2
        assert all(Path(result.raw_file).is_file() for result in results)
        assert (tmp_path / "results.raw").read_text() == "user's existing raw data"

    def test_to_dict(self):
        result = SimulationResult(
            status="ok", sim_type="tran",
            traces={"time": [0, 1], "V(vout)": [3.3, 3.3]},
            metrics={"ripple_mv": 5.0},
            duration_sec=1.23,
        )
        d = result.to_dict()
        assert d["status"] == "ok"
        assert d["sim_type"] == "tran"
        assert d["trace_count"] == 2
        assert d["metrics"]["ripple_mv"] == 5.0
        assert d["duration_sec"] == 1.23


class TestParseRawAscii:
    def test_parse_simple_raw(self, tmp_path):
        raw_content = """Title: Test
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 3
Variables:
0\ttime\ttime
1\tv(out)\tvoltage
Values:
0 0.0
 1 3.3
1 1e-6
 1 3.31
2 2e-6
 1 3.29
"""
        raw_file = tmp_path / "test.raw"
        raw_file.write_text(raw_content)
        traces, var_names = _parse_raw_ascii(raw_file)
        assert "time" in var_names
        assert "v(out)" in var_names
        assert traces["time"] == [0.0, 1e-6, 2e-6]

    def test_empty_raw_file(self, tmp_path):
        raw_file = tmp_path / "empty.raw"
        raw_file.write_text("")
        with pytest.raises(ValueError, match="variable table"):
            _parse_raw_ascii(raw_file)

    def test_reject_incomplete_point_count(self, tmp_path):
        raw_file = tmp_path / "short.raw"
        raw_file.write_text("""Title: test
Flags: real
No. Variables: 2
No. Points: 2
Variables:
0 time time
1 v(out) voltage
Values:
0 0
 1 1.0
""")
        with pytest.raises(ValueError, match="point count"):
            _parse_raw_ascii(raw_file)

    def test_parse_complex_raw_values_as_magnitudes(self, tmp_path):
        raw_file = tmp_path / "complex.raw"
        raw_file.write_text("""Title: test
Flags: complex
No. Variables: 2
No. Points: 2
Variables:
0 frequency frequency
1 v(out) voltage
Values:
0 (10,0)
 1 (3,4)
1 (20,0)
 1 (0,2)
""")
        traces, names = _parse_raw_ascii(raw_file)
        assert names == ["frequency", "v(out)"]
        assert traces["v(out)"] == [5.0, 2.0]


class TestExtractMetrics:
    def test_tran_ripple_extraction(self):
        traces = {
            "time": [0, 1e-6, 2e-6, 3e-6, 4e-6, 5e-6, 6e-6, 7e-6, 8e-6, 9e-6],
            "V(vout)": [0, 0, 3.28, 3.30, 3.32, 3.29, 3.31, 3.30, 3.28, 3.32],
        }
        metrics = _extract_metrics(traces, "tran")
        assert "V(vout)_ripple_mv" in metrics
        assert "V(vout)_avg_v" in metrics
        # Ripple should be (3.32 - 3.28) * 1000 = 40 mV (from steady state)
        assert metrics["V(vout)_ripple_mv"] > 0

    def test_tran_time_duration(self):
        traces = {"time": [0, 1e-6, 2e-6]}
        metrics = _extract_metrics(traces, "tran")
        assert "sim_duration_s" in metrics

    def test_op_metrics(self):
        traces = {"V(vdd)": [3.3], "I(r1)": [0.001]}
        metrics = _extract_metrics(traces, "op")
        assert metrics["V(vdd)"] == 3.3
        assert metrics["I(r1)"] == 0.001

    def test_empty_traces(self):
        metrics = _extract_metrics({}, "tran")
        assert metrics == {}
