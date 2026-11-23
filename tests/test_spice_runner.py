"""Tests for SPICE simulation runner."""

import json
import math
import subprocess
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
    def test_returns_none_when_not_installed(self, monkeypatch):
        monkeypatch.delenv("CIRCUIT_WEAVER_NGSPICE", raising=False)
        with patch("shutil.which", return_value=None):
            assert _find_ngspice() is None

    def test_returns_path_when_installed(self, monkeypatch):
        monkeypatch.delenv("CIRCUIT_WEAVER_NGSPICE", raising=False)
        with patch("shutil.which", return_value="/usr/bin/ngspice"):
            assert _find_ngspice() == "/usr/bin/ngspice"


class TestRunSimulation:
    def test_skipped_when_ngspice_missing(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text("V1 in 0 1\nR1 in 0 1k\n.tran 1u 1m\n.end")
        with patch("circuit_weaver.spice_runner._find_ngspice", return_value=None):
            result = run_simulation(netlist)
        assert result.status == "skipped"
        assert "ngspice not installed" in result.skip_reason

    def test_failed_when_netlist_missing(self, tmp_path):
        with patch("circuit_weaver.spice_runner._find_ngspice", return_value="/usr/bin/ngspice"):
            result = run_simulation(tmp_path / "nonexistent.cir")
        assert result.status == "failed"
        assert "not found" in result.skip_reason

    def test_to_dict(self):
        result = SimulationResult(
            status="ok",
            sim_type="tran",
            traces={"time": [0, 1], "V(vout)": [3.3, 3.3]},
            metrics={"ripple_mv": 5.0, "invalid_nan": math.nan, "invalid_inf": math.inf},
            duration_sec=1.23,
        )
        d = result.to_dict()
        assert d["status"] == "ok"
        assert d["sim_type"] == "tran"
        assert d["trace_count"] == 2
        assert d["metrics"]["ripple_mv"] == 5.0
        assert "invalid_nan" not in d["metrics"]
        assert "invalid_inf" not in d["metrics"]
        json.dumps(d, allow_nan=False)
        assert d["duration_sec"] == 1.23
        assert d["evidence_verified"] is False

    def test_real_zero_exit_and_fresh_ascii_raw_is_verified(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text("V1 in 0 PULSE(0 1 0 1n 1n 1u 2u)\nR1 in out 1k\n.tran 1u 10u\n")
        raw = """Title: Test
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 3
Variables:
0 time time
1 v(out) voltage
Values:
0 0.0
 0.0
1 1e-6
 0.5
2 2e-6
 1.0
"""

        def fake_run(*args, **kwargs):
            Path(args[0][args[0].index("-r") + 1]).write_text(raw)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"),
            patch("circuit_weaver.spice_runner.subprocess.run", side_effect=fake_run),
        ):
            result = run_simulation(netlist, sim_type="tran")
        assert result.status == "ok"
        assert result.evidence_verified is True
        assert result.traces["time"] == [0.0, 1e-6, 2e-6]
        assert result.traces["v(out)"] == [0.0, 0.5, 1.0]

    def test_nonzero_exit_cannot_pass_with_raw_output(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text("V1 in 0 1\nR1 in out 1k\n.tran 1u 10u\n")

        def fake_run(*args, **kwargs):
            Path(args[0][args[0].index("-r") + 1]).write_text("apparently nonempty")
            return SimpleNamespace(returncode=1, stdout="", stderr="fatal error: failed")

        with (
            patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"),
            patch("circuit_weaver.spice_runner.subprocess.run", side_effect=fake_run),
        ):
            result = run_simulation(netlist)
        assert result.status == "failed"
        assert result.evidence_verified is False

    def test_stale_raw_file_is_not_reused(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text("V1 in 0 1\nR1 in out 1k\n.tran 1u 10u\n")
        (tmp_path / "results.raw").write_text("stale output")
        with (
            patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"),
            patch(
                "circuit_weaver.spice_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ),
        ):
            result = run_simulation(netlist)
        assert result.status == "failed"
        assert "no output" in result.skip_reason

    def test_unsupported_component_fails_before_engine_call(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text("V1 in 0 1\n* Skipped U1 (unknown) - no SPICE model\n.tran 1u 10u\n")
        with (
            patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"),
            patch("circuit_weaver.spice_runner.subprocess.run") as run,
        ):
            result = run_simulation(netlist)
        run.assert_not_called()
        assert result.status == "failed"
        assert result.unsupported_components == ["U1"]
        assert result.missing_models == [{"ref": "U1", "mpn": "unknown", "reason": "no SPICE model"}]

    def test_explicit_subcircuit_contract_can_run(self, tmp_path):
        model = tmp_path / "model.lib"
        model.write_text(".subckt divider a b\nR1 a b 1k\n.ends divider\n")
        netlist = tmp_path / "test.cir"
        netlist.write_text(f'.include "{model}"\nV1 in 0 1\nXU1 in out divider\n.tran 1u 10u\n')
        raw = """Title: Test
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 2
Variables:
0 time time
1 v(out) voltage
Values:
0 0.0
 0.0
1 1e-6
 0.5
"""

        def fake_run(*args, **kwargs):
            Path(args[0][args[0].index("-r") + 1]).write_text(raw)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"),
            patch("circuit_weaver.spice_runner.subprocess.run", side_effect=fake_run),
        ):
            result = run_simulation(netlist, sim_type="tran")
        assert result.status == "ok"
        assert result.evidence_verified is True

    def test_ac_run_reports_bandwidth_from_complex_transfer_vectors(self, tmp_path):
        netlist = tmp_path / "filter.cir"
        netlist.write_text("V1 in 0 AC 1\nR1 in out 1k\nC1 out 0 1u\n.ac dec 10 1 1k\n")
        raw = """Title: AC
Plotname: AC Analysis
Flags: complex
No. Variables: 3
No. Points: 3
Variables:
0 frequency frequency grid=3
1 v(in) voltage
2 v(out) voltage
Values:
0 1.0, 0.0
 1.0, 0.0
 1.0, 0.0
1 10.0, 0.0
 1.0, 0.0
 0.7, -0.1
2 100.0, 0.0
 1.0, 0.0
 0.1, -0.01
"""

        def fake_run(*args, **kwargs):
            Path(args[0][args[0].index("-r") + 1]).write_text(raw)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"),
            patch("circuit_weaver.spice_runner.subprocess.run", side_effect=fake_run),
        ):
            result = run_simulation(netlist, sim_type="ac")
        assert result.status == "ok"
        assert result.evidence_verified is True
        assert result.metrics["bandwidth_hz"] == 10.0

    def test_ngspice_stdin_is_closed_for_batch_runs(self, tmp_path):
        netlist = tmp_path / "test.cir"
        netlist.write_text("V1 in 0 1\nR1 in out 1k\n.tran 1u 10u\n")
        raw = """Title: Test
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 2
Variables:
0 time time
1 v(out) voltage
Values:
0 0.0
 0.0
1 1e-6
 0.5
"""

        def fake_run(*args, **kwargs):
            assert kwargs["stdin"] is subprocess.DEVNULL
            Path(args[0][args[0].index("-r") + 1]).write_text(raw)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("circuit_weaver.spice_runner._find_ngspice", return_value="ngspice"),
            patch("circuit_weaver.spice_runner.subprocess.run", side_effect=fake_run),
        ):
            result = run_simulation(netlist, sim_type="tran")
        assert result.status == "ok"

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
        netlist.write_text("V1 in 0 1\nR1 in out 1k\n.tran 1u 2u\n.end")

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
        netlist.write_text("V1 in 0 1\nR1 in out 1k\n.tran 1u 2u\n.end")
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
        netlist.write_text("V1 in 0 1\nR1 in out 1k\n.tran 1u 2u\n.end")

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
        netlist.write_text("V1 in 0 1\nR1 in out 1k\n.tran 1u 2u\n.end")

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
        (tmp_path / "models").mkdir()
        (tmp_path / "models/device.lib").write_text("* local model library\n")
        netlist.write_text('.include "models/device.lib"\nV1 in 0 1\nR1 in out 1k\n.tran 1u 2u\n.end')
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


class TestParseRawAscii:
    def test_parse_simple_raw(self, tmp_path):
        raw_content = """Title: Test
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 3
Variables:
0 time time
1 v(out) voltage
Values:
0 0.0
 3.3
1 1e-6
 3.31
2 2e-6
 3.29
"""
        raw_file = tmp_path / "test.raw"
        raw_file.write_text(raw_content)
        traces, var_names = _parse_raw_ascii(raw_file)
        assert "time" in var_names
        assert "v(out)" in var_names
        assert traces["v(out)"] == [3.3, 3.31, 3.29]

    def test_empty_raw_file_is_not_evidence(self, tmp_path):
        raw_file = tmp_path / "empty.raw"
        raw_file.write_text("")
        with pytest.raises(ValueError):
            _parse_raw_ascii(raw_file)

    def test_parse_ngspice_complex_ac_raw(self, tmp_path):
        raw_file = tmp_path / "ac.raw"
        raw_file.write_text("""Title: AC
Plotname: AC Analysis
Flags: complex
No. Variables: 3
No. Points: 3
Variables:
0 frequency frequency grid=3
1 v(in) voltage
2 v(out) voltage
Values:
0 1.0e+00, 0.0e+00
 1.0e+00, 0.0e+00
 1.0e+00, 0.0e+00
1 1.0e+01, 0.0e+00
 1.0e+00, 0.0e+00
 7.0e-01, -1.0e-01
2 1.0e+02, 0.0e+00
 1.0e+00, 0.0e+00
 1.0e-01, -1.0e-02
""")
        traces, _ = _parse_raw_ascii(raw_file)
        metrics = _extract_metrics(traces, "ac")
        assert metrics["low_frequency_gain"] == 1.0
        assert metrics["bandwidth_hz"] == 10.0

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
        assert traces["v(out)"] == [3 + 4j, 2j]
        assert [abs(value) for value in traces["v(out)"]] == [5.0, 2.0]


class TestExtractMetrics:
    def test_overflowed_ac_gain_is_omitted(self):
        traces = {
            "frequency": [1.0, 10.0],
            "v(in)": [complex(1e-308, 0)] * 2,
            "v(out)": [complex(1e308, 0)] * 2,
        }
        assert _extract_metrics(traces, "ac") == {}

    def test_one_megohm_one_picofarad_cutoff_negative_control(self):
        resistance = 1e6
        capacitance = 1e-12
        cutoff_hz = 1 / (2 * math.pi * resistance * capacitance)
        frequencies = [1_000.0, 150_000.0, 160_000.0]
        traces = {
            "frequency": frequencies,
            "v(in)": [1 + 0j] * len(frequencies),
            "v(out)": [complex(1 / math.sqrt(1 + (frequency / cutoff_hz) ** 2), 0) for frequency in frequencies],
        }
        metrics = _extract_metrics(traces, "ac")
        measured = metrics["bandwidth_hz"]
        assert 150_000 < measured < 160_000
        assert measured == pytest.approx(159_154.943, abs=250)

    @pytest.mark.skip_category("optional-tool")
    def test_ngspice_1m_1pf_cutoff_negative_control(self, tmp_path):
        if _find_ngspice() is None:
            pytest.skip("ngspice is required for the analog cutoff integration check")
        netlist = tmp_path / "one_meg_one_picofarad.cir"
        netlist.write_text(
            """1 Mohm and 1 pF RC low-pass cutoff control
V1 in 0 AC 1
R1 in out 1Meg
C1 out 0 1p
.ac dec 25 1k 1Meg
.control
set filetype=ascii
run
write results.raw
.endc
.end
"""
        )
        result = run_simulation(netlist, sim_type="ac")
        expected_hz = 159_154.943
        assert result.status == "ok", result.skip_reason
        assert result.evidence_verified is True
        assert result.metrics["bandwidth_hz"] == pytest.approx(expected_hz, rel=0.005)

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

    def test_tran_explicit_output_trace_is_selected_exactly(self):
        traces = {
            "time": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
            "V(vout)": [0, 0, 3.28, 3.29, 3.30, 3.30, 3.30, 3.30, 3.30, 3.30],
            "V(vout_aux)": [0, 0, 3.0, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7],
        }
        selected = _extract_metrics(traces, "tran", {"output_trace": "V(vout_aux)"})
        missing = _extract_metrics(traces, "tran", {"output_trace": "V(vout_auxiliary)"})
        assert selected["V(vout_aux)_ripple_mv"] == pytest.approx(700.0)
        assert "V(vout)_ripple_mv" not in selected
        assert not any(name.endswith("_ripple_mv") for name in missing)

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
