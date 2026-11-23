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
_MAX_ROUTER_DANGLING_TRACK_CLEANUP = 64
_ROUTER_DANGLING_RULES = {
    "track_dangling": "CW-DRC-841",
    "via_dangling": "CW-DRC-893",
}


def _remaining_generated_route_cleanup_items(pruned_track_count: int) -> int:
    if (isinstance(pruned_track_count, bool) or not isinstance(pruned_track_count, int)
            or not 0 <= pruned_track_count <= _MAX_ROUTER_DANGLING_TRACK_CLEANUP):
        raise ValueError("Generated width pruning count must be an integer from 0 through 64")
    return _MAX_ROUTER_DANGLING_TRACK_CLEANUP - pruned_track_count


def _write_recovery_json(path: Path, payload: dict[str, Any]) -> None:
    """Persist a recovery diagnostic atomically before evaluating its gate."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _native_drc_evidence(result: Any) -> dict[str, Any]:
    return {
        "drc": result.to_dict(),
        "raw_report": result.raw_report,
        "board_sha256": result.board_sha256,
    }


def _router_dangling_warning_rows(result: Any) -> list[dict[str, Any]] | None:
    """Accept only a complete DRC with open blockers and known dangling warnings."""
    from .routing_recovery import _valid_report

    raw = result.raw_report
    if result.status != "ok" or not isinstance(raw, dict) or not _valid_report(result):
        return None
    if raw.get("schematic_parity"):
        return None
    opens = raw.get("unconnected_items")
    violations = raw.get("violations")
    findings = list(result.findings)
    if not isinstance(opens, list) or not isinstance(violations, list) or len(findings) != len(opens) + len(violations):
        return None
    open_findings = [finding for finding in findings if finding.rule_id == "CW-DRC-002"]
    warning_findings = [finding for finding in findings if finding.rule_id in _ROUTER_DANGLING_RULES.values()]
    if (
        len(open_findings) != len(opens)
        or len(warning_findings) != len(violations)
        or any(f.severity != "blocker" or f.suppressed or f.is_confirmed_blocker is not True for f in open_findings)
        or result.blocker_count != len(opens)
        or any(
            f.severity != "major"
            or f.suppressed
            or f.level != "warning"
            or f.detection_confidence != "verified"
            or f.is_confirmed_blocker is not False
            for f in warning_findings
        )
    ):
        return None
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    row_counts = {rule_id: 0 for rule_id in _ROUTER_DANGLING_RULES.values()}
    for row in violations:
        if not isinstance(row, dict) or row.get("severity") != "warning":
            return None
        warning_type = row.get("type")
        if not isinstance(warning_type, str):
            return None
        rule_id = _ROUTER_DANGLING_RULES.get(warning_type)
        if rule_id is None:
            return None
        items = row.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            return None
        item = items[0]
        uuid = item.get("uuid")
        pos = item.get("pos")
        if not isinstance(uuid, str) or not uuid or uuid in seen or not isinstance(pos, dict):
            return None
        if any(
            isinstance(pos.get(axis), bool)
            or not isinstance(pos.get(axis), (int, float))
            or not math.isfinite(pos.get(axis))
            for axis in ("x", "y")
        ):
            return None
        row_counts[rule_id] += 1
        seen.add(uuid)
        rows.append(row)
    finding_counts = {rule_id: 0 for rule_id in _ROUTER_DANGLING_RULES.values()}
    for finding in warning_findings:
        finding_counts[finding.rule_id] += 1
    if row_counts != finding_counts:
        return None
    return rows


def _strict_native_route_drc(result: Any) -> bool:
    """Whether a fresh report has only verified open connections or is clean."""
    from .routing_recovery import _only_open_findings, _valid_report

    if result.status != "ok" or not _valid_report(result):
        return False
    if _only_open_findings(result):
        return True
    raw = result.raw_report
    return (
        isinstance(raw, dict)
        and not raw["violations"]
        and not raw["unconnected_items"]
        and not raw["schematic_parity"]
        and not result.findings
        and result.blocker_count == 0
    )


def _eligible_bounded_fanout_candidate(initial: dict[str, Any], session_path: Path) -> bool:
    """Gate generated-copper repair to a selected, eligible bounded-fanout SES candidate."""
    status = initial.get("status")
    if status not in {"error", "partial", "review_required"}:
        return False
    if initial.get("routing_profile") != "bounded_fanout":
        return False
    search = initial.get("search")
    if not isinstance(search, dict) or search.get("routing_profile") != "bounded_fanout":
        return False
    canonicalization = search.get("native_dsn_canonicalization")
    if (not isinstance(canonicalization, dict)
            or canonicalization.get("bounded_fanout_input_eligible") is not True):
        return False
    selected = search.get("selected_attempt")
    attempts = search.get("attempts")
    if type(selected) is not int or selected < 1 or not isinstance(attempts, list) or selected > len(attempts):
        return False
    selected_row = attempts[selected - 1]
    if (not isinstance(selected_row, dict) or selected_row.get("routing_profile") != "bounded_fanout"
            or selected_row.get("placement_order_strategy") != "ascending"):
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
    if (not isinstance(artifact, dict) or artifact.get("valid") is not True or artifact.get("kind") != "ses"
            or not isinstance(stats, dict) or type(stats.get("incomplete")) is not int
            or stats["incomplete"] < 0):
        return False
    if status == "partial":
        return stats["incomplete"] > 0 and initial.get("routing_complete") is False and "board_import" not in initial
    if status == "review_required":
        return stats["incomplete"] == 0 and initial.get("routing_complete") is True and "board_import" not in initial
    if stats["incomplete"] != 0:
        return False
    imported = initial.get("board_import")
    drc = imported.get("drc") if isinstance(imported, dict) else None
    return bool(
        isinstance(imported, dict)
        and imported.get("status") == "error"
        and imported.get("message") == _NATIVE_DRC_REJECTION_MESSAGE
        and isinstance(drc, dict)
        and drc.get("status") == "ok"
        and drc.get("passed") is False
    )


def _generated_width_drc_findings_allowed(result: Any, *, allow_clearance: bool) -> bool:
    """Require a complete fresh DRC containing only clearance and open findings."""
    from .routing_recovery import _valid_report

    raw = result.raw_report
    if (result.status != "ok" or result.diagnostics_complete is not True
            or not isinstance(raw, dict) or not _valid_report(result)):
        return False
    violations = raw["violations"]
    opens = raw["unconnected_items"]
    if raw["schematic_parity"]:
        return False
    if not allow_clearance:
        return _router_dangling_warning_rows(result) is not None
    if any(
        not isinstance(row, dict)
        or row.get("type") != "clearance"
        or row.get("severity") != "error"
        for row in violations
    ):
        return False
    findings = list(result.findings)
    clearance_findings = [finding for finding in findings if finding.rule_id == "CW-DRC-001"]
    open_findings = [finding for finding in findings if finding.rule_id == "CW-DRC-002"]
    if (len(findings) != len(violations) + len(opens)
            or len(clearance_findings) != len(violations)
            or len(open_findings) != len(opens)
            or result.blocker_count != len(violations) + len(opens)):
        return False
    def verified_blocker(finding: Any) -> bool:
        return (
            finding.severity == "blocker" and finding.level == "error" and not finding.suppressed
            and finding.detection_confidence == "verified" and finding.is_confirmed_blocker is True
        )

    return all(verified_blocker(finding) for finding in findings)


def _repair_generated_fanout_widths(
    source: Path,
    partial: Path,
    *,
    work: Path,
    python: str | Path | None,
    deadline: float,
    source_snapshots: dict[Path, str | None],
) -> tuple[Path, Any, dict[str, Any]]:
    """Widen a source-empty bounded-fanout candidate and prune only cleared generated tracks."""
    from .drc_runner import run_drc
    from .evidence import EvidenceLedger
    from .kicad_bridge import run_kicad
    from .pcb_layout import _check_snapshots, _prepare_settings, _restore_settings

    root = work / "generated-route-repair"
    root.mkdir(parents=True)
    project_sha = source_snapshots.get(source.with_suffix(".kicad_pro"))
    source_sha = source_snapshots.get(source)
    if (not isinstance(source_sha, str) or len(source_sha) != 64
            or not isinstance(project_sha, str) or len(project_sha) != 64):
        raise ValueError("Generated route repair requires exact source board and project hashes")

    def remaining(limit: int) -> int:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Routing recovery budget expired during generated-route repair")
        return max(1, min(limit, math.ceil(left)))

    def run_and_retain(board: Path, label: str):
        _check_snapshots(source_snapshots)
        result = run_drc(
            board,
            evidence_ledger=EvidenceLedger(),
            timeout=remaining(240),
            project_context=source.parent,
        )
        digest = hashlib.sha256(board.read_bytes()).hexdigest()
        report_path = root / f"{label}-drc.json"
        _write_recovery_json(report_path, _native_drc_evidence(result))
        _check_snapshots(source_snapshots)
        if result.board_sha256 != digest:
            raise ValueError("Fresh generated-route DRC does not match the exact candidate board bytes")
        return result, report_path, digest

    def run_worker(operation: str, current: Path, label: str, *, expected_sha: str, **extra):
        candidate_dir = root / label
        candidate_dir.mkdir()
        candidate = candidate_dir / "candidate.kicad_pcb"
        settings = _prepare_settings(source, candidate)
        _check_snapshots(source_snapshots)
        response = run_kicad(
            operation,
            board=str(current),
            output=str(candidate),
            source_board=str(source),
            expected_board_sha256=expected_sha,
            expected_source_board_sha256=source_sha,
            expected_source_project_sha256=project_sha,
            python_path=python,
            timeout=remaining(180),
            **extra,
        )
        _check_snapshots(source_snapshots)
        if response.get("status") != "ok" or not candidate.is_file():
            raise ValueError(f"Native {operation} failed: {response.get('message', 'candidate missing')}")
        _restore_settings(source, candidate, settings)
        _check_snapshots(source_snapshots)
        report = response.get("generated_route_repair")
        if not isinstance(report, dict):
            raise ValueError(f"Native {operation} did not return a generated-route repair report")
        candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if (report.get("status") != "candidate_only_requires_fresh_drc"
                or report.get("source_copper_item_count") != 0
                or report.get("source_identity_preserved") is not True
                or report.get("source_layout_preserved") is not True
                or report.get("source_geometry_preserved") is not True
                or report.get("other_copper_preserved") is not True
                or report.get("source_copper_exact_subset_preserved") is not True
                or report.get("input_board_sha256") != expected_sha
                or report.get("source_board_sha256") != source_sha
                or report.get("source_project_sha256") != project_sha
                or report.get("board_sha256") != candidate_sha
                or report.get("saved_copper_verified") is not True
                or report.get("saved_source_identity_layout_geometry_verified") is not True
                or report.get("saved_zones_and_stack_preserved") is not True):
            raise ValueError(f"Native {operation} report failed source, save, or hash verification")
        return candidate, report, candidate_sha

    initial_sha = hashlib.sha256(partial.read_bytes()).hexdigest()
    widened, width_report, widened_sha = run_worker(
        "repair_generated_widths", partial, "width-repaired", expected_sha=initial_sha, max_tracks=4096,
    )
    changed = width_report.get("changed_tracks")
    changed_count = width_report.get("changed_track_count")
    if (not isinstance(changed, list) or type(changed_count) is not int
            or changed_count != len(changed) or changed_count > 4096):
        raise ValueError("Native generated width repair returned an invalid changed-track inventory")
    changed_uuids = [row.get("uuid") for row in changed if isinstance(row, dict)]
    if len(changed_uuids) != len(changed) or len(set(changed_uuids)) != len(changed_uuids):
        raise ValueError("Native generated width repair returned malformed or duplicate track UUIDs")

    after_width, after_width_path, after_width_sha = run_and_retain(widened, "after-width")
    if not _generated_width_drc_findings_allowed(after_width, allow_clearance=True):
        raise ValueError(f"Fresh after-width DRC contains incomplete or unknown findings: {after_width_path}")

    prune_report: dict[str, Any] = {"status": "not_needed", "removed_track_count": 0, "removed_tracks": []}
    current, final_drc, final_drc_path, final_sha = widened, after_width, after_width_path, after_width_sha
    if after_width.raw_report["violations"]:
        current, prune_report, pruned_sha = run_worker(
            "prune_generated_width_clearances", widened, "clearance-pruned",
            expected_sha=widened_sha,
            drc_report=dict(after_width.raw_report),
            width_repair_report=width_report,
            max_items=64,
        )
        removed = prune_report.get("removed_tracks")
        removed_count = prune_report.get("removed_track_count")
        removed_uuids = (
            [row.get("uuid") for row in removed if isinstance(row, dict)]
            if isinstance(removed, list)
            else []
        )
        if (not isinstance(removed, list) or type(removed_count) is not int
                or removed_count != len(removed) or removed_count > 64
                or len(removed_uuids) != len(removed) or len(set(removed_uuids)) != len(removed_uuids)
                or not set(removed_uuids).issubset(changed_uuids)):
            raise ValueError("Native generated clearance pruning returned an invalid removal inventory")
        final_drc, final_drc_path, final_sha = run_and_retain(current, "after-prune")
        if not _generated_width_drc_findings_allowed(final_drc, allow_clearance=False):
            raise ValueError(
                "Fresh after-prune DRC contains remaining clearance or unknown findings: "
                f"{final_drc_path}"
            )
        prune_report["fresh_drc"] = str(final_drc_path)
        prune_report["fresh_drc_board_sha256"] = final_sha
        prune_report["input_board_sha256"] = widened_sha
        prune_report["output_board_sha256"] = pruned_sha

    summary = {
        "used": True,
        "status": "fresh_drc_verified",
        "width_repair": width_report,
        "after_width_drc": str(after_width_path),
        "after_width_board_sha256": after_width_sha,
        "clearance_prune": prune_report,
        "final_drc": str(final_drc_path),
        "final_board_sha256": final_sha,
        "pruned_track_count": prune_report.get("removed_track_count", 0),
        "combined_removal_cap": _MAX_ROUTER_DANGLING_TRACK_CLEANUP,
        "source_board_sha256": source_sha,
        "source_project_sha256": project_sha,
    }
    return current, final_drc, summary


def _clean_router_dangling_tracks(
    source: Path,
    partial: Path,
    *,
    work: Path,
    python: str | Path | None,
    deadline: float,
    source_snapshots: dict[Path, str | None],
    max_items: int = _MAX_ROUTER_DANGLING_TRACK_CLEANUP,
) -> tuple[Path, Any, dict[str, Any]]:
    """Normalize only bounded, explicitly identified generated dangling tracks."""
    from .drc_runner import run_drc
    from .evidence import EvidenceLedger
    from .kicad_bridge import run_kicad
    from .pcb_layout import _check_snapshots, _prepare_settings, _restore_settings
    from .routing_recovery import _open_count

    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 0 <= max_items <= 64:
        raise ValueError("max_items must be an integer from 0 through 64")

    root = work / "native-dangling-cleanup"
    root.mkdir(parents=True)
    steps: list[dict[str, Any]] = []
    removed_total = 0
    current = partial

    def remaining(limit: int) -> int:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Routing recovery budget expired during dangling-track cleanup")
        return max(1, min(limit, math.ceil(left)))

    def run_and_retain(board: Path, index: int, label: str):
        result = run_drc(
            board,
            evidence_ledger=EvidenceLedger(),
            timeout=remaining(240),
            project_context=source.parent,
        )
        digest = hashlib.sha256(board.read_bytes()).hexdigest()
        report_path = root / f"step-{index:02d}-{label}-drc.json"
        _write_recovery_json(report_path, _native_drc_evidence(result))
        if result.board_sha256 != digest:
            raise ValueError("Fresh native DRC report does not match the exact partial board bytes")
        return result, report_path, digest

    before, before_path, before_sha = run_and_retain(current, 0, "before")
    if _strict_native_route_drc(before):
        return current, before, {
            "status": "not_needed",
            "steps": [],
            "initial_drc": str(before_path),
            "initial_board_sha256": before_sha,
            "removed_track_count": 0,
            "cap": max_items,
        }

    while True:
        warning_rows = _router_dangling_warning_rows(before)
        if warning_rows is None:
            raise ValueError(
                "Imported router partial has native findings beyond complete open nets and recognized dangling tracks; "
                f"see retained DRC {before_path}"
            )
        if not warning_rows:
            raise ValueError(f"Native DRC is neither opens-only nor a recognized dangling cleanup state: {before_path}")
        if removed_total + len(warning_rows) > max_items:
            raise ValueError(
                f"Router dangling cleanup exceeded its {max_items}-item bound; "
                f"see retained DRC {before_path}"
            )

        index = len(steps) + 1
        # Each staged candidate needs a fresh parent: _prepare_settings copies
        # the project fp-lib-table and local libraries beside the board, and
        # this cleanup may need several sequential passes.
        step_root = root / f"step-{index:02d}"
        step_root.mkdir()
        candidate = step_root / f"partial-cleaned-{index:02d}.kicad_pcb"
        staged_settings = _prepare_settings(source, candidate)
        _check_snapshots(source_snapshots)
        cleanup = run_kicad(
            "cleanup_router_dangling",
            board=str(current),
            output=str(candidate),
            source_board=str(source),
            expected_source_board_sha256=source_snapshots[source],
            expected_board_sha256=before_sha,
            warning_rows=warning_rows,
            max_tracks=max_items - removed_total,
            python_path=python,
            timeout=remaining(180),
        )
        _check_snapshots(source_snapshots)
        if cleanup.get("status") != "ok" or not candidate.is_file():
            message = cleanup.get("message", "candidate missing")
            raise ValueError(f"Native router dangling-track cleanup failed: {message}")
        _restore_settings(source, candidate, staged_settings)
        cleanup_report = cleanup.get("router_dangling_cleanup")
        if not isinstance(cleanup_report, dict):
            raise ValueError("Native cleanup did not return a structured copper-removal report")
        expected_tracks = {
            row["items"][0]["uuid"] for row in warning_rows if row.get("type") == "track_dangling"
        }
        expected_vias = {
            row["items"][0]["uuid"] for row in warning_rows if row.get("type") == "via_dangling"
        }
        if (
            cleanup_report.get("status") != "candidate_only_requires_fresh_drc"
            or cleanup_report.get("source_copper_exact_subset_preserved") is not True
            or (expected_tracks and cleanup_report.get("saved_removed_tracks_verified") is not True)
            or (expected_vias and cleanup_report.get("saved_removed_vias_verified") is not True)
            or (expected_vias and cleanup_report.get("saved_removed_copper_items_verified") is not True)
        ):
            raise ValueError(
                "Native cleanup did not prove exact generated copper removal and source-copper preservation"
            )
        removed_tracks = cleanup_report.get("removed_tracks")
        removed_vias = cleanup_report.get("removed_vias", [])
        if not isinstance(removed_tracks, list) or not isinstance(removed_vias, list):
            raise ValueError("Native cleanup removal inventory is missing track or via rows")
        track_uuids = [row.get("uuid") for row in removed_tracks if isinstance(row, dict)]
        via_uuids = [row.get("uuid") for row in removed_vias if isinstance(row, dict)]
        total_removed = len(removed_tracks) + len(removed_vias)
        if (
            len(track_uuids) != len(removed_tracks)
            or len(set(track_uuids)) != len(track_uuids)
            or set(track_uuids) != expected_tracks
            or len(via_uuids) != len(removed_vias)
            or len(set(via_uuids)) != len(via_uuids)
            or set(via_uuids) != expected_vias
            or type(cleanup_report.get("removed_count")) is not int
            or cleanup_report["removed_count"] != len(removed_tracks)
            or type(cleanup_report.get("removed_track_count")) is not int
            or cleanup_report["removed_track_count"] != len(removed_tracks)
            or type(cleanup_report.get("removed_via_count")) is not int
            or cleanup_report["removed_via_count"] != len(removed_vias)
            or type(cleanup_report.get("removed_item_count")) is not int
            or cleanup_report["removed_item_count"] != total_removed
            or total_removed != len(warning_rows)
        ):
            raise ValueError("Native cleanup removal inventory does not match selected dangling copper items")

        after, after_path, after_sha = run_and_retain(candidate, index, "after")
        after_warnings = _router_dangling_warning_rows(after)
        if after_warnings is None:
            raise ValueError(f"Dangling cleanup introduced or exposed an unrecognized native finding: {after_path}")
        before_open_count = _open_count(before)
        after_open_count = _open_count(after)
        before_warning_count = len(warning_rows)
        after_warning_count = len(after_warnings)
        removed_ids = set(track_uuids) | set(via_uuids)
        after_ids = {
            row["items"][0]["uuid"] for row in after_warnings
            if row.get("items") and isinstance(row["items"][0], dict)
        }
        if (
            len(removed_ids) != total_removed
            or removed_ids.intersection(after_ids)
            or after_open_count > before_open_count
            or after_warning_count > before_warning_count
        ):
            raise ValueError(f"Dangling cleanup failed bounded progress/no-regression checks: {after_path}")
        steps.append({
            "step": index,
            "before_board_sha256": before_sha,
            "before_drc": str(before_path),
            "after_board_sha256": after_sha,
            "after_drc": str(after_path),
            "removed_tracks": removed_tracks,
            "removed_vias": removed_vias,
            "removed_track_count": len(removed_tracks),
            "removed_via_count": len(removed_vias),
            "removed_item_count": total_removed,
            "open_count_before": before_open_count,
            "open_count_after": after_open_count,
            "warning_count_before": before_warning_count,
            "warning_count_after": after_warning_count,
            "source_copper_exact_subset_preserved": True,
        })
        removed_total += total_removed
        current, before, before_path, before_sha = candidate, after, after_path, after_sha
        if _strict_native_route_drc(before):
            return current, before, {
                "status": "cleaned",
                "steps": steps,
                "initial_drc": str(root / "step-00-before-drc.json"),
                "final_drc": str(before_path),
                "final_board_sha256": before_sha,
                "removed_track_count": sum(len(step["removed_tracks"]) for step in steps),
                "removed_via_count": sum(len(step["removed_vias"]) for step in steps),
                "removed_item_count": removed_total,
                "cap": max_items,
                "source_copper_exact_subset_preserved": True,
                "no_added_copper": True,
                "open_count_nonincreasing": True,
                "dangling_warning_count_nonincreasing": True,
            }
        if removed_total >= max_items:
            raise ValueError(
                f"Router dangling cleanup reached its {max_items}-item bound; "
                f"see retained DRC {before_path}"
            )


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
    if type(blockers) is not int or blockers < 0 or not isinstance(findings, list) or not findings:
        return False
    open_findings = [finding for finding in findings if isinstance(finding, dict)
                     and finding.get("rule_id") == "CW-DRC-002"]
    dangling_findings = [finding for finding in findings if isinstance(finding, dict)
                         and finding.get("rule_id") in _ROUTER_DANGLING_RULES.values()]
    if (blockers != len(open_findings)
            or not (open_findings or dangling_findings)
            or len(findings) != len(open_findings) + len(dangling_findings)):
        return False
    if any(
        finding.get("severity") != "blocker"
        or finding.get("level") != "error"
        or finding.get("detection_confidence") != "verified"
        or finding.get("suppressed") is not False
        or finding.get("confirmed_blocker") is not True
        for finding in open_findings
    ):
        return False
    if any(
        finding.get("severity") != "major"
        or finding.get("level") != "warning"
        or finding.get("detection_confidence") != "verified"
        or finding.get("suppressed") is not False
        or finding.get("confirmed_blocker") is not False
        for finding in dangling_findings
    ):
        return False
    return True


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
    from .routing_recovery import _open_count, recover_routing

    work: Path | None = None
    dangling_cleanup: dict[str, Any] = {"status": "not_run"}
    generated_route_repair: dict[str, Any] = {"used": False}
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
        initial_router_options = dict(router_options)
        initial_router_options.update(_allow_partial_nets=True, _native_fanout_profile=True)
        initial = autoroute_pcb(
            source,
            str(initial_session),
            routed_board_path=work / "initial-complete.kicad_pcb",
            **initial_router_options,
        )
        _check_snapshots(snapshots)
        native_open_rejection = _eligible_native_open_rejection(initial, initial_session)
        native_generated_rejection = _eligible_bounded_fanout_candidate(initial, initial_session)
        if (
            initial.get("status") not in {"ok", "partial", "review_required"}
            and not native_open_rejection and not native_generated_rejection
        ) or not initial_session.is_file():
            return {
                **initial,
                "recovery_evidence_dir": str(work),
                "routing_complete": False,
                "generated_route_repair": generated_route_repair,
            }
        recovery_trigger = None
        private_session_sha256 = None
        if native_generated_rejection:
            private_session_sha256 = hashlib.sha256(initial_session.read_bytes()).hexdigest()
            recovery_trigger = {
                "kind": (
                    "bounded_native_fanout_import_rejected"
                    if initial.get("status") == "error"
                    else "bounded_native_fanout_partial_session"
                ),
                "message": (
                    "The selected bounded-fanout native portfolio candidate did not pass its initial native route "
                    "gate; "
                    "the validated SES is quarantined for source-bound width repair and fresh native DRC."
                ),
                "routing_profile": "bounded_fanout",
                "session_path": str(initial_session),
                "session_sha256": private_session_sha256,
                "router_incomplete": initial["stats"]["incomplete"],
                "native_drc": (
                    initial.get("board_import", {}).get("drc")
                    if isinstance(initial.get("board_import"), dict)
                    else None
                ),
            }
        elif native_open_rejection:
            private_session_sha256 = hashlib.sha256(initial_session.read_bytes()).hexdigest()
            native_drc = initial["board_import"]["drc"]
            native_findings = native_drc["findings"]
            dangling_track_warning_count = sum(
                finding.get("rule_id") == _ROUTER_DANGLING_RULES["track_dangling"] for finding in native_findings
            )
            dangling_via_warning_count = sum(
                finding.get("rule_id") == _ROUTER_DANGLING_RULES["via_dangling"] for finding in native_findings
            )
            dangling_warning_count = dangling_track_warning_count + dangling_via_warning_count
            warning_triggered = dangling_warning_count > 0
            mixed_triggered = warning_triggered and native_drc["blocker_count"] > 0
            recovery_trigger = {
                "kind": (
                    "native_open_and_dangling_drc_after_router_reported_zero_incomplete"
                    if mixed_triggered
                    else "native_dangling_drc_after_router_reported_zero_incomplete"
                    if warning_triggered
                    else "native_open_drc_after_router_reported_zero_incomplete"
                ),
                "message": (
                    "Native DRC found verified open connections and recognized router dangling-copper warnings "
                    "despite a zero-incomplete router statistic; the validated private SES is being re-imported "
                    "into quarantine for fresh-DRC cleanup and recovery."
                    if mixed_triggered
                    else "Native DRC found recognized router dangling-copper warnings despite a zero-incomplete "
                    "router statistic; the validated private SES is being re-imported into quarantine for "
                    "fresh-DRC cleanup and strict final validation."
                    if warning_triggered
                    else "Native DRC found open connections despite a zero-incomplete router statistic; "
                    "the validated private SES is being re-imported into quarantine for fresh-DRC recovery."
                ),
                "session_path": str(initial_session),
                "session_sha256": private_session_sha256,
                "router_incomplete": initial["stats"]["incomplete"],
                "native_drc": {
                    "board_sha256": native_drc.get("board_sha256"),
                    "blocker_count": native_drc["blocker_count"],
                    "finding_count": len(native_findings),
                    "dangling_warning_count": dangling_warning_count,
                    "dangling_track_warning_count": dangling_track_warning_count,
                    "dangling_via_warning_count": dangling_via_warning_count,
                    "findings": [
                        {
                            key: finding.get(key)
                            for key in ("rule_id", "severity", "level", "suppressed", "confirmed_blocker",
                                        "ref", "net", "message")
                            if key in finding
                        }
                        for finding in native_findings
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
                (native_open_rejection or native_generated_rejection)
                and hashlib.sha256(initial_session.read_bytes()).hexdigest() != private_session_sha256
            ):
                raise ValueError("Validated private SES changed during native quarantine import")
            _restore_settings(source, partial, staged_settings)
            max_dangling_items = _MAX_ROUTER_DANGLING_TRACK_CLEANUP
            if native_generated_rejection:
                partial, partial_drc, generated_route_repair = _repair_generated_fanout_widths(
                    source,
                    partial,
                    work=work,
                    python=python,
                    deadline=deadline,
                    source_snapshots=snapshots,
                )
                max_dangling_items = _remaining_generated_route_cleanup_items(
                    generated_route_repair["pruned_track_count"]
                )
            partial, partial_drc, dangling_cleanup = _clean_router_dangling_tracks(
                source,
                partial,
                work=work,
                python=python,
                deadline=deadline,
                source_snapshots=snapshots,
                max_items=max_dangling_items,
            )
            if generated_route_repair.get("used"):
                dangling_removed = dangling_cleanup.get("removed_item_count")
                if dangling_removed is None:
                    dangling_removed = (
                        dangling_cleanup.get("removed_track_count", 0)
                        + dangling_cleanup.get("removed_via_count", 0)
                    )
                generated_route_repair["dangling_cleanup_removed_item_count"] = dangling_removed
                generated_route_repair["combined_removed_item_count"] = (
                    generated_route_repair["pruned_track_count"] + dangling_removed
                )
                generated_route_repair["combined_removal_cap"] = _MAX_ROUTER_DANGLING_TRACK_CLEANUP
                if generated_route_repair["combined_removed_item_count"] > _MAX_ROUTER_DANGLING_TRACK_CLEANUP:
                    raise ValueError("Generated width pruning and dangling cleanup exceeded the combined 64-item cap")
            if _open_count(partial_drc) > 0:
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
                        "router_dangling_cleanup": dangling_cleanup,
                        "generated_route_repair": generated_route_repair,
                        "recovery_evidence_dir": str(work),
                        "fabrication_ready": False,
                        **({"recovery_trigger": recovery_trigger} if recovery_trigger else {}),
                    }
                serialization_source = Path(repair["output_path"])
            else:
                # A native-clean partial needs no link repair, but is still
                # serialized and submitted to the ordinary final import gate.
                repair = {
                    "used": True,
                    "status": "not_needed_native_drc_clean",
                    "output_path": str(partial),
                    "router_dangling_cleanup": dangling_cleanup,
                    "generated_route_repair": generated_route_repair,
                }
                serialization_source = partial
            completed_session = work / "complete.ses"
            # The stock router serializes all copper with autorouter, fanout,
            # and optimizer explicitly disabled; complete-SES validation and
            # the ordinary native import/DRC gate remain mandatory.
            serialization = _serialize_native_session(
                serialization_source, completed_session,
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
                    "router_dangling_cleanup": dangling_cleanup,
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
                    "router_dangling_cleanup": dangling_cleanup,
                    "generated_route_repair": generated_route_repair,
                    **({"recovery_trigger": recovery_trigger} if recovery_trigger else {})}
        return {
            **accepted,
            "session_path": str(session),
            "recovery": repair,
            "router_dangling_cleanup": dangling_cleanup,
            "generated_route_repair": generated_route_repair,
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
            "router_dangling_cleanup": dangling_cleanup,
            "generated_route_repair": generated_route_repair,
            "fabrication_ready": False,
        }
