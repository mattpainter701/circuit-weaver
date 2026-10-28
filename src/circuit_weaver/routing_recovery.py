"""Bounded host-side recovery of already-partial native PCB routing."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .autoroute import _prepare_destination
from .drc_runner import run_drc
from .evidence import EvidenceLedger
from .kicad_bridge import run_kicad
from .kicad_project_context import stage_project_footprint_context
from .pcb_handoff import _pcb_output_lock, _publish_staged_transaction
from .pcb_layout import _check_snapshots, _prepare_settings, _restore_settings


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _remaining(deadline: float) -> int:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("Routing recovery exhausted its total time budget")
    return max(1, math.ceil(left))


def _open_count(result: Any) -> int:
    return sum(
        1
        for finding in result.findings
        if finding.rule_id == "CW-DRC-002" and finding.severity == "blocker" and not finding.suppressed
    )


def _valid_report(result: Any) -> bool:
    raw = result.raw_report
    if not isinstance(raw, dict):
        return False
    # KiCad reports these arrays as the authoritative finding inventory. Reject
    # malformed/truncated variants rather than trying to recover from partial JSON.
    if any(not isinstance(raw.get(key), list) for key in ("violations", "unconnected_items", "schematic_parity")):
        return False

    def has_truncation(value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                ("truncat" in str(key).lower() or "omitted" in str(key).lower()) and bool(item)
                for key, item in value.items()
            ) or any(has_truncation(v) for v in value.values())
        if isinstance(value, list):
            return any(has_truncation(item) for item in value)
        return False

    return not has_truncation(raw)


def _only_open_findings(result: Any) -> bool:
    return (
        result.status == "ok"
        and _valid_report(result)
        and result.blocker_count > 0
        and all(f.rule_id == "CW-DRC-002" for f in result.findings)
        and len(result.raw_report["unconnected_items"]) == result.blocker_count
        and not result.raw_report["violations"]
        and not result.raw_report["schematic_parity"]
    )


def _only_open_blockers(result: Any) -> bool:
    return (
        result.status == "ok"
        and _valid_report(result)
        and all(f.rule_id == "CW-DRC-002" for f in result.findings if f.severity == "blocker" and not f.suppressed)
    )


def _ordered_pairs(raw_report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the same distance order used by the native repair worker."""
    rows = raw_report["unconnected_items"]

    def distance(row: Any) -> float:
        try:
            items = row["items"]
            if len(items) != 2:
                raise ValueError
            points = [(item["pos"]["x"], item["pos"]["y"]) for item in items]
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for point in points
                for value in point
            ):
                raise ValueError
            return math.dist(*points)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Fresh DRC contains a malformed unconnected pair") from exc

    return sorted(rows, key=distance)


def _pair_key(row: dict[str, Any]) -> tuple[str, str, str]:
    items = row["items"]
    uuids = tuple(sorted(str(item.get("uuid", "")) for item in items))
    net = str(row.get("net", items[0].get("net", "")))
    return net, uuids[0], uuids[1]


def _publication_set(staged: Path, destination: Path) -> list[tuple[Path, Path]]:
    publications = [(staged, destination)]
    for suffix in (".kicad_pro", ".kicad_dru"):
        source = staged.with_suffix(suffix)
        if source.is_file():
            publications.append((source, destination.with_suffix(suffix)))
    return publications


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _retain_attempt(
    evidence_root: Path,
    index: int,
    *,
    entry: dict[str, Any],
    before: Any,
    after: Any = None,
    native: Any = None,
    candidate: Path | None = None,
    project_root: Path,
) -> Path:
    folder = evidence_root / f"attempt-{index:04d}"
    folder.mkdir()
    if candidate is not None and candidate.is_file():
        shutil.copy2(candidate, folder / "candidate.kicad_pcb")
        for suffix in (".kicad_pro", ".kicad_dru"):
            sidecar = candidate.with_suffix(suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, folder / sidecar.name)
    context = folder / "project-context"
    context.mkdir()
    context_report = stage_project_footprint_context(project_root, context)
    record = {
        "entry": entry,
        "before_drc": before.to_dict(),
        "before_raw_report": before.raw_report,
        "after_drc": after.to_dict() if after is not None else None,
        "after_raw_report": after.raw_report if after is not None else None,
        "native_response": native,
        "project_context": context_report,
        "candidate_sha256": _sha256(candidate) if candidate is not None and candidate.is_file() else None,
    }
    _write_json(folder / "attempt.json", record)
    return folder


