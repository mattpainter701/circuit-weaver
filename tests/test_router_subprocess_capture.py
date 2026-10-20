from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from test_autoroute import VALID_SES, _routing_output, _write_dsn

import circuit_weaver.autoroute as autoroute_module
from circuit_weaver.autoroute import (
    _ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES,
    _ROUTER_LOG_HEAD_BYTES,
    _ROUTER_LOG_PARSE_TAIL_BYTES,
    _parse_routing_stats,
    _read_router_diagnostic,
    _route_candidates,
    _route_specctra,
    _router_log_bundle,
    _router_log_window,
    _run_router_to_logs,
)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "stdout.log", tmp_path / "stderr.log"


def _parse_window(path: Path) -> tuple[str, bool]:
    head, tail = _router_log_window(path, head_bytes=_ROUTER_LOG_HEAD_BYTES, tail_bytes=_ROUTER_LOG_PARSE_TAIL_BYTES)
    meta = _router_log_bundle(path, path)["stdout"]
    return (tail if meta["parse_window_truncated"] else head + tail), meta["parse_window_truncated"]


def test_log_windows_keep_bounded_head_tail_and_report_truncation(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    stdout.write_bytes(
        b"Freerouting v2.5.0\n" + b"x" * (_ROUTER_LOG_PARSE_TAIL_BYTES + _ROUTER_LOG_HEAD_BYTES + 17)
        + b"\nRouting finished: 0 incomplete"
    )
    stderr.write_bytes(b"warning\n" + b"e" * (_ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES + 7))

    head, tail = _router_log_window(stdout, head_bytes=_ROUTER_LOG_HEAD_BYTES, tail_bytes=_ROUTER_LOG_PARSE_TAIL_BYTES)
    bundle = _router_log_bundle(stdout, stderr)
    diagnostic = _read_router_diagnostic(stderr)

    assert len(head.encode("utf-8")) <= _ROUTER_LOG_HEAD_BYTES
    assert len(tail.encode("utf-8")) <= _ROUTER_LOG_PARSE_TAIL_BYTES
    assert tail.endswith("Routing finished: 0 incomplete")
    assert bundle["stdout"]["parse_window_truncated"] is True
    assert bundle["stdout"]["preserved_full_log"] is True
    assert bundle["stdout"]["bytes"] == stdout.stat().st_size
    assert bundle["stdout"]["sha256"] == hashlib.sha256(stdout.read_bytes()).hexdigest()
    assert bundle["stderr"]["diagnostic_tail_truncated"] is True
    assert len(diagnostic.encode("utf-8")) <= _ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES


def test_log_window_covers_entire_file_below_and_between_limits(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    stderr.write_bytes(b"")
    for size in (50 * 1024, _ROUTER_LOG_HEAD_BYTES + 1024 * 1024):
        payload = b"a" * (size - len(b"Final: 0 incomplete")) + b"Final: 0 incomplete"
        stdout.write_bytes(payload)
        head, tail = _router_log_window(
            stdout, head_bytes=_ROUTER_LOG_HEAD_BYTES, tail_bytes=_ROUTER_LOG_PARSE_TAIL_BYTES
        )
        assert head + tail == payload.decode("utf-8")
        bundle = _router_log_bundle(stdout, stderr)
        assert bundle["stdout"]["parse_window_truncated"] is False


def test_log_windows_handle_giant_line_and_invalid_utf8(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    stdout.write_bytes(b"start\n" + b"z" * (_ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES * 2) + b"\xffEND")
    stderr.write_bytes(b"")

    diagnostic = _read_router_diagnostic(stdout)
    assert "\ufffdEND" in diagnostic
    assert len(diagnostic.encode("utf-8")) <= _ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES + 3


def test_bounded_parser_preserves_normal_structured_statistics(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    text = _routing_output(incomplete=0, vias=7, traces=79)
    stdout.write_text(text, encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    bounded, truncated = _parse_window(stdout)

    assert truncated is False
    assert _parse_routing_stats(bounded) == _parse_routing_stats(text)
    assert _parse_routing_stats(bounded)["incomplete"] == 0


def test_truncated_parser_ignores_early_complete_zero_if_tail_reports_incomplete(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    payload = (
        b"Freerouting v2.4.1\nFinal: 0 incomplete\n"
        + b"progress " * ((_ROUTER_LOG_PARSE_TAIL_BYTES + _ROUTER_LOG_HEAD_BYTES) // len(b"progress ") + 1)
        + b"\nFinal routing summary: 2 incomplete"
    )
    stdout.write_bytes(payload)
    stderr.write_bytes(b"")
    bounded, truncated = _parse_window(stdout)

    assert truncated is True
    assert _parse_routing_stats(bounded)["incomplete"] == 2


def test_truncated_tail_discards_partial_leading_incomplete_count(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    # Place the tail boundary at the second digit of "10 incomplete". A naive
    # fixed tail slice would parse the fragment as a false zero.
    prefix = b"P" * (_ROUTER_LOG_HEAD_BYTES + 1)
    partial = b"10 incomplete\n"
    final = b"Final: 10 incomplete\n"
    filler = b"x" * (_ROUTER_LOG_PARSE_TAIL_BYTES - len(partial) - len(final)) + b"\n"
    payload = prefix + partial + filler + final
    stdout.write_bytes(payload)
    stderr.write_bytes(b"")

    head, tail = _router_log_window(
        stdout, head_bytes=_ROUTER_LOG_HEAD_BYTES, tail_bytes=_ROUTER_LOG_PARSE_TAIL_BYTES
    )
    assert not tail.startswith("0 incomplete")
    bounded, truncated = _parse_window(stdout)
    assert truncated is True
    assert _parse_routing_stats(bounded)["incomplete"] == 10


def test_truncated_log_without_complete_tail_line_cannot_claim_final_zero(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    budget = _ROUTER_LOG_HEAD_BYTES + _ROUTER_LOG_PARSE_TAIL_BYTES
    payload = b"Final: 0 incomplete\n" + b"x" * (budget + 100)
    stdout.write_bytes(payload)
    stderr.write_bytes(b"")
    bounded, truncated = _parse_window(stdout)

    assert truncated is True
    assert _parse_routing_stats(bounded)["incomplete"] is None


def test_log_metadata_read_failure_is_structured_and_unavailable(tmp_path: Path, monkeypatch):
    path = tmp_path / "unreadable.log"
    path.write_bytes(b"anything")
    original_open = Path.open

    def failing_open(self, *args, **kwargs):
        if self == path:
            raise OSError("simulated disk read failure")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    metadata = autoroute_module._router_log_metadata(path, parse_tail_bytes=_ROUTER_LOG_PARSE_TAIL_BYTES)

    assert metadata["available"] is False
    assert "simulated disk read failure" in metadata["error"]


def test_missing_final_completeness_remains_unknown(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    stdout.write_text("Freerouting v2.4.1\nprogress only", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    bounded, _truncated = _parse_window(stdout)
    assert _parse_routing_stats(bounded)["incomplete"] is None


def test_dual_stream_50mib_child_is_spooled_to_disk_without_capture(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    chunk = 1024 * 1024
    code = (
        "import sys; "
        "out=b'O'*1048576; err=b'E'*1048576; "
        "[sys.stdout.buffer.write(out) for _ in range(25)]; "
        "[sys.stderr.buffer.write(err) for _ in range(25)]; "
        "sys.stdout.buffer.write(b'\\nFinal: 0 incomplete\\n'); sys.stdout.flush(); sys.stderr.flush()"
    )
    result = _run_router_to_logs(
        [sys.executable, "-c", code], timeout_seconds=30, cwd=tmp_path, stdout_path=stdout, stderr_path=stderr
    )

    assert result.returncode == 0
    assert result.stdout is None and result.stderr is None
    assert stdout.stat().st_size == 25 * chunk + len(b"\nFinal: 0 incomplete\n")
    assert stderr.stat().st_size == 25 * chunk
    bundle = _router_log_bundle(stdout, stderr)
    assert bundle["stdout"]["bytes"] == stdout.stat().st_size
    assert bundle["stderr"]["bytes"] == stderr.stat().st_size
    bounded, truncated = _parse_window(stdout)
    assert truncated is True
    assert _parse_routing_stats(bounded)["incomplete"] == 0


def test_timed_out_child_is_reaped_and_full_partial_logs_remain(tmp_path: Path):
    stdout, stderr = _paths(tmp_path)
    code = (
        "import sys,time; "
        "sys.stdout.buffer.write(b'partial before sleep\\n'); sys.stdout.flush(); "
        "sys.stderr.buffer.write(b'partial stderr\\n'); sys.stderr.flush(); time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _run_router_to_logs(
            [sys.executable, "-c", code], timeout_seconds=0.5, cwd=tmp_path, stdout_path=stdout, stderr_path=stderr
        )

    assert stdout.exists() and stderr.exists()
    assert _read_router_diagnostic(stdout).startswith("partial before sleep")
    assert _read_router_diagnostic(stderr).startswith("partial stderr")
    assert _router_log_bundle(stdout, stderr)["stdout"]["preserved_full_log"] is True


def _portfolio(tmp_path: Path, monkeypatch, *, mode: str):
    dsn = _write_dsn(tmp_path)
    destination = tmp_path / "portfolio.ses"

    def fake_router(command, *, timeout_seconds, cwd, stdout_path, stderr_path):
        stdout_path.write_text(_routing_output(incomplete=0, vias=2, traces=30), encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        ses = Path(command[command.index("-do") + 1])
        ses.write_text(VALID_SES if mode != "invalid_ses" else "not an SES", encoding="utf-8")
        if mode == "timeout":
            raise subprocess.TimeoutExpired(command, timeout_seconds)
        return subprocess.CompletedProcess(
            command, 17 if mode in {"nonzero_partial", "nonzero_diagnostic_error"} else 0, None, None
        )

    monkeypatch.setattr(autoroute_module, "_run_router_to_logs", fake_router)
    result = _route_candidates(
        dsn,
        destination,
        ["freerouting"],
        attempts=2,
        passes=100,
        timeout_seconds=30,
        overwrite=False,
        headless=True,
        optimization_threads=0,
        optimizer_strategy=None,
        optimizer_hybrid_ratio=None,
        optimizer_item_selection=None,
        optimizer_improvement_threshold=None,
        seed=7,
        capability_probe={"seed": False},
        clearance_baseline=None,
    )
    return result, destination


def _result_rows(result: dict) -> list[dict]:
    return result.get("attempts") or result.get("search", {}).get("attempts") or []


def _assert_logs_persist(rows: list[dict], *, output_parent: Path):
    metas = [row["router_logs"] for row in rows]
    for meta in metas:
        for stream in ("stdout", "stderr"):
            path = Path(meta[stream]["path"])
            assert path.is_file()
            assert output_parent in path.parents
            assert not any(part.startswith(".cw-routing-") for part in path.parts)
            assert path.stat().st_size == meta[stream]["bytes"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == meta[stream]["sha256"]


def test_portfolio_success_log_paths_survive_candidate_temp_cleanup(tmp_path: Path, monkeypatch):
    result, destination = _portfolio(tmp_path, monkeypatch, mode="success")

    assert result["status"] == "ok"
    rows = result["search"]["attempts"]
    _assert_logs_persist(rows, output_parent=tmp_path)
    selected = rows[result["search"]["selected_attempt"] - 1]
    assert result["router_logs"]["stdout"]["path"] == selected["router_logs"]["stdout"]["path"]
    assert destination.is_file()
    assert not list(tmp_path.glob(".cw-routing-*"))


def test_portfolio_timeout_log_paths_survive_candidate_temp_cleanup(tmp_path: Path, monkeypatch):
    result, _destination = _portfolio(tmp_path, monkeypatch, mode="timeout")

    assert result["status"] == "error"
    _assert_logs_persist(_result_rows(result), output_parent=tmp_path)
    assert not list(tmp_path.glob(".cw-routing-*"))


def test_invalid_ses_log_paths_survive_candidate_temp_cleanup(tmp_path: Path, monkeypatch):
    result, _destination = _portfolio(tmp_path, monkeypatch, mode="invalid_ses")

    assert result["status"] == "error"
    _assert_logs_persist(_result_rows(result), output_parent=tmp_path)
    assert not list(tmp_path.glob(".cw-routing-*"))


def _run_direct_nonzero(tmp_path: Path, monkeypatch, *, diagnostic_error: bool):
    dsn = _write_dsn(tmp_path)
    destination = tmp_path / "direct.ses"

    def fake_router(command, *, timeout_seconds, cwd, stdout_path, stderr_path):
        stdout_path.write_text(_routing_output(incomplete=0, vias=2, traces=30), encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        staging = Path(command[command.index("-do") + 1])
        staging.write_text(VALID_SES, encoding="utf-8")
        return subprocess.CompletedProcess(command, 17, None, None)

    monkeypatch.setattr(autoroute_module, "_run_router_to_logs", fake_router)
    if diagnostic_error:
        def fail_diagnostic(_path: Path) -> str:
            raise OSError("simulated diagnostic read failure")

        monkeypatch.setattr(autoroute_module, "_read_router_diagnostic", fail_diagnostic)
    result = _route_specctra(
        dsn,
        destination,
        ["freerouting"],
        passes=10,
        timeout_seconds=10,
        overwrite=False,
        headless=True,
        optimization_threads=None,
        optimizer_strategy=None,
        optimizer_hybrid_ratio=None,
        optimizer_item_selection=None,
        optimizer_improvement_threshold=None,
        seed=None,
        capability_probe={"probe_ok": False},
        logs_root=tmp_path / "persistent-logs",
    )
    return result, destination


def test_nonzero_router_exit_removes_partial_staging_ses_and_keeps_logs(tmp_path: Path, monkeypatch):
    result, destination = _run_direct_nonzero(tmp_path, monkeypatch, diagnostic_error=False)

    assert result["status"] == "error"
    assert "Freerouting failed:" in result["message"]
    assert not destination.exists()
    assert not list(tmp_path.glob(".direct.cw-stage-*.ses"))
    _assert_logs_persist([result], output_parent=tmp_path)


def test_nonzero_router_diagnostic_read_error_removes_partial_staging_ses(tmp_path: Path, monkeypatch):
    result, destination = _run_direct_nonzero(tmp_path, monkeypatch, diagnostic_error=True)

    assert result["status"] == "error"
    assert "could not read bounded diagnostics" in result["message"]
    assert not destination.exists()
    assert not list(tmp_path.glob(".direct.cw-stage-*.ses"))
    _assert_logs_persist([result], output_parent=tmp_path)
