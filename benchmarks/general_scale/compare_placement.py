"""Compare both placement engines through saved placement, routing and native DRC.

Pass a saved general_scale case directory (unplaced board, design and contracts).
The output directory can hold multiple cases. Run once on fresh destinations,
or use --summarize to inspect the saved outputs again without timing new runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(board_path):
    """Runs in KiCad Python. Keep poses in integer native units including label placement."""
    import pcbnew

    board = pcbnew.LoadBoard(str(board_path))
    poses, labels = {}, {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref in poses:
            raise ValueError(f"Duplicate footprint reference {ref}")
        p = fp.GetPosition()
        poses[ref] = [p.x, p.y, fp.GetOrientationDegrees(), int(fp.GetLayer())]
        label = fp.Reference()
        p = label.GetPosition()
        labels[ref] = [p.x, p.y, label.GetTextAngle().AsDegrees(), int(label.GetLayer()), label.IsVisible()]
    vias, lengths = 0, []
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            vias += 1
        else:
            lengths.append(item.GetLength())
    return dict(
        poses=poses, labels=labels, vias=vias, tracks=len(lengths), copper_mm=round(pcbnew.ToMM(math.fsum(lengths)), 4)
    )


def measure(arm, interpreter):
    def native(name):
        text = subprocess.check_output([interpreter, __file__, "--inventory", str(arm / name)], text=True)
        return json.loads(text)

    times = read(arm / "timings.json")
    placed = read(arm / "place.stdout")
    if times["place"]["returncode"] or placed["status"] != "ok":
        raise RuntimeError(f"Placement failed. See {arm}")
    place = placed["placement"]
    layout = native("placed.kicad_pcb")
    if sha(arm / "placed.kicad_pcb") != placed["drc"]["board_sha256"]:
        raise ValueError("Saved placement differs from its KiCad DRC evidence")
    result = dict(
        place_seconds=times["place"]["seconds"],
        final_cost=place["final_cost"],
        scoring=place["search"].get("scoring"),
        footprints=len(layout["poses"]),
        silk_conflicts=place["silkscreen"]["remaining_conflict_count"],
    )
    if "route" not in times:
        return result | {"route_status": "pending"}, layout
    result["route_seconds"] = times["route"]["seconds"]
    route = read(arm / "route.stdout")
    if times["route"]["returncode"] or route["status"] != "ok":
        return result | {"route_status": route.get("status", "failed"), "error": route.get("message")}, layout
    board = arm / "routed.kicad_pcb"
    drc = route["drc"]
    if sha(board) != drc["board_sha256"]:
        raise ValueError("Saved routed board differs from its KiCad DRC evidence")
    routed = native(board.name)
    if (routed["poses"], routed["labels"]) != (layout["poses"], layout["labels"]):
        raise ValueError("Routing changed a saved footprint or reference label")
    result.update(
        route_status=route["status"],
        routing_complete=route["routing_complete"],
        drc_passed=drc["passed"],
        drc_blockers=drc["blocker_count"],
        drc_complete=drc["diagnostics_complete"],
        vias=routed["vias"],
        copper_mm=routed["copper_mm"],
        tracks=routed["tracks"],
    )
    from connectivity import verify_native_connectivity
    from signal_connectivity import verify_native_signal_paths

    from circuit_weaver.kicad_bridge import run_kicad

    contracts = read(arm / "native-contracts.json")
    saved = run_kicad("inspect", board=str(board), python_path=interpreter, timeout=300)
    if contracts.get("required_channels"):
        result["verified_channels"] = verify_native_connectivity(saved, contracts["required_channels"])[
            "verified_channels"
        ]
    if contracts.get("required_paths"):
        result["verified_paths"] = verify_native_signal_paths(saved, contracts["required_paths"])["verified_paths"]
    return result, layout


def run_arm(source, dest, mode, args):
    dest.mkdir(parents=True, exist_ok=False)
    for name in (
        "design.yaml",
        "unplaced.kicad_pcb",
        "unplaced.kicad_pro",
        "unplaced.kicad_dru",
        "native-contracts.json",
    ):
        if (source / name).is_file():
            shutil.copy2(source / name, dest / name)
    env = os.environ | {"CIRCUIT_WEAVER_PLACEMENT_BACKEND": mode, "PYTHONUTF8": "1"}
    times = {}
    for name, command in [
        (
            "place",
            [
                "place-pcb",
                "unplaced.kicad_pcb",
                "-o",
                "placed.kicad_pcb",
                "--iterations",
                str(args.iterations),
                "--seed",
                str(args.seed),
                "--design",
                "design.yaml",
                "--tidy-references",
            ],
        ),
        (
            "route",
            [
                "autoroute",
                "placed.kicad_pcb",
                "--routed-board",
                "routed.kicad_pcb",
                "--attempts",
                "3",
                "--timeout",
                "1800",
                "--max-passes",
                "20",
                "--optimization-threads",
                "0",
                "--repair-open-connections",
                "--recovery-timeout",
                "1800",
            ],
        ),
    ]:
        start = time.perf_counter()
        with (dest / f"{name}.stdout").open("wb") as out, (dest / f"{name}.stderr").open("wb") as err:
            proc = subprocess.run(
                [sys.executable, "-m", "circuit_weaver", *command], cwd=dest, env=env, stdout=out, stderr=err
            )
        times[name] = dict(seconds=time.perf_counter() - start, returncode=proc.returncode, command=command)
        (dest / "timings.json").write_text(json.dumps(times, indent=2), encoding="utf-8")
        if proc.returncode:
            break


def compare(source, output):
    from circuit_weaver.kicad_bridge import find_kicad_python

    interpreter = find_kicad_python()
    if not interpreter:
        raise RuntimeError("Comparison requires KiCad Python")
    results, layouts, commands = {}, {}, {}
    inputs = {
        p.name: sha(p)
        for p in source.glob("unplaced.kicad_*")
        if p.suffix in {".kicad_pcb", ".kicad_pro", ".kicad_dru"}
    }
    for name in ("design.yaml", "native-contracts.json"):
        inputs[name] = sha(source / name)
    for mode in ("python", "rust"):
        arm = output / mode / source.name
        if any(sha(arm / name) != digest for name, digest in inputs.items()):
            raise ValueError(f"Input mismatch in {arm}")
        commands[mode] = {name: timing["command"] for name, timing in read(arm / "timings.json").items()}
        results[mode], layouts[mode] = measure(arm, interpreter)
        backend = (results[mode]["scoring"] or {}).get("backend")
        # Older Python reference revisions predate engine selection.
        if backend != mode and (mode != "python" or backend is not None):
            raise ValueError(f"Expected {mode} placement scoring in {arm}")
    for stage in commands["python"].keys() & commands["rust"].keys():
        if commands["python"][stage] != commands["rust"][stage]:
            raise ValueError(f"Different {stage} arguments between placement engines")
    left, right = layouts["python"], layouts["rust"]
    refs = set(left["poses"]) | set(right["poses"])
    deltas = {
        key: sorted(ref for ref in refs if left[key].get(ref) != right[key].get(ref)) for key in ("poses", "labels")
    }
    return dict(
        case=source.name,
        inputs_sha256=inputs,
        commands=commands,
        engines=results,
        differences=deltas,
        placement_speedup=results["python"]["place_seconds"] / results["rust"]["place_seconds"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--iterations", type=int, default=6000)
    parser.add_argument("--summarize", action="store_true")
    args = parser.parse_args()
    source, output = args.case_dir.resolve(), args.output.resolve()
    if not args.summarize:
        for mode in ("python", "rust"):
            run_arm(source, output / mode / source.name, mode, args)
    data = compare(source, output)
    (output / f"{source.name}.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, indent=2))
    return int(
        any(data["differences"].values())
        or any(
            not (arm.get("routing_complete") and arm.get("drc_passed") and arm.get("drc_complete"))
            for arm in data["engines"].values()
        )
    )


if __name__ == "__main__":
    if sys.argv[1:2] == ["--inventory"]:
        print(json.dumps(inventory(Path(sys.argv[2]))))
    else:
        sys.exit(main())
