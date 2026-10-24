"""Run pcbnew in KiCad's own Python, independently of the application's ABI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def find_kicad_python(explicit: str | Path | None = None) -> str | None:
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
        try:
            probe = subprocess.run(
                [executable, "-c", "import pcbnew; print('CW_PCBNEW_READY')"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and "CW_PCBNEW_READY" in probe.stdout:
            return executable
    return None


def run_kicad(
    operation: str, *, python_path: str | Path | None = None, timeout: float = 120, **request: Any,
) -> dict[str, Any]:
    executable = find_kicad_python(python_path)
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
            process = subprocess.run(
                [executable, str(Path(__file__).with_name("_kicad_worker.py"))],
                input=json.dumps({"operation": operation, **request, "board": str(board_copy)}),
                capture_output=True, text=True, timeout=timeout,
            )
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