def recover_routing(
    board_path: str | Path,
    output_path: str | Path,
    *,
    kicad_python_path: str | Path | None = None,
    timeout_seconds: float = 600,
    max_links: int = 256,
    max_pair_attempts: int = 12,
    evidence_dir: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Recover links on a partial board and publish only after clean native DRC.

    Each native repair is evaluated against a fresh DRC snapshot. Intermediate
    boards are private evidence artifacts and never masquerade as completion.
    All fresh pairs are eligible; ``max_links * max_pair_attempts`` bounds the
    total proposal count alongside the wall-clock deadline.
    """
    source, destination = Path(board_path).expanduser().resolve(), Path(output_path).expanduser().resolve()
    started, history = time.monotonic(), []
    evidence_root: Path | None = None
    original_hash: str | None = None
    initial_open: int | None = None
    remaining_open: int | None = None
    attempts = 0
    links = 0
    search_mode = "standard"
    search_modes_tried = [search_mode]
    search_profiles = [
        ("standard", None),
        ("bidirectional", 0.5),
        ("bidirectional", 0.25),
        ("bidirectional", 0.125),
    ]
    search_profile_index = 0
    search_profiles_tried = [{"mode": search_mode, "grid_step_mm": None}]
    try:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and greater than zero")
        for label, value in (("max_links", max_links), ("max_pair_attempts", max_pair_attempts)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if destination.suffix.lower() != ".kicad_pcb" or source.suffix.lower() != ".kicad_pcb":
            raise ValueError("Input and output must be .kicad_pcb boards")
        if source == destination or not source.is_file():
            raise ValueError("Input must be an existing board and output must be a separate path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if evidence_dir is not None:
            evidence_parent = Path(evidence_dir).expanduser().resolve()
            evidence_parent.mkdir(parents=True, exist_ok=True)
            evidence_root = Path(tempfile.mkdtemp(prefix="routing-recovery-", dir=evidence_parent))
        else:
            evidence_root = Path(tempfile.mkdtemp(prefix=".cw-routing-recovery-evidence-", dir=destination.parent))
        error = _prepare_destination(destination, overwrite=overwrite)
        if error:
            raise ValueError(error)
        deadline = started + float(timeout_seconds)
        original_hash = _sha256(source)
        with tempfile.TemporaryDirectory(prefix=".cw-routing-recovery-", dir=destination.parent) as folder:
            work = Path(folder)
            current = work / "progress-000.kicad_pcb"
            shutil.copy2(source, current)
            snapshots = _prepare_settings(source, current)
            if snapshots.get(source) != original_hash:
                raise ValueError("Source board changed while preparing recovery")
            before = run_drc(
                current, evidence_ledger=EvidenceLedger(), timeout=_remaining(deadline), project_context=source.parent
            )
            _check_snapshots(snapshots)
            # Retain the rejected inventory as well as accepted opens-only
            # inventories. A generic precondition error must not discard the
            # native findings needed to diagnose a router-generated partial.
            shutil.copy2(current, evidence_root / "input-partial.kicad_pcb")
            for suffix in (".kicad_pro", ".kicad_dru"):
                sidecar = current.with_suffix(suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, evidence_root / f"input-partial{suffix}")
            initial_context = evidence_root / "project-context"
            initial_context.mkdir()
            context_report = stage_project_footprint_context(source.parent, initial_context)
            _write_json(
                evidence_root / "initial-drc.json",
                {
                    "drc": before.to_dict(),
                    "raw_report": before.raw_report,
                    "project_context": context_report,
                    "board_sha256": _sha256(current),
                },
            )
            if before.board_sha256 != _sha256(current) or not _only_open_findings(before):
                raise ValueError("Input must have only complete, fresh CW-DRC-002 open-net blockers")
            initial_open = _open_count(before)
            remaining_open = initial_open
            failed_pairs: set[tuple[str, str, str]] = set()
            attempt_budget = max_links * max_pair_attempts

            def write_history(status: str, **extra: Any) -> None:
                _write_json(
                    evidence_root / "history.json",
                    {
                        "status": status,
                        "history": history,
                        "source_sha256": original_hash,
                        "search_modes_tried": list(search_modes_tried),
                        "search_profiles_tried": list(search_profiles_tried),
                        **extra,
                    },
                )

            _write_json(
                evidence_root / "history.json",
                {"status": "running", "history": history, "source_sha256": original_hash,
                 "search_modes_tried": list(search_modes_tried),
                 "search_profiles_tried": list(search_profiles_tried)},
            )
            while before.blocker_count:
                if links >= max_links or attempts >= attempt_budget:
                    break
                accepted_this_round = False
                ordered_pairs = _ordered_pairs(before.raw_report)
                # Sweep every fresh DRC pair, prioritizing pairs not rejected
                # against an earlier board state. A rejected pair remains
                # retryable after same-net copper is added, but is not allowed
                # to crowd out untouched pairs.
                pair_order = sorted(
                    range(len(ordered_pairs)),
                    key=lambda index: (
                        _pair_key(ordered_pairs[index]) in failed_pairs,
                        -index if search_mode == "bidirectional" else index,
                    ),
                )
                for pair_index in pair_order:
                    if attempts >= attempt_budget:
                        break
                    attempts += 1
                    attempt_dir = work / f"attempt-{attempts:04d}"
                    attempt_dir.mkdir()
                    candidate = attempt_dir / "candidate.kicad_pcb"
                    step_snapshots = _prepare_settings(current, candidate, project_context=source.parent)
                    _check_snapshots(snapshots)
                    request = {
                        "board": str(current),
                        "output": str(candidate),
                        "drc_report": before.raw_report,
                        "pair_index": pair_index,
                        "timeout": _remaining(deadline),
                        "python_path": kicad_python_path,
                    }
                    if search_mode == "bidirectional":
                        request.update(
                            {
                                "search_mode": "bidirectional",
                                "grid_step_mm": search_profiles[search_profile_index][1],
                                "max_expanded_states": 150000,
                            }
                        )
                    response = run_kicad("repair_open", **request)
                    if candidate.is_file():
                        _restore_settings(current, candidate, step_snapshots)
                    else:
                        _check_snapshots(step_snapshots)
                    _check_snapshots(snapshots)
                    entry = {
                        "attempt": attempts,
                        "search_mode": search_mode,
                        "search_profile_grid_mm": search_profiles[search_profile_index][1],
                        "pair_index": pair_index,
                        "pair_key": list(_pair_key(ordered_pairs[pair_index])),
                        "before_sha256": _sha256(current),
                        "before_open_count": _open_count(before),
                        "worker_status": response.get("status") if isinstance(response, dict) else None,
                    }
                    after = None
                    accepted = False
                    report = response.get("route_repair") if isinstance(response, dict) else None
                    required_flags = (
                        "source_identity_preserved",
                        "original_footprints_and_text_preserved",
                        "original_board_geometry_preserved",
                        "existing_copper_preserved",
                    )
                    numeric_fields_ok = (
                        isinstance(report, dict)
                        and isinstance(report.get("added_tracks"), int)
                        and not isinstance(report.get("added_tracks"), bool)
                        and report["added_tracks"] >= 1
                        and isinstance(report.get("added_vias"), int)
                        and not isinstance(report.get("added_vias"), bool)
                        and report["added_vias"] >= 0
                        and isinstance(report.get("expanded_states"), int)
                        and not isinstance(report.get("expanded_states"), bool)
                        and report["expanded_states"] >= 0
                    )
                    report_ok = (
                        isinstance(response, dict)
                        and response.get("status") == "ok"
                        and isinstance(report, dict)
                        and report.get("status") == "candidate_only_requires_full_drc"
                        and all(report.get(key) is True for key in required_flags)
                        and all(
                            isinstance(report.get(key), str) and report[key].strip()
                            for key in ("net", "from_uuid", "to_uuid")
                        )
                        and numeric_fields_ok
                        and candidate.is_file()
                    )
                    if search_mode == "bidirectional":
                        requested_step = report.get("requested_grid_step_mm") if isinstance(report, dict) else None
                        actual_step = report.get("search_grid_step_mm") if isinstance(report, dict) else None
                        state_budget = report.get("search_state_budget") if isinstance(report, dict) else None
                        expanded_states = report.get("expanded_states") if isinstance(report, dict) else None
                        report_ok = report_ok and (
                            report.get("search_mode") == "bidirectional"
                            and report.get("search_direction") == "bidirectional"
                            and isinstance(requested_step, (int, float))
                            and not isinstance(requested_step, bool)
                            and math.isfinite(requested_step)
                            and math.isclose(
                                requested_step, search_profiles[search_profile_index][1],
                                rel_tol=0.0, abs_tol=1e-12,
                            )
                            and isinstance(actual_step, (int, float))
                            and not isinstance(actual_step, bool)
                            and math.isfinite(actual_step)
                            and math.isclose(
                                actual_step, search_profiles[search_profile_index][1],
                                rel_tol=0.0, abs_tol=1e-12,
                            )
                            and isinstance(state_budget, int)
                            and not isinstance(state_budget, bool)
                            and state_budget == 150000
                            and isinstance(expanded_states, int)
                            and not isinstance(expanded_states, bool)
                            and 0 < expanded_states <= 150000
                        )
                    if report_ok:
                        after = run_drc(
                            candidate,
                            evidence_ledger=EvidenceLedger(),
                            timeout=_remaining(deadline),
                            project_context=source.parent,
                        )
                        _check_snapshots(snapshots)
                        entry.update(
                            {
                                "candidate_sha256": _sha256(candidate),
                                "after_open_count": _open_count(after),
                                "drc_status": after.status,
                                "drc_blocker_count": after.blocker_count,
                            }
                        )
                        accepted = (
                            after.status == "ok"
                            and after.board_sha256 == _sha256(candidate)
                            and _valid_report(after)
                            and after.blocker_count < before.blocker_count
                            and after.blocker_count == _open_count(after)
                            and _only_open_blockers(after)
                        )
                    if accepted:
                        entry["evidence_path"] = str(evidence_root / f"attempt-{attempts:04d}")
                        entry.update({"links_repaired": 1, "accepted": True, "native_report": report})
                        history.append(entry)
                        links += 1
                        repaired_net = str(report["net"])
                        failed_pairs = {key for key in failed_pairs if key[0] != repaired_net}
                        _retain_attempt(
                            evidence_root,
                            attempts,
                            entry=entry,
                            before=before,
                            after=after,
                            native=response,
                            candidate=candidate,
                            project_root=source.parent,
                        )
                        shutil.copy2(candidate, evidence_root / "partial-progress.kicad_pcb")
                        for suffix in (".kicad_pro", ".kicad_dru"):
                            sidecar = candidate.with_suffix(suffix)
                            target = evidence_root / f"partial-progress{suffix}"
                            if sidecar.is_file():
                                shutil.copy2(sidecar, target)
                            else:
                                target.unlink(missing_ok=True)
                        current, before = candidate, after
                        remaining_open = _open_count(before)
                        accepted_this_round = True
                        _write_json(
                            evidence_root / "history.json",
                            {"status": "running", "history": history, "source_sha256": original_hash,
                             "search_modes_tried": list(search_modes_tried),
                             "search_profiles_tried": list(search_profiles_tried)},
                        )
                        break
                    entry["accepted"] = False
                    failed_pairs.add(_pair_key(ordered_pairs[pair_index]))
                    entry["evidence_path"] = str(evidence_root / f"attempt-{attempts:04d}")
                    if isinstance(response, dict):
                        entry["native_report"] = report
                    if after is not None:
                        entry["drc_status"] = after.status
                        entry["drc_blocker_count"] = after.blocker_count
                    _retain_attempt(
                        evidence_root,
                        attempts,
                        entry=entry,
                        before=before,
                        after=after,
                        native=response,
                        candidate=candidate if candidate.is_file() else None,
                        project_root=source.parent,
                    )
                    history.append(entry)
                    _write_json(
                        evidence_root / "history.json",
                        {"status": "running", "history": history, "source_sha256": original_hash,
                         "search_modes_tried": list(search_modes_tried),
                         "search_profiles_tried": list(search_profiles_tried)},
                    )
                if not accepted_this_round:
                    if (search_profile_index + 1 < len(search_profiles)
                            and attempts < attempt_budget and links < max_links
                            and time.monotonic() < deadline):
                        # Advance only after a complete no-progress sweep. All
                        # profiles share the original caps and never oscillate.
                        search_profile_index += 1
                        search_mode = search_profiles[search_profile_index][0]
                        if search_mode not in search_modes_tried:
                            search_modes_tried.append(search_mode)
                        search_profiles_tried.append(
                            {"mode": search_mode,
                             "grid_step_mm": search_profiles[search_profile_index][1]}
                        )
                        failed_pairs.clear()
                        write_history(
                            "running",
                            profile_transition_after_attempt=attempts,
                            active_profile=search_profiles_tried[-1],
                        )
                        continue
                    break
            _check_snapshots(snapshots)
            _write_json(
                evidence_root / "history.json",
                {
                    "status": "routing_complete_pending_final_drc" if before.blocker_count == 0 else "partial",
                    "history": history,
                    "search_modes_tried": list(search_modes_tried),
                    "search_profiles_tried": list(search_profiles_tried),
                    "source_sha256": original_hash,
                    "remaining_open_count": _open_count(before),
                },
            )
            if before.blocker_count != 0 or before.status != "ok" or not _valid_report(before):
                return {
                    "status": "partial",
                    "message": "Routing recovery stopped before clean native DRC; no final board was published",
                    "source_sha256": original_hash,
                    "initial_open_count": initial_open,
                    "remaining_open_count": _open_count(before),
                    "links_repaired": links,
                    "attempts": attempts,
                    "history": history,
                    "search_modes_tried": list(search_modes_tried),
                    "search_profiles_tried": list(search_profiles_tried),
                    "partial_board": str(evidence_root / "partial-progress.kicad_pcb") if evidence_root else None,
                    "evidence_dir": str(evidence_root),
                    "fabrication_ready": False,
                }
            final_staged = work / destination.name
            shutil.copy2(current, final_staged)
            _restore_settings(source, final_staged, snapshots)
            final_drc = run_drc(
                final_staged,
                evidence_ledger=EvidenceLedger(),
                timeout=_remaining(deadline),
                project_context=source.parent,
            )
            if not final_drc.passed or not _valid_report(final_drc) or final_drc.board_sha256 != _sha256(final_staged):
                raise ValueError("Final board failed fresh native DRC; output was not published")
            publications = _publication_set(final_staged, destination)
            with _pcb_output_lock(destination.parent):
                _check_snapshots(snapshots)
                # Recheck destination and settings sidecars while holding the publish lock.
                for _src, target in publications:
                    target_error = _prepare_destination(target, overwrite=overwrite)
                    if target_error:
                        raise ValueError(target_error)
                if (
                    destination.with_suffix(".kicad_pro").exists()
                    and not final_staged.with_suffix(".kicad_pro").exists()
                ):
                    raise ValueError("Output has unrelated project settings; choose a fresh output name")
                if (
                    destination.with_suffix(".kicad_dru").exists()
                    and not final_staged.with_suffix(".kicad_dru").exists()
                ):
                    raise ValueError("Output has unrelated project rules; choose a fresh output name")
                _publish_staged_transaction(final_staged.parent, publications)
            _write_json(
                evidence_root / "history.json",
                {
                    "status": "completed",
                    "history": history,
                    "search_modes_tried": list(search_modes_tried),
                    "search_profiles_tried": list(search_profiles_tried),
                    "source_sha256": original_hash,
                    "board_sha256": _sha256(destination),
                    "final_drc": final_drc.to_dict(),
                    "remaining_open_count": 0,
                },
            )
        return {
            "status": "ok",
            "output_path": str(destination),
            "output_kind": "recovered_kicad_board",
            "source_sha256": original_hash,
            "board_sha256": _sha256(destination),
            "initial_open_count": initial_open,
            "remaining_open_count": 0,
            "links_repaired": links,
            "attempts": attempts,
            "history": history,
            "search_modes_tried": list(search_modes_tried),
            "search_profiles_tried": list(search_profiles_tried),
            "drc": {**final_drc.to_dict(), "board": str(destination)},
            "fabrication_ready": False,
            "evidence_dir": str(evidence_root),
        }
    except (OSError, ValueError, TimeoutError, TypeError, AttributeError) as exc:
        partial_path = evidence_root / "partial-progress.kicad_pcb" if evidence_root is not None else None
        partial_exists = partial_path is not None and partial_path.is_file()
        partial_hash = None
        if partial_exists:
            try:
                partial_hash = _sha256(partial_path)
            except OSError:
                pass
        if evidence_root is not None and evidence_root.exists():
            try:
                _write_json(
                    evidence_root / "history.json",
                    {
                        "status": "error",
                        "message": str(exc),
                        "history": history,
                        "search_modes_tried": list(search_modes_tried),
                        "search_profiles_tried": list(search_profiles_tried),
                        "source_sha256": original_hash,
                        "initial_open_count": initial_open,
                        "remaining_open_count": remaining_open,
                        "links_repaired": links,
                        "attempts": attempts,
                        "partial_board": str(partial_path) if partial_exists else None,
                        "partial_board_sha256": partial_hash,
                    },
                )
            except Exception:
                # Evidence finalization must never replace the original failure.
                pass
        return {
            "status": "error",
            "message": str(exc),
            "source_sha256": original_hash,
            "initial_open_count": initial_open,
            "remaining_open_count": remaining_open,
            "links_repaired": links,
            "attempts": attempts,
            "history": history,
            "search_modes_tried": list(search_modes_tried),
            "search_profiles_tried": list(search_profiles_tried),
            "partial_board": str(partial_path) if partial_exists else None,
            "partial_board_sha256": partial_hash,
            "evidence_dir": str(evidence_root) if evidence_root is not None and evidence_root.exists() else None,
            "fabrication_ready": False,
        }
