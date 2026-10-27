"""Pure tests for the stock-router serialization-only transport path."""

from __future__ import annotations

import subprocess
from pathlib import Path

from circuit_weaver import autoroute, autoroute_recovery


def _configure_stock_serialization(monkeypatch, tmp_path, *, ses_valid=True):
    source = tmp_path / "recovered.kicad_pcb"
    source.write_bytes(b"fully connected native board")
    validation_calls = []

    def export(operation, *, output, **_kwargs):
        assert operation == "export_dsn"
        Path(output).write_bytes(b"native DSN fixture")
        return {"status": "ok", "version": "10.0.4"}

    monkeypatch.setattr("circuit_weaver.kicad_bridge.run_kicad", export)
    monkeypatch.setattr(autoroute, "_find_freerouting_command", lambda _path: ["java", "-jar", "router.jar"])
    monkeypatch.setattr(
        autoroute,
        "_probe_freerouting_capabilities",
        lambda _command, timeout_seconds: {"probe_ok": True, "version": "2.5.0", "seed": False,
                                           "reason": ""},
    )

    def validate(path, kind, **kwargs):
        validation_calls.append((kind, kwargs))
        return {"valid": kind == "dsn" or ses_valid, "kind": kind,
                "reason": "missing net" if kind == "ses" and not ses_valid else ""}

    monkeypatch.setattr(autoroute, "_validate_specctra_artifact", validate)
    observed = {}

    def run(command, *, stdin, stdout, stderr, timeout, cwd):
        observed.update(command=command, timeout=timeout, cwd=Path(cwd))
        session = Path(command[command.index("-do") + 1])
        session.write_bytes(b"validated SES fixture")
        stdout.write(b"serialization only\n")
        stderr.write(b"")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(autoroute_recovery.subprocess, "run", run)
    return source, validation_calls, observed


def test_serialization_uses_stock_freerouting_with_routing_stages_disabled(monkeypatch, tmp_path):
    source, validation_calls, observed = _configure_stock_serialization(monkeypatch, tmp_path)
    work = tmp_path / "serialization"
    session = tmp_path / "complete.ses"

    result = autoroute_recovery._serialize_native_session(
        source, session, work_dir=work, freerouting_path=tmp_path / "router.jar",
        kicad_python_path=tmp_path / "pcbnew-python.exe", timeout_seconds=30,
    )

    command = observed["command"]
    assert result["status"] == "ok"
    assert session.is_file() and result["session_path"] == str(session.resolve())
    assert {flag for flag in (
        "--router.autorouter.enabled=false", "--router.fanout.enabled=false",
        "--router.optimizer.enabled=false",
    )} <= set(command)
    assert "-mp" not in command
    assert any(arg.startswith("--user_data_path=") for arg in command)
    assert observed["cwd"] == work.resolve()
    assert result["routing_stats"]["available"] is False
    assert "incomplete" not in result["routing_stats"]
    assert validation_calls == [("dsn", {}), ("ses", {"source_dsn": work / "native-board.dsn"})]
    assert result["logs"]["stdout"]["sha256"]
    assert (work / "freerouting-stdout.log").read_bytes() == b"serialization only\n"


def test_serialization_rejects_unverified_router_capability(monkeypatch, tmp_path):
    source, _, _ = _configure_stock_serialization(monkeypatch, tmp_path)
    monkeypatch.setattr(
        autoroute,
        "_probe_freerouting_capabilities",
        lambda _command, timeout_seconds: {"probe_ok": True, "version": "2.4.1", "seed": False},
    )
    called = False

    def forbidden_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("router should not be launched without serialization capability")

    monkeypatch.setattr(autoroute_recovery.subprocess, "run", forbidden_run)
    result = autoroute_recovery._serialize_native_session(
        source, tmp_path / "complete.ses", work_dir=tmp_path / "serialization", timeout_seconds=30,
    )

    assert result["status"] == "error"
    assert result["capabilities"]["version"] == "2.4.1"
    assert called is False


def test_serialization_preserves_strict_ses_failure_diagnostics(monkeypatch, tmp_path):
    source, validation_calls, _ = _configure_stock_serialization(monkeypatch, tmp_path, ses_valid=False)
    session = tmp_path / "complete.ses"
    result = autoroute_recovery._serialize_native_session(
        source, session, work_dir=tmp_path / "serialization", timeout_seconds=30,
    )

    assert result["status"] == "error"
    assert result["ses_validation"]["valid"] is False
    assert result["logs"]["stdout"]["sha256"]
    assert not session.exists()
    assert len(validation_calls) == 2


def test_serialization_rejects_source_changed_by_another_process(monkeypatch, tmp_path):
    source, _, _ = _configure_stock_serialization(monkeypatch, tmp_path)
    router_run = autoroute_recovery.subprocess.run

    def concurrent_edit(*args, **kwargs):
        completed = router_run(*args, **kwargs)
        source.write_bytes(b"concurrent source revision")
        return completed

    monkeypatch.setattr(autoroute_recovery.subprocess, "run", concurrent_edit)
    session = tmp_path / "complete.ses"
    result = autoroute_recovery._serialize_native_session(
        source, session, work_dir=tmp_path / "serialization", timeout_seconds=30,
    )
    assert result["status"] == "error" and "changed" in result["message"]
    assert result["source_hashes_before"] != result["source_hashes_after"]
    assert not session.exists()


def test_serialization_deadline_is_shared_across_native_export_probe_and_process(monkeypatch, tmp_path):
    source, _, observed = _configure_stock_serialization(monkeypatch, tmp_path)
    import circuit_weaver.kicad_bridge as bridge

    elapsed = [100.0]
    monkeypatch.setattr(autoroute_recovery.time, "monotonic", lambda: elapsed[0])
    export = bridge.run_kicad
    probe = autoroute._probe_freerouting_capabilities

    def timed_export(*args, **kwargs):
        elapsed[0] += 10
        return export(*args, **kwargs)

    def timed_probe(*args, **kwargs):
        elapsed[0] += 5
        return probe(*args, **kwargs)

    monkeypatch.setattr(bridge, "run_kicad", timed_export)
    monkeypatch.setattr(autoroute, "_probe_freerouting_capabilities", timed_probe)
    result = autoroute_recovery._serialize_native_session(
        source, tmp_path / "complete.ses", work_dir=tmp_path / "serialization", timeout_seconds=30,
    )
    assert result["status"] == "ok"
    assert observed["timeout"] == 15
