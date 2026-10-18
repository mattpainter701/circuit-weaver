"""Remove explicitly selected copper from a copied KiCad board."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from .autoroute import _prepare_destination
from .drc_runner import run_drc
from .evidence import EvidenceLedger
from .kicad_bridge import run_kicad
from .pcb_layout import _check_snapshots, _prepare_settings, _publish, _restore_settings
from .pcb_return_paths import _validate_connections


def _validate_nets(nets: Any) -> list[str]:
    if not isinstance(nets, list) or not nets:
        raise ValueError("nets must be a non-empty list")
    if any(not isinstance(net, str) or not net.strip() for net in nets):
        raise ValueError("nets must contain only non-empty strings")
    normalized = [net.strip() for net in nets]
    if len(set(normalized)) != len(normalized):
        raise ValueError("nets must not contain duplicates")
    return normalized


def _validate_protected_connections(connections: Any) -> list[dict[str, Any]]:
    normalized = _validate_connections(connections)
    seen = set()
    for index, connection in enumerate(normalized):
        key = (connection["from_ref"], connection["from_pad"],
               connection["to_ref"], connection["to_pad"])
        if key in seen:
            raise ValueError(f"protected_connections[{index}] duplicates a directed endpoint pair")
        seen.add(key)
    return normalized


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_cleanup_plan(path: str | Path) -> dict[str, Any]:
    """Load the strict JSON plan accepted by the public cleanup command."""
    value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != {"nets", "protected_connections"}:
        raise ValueError("Cleanup plan must contain only 'nets' and 'protected_connections'")
    return {
        "nets": _validate_nets(value["nets"]),
        "protected_connections": _validate_protected_connections(value["protected_connections"]),
    }


def cleanup_copper(
    board_path: str | Path,
    output_path: str | Path,
    nets: list[str],
    protected_connections: list[dict[str, Any]],
    *,
    overwrite: bool = False,
    kicad_python_path: str | Path | None = None,
) -> dict[str, Any]:
    """Clean selected copper on a private board copy and publish after verification."""
    source, destination = Path(board_path).resolve(), Path(output_path).resolve()
    try:
        nets = _validate_nets(nets)
        protected_connections = _validate_protected_connections(protected_connections)
        if destination.suffix.lower() != ".kicad_pcb":
            raise ValueError("Cleaned output must be a .kicad_pcb file")
        if source == destination:
            raise ValueError("Choose a separate output board to preserve the original PCB")
        if not source.is_file() or source.suffix.lower() != ".kicad_pcb":
            raise ValueError("Input must be an existing .kicad_pcb board")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        error = _prepare_destination(destination, overwrite=overwrite)
        if error:
            raise ValueError(error)
        with tempfile.TemporaryDirectory(prefix=".cw-copper-cleanup-", dir=destination.parent) as folder:
            staged = Path(folder) / destination.name
            snapshots = _prepare_settings(source, staged)
            if snapshots[source] != source_hash:
                raise ValueError("Source board changed during processing")
            input_drc = run_drc(source, evidence_ledger=EvidenceLedger())
            _check_snapshots(snapshots)
            if not input_drc.passed:
                return {"status": "error", "message": "Input board must pass full KiCad DRC before cleanup",
                        "drc": input_drc.to_dict()}
            if input_drc.board_sha256 != source_hash:
                raise ValueError("Input DRC evidence does not match the source board bytes")
            changed = run_kicad(
                "cleanup_copper", board=str(source), output=str(staged), nets=nets,
                connections=protected_connections, python_path=kicad_python_path,
            )
            if changed.get("status") != "ok":
                return changed
            silkscreen = changed.get("silkscreen", {})
            if (not isinstance(silkscreen, dict) or silkscreen.get("unresolved_count") != 0
                    or silkscreen.get("remaining_conflict_count") != 0):
                return {"status": "error", "message": "Cleaned board failed native silkscreen verification; "
                        "output was not published", "silkscreen": silkscreen}
            if not staged.is_file():
                return {"status": "error", "message": "KiCad cleanup worker produced no staged board"}
            report = changed.get("cleanup")
            required_flags = (
                "all_pads_connected", "protected_paths_preserved", "unselected_copper_preserved",
                "original_footprints_and_text_preserved", "copper_changes_limited_to_selected_nets",
                "locked_copper_preserved",
            )
            rows = report.get("protected_connections") if isinstance(report, dict) else None
            if (not isinstance(rows, list) or len(rows) != len(protected_connections)
                    or any(not isinstance(row, dict) or row.get("status") != "connected"
                           or row.get("protected_path_preserved") is not True for row in rows)):
                return {"status": "error", "message": "Native cleanup report does not verify every protected "
                        "connection", "cleanup": report}
            if (not isinstance(report, dict)
                    or any(report.get(key) is not True for key in required_flags)):
                return {"status": "error", "message": "Native cleanup report does not confirm required "
                        "board preservation and copper constraints", "cleanup": report}
            _restore_settings(source, staged, snapshots)
            drc = run_drc(staged, evidence_ledger=EvidenceLedger())
            if not drc.passed:
                return {"status": "error", "message": "Cleaned board failed full KiCad DRC; output was not published",
                        "drc": drc.to_dict()}
            if drc.board_sha256 != hashlib.sha256(staged.read_bytes()).hexdigest():
                raise ValueError("Final DRC evidence does not match the cleaned board bytes")
            _publish(staged, destination, snapshots, overwrite)
        return {"status": "ok", "output_path": str(destination), "output_kind": "cleaned_kicad_board",
                "source_sha256": source_hash, "cleanup": report, "silkscreen": silkscreen,
                "drc": {**drc.to_dict(), "board": str(destination)}, "fabrication_ready": False}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "error", "message": str(exc)}
