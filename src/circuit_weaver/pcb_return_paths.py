"""Add explicit local copper return shortcuts to an existing KiCad board."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Any

from .autoroute import _prepare_destination
from .drc_runner import run_drc
from .evidence import EvidenceLedger
from .kicad_bridge import run_kicad
from .pcb_layout import _check_snapshots, _prepare_settings, _publish, _restore_settings


def _validate_connections(connections: Any) -> list[dict[str, Any]]:
    if not isinstance(connections, list) or not connections:
        raise ValueError("connections must be a non-empty list")
    required = {"from_ref", "from_pad", "to_ref", "to_pad"}
    allowed = required | {"max_length_mm"}
    result: list[dict[str, Any]] = []
    for index, item in enumerate(connections):
        if not isinstance(item, dict) or not required <= set(item) or not set(item) <= allowed:
            raise ValueError(
                f"connections[{index}] must contain {', '.join(sorted(required))} "
                "and optional max_length_mm"
            )
        if any(not isinstance(item[key], str) or not item[key].strip() for key in required):
            raise ValueError(f"connections[{index}] endpoint fields must be non-empty strings")
        connection = {key: item[key].strip() for key in ("from_ref", "from_pad", "to_ref", "to_pad")}
        if "max_length_mm" in item:
            value = item["max_length_mm"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"connections[{index}].max_length_mm must be a positive finite number")
            connection["max_length_mm"] = float(value)
        result.append(connection)
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_return_plan(path: str | Path) -> dict[str, Any]:
    """Load the intentionally small, strict JSON interface used by the CLI."""
    value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != {"connections"}:
        raise ValueError("Return plan must be a JSON object containing only 'connections'")
    return {"connections": _validate_connections(value["connections"])}


def refine_return_paths(
    board_path: str | Path,
    output_path: str | Path,
    connections: list[dict[str, Any]],
    *,
    overwrite: bool = False,
    kicad_python_path: str | Path | None = None,
) -> dict[str, Any]:
    """Apply local return shortcuts to a copy, publishing only after full native DRC."""
    source, destination = Path(board_path).resolve(), Path(output_path).resolve()
    try:
        connections = _validate_connections(connections)
        if destination.suffix.lower() != ".kicad_pcb":
            raise ValueError("Refined output must be a .kicad_pcb file")
        if source == destination:
            raise ValueError("Choose a separate output board to preserve the original PCB")
        if not source.is_file() or source.suffix.lower() != ".kicad_pcb":
            raise ValueError("Input must be an existing .kicad_pcb board")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        error = _prepare_destination(destination, overwrite=overwrite)
        if error:
            raise ValueError(error)
        with tempfile.TemporaryDirectory(prefix=".cw-returns-", dir=destination.parent) as folder:
            staged = Path(folder) / destination.name
            snapshots = _prepare_settings(source, staged)
            if snapshots[source] != source_hash:
                raise ValueError("Source board changed during processing")
            input_drc = run_drc(source, evidence_ledger=EvidenceLedger())
            _check_snapshots(snapshots)
            if not input_drc.passed:
                return {"status": "error", "message": "Input board must pass full KiCad DRC before refinement",
                        "drc": input_drc.to_dict()}
            if input_drc.board_sha256 != source_hash:
                raise ValueError("Input DRC evidence does not match the source board bytes")
            changed = run_kicad("refine_returns", board=str(source), output=str(staged),
                                connections=connections, python_path=kicad_python_path)
            if changed.get("status") != "ok":
                return changed
            silkscreen = changed.get("silkscreen", {})
            if not isinstance(silkscreen, dict) or silkscreen.get("unresolved_count") != 0 \
                    or silkscreen.get("remaining_conflict_count") != 0:
                return {"status": "error",
                        "message": ("Refined board failed native silkscreen verification; "
                                    "output was not published"),
                        "silkscreen": silkscreen}
            if not staged.is_file():
                return {"status": "error", "message": "KiCad return-path worker produced no staged board"}
            report = changed.get("refinement")
            rows = report.get("connections") if isinstance(report, dict) else None
            if (not isinstance(rows, list) or len(rows) != len(connections)
                    or any(not isinstance(row, dict) or row.get("status") != "routed" for row in rows)):
                return {"status": "error",
                        "message": "Native refinement report does not verify every requested connection",
                        "refinement": report}
            if (report.get("original_copper_preserved") is not True
                    or report.get("original_footprints_and_text_preserved") is not True):
                return {"status": "error",
                        "message": "Native refinement report does not confirm original board preservation",
                        "refinement": report}
            _restore_settings(source, staged, snapshots)
            drc = run_drc(staged, evidence_ledger=EvidenceLedger())
            if not drc.passed:
                return {"status": "error", "message": "Refined board failed full KiCad DRC; output was not published",
                        "drc": drc.to_dict()}
            _publish(staged, destination, snapshots, overwrite)
        return {"status": "ok", "output_path": str(destination), "output_kind": "refined_kicad_board",
                "source_sha256": source_hash, "refinement": report,
                "silkscreen": silkscreen,
                "drc": {**drc.to_dict(), "board": str(destination)}, "fabrication_ready": False}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "error", "message": str(exc)}
