"""Map one Tiny Tapeout digital project to its shuttle and demo board."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .tt_slot_board import map_tt_board
from .tt_slot_die import render_tt_slot
from .tt_slot_index import lookup_tt_slot
from .tt_slot_report import write_slot_report
from .tt_slot_source import fetch_tt_index, fetch_tt_info, fetch_tt_layout


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _repo(value: str) -> str:
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/?", value, re.IGNORECASE)
    if not match:
        raise ValueError("expected project repository must be a GitHub HTTPS repository URL")
    return match[1].lower().removesuffix(".git")


def map_tt_slot(
    macro: str, *, shuttle: str = "ttsky26d", board: str = "tt-etr-v3p3-sky130",
    info: str | Path | None = None, expected_repo: str | None = None,
    index: str | Path | None = None, layout: str | Path | None = None,
    layout_commit: str | None = None, layout_sha256: str | None = None,
    report: str | Path | None = None,
) -> dict[str, Any]:
    """Read the published assignment. Optionally generate a self-contained HTML bring-up sheet."""
    if not re.fullmatch(r"tt_um_[a-zA-Z0-9_]+", macro):
        raise ValueError("use the exact tt_um_ Verilog top module")
    if shuttle.lower() != "ttsky26d" or board != "tt-etr-v3p3-sky130":
        raise ValueError("supported combination: ttsky26d and tt-etr-v3p3-sky130")
    wanted_repo = _repo(expected_repo) if expected_repo else None
    inputs = [Path(path).resolve() for path in (index, info, layout) if path is not None]
    destination = Path(report).resolve() if report is not None else None
    if destination and (destination.suffix.lower() != ".html" or destination in inputs):
        raise ValueError("report must be a separate .html file")
    if layout is not None:
        if index is None or destination is None or not layout_commit or not layout_sha256:
            raise ValueError("local layout requires a local index, report path, layout commit and layout SHA-256")
        if not re.fullmatch(r"[a-f0-9]{64}", layout_sha256):
            raise ValueError("layout SHA-256 must be a full lowercase digest")
    elif layout_commit or layout_sha256:
        raise ValueError("layout evidence requires a local layout")

    with tempfile.TemporaryDirectory(prefix="cw-tt-slot-") as tmp:
        directory = Path(tmp)
        if index is None:
            index_path, source = fetch_tt_index(shuttle.lower(), directory)
        else:
            index_path, source = Path(index), None
        result = lookup_tt_slot(index_path, shuttle="Tiny Tapeout SKY 26d", macro=macro)
        revision = result["provider"]["commit"]
        if source and (source["commit"] != revision or source["index_sha256"] != result["index_sha256"]):
            raise ValueError("the shuttle index differs from the selected provider build")
        if layout is not None and (revision != layout_commit or _digest(Path(layout)) != layout_sha256):
            raise ValueError("local layout revision or SHA-256 differs from the supplied evidence")
        result["source"] = source or {"kind": "local_index", "commit": revision}
        assignment = result["assignment"]
        if assignment and wanted_repo and _repo(assignment["repo"]) != wanted_repo:
            raise ValueError(f"macro belongs to a different project repository: {assignment['repo']}")

        issues: list[str] = []
        local_board = map_tt_board(Path(info), macro=macro, board=board) if info is not None else None
        result["board"] = local_board
        result["pinout_source"] = "local project metadata" if local_board else None
        if assignment and source:
            try:
                released = fetch_tt_info(source, macro, directory)
                published_board = map_tt_board(released, macro=macro, board=board)
                result["board"] = published_board
                result["pinout_source"] = "published project metadata at the shuttle commit"
                result["published_info_sha256"] = _digest(released)
                if local_board and local_board["pins"] != published_board["pins"]:
                    issues.append("Local pin functions differ. The wiring table uses the published functions.")
            except (OSError, ValueError) as exc:
                # Preserve the known assignment without directing wiring from unverified source.
                result["board"] = None
                result["pinout_source"] = None
                issues.append(f"Published pinout unavailable: {exc}")
        elif assignment and destination and local_board is None:
            issues.append("Supply this project's info.yaml with the offline index for a board wiring table.")

        image = None
        die: dict[str, Any] = {"status": "awaiting_assignment" if assignment is None else "not_requested"}
        if assignment and destination:
            try:
                if layout is not None:
                    physical = Path(layout)
                    evidence = {
                        "kind": "operator-supplied layout evidence", "commit": revision, "sha256": layout_sha256,
                    }
                elif source:
                    physical, evidence = fetch_tt_layout(source, directory)
                else:
                    raise ValueError("supply the indexed revision's full-chip layout and its digest for an offline map")
                image_path = directory / "highlight.png"
                details = render_tt_slot(physical, image_path, macro=macro)
                if details["input_sha256"] != evidence["sha256"]:
                    raise ValueError("rendered layout digest differs from the selected file evidence")
                # Filenames in the temporary workspace expire after the report is written.
                die = {k: v for k, v in details.items() if k not in {"path", "input_path", "output", "layout"}}
                die.update(status="located", evidence=evidence)
                image = image_path.read_bytes()
            except (OSError, ValueError) as exc:
                die = {"status": "unavailable", "reason": str(exc)}
                issues.append(f"Die view unavailable: {exc}")
        result.update(die=die, issues=issues)
        if destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".html")
            os.close(fd)
            try:
                write_slot_report(Path(temporary), result, image)
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
            result["report"] = str(destination)
        return result
