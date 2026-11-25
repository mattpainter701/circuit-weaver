"""Replay native repair on a saved PCB or a placed board plus a partial SES."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from circuit_weaver import routing_recovery
from circuit_weaver.drc_runner import run_drc
from circuit_weaver.evidence import EvidenceLedger
from circuit_weaver.kicad_bridge import find_kicad_python, run_kicad
from circuit_weaver.pcb_layout import _check_snapshots, _prepare_settings, _restore_settings
from circuit_weaver.project_state import file_sha256
from circuit_weaver.routing_recovery import _only_open_findings, _ordered_pairs, _valid_report, recover_routing


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def checked_drc(board):
    start = time.perf_counter()
    drc = run_drc(board, evidence_ledger=EvidenceLedger(), project_context=board.parent)
    elapsed = time.perf_counter() - start
    if (drc.status != "ok" or not drc.diagnostics_complete or not _valid_report(drc)
            or drc.board_sha256 != file_sha256(board)):
        raise RuntimeError(f"Incomplete native DRC for {board}: {drc.failure_reason}")
    return drc, elapsed


def sample_indices(count, limit):
    if limit is None or count <= limit:
        return range(count)
    if limit <= 1:
        return range(limit)
    return [i * (count - 1) // (limit - 1) for i in range(limit)]


def timed_recovery(partial, destination, seconds, python):
    times = {"worker_s": 0.0, "drc_s": 0.0, "worker_calls": 0, "drc_calls": 0}

    def timed(fn, kind):
        def measure(*args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                times[kind + "_s"] += time.perf_counter() - start
                times[kind + "_calls"] += 1
        return measure

    with patch.object(routing_recovery, "run_kicad", timed(run_kicad, "worker")), \
            patch.object(routing_recovery, "run_drc", timed(run_drc, "drc")):
        start = time.perf_counter()
        result = recover_routing(partial, destination, timeout_seconds=seconds, kicad_python_path=python,
                                 evidence_dir=destination.parent / "recovery-evidence")
        times["wall_s"] = time.perf_counter() - start
    return result, times


def inventory(python, board):
    script = Path(__file__).with_name("compare_placement.py")
    result = json.loads(subprocess.check_output([python, str(script), "--inventory", str(board)], text=True))
    return {key: result[key] for key in ("copper_mm", "vias", "tracks", "track_via_sha256")}


def physical_findings(raw):
    return Counter(
        (group, row["type"], row["severity"], tuple(sorted(item["uuid"] for item in row["items"])))
        for group in ("violations", "schematic_parity") for row in raw[group]
    )


def probe_pair(board, report, index, endpoints, args, python, initial_findings):
    checked = {"index": index, "items": endpoints}
    with tempfile.TemporaryDirectory(prefix="pair-", dir=args.output) as temporary:
        folder = Path(temporary)
        source = folder / "input.kicad_pcb"
        candidate = folder / "candidate.kicad_pcb"
        shutil.copyfile(board, source)
        snapshots = _prepare_settings(board, source)
        request = {"operation": "repair_open", "board": str(source), "output": str(candidate),
                   "drc_report": str(report), "pair_index": index, "max_expanded_states": args.states,
                   "grid_step_mm": args.grid_step, "connected_endpoints": args.search_mode == "connected",
                   "search_mode": "standard" if args.search_mode == "standard" else "bidirectional"}
        worker = Path(__file__).with_name("profile_repair_worker.py")
        response = folder / "probe.json"
        started = time.perf_counter()
        try:
            proc = subprocess.run([python, str(worker), str(response)], input=json.dumps(request),
                                  text=True, capture_output=True, timeout=args.pair_timeout)
        except subprocess.TimeoutExpired:
            return {**checked, "status": "timeout", "worker_wall_s": time.perf_counter() - started}
        elapsed = time.perf_counter() - started
        if proc.returncode or not response.exists():
            raise RuntimeError(f"KiCad probe failed ({proc.returncode}): {proc.stderr[-1000:]}")
        checked.update(json.loads(response.read_text(encoding="utf-8")), worker_wall_s=elapsed)
        checked["process_overhead_s"] = elapsed - checked["timings"]["process_body_s"]
        _check_snapshots(snapshots)
        if checked["status"] == "candidate":
            _restore_settings(board, candidate, snapshots)
            after, duration = checked_drc(candidate)
            checked.update(drc_s=duration, opens_after=len(after.raw_report["unconnected_items"]),
                           findings_after=dict(Counter(row["type"] for row in after.raw_report["violations"])),
                           new_physical_findings=sum((physical_findings(after.raw_report) - initial_findings).values()),
                           board_sha256=after.board_sha256, output_copper=inventory(python, candidate))
    return checked


def replay(args):
    board = args.board.resolve(strict=True)
    session = args.session.resolve(strict=True) if args.session else None
    python = find_kicad_python(args.kicad_python)
    if python is None:
        raise RuntimeError("No KiCad Python interpreter with pcbnew found")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    partial = args.output / "partial.kicad_pcb"
    snapshots = _prepare_settings(board, partial)
    if session:
        snapshots[session] = file_sha256(session)
        imported = run_kicad("import_ses", board=str(board), session=str(session), output=str(partial),
                             python_path=python)
        if imported["status"] != "ok":
            raise RuntimeError(imported.get("message", "Native SES import failed"))
        _restore_settings(board, partial, snapshots)
    else:
        shutil.copyfile(board, partial)
    before, drc_s = checked_drc(partial)
    raw_path = args.output / "initial-drc.json"
    write(raw_path, before.raw_report)
    pairs = _ordered_pairs(before.raw_report)
    eligible = _only_open_findings(before)
    summary = {"input_sha256": snapshots[board], "session_sha256": snapshots.get(session),
               "partial_sha256": before.board_sha256, "initial_drc_s": drc_s, "initial_opens": len(pairs),
               "initial_findings": dict(Counter(row["type"] for row in before.raw_report["violations"])),
               "recovery_eligible": eligible, "input_copper": inventory(python, partial),
               "pair_state_budget": args.states, "pair_wall_limit_s": args.pair_timeout,
               "pair_grid_mm": args.grid_step, "pair_mode": args.search_mode,
               "recovery_budget_s": args.recovery_seconds}
    if eligible and args.recovery_seconds:
        destination = args.output / "recovered.kicad_pcb"
        recovery, times = timed_recovery(partial, destination, args.recovery_seconds, python)
        summary.update(recovery_s=times["wall_s"], recovery_timings=times)
        write(args.output / "recovery.json", recovery)
        summary["recovery_status"] = recovery["status"]
        summary["remaining_opens"] = recovery.get("remaining_open_count")
        if recovery["status"] == "ok":
            after, seconds = checked_drc(destination)
            if any(after.raw_report[k] for k in ("violations", "unconnected_items", "schematic_parity")):
                raise RuntimeError("Published recovery output failed independent native verification")
            summary["independent_drc_s"] = seconds
            summary["output_copper"] = inventory(python, destination)
    summary_path = args.output / "summary.json"
    write(summary_path, summary)
    selected = sample_indices(len(pairs), args.pair_limit)
    initial_findings = physical_findings(before.raw_report)
    frozen = {partial: before.board_sha256, raw_path: file_sha256(raw_path)}
    with (args.output / "pairs.jsonl").open("x", encoding="utf-8") as out:
        for index in selected:
            _check_snapshots(frozen)
            row = probe_pair(partial, raw_path, index, pairs[index]["items"], args, python, initial_findings)
            out.write(json.dumps(row) + "\n")
            out.flush()
    _check_snapshots(frozen)
    _check_snapshots(snapshots)
    summary["pairs_finished"] = len(selected)
    write(summary_path, summary)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("board", type=Path)
    p.add_argument("--session", type=Path, help="Import this saved SES onto the placed input first")
    p.add_argument("--output", type=Path, required=True, help="New output directory")
    p.add_argument("--kicad-python")
    p.add_argument("--states", type=int, default=4000, help="Expansion budget for each independent pair probe")
    p.add_argument("--search-mode", choices=("standard", "bidirectional", "connected"), default="standard",
                   help="Pair search strategy; connected uses multiple verified native copper anchors")
    p.add_argument("--grid-step", type=float, default=0.25, help="Initial pair search grid in mm")
    p.add_argument("--pair-limit", type=int, help="Sample N pairs across production distance order (default all)")
    p.add_argument("--pair-timeout", type=float, default=30)
    p.add_argument("--recovery-seconds", type=float, default=0,
                   help="Stock recovery time budget on eligible inputs (0 to skip)")
    args = p.parse_args()
    if args.states <= 0 or (args.pair_limit is not None and args.pair_limit < 0):
        p.error("Use positive states and a nonnegative pair limit")
    if not math.isfinite(args.grid_step) or args.grid_step <= 0:
        p.error("Use a finite positive grid step")
    if (not math.isfinite(args.pair_timeout) or args.pair_timeout <= 0
            or not math.isfinite(args.recovery_seconds) or args.recovery_seconds < 0):
        p.error("Use a positive pair timeout and a nonnegative recovery budget")
    replay(args)


if __name__ == "__main__":
    main()
