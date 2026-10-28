"""Opt-in routing recovery; private candidates never bypass native publication."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

_NATIVE_DRC_REJECTION_MESSAGE = "Imported routing failed KiCad DRC; board was not published"


def _eligible_native_open_rejection(initial: dict[str, Any], session_path: Path) -> bool:
    """Allow recovery only for a validated SES rejected by opens-only native DRC."""
    if initial.get("status") != "error":
        return False
    if initial.get("output_kind") != "specctra_session":
        return False
    try:
        if Path(initial.get("output_path", "")).resolve() != session_path.resolve():
            return False
        if Path(initial["artifact"]["path"]).resolve() != session_path.resolve():
            return False
    except (KeyError, TypeError, ValueError, OSError):
        return False
    artifact = initial.get("artifact")
    stats = initial.get("stats")
    if not isinstance(artifact, dict) or artifact.get("valid") is not True or artifact.get("kind") != "ses":
        return False
    if not isinstance(stats, dict) or type(stats.get("incomplete")) is not int or stats["incomplete"] != 0:
        return False

    imported = initial.get("board_import")
    if not isinstance(imported, dict) or imported.get("status") != "error":
        return False
    if imported.get("message") != _NATIVE_DRC_REJECTION_MESSAGE:
        return False
    drc = imported.get("drc")
    if not isinstance(drc, dict) or drc.get("status") != "ok" or drc.get("passed") is not False:
        return False
    blockers = drc.get("blocker_count")
    findings = drc.get("findings")
    if type(blockers) is not int or blockers <= 0 or not isinstance(findings, list) or len(findings) != blockers:
        return False
    return all(
        isinstance(finding, dict)
        and finding.get("rule_id") == "CW-DRC-002"
        and finding.get("severity") == "blocker"
        and finding.get("suppressed") is False
        and finding.get("confirmed_blocker") is True
        for finding in findings
    )


def _serialize_native_session(
    board_path: str | Path,
    session_path: str | Path,
    *,
    work_dir: str | Path,
    freerouting_path: str | Path | None = None,
    kicad_cli_path: str | Path | None = None,
    kicad_python_path: str | Path | None = None,
    timeout_seconds: float = 180,
) -> dict[str, Any]:
    """Serialize a fully connected native board to SES with autorouting disabled.

    Freerouting is used only as the Specctra session writer. Completeness and
    acceptance remain the responsibility of the strict SES validator and the
    ordinary native import/fresh-DRC gate.
    """
    from .autoroute import (
        _find_freerouting_command,
        _freerouting_v25_or_newer,
        _probe_freerouting_capabilities,
        _publish_staged_file,
        _router_log_bundle,
        _validate_specctra_artifact,
    )
    from .kicad_bridge import run_kicad

    source = Path(board_path).resolve()
    destination = Path(session_path).resolve()
    root = Path(work_dir).resolve()
    dsn = root / "native-board.dsn"
    staged_session = root / "serialized.ses"
    stdout_path, stderr_path = root / "freerouting-stdout.log", root / "freerouting-stderr.log"
    command_path = root / "command.json"

    def digest(path: Path) -> str | None:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    def snapshots() -> dict[str, str | None]:
        return {str(path): digest(path) for path in (
            source, source.with_suffix(".kicad_pro"), source.with_suffix(".kicad_dru"),
        )}

    def result(status: str, message: str, **fields: Any) -> dict[str, Any]:
        payload = {"status": status, "message": message, "serialization_only": True, **fields}
        if root.is_dir():
            report = root / "serialization.json"
            temporary_report = report.with_name(report.name + ".tmp")
            try:
                temporary_report.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
                temporary_report.replace(report)
            except OSError as exc:
                payload["diagnostics_write_error"] = f"{type(exc).__name__}: {exc}"
        return payload

    if (
        not source.is_file()
        or source.suffix.lower() != ".kicad_pcb"
        or destination.suffix.lower() != ".ses"
        or destination.exists()
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 180
    ):
        return result("error", "Serialization requires a native PCB, SES destination, and timeout <= 180 seconds")
    root.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + float(timeout_seconds)

    def remaining(cap: float | None = None) -> float:
        time_left = deadline - time.monotonic()
        if time_left <= 0:
            return 0.0
        return min(time_left, cap) if cap is not None else time_left

    try:
        initial_hashes = snapshots()
    except OSError as exc:
        return result("error", f"Could not read native board or project sidecars: {exc}")
    if initial_hashes[str(source)] is None:
        return result("error", "Could not hash the fully connected native board before serialization")

    export_timeout = remaining(120)
    if export_timeout <= 0:
        return result("error", "Serialization deadline expired before native DSN export",
                      source_hashes=initial_hashes)
    export = run_kicad(
        "export_dsn", board=str(source), output=str(dsn),
        python_path=kicad_python_path, timeout=export_timeout,
    )
    if not isinstance(export, dict) or export.get("status") != "ok" or not dsn.is_file():
        return result("error", "Native DSN export failed", export=export, dsn_path=str(dsn))
    dsn_validation = _validate_specctra_artifact(dsn, "dsn")
    if not dsn_validation.get("valid"):
        return result("error", "Native DSN export failed strict validation", export=export,
                      dsn_validation=dsn_validation, dsn_path=str(dsn), dsn_sha256=digest(dsn))

    command = _find_freerouting_command(freerouting_path)
    if command is None:
        return result("error", "Freerouting was not found for native session serialization",
                      export=export, dsn_validation=dsn_validation, dsn_path=str(dsn), dsn_sha256=digest(dsn))
    probe_timeout = remaining(10)
    if probe_timeout <= 0:
        return result("error", "Serialization deadline expired before router capability probe",
                      export=export, dsn_validation=dsn_validation,
                      dsn_path=str(dsn), dsn_sha256=digest(dsn))
    capabilities = _probe_freerouting_capabilities(command, timeout_seconds=probe_timeout)
    if not capabilities.get("probe_ok") or not _freerouting_v25_or_newer(capabilities):
        return result("error", "Serialization requires verified Freerouting 2.5 or newer",
                      export=export, dsn_validation=dsn_validation, capabilities=capabilities,
                      dsn_path=str(dsn), dsn_sha256=digest(dsn))

    user_data = root / "user-data"
    user_data.mkdir()
    invocation = [
        *command, "-de", str(dsn), "-do", str(staged_session), "-l", "en", "-da",
        "--gui.enabled=false", "--router.autorouter.enabled=false",
        "--router.fanout.enabled=false", "--router.optimizer.enabled=false",
        "--user_data_path=" + str(user_data),
    ]
    command_path.write_text(json.dumps(invocation, indent=2) + "\n", encoding="utf-8")
    exit_code = None
    error = None
    route_timeout = remaining()
    if route_timeout <= 0:
        return result("error", "Serialization deadline expired before Freerouting launch",
                      export=export, dsn_validation=dsn_validation, capabilities=capabilities,
                      dsn_path=str(dsn), dsn_sha256=digest(dsn))
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.run(
                invocation, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                timeout=route_timeout, cwd=root,
            )
        exit_code = process.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = f"{type(exc).__name__}: {exc}"
    logs = _router_log_bundle(stdout_path, stderr_path)
    if error or exit_code != 0 or not staged_session.is_file():
        return result("error", "Stock Freerouting serialization did not produce a successful SES",
                      export=export, dsn_validation=dsn_validation, capabilities=capabilities,
                      command=invocation, command_path=str(command_path), exit_code=exit_code,
                      process_error=error, dsn_path=str(dsn), dsn_sha256=digest(dsn),
                      session_path=str(staged_session), session_sha256=digest(staged_session), logs=logs)

    ses_validation = _validate_specctra_artifact(staged_session, "ses", source_dsn=dsn)
    if not ses_validation.get("valid"):
        return result("error", "Serialized SES failed strict source-DSN validation",
                      export=export, dsn_validation=dsn_validation, ses_validation=ses_validation,
                      capabilities=capabilities, command=invocation, command_path=str(command_path),
                      exit_code=exit_code, dsn_path=str(dsn), dsn_sha256=digest(dsn),
                      session_path=str(staged_session), session_sha256=digest(staged_session), logs=logs)
    try:
        after_hashes = snapshots()
    except OSError as exc:
        return result("error", f"Could not recheck native board or project sidecars: {exc}",
                      export=export, dsn_validation=dsn_validation, ses_validation=ses_validation,
                      capabilities=capabilities, command=invocation, command_path=str(command_path),
                      exit_code=exit_code, source_hashes_before=initial_hashes, snapshot_error=str(exc),
                      dsn_path=str(dsn), dsn_sha256=digest(dsn), session_path=str(staged_session),
                      session_sha256=digest(staged_session), logs=logs)
    if after_hashes != initial_hashes:
        return result("error", "Native board or project sidecars changed during session serialization",
                      export=export, dsn_validation=dsn_validation, ses_validation=ses_validation,
                      capabilities=capabilities, command=invocation, command_path=str(command_path),
                      exit_code=exit_code, source_hashes_before=initial_hashes,
                      source_hashes_after=after_hashes, dsn_path=str(dsn), dsn_sha256=digest(dsn),
                      session_path=str(staged_session), session_sha256=digest(staged_session), logs=logs)
    publish_error = _publish_staged_file(staged_session, destination, overwrite=False)
    if publish_error:
        return result("error", publish_error, export=export, dsn_validation=dsn_validation,
                      ses_validation=ses_validation, capabilities=capabilities,
                      command=invocation, command_path=str(command_path), exit_code=exit_code,
                      dsn_path=str(dsn), dsn_sha256=digest(dsn), session_path=str(staged_session),
                      session_sha256=digest(staged_session), logs=logs)
    return result(
        "ok", "Stock Freerouting serialized a strictly validated complete SES with routing disabled",
        output_path=str(destination), session_path=str(destination), session_sha256=digest(destination),
        dsn_path=str(dsn), dsn_sha256=digest(dsn), source_hashes=initial_hashes,
        export=export, dsn_validation=dsn_validation, ses_validation=ses_validation,
        capabilities=capabilities, command=invocation, command_path=str(command_path),
        exit_code=exit_code, logs=logs,
        routing_stats={
            "available": False,
            "reason": "No routing or router statistics are produced in serialization-only mode",
        },
    )


def autoroute_with_recovery(
    pcb_path: str | Path,
    output_path: str | Path | None,
    *,
    routed_board_path: str | Path | None,
    recovery_timeout_seconds: float = 600,
    max_repair_links: int = 256,
    **router_options: Any,
) -> dict[str, Any]:
    from .autoroute import _prepare_destination, _publish_staged_file, autoroute_pcb
    from .kicad_bridge import run_kicad
    from .kicad_project_context import inspect_project_footprint_context
    from .pcb_layout import _check_snapshots, _prepare_settings, _restore_settings, import_routing_session
    from .routing_recovery import recover_routing

    work: Path | None = None
    try:
        source = Path(pcb_path).resolve()
        if source.suffix.lower() != ".kicad_pcb" or not source.is_file() or routed_board_path is None:
            raise ValueError("Routing recovery requires a native PCB and a separate --routed-board output")
        if (
            isinstance(recovery_timeout_seconds, bool)
            or not isinstance(recovery_timeout_seconds, (int, float))
            or not math.isfinite(recovery_timeout_seconds)
            or not 0 < recovery_timeout_seconds <= 1800
        ):
            raise ValueError("recovery_timeout_seconds must be finite and between 0 and 1800 seconds")
        if (
            isinstance(max_repair_links, bool)
            or not isinstance(max_repair_links, int)
            or not 1 <= max_repair_links <= 512
        ):
            raise ValueError("max_repair_links must be an integer from 1 through 512")
        destination = Path(routed_board_path).resolve()
        session = Path(output_path).resolve() if output_path else source.with_suffix(".ses")
        if destination == source or destination.suffix.lower() != ".kicad_pcb" or session.suffix.lower() != ".ses":
            raise ValueError("Choose separate .kicad_pcb and .ses output paths")
        overwrite = router_options.get("overwrite", False)
        for path in (destination, session):
            error = _prepare_destination(path, overwrite=overwrite)
            if error:
                raise ValueError(error)
        destination.parent.mkdir(parents=True, exist_ok=True)
        session.parent.mkdir(parents=True, exist_ok=True)
        snapshots = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            for path in (source, source.with_suffix(".kicad_pro"), source.with_suffix(".kicad_dru"))
        }
        library = inspect_project_footprint_context(source.parent)
        work = Path(tempfile.mkdtemp(prefix=".cw-route-recovery-", dir=destination.parent))
        initial_session = work / "initial.ses"
        python = router_options.get("kicad_python_path")
        initial = autoroute_pcb(
            source,
            str(initial_session),
            routed_board_path=work / "initial-complete.kicad_pcb",
            _allow_partial_nets=True,
            **router_options,
        )
        _check_snapshots(snapshots)
        native_open_rejection = _eligible_native_open_rejection(initial, initial_session)
        if (
            initial.get("status") not in {"ok", "partial", "review_required"} and not native_open_rejection
        ) or not initial_session.is_file():
            return {**initial, "recovery_evidence_dir": str(work), "routing_complete": False}
        recovery_trigger = None
        private_session_sha256 = None
        if native_open_rejection:
            private_session_sha256 = hashlib.sha256(initial_session.read_bytes()).hexdigest()
            native_drc = initial["board_import"]["drc"]
            recovery_trigger = {
                "kind": "native_open_drc_after_router_reported_zero_incomplete",
                "message": "Native DRC found open connections despite a zero-incomplete router statistic; "
                "the validated private SES is being re-imported into quarantine for fresh-DRC recovery.",
                "session_path": str(initial_session),
                "session_sha256": private_session_sha256,
                "router_incomplete": initial["stats"]["incomplete"],
                "native_drc": {
                    "board_sha256": native_drc.get("board_sha256"),
                    "blocker_count": native_drc["blocker_count"],
                    "findings": [
                        {
                            key: finding.get(key)
                            for key in ("rule_id", "severity", "ref", "net", "message")
                            if key in finding
                        }
                        for finding in native_drc["findings"]
                    ],
                },
            }
        completed_session = initial_session
        repair: dict[str, Any] = {"used": False}
        if initial.get("status") != "ok" or not initial.get("routing_complete"):
            # Import into quarantine, retaining all original pads/nets. The
            # public import gate is used only for the final zero-open session.
            deadline = time.monotonic() + float(recovery_timeout_seconds)

            def remaining(cap: float) -> float:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("Routing recovery exhausted its time budget")
                return min(cap, left)

            partial = work / "partial.kicad_pcb"
            staged_settings = _prepare_settings(source, partial)
            imported = run_kicad(
                "import_ses",
                board=str(source),
                session=str(initial_session),
                output=str(partial),
                python_path=python,
                timeout=remaining(180),
            )
            if imported.get("status") != "ok" or not partial.is_file():
                raise ValueError("Could not import the partial session into private native quarantine")
            if (
                native_open_rejection
                and hashlib.sha256(initial_session.read_bytes()).hexdigest() != private_session_sha256
            ):
                raise ValueError("Validated private SES changed during native quarantine import")
            _restore_settings(source, partial, staged_settings)
            repair = recover_routing(
                partial,
                work / "recovered.kicad_pcb",
                kicad_python_path=python,
                timeout_seconds=remaining(float(recovery_timeout_seconds)),
                max_links=max_repair_links,
                evidence_dir=work / "native-recovery",
            )
            if repair.get("status") != "ok":
                return {
                    "status": repair.get("status", "error"),
                    "message": repair.get("message", "Native recovery incomplete"),
                    "routing_complete": False,
                    "recovery": repair,
                    "recovery_evidence_dir": str(work),
                    "fabrication_ready": False,
                    **({"recovery_trigger": recovery_trigger} if recovery_trigger else {}),
                }
            completed_session = work / "complete.ses"
            # The stock router serializes all copper with autorouter, fanout,
            # and optimizer explicitly disabled; complete-SES validation and
            # the ordinary native import/DRC gate remain mandatory.
            serialization = _serialize_native_session(
                repair["output_path"], completed_session,
                work_dir=work / "session-serialization",
                freerouting_path=router_options.get("freerouting_path"),
                kicad_cli_path=router_options.get("kicad_cli_path"),
                kicad_python_path=python,
                timeout_seconds=180,
            )
            if serialization.get("status") != "ok" or not completed_session.is_file():
                return {
                    "status": "error",
                    "message": serialization.get("message", "Stock Freerouting SES serialization failed"),
                    "routing_complete": False,
                    "recovery": repair,
                    "serialization": serialization,
                    "recovery_evidence_dir": str(work),
                    "fabrication_ready": False,
                    **({"recovery_trigger": recovery_trigger} if recovery_trigger else {}),
                }
        _check_snapshots(snapshots)
        if inspect_project_footprint_context(source.parent) != library:
            raise ValueError("Project footprint context changed during recovery")
        # Preserve the ordinary output behavior: a complete validated SES may
        # remain on native rejection; no PCB is published until fresh native DRC.
        error = _publish_staged_file(completed_session, session, overwrite=overwrite)
        if error:
            raise ValueError(error)
        accepted = import_routing_session(
            source,
            session,
            destination,
            overwrite=overwrite,
            kicad_python_path=python,
            expected_hashes=snapshots,
        )
        if accepted.get("status") != "ok":
            return {**accepted, "routing_complete": False, "recovery": repair,
                    "serialization": serialization if repair.get("used") is not False else None,
                    "recovery_evidence_dir": str(work),
                    **({"recovery_trigger": recovery_trigger} if recovery_trigger else {})}
        return {
            **accepted,
            "session_path": str(session),
            "recovery": repair,
            **({"serialization": serialization} if repair.get("used") is not False else {}),
            "recovery_evidence_dir": str(work),
            **({"recovery_trigger": recovery_trigger} if recovery_trigger else {}),
            "artifact": {
                "valid": True,
                "kind": "kicad_pcb",
                "path": str(destination),
                "sha256": accepted["drc"]["board_sha256"],
            },
            "verification": {"kicad_drc": "passed", "requires_kicad_drc": False},
            "fabrication_ready": False,
        }
    except (OSError, ValueError, TimeoutError, TypeError) as exc:
        return {
            "status": "error",
            "message": str(exc),
            "routing_complete": False,
            "recovery_evidence_dir": str(work) if work is not None else None,
            "fabrication_ready": False,
        }
