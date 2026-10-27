"""Opt-in routing recovery; private candidates never bypass native publication."""

from __future__ import annotations

import hashlib
import math
import tempfile
import time
from pathlib import Path
from typing import Any


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
        if initial.get("status") not in {"ok", "partial", "review_required"} or not initial_session.is_file():
            return {**initial, "recovery_evidence_dir": str(work), "routing_complete": False}
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
                }
            completed_session = work / "complete.ses"
            # A single pass on the fully connected board serializes all copper;
            # optimizer/fanout stay disabled and ordinary complete-SES validation
            # remains mandatory. No private Java adapter is required.
            converted = autoroute_pcb(
                repair["output_path"],
                str(completed_session),
                max_passes=1,
                attempts=1,
                timeout_seconds=180,
                optimization_threads=0,
                freerouting_path=router_options.get("freerouting_path"),
                kicad_cli_path=router_options.get("kicad_cli_path"),
                kicad_python_path=python,
            )
            if (
                converted.get("status") != "ok"
                or converted.get("stats", {}).get("incomplete") != 0
                or not completed_session.is_file()
            ):
                raise ValueError("Could not produce a fully validated complete SES after native recovery")
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
            return {**accepted, "routing_complete": False, "recovery": repair, "recovery_evidence_dir": str(work)}
        return {
            **accepted,
            "session_path": str(session),
            "recovery": repair,
            "recovery_evidence_dir": str(work),
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
