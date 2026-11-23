"""Run pcbnew in KiCad's own Python, independently of the application's ABI."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .kicad_project_context import inspect_project_footprint_context, stage_project_footprint_context


def _validated_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise ValueError("deadline must be a finite absolute monotonic timestamp")
    try:
        value = float(deadline)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("deadline must be a finite absolute monotonic timestamp") from exc
    if not math.isfinite(value):
        raise ValueError("deadline must be a finite absolute monotonic timestamp")
    return value


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


def find_kicad_python(
    explicit: str | Path | None = None, *, deadline: float | None = None,
) -> str | None:
    deadline = _validated_deadline(deadline)
    candidates = [explicit, os.getenv("CIRCUIT_WEAVER_KICAD_PYTHON"), os.getenv("KICAD_PYTHON")]
    if not any(candidates):
        candidates.extend([
            sys.executable,
            *(f"C:/Program Files/KiCad/{v}/bin/python.exe" for v in ("10.0", "9.0", "8.0")),
            "/usr/bin/python3",
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
        ])
    for candidate in dict.fromkeys(str(p) for p in candidates if p):
        executable = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if not executable:
            continue
        probe_timeout = 10 if deadline is None else min(10.0, _remaining(deadline) or 0.0)
        if probe_timeout <= 0:
            return None
        try:
            probe = subprocess.run(
                [executable, "-c", "import pcbnew; print('CW_PCBNEW_READY')"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=probe_timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and "CW_PCBNEW_READY" in probe.stdout:
            return executable
    return None


def run_kicad(
    operation: str, *, python_path: str | Path | None = None, timeout: float = 120,
    deadline: float | None = None, **request: Any,
) -> dict[str, Any]:
    try:
        deadline = _validated_deadline(deadline)
    except ValueError as exc:
        return {"status": "error", "message": f"KiCad {operation} failed: {exc}"}
    if deadline is not None and (_remaining(deadline) or 0.0) <= 0:
        return {"status": "error", "message": f"KiCad {operation} deadline expired before discovery"}
    executable = (
        find_kicad_python(python_path)
        if deadline is None
        else find_kicad_python(python_path, deadline=deadline)
    )
    if executable is None:
        return {"status": "error", "message": "KiCad Python (pcbnew) is unavailable; set CIRCUIT_WEAVER_KICAD_PYTHON"}
    try:
        # Native KiCad can save project/UI settings even during an export.
        # Load an isolated copy so a read/route operation cannot touch originals.
        with tempfile.TemporaryDirectory(prefix="cw-kicad-") as folder:
            source = Path(request["board"]).resolve()
            board_copy = Path(folder) / source.name
            shutil.copy2(source, board_copy)
            for suffix in (".kicad_pro", ".kicad_dru"):
                settings = source.with_suffix(suffix)
                if settings.is_file():
                    shutil.copy2(settings, board_copy.with_suffix(suffix))
            # Preserve only explicitly declared, bounded project-local libraries.
            context_before = inspect_project_footprint_context(source.parent)
            stage_project_footprint_context(source.parent, Path(folder))
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0:
                return {"status": "error", "message": f"KiCad {operation} deadline expired before native launch"}
            process_timeout = timeout if remaining is None else min(timeout, remaining)
            process = subprocess.run(
                [executable, str(Path(__file__).with_name("_kicad_worker.py"))],
                input=json.dumps({"operation": operation, **request, "board": str(board_copy)}),
                capture_output=True, text=True, timeout=process_timeout,
            )
            if inspect_project_footprint_context(source.parent) != context_before:
                return {"status": "error", "message": "Project footprint context changed during KiCad operation"}
        for line in reversed(process.stdout.splitlines()):
            if line.startswith("CW_KICAD_RESULT="):
                result = json.loads(line.partition("=")[2])
                if not isinstance(result, dict) or result.get("status") not in {"ok", "error"}:
                    return {"status": "error", "message": "KiCad worker returned an invalid result"}
                if process.returncode != 0:
                    return {"status": "error", "message": result.get("message", "KiCad worker failed")}
                return result
        return {"status": "error", "message": f"KiCad worker failed: {(process.stderr or process.stdout)[-1000:]}"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "message": f"KiCad {operation} failed: {exc}"}
