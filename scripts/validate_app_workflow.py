"""Run a real CLI/optional MCP demo and retain inspectable evidence.

Requires KiCad with pcbnew and Freerouting. Outputs are prototype artifacts;
the manufacturing export gate must still refuse an unapproved delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

REPO = Path(__file__).resolve().parents[1]
_BOARD_TARGETS = {"board", "board_outline", "pcb", "outline"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _is_board_dimension_constraint(raw: dict) -> bool:
    """Identify placement-stage outline declarations, excluding keepout geometry."""
    kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()
    if kind not in {"", "placement"}:
        return False
    target = str(raw.get("target") or raw.get("ref") or "").strip().lower()
    nested = raw.get("board") if isinstance(raw.get("board"), dict) else {}
    return target in _BOARD_TARGETS or (
        "board_width_mm" in raw or "board_height_mm" in raw or bool(nested)
    )


def _design_board_size(design: dict, default: tuple[float, float] = (80.0, 60.0)) -> tuple[float, float]:
    """Resolve placement-stage board dimensions, never keepout rectangles."""
    width, height = default
    constraints = design.get("pcb_constraints")
    if not isinstance(constraints, list):
        return width, height
    for raw in constraints:
        if not isinstance(raw, dict):
            continue
        if not _is_board_dimension_constraint(raw):
            continue
        target = str(raw.get("target") or raw.get("ref") or "").strip().lower()
        nested = raw.get("board") if isinstance(raw.get("board"), dict) else {}
        candidate_width = _finite_number(raw.get(
            "board_width_mm",
            nested.get("width_mm", raw.get("width_mm") if target in _BOARD_TARGETS else None),
        ))
        candidate_height = _finite_number(raw.get(
            "board_height_mm",
            nested.get("height_mm", raw.get("height_mm") if target in _BOARD_TARGETS else None),
        ))
        if candidate_width is not None:
            width = candidate_width
        if candidate_height is not None:
            height = candidate_height
    return width, height


def _apply_size_overrides(design: dict, width: float, height: float) -> None:
    """Make copied placement constraints agree with the prototype outline."""
    constraints = design.get("pcb_constraints")
    if constraints is None:
        constraints = []
    if not isinstance(constraints, list):
        raise RuntimeError("Cannot apply board-size overrides: pcb_constraints must be a list")

    for raw in constraints:
        if not isinstance(raw, dict):
            continue
        if not _is_board_dimension_constraint(raw):
            continue
        target = str(raw.get("target") or raw.get("ref") or "").strip().lower()
        nested = raw.get("board") if isinstance(raw.get("board"), dict) else None

        if "board_width_mm" in raw:
            raw["board_width_mm"] = width
        if "board_height_mm" in raw:
            raw["board_height_mm"] = height
        if nested is not None:
            nested["width_mm"] = width
            nested["height_mm"] = height
        if target in _BOARD_TARGETS:
            raw["width_mm"] = width
            raw["height_mm"] = height

    # A final canonical constraint ensures partial or conflicting duplicate
    # declarations cannot override either effective dimension later in the list.
    constraints.append({
        "kind": "placement", "target": "board", "width_mm": width, "height_mm": height,
    })
    design["pcb_constraints"] = constraints


def prepare_working_spec(source: Path, destination: Path, width: float | None,
                         height: float | None) -> dict:
    """Copy a design verbatim unless dimensions are explicitly overridden."""
    shutil.copy2(source, destination)
    original_bytes = source.read_bytes()
    design = yaml.safe_load(original_bytes.decode("utf-8-sig"))
    if not isinstance(design, dict):
        raise RuntimeError("Design specification must contain a YAML mapping")
    original_size = _design_board_size(design)
    effective_size = (width if width is not None else original_size[0],
                      height if height is not None else original_size[1])
    overridden = width is not None or height is not None
    if overridden:
        for name, value in (("width", effective_size[0]), ("height", effective_size[1])):
            if not math.isfinite(value) or value <= 0:
                raise RuntimeError(f"Effective board {name} must be a positive finite number")
        _apply_size_overrides(design, *effective_size)
        destination.write_text(yaml.safe_dump(design, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {
        "source_spec_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "working_spec_sha256": digest(destination),
        "board_size_original_mm": list(original_size),
        "board_size_effective_mm": list(effective_size),
        "board_size_overrides_mm": {"width_mm": width, "height_mm": height},
        "board_size_override_applied": overridden,
    }


def initialize_workflow_project(output: Path, *, cwd: Path, env: dict) -> None:
    """Give each validation case private state before discovering any ancestors."""
    process = subprocess.run(
        [sys.executable, "-c",
         "from pathlib import Path; import sys; "
         "from circuit_weaver.project_state import ensure_project_state, save_project_state; "
         "root=Path(sys.argv[1]); save_project_state(root, ensure_project_state(root))", str(output)],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if process.returncode:
        raise RuntimeError(f"Cannot initialize private workflow project: {process.stderr.strip()}")
    state = output / ".circuit-weaver" / "project.json"
    if not state.is_file():
        raise RuntimeError("Workflow project initialization did not produce a local state manifest")


def native_contract_preflight(
    board: Path, contracts: Path, expected_contract_sha256: str, demo: "Demo", output: Path
) -> dict:
    """Check snapshotted channel/path pad nets on the unplaced native board."""
    contract_hash = digest(contracts)
    if contract_hash != expected_contract_sha256:
        raise RuntimeError("native contract snapshot hash does not match the runner's expected hash")
    contract_data = json.loads(contracts.read_text(encoding="utf-8"))
    if not isinstance(contract_data, dict) or not (
        contract_data.get("required_channels") or contract_data.get("required_paths")
    ):
        raise RuntimeError("native contract snapshot must contain channels or signal paths")
    board_hash = digest(board)
    helper_dir = REPO / "benchmarks" / "general_scale"
    script = (
        "import json,sys; sys.path.insert(0,sys.argv[3]); "
        "from circuit_weaver.kicad_bridge import run_kicad; "
        "from connectivity import verify_native_connectivity; "
        "from signal_connectivity import verify_native_signal_paths; "
        "inventory=run_kicad('inspect',board=sys.argv[1],timeout=float(sys.argv[2])); "
        "contracts=json.load(open(sys.argv[4],encoding='utf-8')); "
        "channels=contracts.get('required_channels',[]); paths=contracts.get('required_paths',[]); "
        "proof={'inventory':inventory}; "
        "proof.update({'connectivity':verify_native_connectivity(inventory,channels)} if channels else {}); "
        "proof.update({'signal_connectivity':verify_native_signal_paths(inventory,paths)} if paths else {}); "
        "print(json.dumps(proof))"
    )
    timeout = min(max(demo.timeout, 120), 600)
    evidence_path = output / "native-contract-preflight.json"

    def fail(message: str, **details: object) -> None:
        proof = {
            "status": "failed",
            "error": message,
            "contract_sha256": contract_hash,
            "board_sha256_before": board_hash,
            **details,
        }
        try:
            proof["board_sha256_after"] = digest(board)
            proof["contract_sha256_after"] = digest(contracts)
        except OSError:
            pass
        evidence_path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(board.resolve()),
                str(timeout),
                str(helper_dir),
                str(contracts.resolve()),
            ],
            cwd=demo.cwd,
            env=demo.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 30,
        )
    except Exception as exc:
        fail(exception_message(exc))
        raise RuntimeError("native pad/net contract preflight could not inspect the unplaced board") from exc
    (output / "native-contract-preflight.stdout.log").write_text(result.stdout, encoding="utf-8")
    (output / "native-contract-preflight.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        fail(
            "native contract verifier rejected the unplaced board",
            verifier_exit_code=result.returncode,
            stdout_log="native-contract-preflight.stdout.log",
            stderr_log="native-contract-preflight.stderr.log",
        )
        raise RuntimeError("native pad/net contract preflight failed; placement and routing were not started")
    try:
        details = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        fail("native contract preflight returned invalid JSON", stdout_log="native-contract-preflight.stdout.log")
        raise RuntimeError("native contract preflight returned invalid JSON") from exc
    inventory = details.get("inventory")
    connectivity = details.get("connectivity")
    paths = details.get("signal_connectivity")
    after_board_hash = digest(board)
    after_contract_hash = digest(contracts)
    if after_board_hash != board_hash or after_contract_hash != contract_hash:
        fail("native contract preflight changed its board or contract input")
        raise RuntimeError("native contract preflight changed its board or contract input")
    if not isinstance(inventory, dict) or inventory.get("status") != "ok":
        fail("native contract preflight did not receive an OK inventory")
        raise RuntimeError("native contract preflight did not receive an OK inventory")
    expected_channels = len(contract_data.get("required_channels", []))
    expected_paths = len(contract_data.get("required_paths", []))
    if expected_channels and (
        not isinstance(connectivity, dict)
        or connectivity.get("status") != "pass"
        or connectivity.get("verified_channels") != expected_channels
    ):
        fail("native channel preflight did not pass every contract")
        raise RuntimeError("native channel preflight did not pass every contract")
    if expected_paths and (
        not isinstance(paths, dict)
        or paths.get("status") != "pass"
        or paths.get("verified_paths") != expected_paths
    ):
        fail("native signal-path preflight did not pass every contract")
        raise RuntimeError("native signal-path preflight did not pass every contract")
    proof = {
        "status": "pass",
        "board_sha256": board_hash,
        "contract_sha256": contract_hash,
        "native_inventory_sha256": hashlib.sha256(json.dumps(inventory, sort_keys=True).encode("utf-8")).hexdigest(),
        "verified_channels": connectivity.get("verified_channels", 0) if connectivity else 0,
        "verified_paths": paths.get("verified_paths", 0) if paths else 0,
        "verified_return_assertions": paths.get("verified_return_assertions", 0) if paths else 0,
    }
    evidence_path.write_text(json.dumps({**proof, "checks": details}, indent=2) + "\n", encoding="utf-8")
    return proof

def place_after_native_contracts(
    demo: "Demo",
    board: Path,
    contracts: Path | None,
    expected_contract_sha256: str | None,
    output: Path,
    placement_arguments: list[str],
) -> tuple[dict, dict | None]:
    """Make native preflight a hard barrier before placement starts."""
    proof = None
    if contracts is not None:
        if expected_contract_sha256 is None:
            raise RuntimeError("native contract preflight is missing its expected hash")
        proof = native_contract_preflight(board, contracts, expected_contract_sha256, demo, output)
    placement = demo.command("place", placement_arguments)
    return placement, proof


def _payload_hash(files: dict[str, str]) -> str:
    hasher = hashlib.sha256()
    for name, content_hash in sorted(files.items()):
        hasher.update(f"{name}\0{content_hash}\n".encode("utf-8"))
    return hasher.hexdigest()


def verify_installed_wheel_payload(package_root: Path, wheel_path: Path) -> dict:
    """Require exact installed package bytes to match a wheel's package payload."""
    if not package_root.is_dir():
        raise RuntimeError(f"Installed package directory does not exist: {package_root}")
    if not wheel_path.is_file():
        raise RuntimeError(f"Expected wheel does not exist: {wheel_path}")

    expected: dict[str, str] = {}
    seen: set[str] = set()
    try:
        wheel_bytes = wheel_path.read_bytes()
        wheel_sha256 = hashlib.sha256(wheel_bytes).hexdigest()
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            for info in archive.infolist():
                name = info.filename
                if (not name or "\\" in name or name.startswith("/")
                        or PureWindowsPath(name).drive
                        or any(part in {".", ".."} for part in name.rstrip("/").split("/"))):
                    raise RuntimeError(f"Expected wheel contains an unsafe archive path: {name!r}")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise RuntimeError(f"Expected wheel contains a symbolic link: {name!r}")
                normalized = PurePosixPath(name).as_posix()
                if normalized in seen:
                    raise RuntimeError(f"Expected wheel contains a duplicate path: {name!r}")
                seen.add(normalized)
                if info.is_dir() or not normalized.startswith("circuit_weaver/"):
                    continue
                relative = normalized.removeprefix("circuit_weaver/")
                if not relative or relative.startswith("__pycache__/") or relative.endswith(".pyc"):
                    continue
                expected[relative] = hashlib.sha256(archive.read(info)).hexdigest()
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith("Expected wheel"):
            raise
        raise RuntimeError(f"Cannot inspect expected wheel: {exc}") from exc
    if not expected:
        raise RuntimeError("Expected wheel contains no circuit_weaver package payload")

    installed: dict[str, str] = {}
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Installed package contains a symbolic link: {path}")
        if path.is_dir():
            continue
        relative_path = path.relative_to(package_root)
        if "__pycache__" in relative_path.parts or path.suffix == ".pyc":
            continue
        if not path.is_file():
            raise RuntimeError(f"Installed package contains an unsupported payload entry: {path}")
        relative = relative_path.as_posix()
        installed[relative] = digest(path)

    missing = sorted(set(expected) - set(installed))
    unexpected = sorted(set(installed) - set(expected))
    changed = sorted(name for name in set(expected) & set(installed)
                     if expected[name] != installed[name])
    if missing or unexpected or changed:
        raise RuntimeError(
            "Installed package payload does not match expected wheel: "
            f"missing={missing}, unexpected={unexpected}, changed={changed}"
        )
    if digest(wheel_path) != wheel_sha256:
        raise RuntimeError("Expected wheel changed while its payload was being verified")
    return {
        "wheel_path": str(wheel_path.resolve()),
        "wheel_sha256": wheel_sha256,
        "package_path": str(package_root.resolve()),
        "payload_file_count": len(expected),
        "expected_payload_sha256": _payload_hash(expected),
        "installed_payload_sha256": _payload_hash(installed),
        "payload_matches": True,
    }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def exception_message(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", ())
    if nested:
        return "; ".join(exception_message(child) for child in nested)
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or "").strip()
        return f"{exc}: {detail}" if detail else str(exc)
    return str(exc)


def verify_routing(payload: dict, board: Path) -> None:
    """Accept only native DRC evidence bound to the exact final board bytes."""
    drc = payload.get("drc", {})
    require(payload.get("routing_complete") is True, "Routing is incomplete")
    require(payload.get("output_kind") == "routed_kicad_board", "Router returned no native board")
    require(drc.get("status") == "ok" and drc.get("passed") is True, "Native DRC did not pass")
    require(drc.get("blocker_count") == 0, "Native DRC has blockers")
    require(Path(drc.get("board", "")).resolve() == board.resolve(), "DRC refers to a different board")
    require(drc.get("board_sha256") == digest(board), "DRC does not match final board bytes")


def verify_refinement(payload: dict, board: Path, source: Path, expected_connections: int) -> None:
    """Bind optional return refinement evidence to both exact board images."""
    drc = payload.get("drc", {})
    require(payload.get("status") == "ok" and payload.get("output_kind") == "refined_kicad_board",
            "Return-path refinement produced no native board")
    require(payload.get("source_sha256") == digest(source), "Refinement refers to different source board bytes")
    require(drc.get("status") == "ok" and drc.get("passed") is True and drc.get("blocker_count") == 0,
            "Refined board did not pass full native DRC")
    require(Path(drc.get("board", "")).resolve() == board.resolve(), "Final DRC refers to a different board")
    require(drc.get("board_sha256") == digest(board), "Final DRC does not match final board bytes")
    refinement = payload.get("refinement", {})
    require(refinement.get("original_copper_preserved") is True,
            "Native refinement did not confirm original copper preservation")
    require(refinement.get("original_footprints_and_text_preserved") is True,
            "Native refinement did not confirm footprint and text preservation")
    connections = refinement.get("connections")
    require(isinstance(connections, list) and len(connections) == expected_connections,
            "Native refinement report does not cover every requested connection")
    require(all(isinstance(item, dict) and item.get("status") == "routed" for item in connections),
            "Native refinement report contains an unrouted connection")
    silkscreen = payload.get("silkscreen", {})
    require(silkscreen.get("unresolved_count") == 0 and silkscreen.get("remaining_conflict_count") == 0,
            "Refined board failed native silkscreen verification")


def verify_cleanup(payload: dict, board: Path, source: Path, expected_connections: int) -> None:
    """Bind native copper-cleanup evidence to its exact input and output images."""
    drc = payload.get("drc", {})
    require(payload.get("status") == "ok" and payload.get("output_kind") == "cleaned_kicad_board",
            "Copper cleanup produced no native board")
    require(payload.get("source_sha256") == digest(source), "Cleanup refers to different source board bytes")
    require(drc.get("status") == "ok" and drc.get("passed") is True and drc.get("blocker_count") == 0,
            "Cleaned board did not pass full native DRC")
    require(Path(drc.get("board", "")).resolve() == board.resolve(), "Final DRC refers to a different board")
    require(drc.get("board_sha256") == digest(board), "Final DRC does not match final board bytes")
    cleanup = payload.get("cleanup", {})
    for key in ("all_pads_connected", "protected_paths_preserved", "unselected_copper_preserved",
                "original_footprints_and_text_preserved", "copper_changes_limited_to_selected_nets",
                "locked_copper_preserved"):
        require(cleanup.get(key) is True, f"Native cleanup did not confirm {key.replace('_', ' ')}")
    rows = cleanup.get("protected_connections")
    require(isinstance(rows, list) and len(rows) == expected_connections,
            "Native cleanup report does not cover every protected connection")
    require(all(isinstance(item, dict) and item.get("status") == "connected"
                and item.get("protected_path_preserved") is True for item in rows),
            "Native cleanup report contains an unverified protected connection")
    silkscreen = payload.get("silkscreen", {})
    require(silkscreen.get("unresolved_count") == 0 and silkscreen.get("remaining_conflict_count") == 0,
            "Cleaned board failed native silkscreen verification")


def verify_ground_plane(payload: dict, board: Path, source: Path, plan: dict) -> None:
    """Require saved native fill and preserved routes for the exact final PCB."""
    require(payload.get("status") == "ok" and payload.get("output_kind") == "ground_plane_kicad_board",
            "Ground-plane operation produced no native board")
    require(payload.get("source_sha256") == digest(source),
            "Ground plane refers to different source board bytes")
    drc = payload.get("drc", {})
    require(drc.get("status") == "ok" and drc.get("passed") is True and drc.get("blocker_count") == 0,
            "Ground-plane board did not pass full native DRC")
    require(Path(drc.get("board", "")).resolve() == board.resolve(), "Final DRC refers to a different board")
    require(drc.get("board_sha256") == digest(board), "Final DRC does not match final board bytes")
    plane = payload.get("ground_plane", {})
    for key in ("tracks_preserved", "original_footprints_and_text_preserved",
                "original_board_geometry_preserved", "saved_fill_geometry_verified",
                "saved_connectivity_verified"):
        require(plane.get(key) is True, f"Native ground plane did not confirm {key.replace('_', ' ')}")
    require(plane.get("net") == plan["net"] and plane.get("layers") == plan["layers"],
            "Ground-plane net or layers disagree with the input plan")
    require(plane.get("added_zones") == len(plan["layers"])
            and plane.get("filled_zones") == len(plan["layers"]), "Ground plane has missing or unfilled zones")
    area = plane.get("filled_area_mm2")
    require(isinstance(area, (int, float)) and not isinstance(area, bool)
            and math.isfinite(area) and area > 0,
            "Ground plane has no native filled copper area")
    stats = plane.get("zone_stats")
    require(isinstance(stats, list) and len(stats) == len(plan["layers"])
            and all(isinstance(row, dict) and isinstance(row.get("polygon_count"), int)
                    and not isinstance(row["polygon_count"], bool) and row["polygon_count"] > 0
                    and row.get("island_count") == 0 and isinstance(row.get("polygon_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", row["polygon_sha256"])
                    for row in stats), "Ground plane lacks verified filled polygon inventory")
    anchors = plane.get("zone_connected_copper_counts")
    require(isinstance(anchors, list) and len(anchors) == len(plan["layers"])
            and all(isinstance(count, int) and not isinstance(count, bool) and count > 0 for count in anchors),
            "Ground plane lacks native copper connectivity")
    require(plane.get("island_count") == 0, "Ground plane contains unconnected copper islands")
    rows = plane.get("protected_connections")
    require(isinstance(rows, list) and len(rows) == len(plan["protected_connections"]),
            "Native ground-plane report does not cover every protected connection")
    endpoints = ("from_ref", "from_pad", "to_ref", "to_pad")
    require(all(isinstance(item, dict) and item.get("status") == "preserved"
                and all(item.get(key) == connection[key] for key in endpoints)
                for item, connection in zip(rows, plan["protected_connections"])),
            "Native ground-plane report contains an unverified protected connection")
    silkscreen = payload.get("silkscreen", {})
    require(silkscreen.get("unresolved_count") == 0 and silkscreen.get("remaining_conflict_count") == 0,
            "Ground-plane board failed native silkscreen verification")


def verify_plane_cleanup(payload: dict, board: Path, source: Path, plan: dict) -> None:
    """Bind deletion-only plane cleanup and its native checks to exact PCB bytes."""
    require(payload.get("status") == "ok" and payload.get("output_kind") == "plane_cleaned_kicad_board",
            "Plane cleanup produced no native board")
    require(payload.get("source_sha256") == digest(source), "Plane cleanup refers to different source board bytes")
    drc = payload.get("drc", {})
    require(drc.get("status") == "ok" and drc.get("passed") is True and drc.get("blocker_count") == 0,
            "Plane-cleaned board did not pass full native DRC")
    require(Path(drc.get("board", "")).resolve() == board.resolve(), "Final DRC refers to a different board")
    require(drc.get("board_sha256") == digest(board), "Final DRC does not match final board bytes")
    report = payload.get("plane_cleanup", {})
    for key in ("existing_filled_planes_verified", "only_covered_unlocked_traces_removed", "no_copper_added",
                "vias_preserved", "nonselected_copper_preserved", "locked_copper_preserved",
                "original_geometry_preserved", "original_board_geometry_preserved",
                "saved_fill_geometry_verified", "saved_coverage_verified",
                "saved_connectivity_verified", "protected_paths_preserved"):
        require(report.get(key) is True, f"Native plane cleanup did not confirm {key.replace('_', ' ')}")
    require(report.get("net") == plan["net"], "Plane cleanup net disagrees with the input plan")
    removed, before, after = (report.get(key) for key in ("removed_count", "track_count_before", "track_count_after"))
    require(all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in (removed, before, after)) and before - after == removed
            and isinstance(report.get("removed_tracks"), list) and len(report["removed_tracks"]) == removed,
            "Plane cleanup lacks a consistent removed-track inventory")
    rows = report.get("protected_connections")
    require(isinstance(rows, list) and len(rows) == len(plan["protected_connections"]),
            "Plane cleanup does not cover every protected connection")
    require(all(isinstance(row, dict) and row.get("status") == "preserved"
                and row.get("protected_path_preserved") is True
                and all(row.get(key) == pair[key] for key in ("from_ref", "from_pad", "to_ref", "to_pad"))
                for row, pair in zip(rows, plan["protected_connections"])),
            "Plane cleanup contains an unverified protected connection")
    silkscreen = payload.get("silkscreen", {})
    require(silkscreen.get("unresolved_count") == 0 and silkscreen.get("remaining_conflict_count") == 0,
            "Plane-cleaned board failed native silkscreen verification")


def verify_preview(payload: dict, board: Path, svg: Path, side: str, appearance: str = "layers") -> None:
    """Bind a native preview to the exact routed board and SVG bytes."""
    require(payload.get("status") == "ok", "Native board preview failed")
    require(payload.get("source_sha256") == digest(board), "Preview refers to different board bytes")
    require(payload.get("svg_sha256") == digest(svg), "Preview hash does not match SVG bytes")
    require(payload.get("side") == side, "Preview shows the wrong board side")
    require(payload.get("prototype") is True, "Preview omitted its review-only classification")
    root = ET.parse(svg).getroot()
    require(root.tag == "{http://www.w3.org/2000/svg}svg", "Preview is not an SVG document")
    if appearance == "assembly":
        require(payload.get("appearance") == "assembly", "Preview does not report assembly appearance")
        require(root.get("data-appearance") == "assembly", "SVG is not marked as an assembly preview")
        require(root.get("data-side") == side, "Assembly SVG shows the wrong board side")
        require(payload.get("model_preview", {}).get("status") == "ok", "Assembly model render failed")
        model = payload["model_preview"]
        require(model.get("source_sha256") == digest(board), "Model render refers to different board bytes")
        coverage = model.get("model_coverage")
        require(isinstance(coverage, dict), "Assembly preview omitted model coverage")
        expected = coverage.get("expected")
        resolved = coverage.get("resolved")
        missing = coverage.get("missing")
        require(isinstance(expected, int) and not isinstance(expected, bool) and expected > 0,
                "Assembly preview found no native 3D models")
        require(isinstance(resolved, int) and not isinstance(resolved, bool),
                "Assembly model resolution count is invalid")
        require(isinstance(missing, int) and not isinstance(missing, bool),
                "Assembly missing model count is invalid")
        require(resolved == expected and missing == 0, "Assembly preview has missing or unresolved models")
        models = coverage.get("models")
        require(isinstance(models, list) and len(models) == expected,
                "Assembly model coverage inventory is incomplete")
        require(all(isinstance(item, dict) and item.get("resolved_path") for item in models),
                "Assembly model inventory contains an unresolved model")
        for item in models:
            model_path = Path(item["resolved_path"])
            require(model_path.is_file(), "Assembly model source file is missing")
            require(item.get("sha256") == digest(model_path), "Assembly model source changed after rendering")
        components = payload.get("component_coverage")
        require(isinstance(components, dict), "Assembly preview omitted component coverage")
        component_expected = components.get("expected")
        component_represented = components.get("represented")
        component_missing = components.get("missing")
        require(isinstance(component_expected, int) and not isinstance(component_expected, bool)
                and component_expected > 0, "Assembly preview found no populated electrical components")
        require(isinstance(component_represented, int) and not isinstance(component_represented, bool)
                and isinstance(component_missing, int) and not isinstance(component_missing, bool),
                "Assembly component coverage counts are invalid")
        require(component_represented == component_expected and component_missing == 0,
                "Assembly preview omits one or more populated electrical components")
        require(components.get("missing_references") == [],
                "Assembly preview has components without visible native models")
        component_records = components.get("components")
        require(isinstance(component_records, list), "Assembly component coverage inventory is missing")
        references = [str(item.get("reference", "")).strip() for item in component_records
                      if isinstance(item, dict)]
        require(len(references) == len(component_records) and all(references),
                "Assembly component coverage inventory is malformed")
        require(len(references) == len(set(references)), "Assembly component coverage has duplicate references")
        required_components = [item for item in component_records if item.get("required") is True]
        require(len(required_components) == component_expected,
                "Assembly required component inventory disagrees with expected count")
        require(all(isinstance(item.get("visible_model_count"), int)
                    and not isinstance(item.get("visible_model_count"), bool)
                    and item["visible_model_count"] > 0 for item in required_components),
                "Assembly preview omits one or more populated electrical components")
        require(all(item.get("required") is True or item.get("required") is False
                    for item in component_records), "Assembly component required flags are invalid")

        image_nodes = [node for node in root.iter() if node.tag == "{http://www.w3.org/2000/svg}image"]
        require(len(image_nodes) == 1 and image_nodes[0].get("id") == "assembled-board-models",
                "Assembly SVG must contain exactly one embedded model image")
        image_href = image_nodes[0].get("href") or image_nodes[0].get("{http://www.w3.org/1999/xlink}href")
        require(isinstance(image_href, str) and image_href.startswith("data:image/png;base64,"),
                "Assembly model image is not embedded as a PNG data URL")
        xlink_href = image_nodes[0].get("{http://www.w3.org/1999/xlink}href")
        require(xlink_href is None or xlink_href == image_href,
                "Assembly SVG has conflicting SVG2 and xlink image references")
        try:
            png_bytes = base64.b64decode(image_href.partition(",")[2], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("Assembly model image contains invalid base64") from exc
        require(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"), "Embedded assembly image is not a PNG")
        offset = 8
        width = height = None
        seen_idat = seen_iend = False
        while offset < len(png_bytes):
            require(len(png_bytes) - offset >= 12, "Embedded assembly PNG has a truncated chunk")
            length = struct.unpack_from(">I", png_bytes, offset)[0]
            end = offset + 8 + length
            require(end + 4 <= len(png_bytes), "Embedded assembly PNG has a truncated chunk")
            chunk_type = png_bytes[offset + 4:offset + 8]
            chunk_data = png_bytes[offset + 8:end]
            crc = struct.unpack_from(">I", png_bytes, end)[0]
            require((zlib.crc32(chunk_type + chunk_data) & 0xffffffff) == crc,
                    "Embedded assembly PNG has an invalid chunk checksum")
            if offset == 8:
                require(chunk_type == b"IHDR" and length == 13,
                        "Embedded assembly PNG has an invalid header")
                width, height = struct.unpack_from(">II", chunk_data)
            if chunk_type == b"IDAT":
                seen_idat = True
            if chunk_type == b"IEND":
                require(length == 0 and end + 4 == len(png_bytes),
                        "Embedded assembly PNG has invalid trailing data")
                seen_iend = True
                break
            offset = end + 4
        require(seen_idat and seen_iend, "Embedded assembly PNG is incomplete")
        require(width is not None and height is not None, "Embedded assembly PNG has no dimensions")
        require(width > 0 and height > 0, "Embedded assembly PNG has invalid dimensions")
        png_hash = hashlib.sha256(png_bytes).hexdigest()
        require(payload.get("embedded_png_sha256") == png_hash,
                "Embedded assembly PNG hash does not match renderer evidence")
        require(model.get("png_sha256") == png_hash, "Model render hash does not match embedded PNG bytes")
        require(model.get("png_width") == width and model.get("png_height") == height,
                "Model render dimensions do not match embedded PNG bytes")
        require(payload.get("png_width") == width and payload.get("png_height") == height,
                "Assembly SVG dimensions do not match embedded PNG bytes")
        expected_model_side = "top" if side == "front" else "bottom"
        require(model.get("side") == expected_model_side, "Model render shows the wrong board side")
        require(root.get("width") == f"{width}px" and root.get("height") == f"{height}px",
                "Assembly SVG frame dimensions do not match the embedded PNG")
        require(root.get("viewBox") == f"0 0 {width} {height}",
                "Assembly SVG viewBox does not match the embedded PNG")
        require(image_nodes[0].get("x") == "0" and image_nodes[0].get("y") == "0"
                and image_nodes[0].get("width") == str(width)
                and image_nodes[0].get("height") == str(height)
                and image_nodes[0].get("preserveAspectRatio", "xMidYMid meet").endswith("meet"),
                "Assembly image does not preserve the PNG aspect ratio in the SVG frame")
        metadata_nodes = [node for node in root.iter()
                          if node.tag == "{http://www.w3.org/2000/svg}metadata"]
        require(len(metadata_nodes) == 1, "Assembly SVG metadata is missing or duplicated")
        metadata = metadata_nodes[0]
        require(metadata.get("source_sha256") == digest(board), "Assembly SVG metadata refers to different board bytes")
        require(metadata.get("png_sha256") == png_hash, "Assembly SVG metadata PNG hash is stale")
        require(metadata.get("png_width") == str(width) and metadata.get("png_height") == str(height),
                "Assembly SVG metadata dimensions are stale")
        require(metadata.get("side") == side and metadata.get("appearance") == "assembly",
                "Assembly SVG metadata has the wrong view")
        require(metadata.get("model_expected") == str(expected)
                and metadata.get("model_resolved") == str(resolved)
                and metadata.get("model_missing") == str(missing),
                "Assembly SVG metadata model counts are stale")
        require(metadata.get("component_expected") == str(component_expected)
                and metadata.get("component_represented") == str(component_represented)
                and metadata.get("component_missing") == str(component_missing),
                "Assembly SVG metadata component counts are stale")
        require(payload.get("output_path") and Path(payload["output_path"]).resolve() == svg.resolve(),
                "Assembly preview points to a different SVG")
        for node in root.iter():
            for name, value in node.attrib.items():
                if name.rsplit("}", 1)[-1] in {"href", "src"}:
                    require(value.startswith("data:image/png;base64,") or value.startswith("#"),
                            "Assembly SVG contains an external resource reference")
        svg_text = svg.read_text(encoding="utf-8")
        require(not re.search(r"url\(\s*(?!['\"]?#)(?:https?:|file:|//|[^)]+)", svg_text, re.I),
                "Assembly SVG contains an external CSS resource reference")
    else:
        require(appearance == "layers", f"Unsupported preview appearance: {appearance}")
        require(payload.get("outline_count", 0) > 0, "Preview has no actual board outline")
        require(bool(payload.get("layers")), "Preview has no native layer inventory")
        model = payload.get("model_preview")
        if model is not None:
            require(model.get("status") == "ok", "Requested model preview failed")
            require(model.get("source_sha256") == digest(board), "Model render refers to different board bytes")
            png = Path(model.get("output_path", ""))
            require(png.is_file() and png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"),
                    "Model render is not a PNG")
            require(model.get("png_sha256") == digest(png), "Model render hash does not match PNG bytes")


def verify_blocked_assembly_delivery(payload: dict, delivery_dir: Path) -> dict:
    """Verify blocked delivery still contains a complete, reconciled BOM."""
    require(payload.get("status") == "blocked", "Prototype assembly export was unexpectedly authorized")
    require(payload.get("fabrication_ready") is False, "Assembly export overclaimed fabrication readiness")

    bom_path = delivery_dir / "bom_jlcpcb.csv"
    assembly_path = delivery_dir / "assembly_manifest.json"
    delivery_path = delivery_dir / "delivery_manifest.json"
    for path in (bom_path, assembly_path, delivery_path):
        require(path.is_file(), f"Assembly delivery artifact is missing: {path.name}")

    assembly = json.loads(assembly_path.read_text(encoding="utf-8"))
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    items = assembly.get("items")
    require(isinstance(items, list), "Assembly manifest has no item inventory")
    references = [str(item.get("reference", "")).strip() for item in items if isinstance(item, dict)]
    require(len(references) == len(items), "Assembly manifest contains a malformed item")
    require(all(references), "Assembly manifest contains an empty reference")
    require(len(references) == len(set(references)), "Assembly manifest contains duplicate references")

    expected_refs = {
        str(item["reference"]).strip()
        for item in items
        if not item.get("dnp") and not item.get("exclude_from_bom")
    }
    require(assembly.get("item_count") == len(items), "Assembly manifest item count is inconsistent")
    require(payload.get("assembly_item_count") == len(items), "CLI assembly item count disagrees with its manifest")
    require(delivery.get("assembly_item_count") == len(items), "Delivery item count disagrees with assembly manifest")
    require(delivery.get("status") == "blocked", "Delivery manifest did not retain blocked status")
    require(delivery.get("fabrication_ready") is False, "Delivery manifest overclaimed fabrication readiness")

    artifacts = delivery.get("artifacts")
    require(isinstance(artifacts, list), "Delivery manifest has no artifact inventory")
    bom_artifacts = [item for item in artifacts if item.get("kind") == "bom"]
    require(len(bom_artifacts) == 1 and bom_artifacts[0].get("status") == "ready",
            "Delivery manifest does not mark exactly one BOM as ready")
    require(Path(bom_artifacts[0].get("path", "")).name == bom_path.name,
            "Delivery manifest points to a different BOM")
    cpl_artifacts = [item for item in artifacts if item.get("kind") == "cpl"]
    require(len(cpl_artifacts) == 1 and cpl_artifacts[0].get("status") == "blocked",
            "Delivery manifest did not preserve the blocked CPL gate")

    with bom_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        require(reader.fieldnames == ["Comment", "Designator", "Footprint", "LCSC Part#"],
                "JLCPCB BOM has an unexpected header")
        rows = list(reader)
    actual_refs: list[str] = []
    for row in rows:
        designators = [reference.strip() for reference in (row.get("Designator") or "").split(",")]
        require(all(designators), "JLCPCB BOM contains an empty designator")
        actual_refs.extend(designators)
    require(len(actual_refs) == len(set(actual_refs)), "JLCPCB BOM contains duplicate designators")
    require(set(actual_refs) == expected_refs,
            f"JLCPCB BOM designators disagree with active assembly inventory: "
            f"missing={sorted(expected_refs - set(actual_refs))}, "
            f"unexpected={sorted(set(actual_refs) - expected_refs)}")
    require(len(actual_refs) == len(expected_refs), "JLCPCB BOM designator count is inconsistent")
    require(payload.get("bom_rows") == len(rows), "CLI BOM row count disagrees with the CSV")
    return {"assembly_items": len(items), "bom_rows": len(rows), "bom_designators": len(actual_refs)}


class Demo:
    def __init__(self, output: Path, timeout: int, *, installed: bool = False):
        self.output = output
        self.timeout = timeout
        self.steps: list[dict] = []
        self.cwd = output if installed else REPO
        self.env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO / "src")}
        if installed:
            self.env.pop("PYTHONPATH", None)

    def command(self, name: str, arguments: list[str], *, json_output=True, exit_code=0) -> dict:
        command = [sys.executable, "-m", "circuit_weaver", *map(str, arguments)]
        return self.external(name, command, json_output=json_output, exit_code=exit_code)

    def external(self, name: str, command: list[str], *, json_output=False, exit_code=0) -> dict:
        print(f"[{name}] starting", flush=True)
        step = {"name": name, "command": command, "passed": False}
        self.steps.append(step)
        started = time.monotonic()
        try:
            process = subprocess.run(command, cwd=self.cwd, env=self.env, capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=self.timeout)
            step.update(exit_code=process.returncode, seconds=round(time.monotonic() - started, 2))
            (self.output / f"{name}.stdout.log").write_text(process.stdout, encoding="utf-8")
            (self.output / f"{name}.stderr.log").write_text(process.stderr, encoding="utf-8")
            expected_codes = {exit_code} if isinstance(exit_code, int) else set(exit_code)
            require(process.returncode in expected_codes,
                    f"{name}: exit {process.returncode}, expected {exit_code}; see stderr/stdout logs")
            payload = json.loads(process.stdout) if json_output else {}
            step["passed"] = True
            print(f"[{name}] passed ({step['seconds']}s)", flush=True)
            return payload
        except Exception as exc:
            step["error"] = str(exc)
            raise

    def report(self, summary: dict) -> None:
        summary["steps"] = self.steps
        (self.output / "evidence.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        rows = "".join(
            f"<tr><td>{html.escape(s['name'])}</td><td>{'PASS' if s['passed'] else 'FAIL'}</td>"
            f"<td>{s.get('seconds', '')}</td><td><a href='{s['name']}.stdout.log'>output</a> "
            f"<a href='{s['name']}.stderr.log'>diagnostics</a></td></tr>" for s in self.steps
        )
        preview_views = [
            ("assembly", "Assembly view", "board-preview.svg",
             "Native 3D component models embedded in a standalone SVG.")
            if summary.get("preview_appearance") == "assembly" else
            ("front", "Front layers", "board-preview.svg",
             "Actual copper, mask openings, silkscreen and footprint body outlines from KiCad."),
        ]
        if (self.output / "board-layers.svg").is_file():
            preview_views.append(("front-layers", "Front layers", "board-layers.svg",
                                  "Native copper, mask, silkscreen and footprint geometry from KiCad."))
        preview_views.append(("back", "Back layers", "board-preview-back.svg",
                              "Back-side native geometry, mirrored as viewed from beneath the board."))
        available = [view for view in preview_views if (self.output / view[2]).is_file()]
        buttons = "".join(
            f'<button type="button" data-board-view="{key}" aria-pressed="{str(i == 0).lower()}">{label}</button>'
            for i, (key, label, _, _) in enumerate(available)
        )
        panels = "".join(
            f'<figure id="board-view-{key}" class="board-view" {"" if i == 0 else "hidden"}>'
            f'<figcaption>{caption} <a href="{filename}" target="_blank" rel="noopener">Open full size</a></figcaption>'
            f'<img src="{filename}" alt="{label} of the routed prototype board"></figure>'
            for i, (key, label, filename, caption) in enumerate(available)
        )
        quality = summary.get("placement_quality", {})
        dimensions = summary.get("board_size_mm", [])
        layout_summary = (
            f'<p>{dimensions[0]:g} × {dimensions[1]:g} mm · '
            f'{len(quality.get("support_distances", []))} owner/support links · '
            f'{len(quality.get("overlaps", []))} courtyard overlaps · '
            f'{summary.get("silkscreen", {}).get("unresolved_count", "unchecked")} unresolved references</p>'
        ) if len(dimensions) == 2 else ""
        previews = (
            '<section aria-label="Board preview"><h2>Board preview</h2>'
            '<p>Engineering preview from the routed board. These views do not grant manufacturing approval.</p>'
            f'{layout_summary}<div class="board-controls" role="group" aria-label="Board view">'
            f'{buttons}</div>{panels}</section>'
        ) if available else ""
        schematic = "".join(
            f"<li><a href='{p.relative_to(self.output).as_posix()}'>{html.escape(p.name)}</a></li>"
            for p in sorted((self.output / "generated").rglob("*.svg"))
        )
        page = f"""<!doctype html><meta charset="utf-8"><title>Circuit Weaver workflow demo</title>
<style>body{{font:16px system-ui;background:#101827;color:#e3ecf7;margin:32px auto;max-width:1100px}}
a{{color:#79d3ff}}h1{{font-size:30px}}.badge{{color:#8ce8b0}}table{{width:100%;border-collapse:collapse}}
td,th{{padding:12px;border-bottom:1px solid #33445c;text-align:left}}
figure{{margin:24px 0}}code{{overflow-wrap:anywhere}}p{{line-height:1.6}}
.board-controls{{display:flex;gap:8px;flex-wrap:wrap}}button{{font:inherit;padding:10px 16px;border:1px solid #4a647c;
border-radius:8px;background:#17283b;color:#e3ecf7;cursor:pointer}}button[aria-pressed="true"]{{background:#276652}}
.board-view img{{display:block;width:100%;height:auto;max-height:78vh;object-fit:contain;background:#0b1220;
border-radius:10px}}figcaption{{margin:12px 0;color:#b9cbdc}}[hidden]{{display:none!important}}
details{{padding:14px 0;border-block:1px solid #33445c}}summary{{cursor:pointer;color:#79d3ff}}
@media(max-width:1164px){{body{{margin:20px}}}}</style>
<h1>Circuit Weaver · automated workflow</h1><p class="badge">{html.escape(summary['status'])}</p>
{previews}<details><summary>Workflow details and downloads</summary>
<p>Real KiCad schematic export, library footprints, placement, Freerouting, native DRC and delivery gates.
Manufacturing authorization remains <strong>not ready</strong>: identity evidence and placement approval
have not been supplied. A blocked manufacturing export is an expected successful gate check.</p>
<p>Source revision: <code>{html.escape(summary.get('revision', 'unknown'))}</code>.
<a href="evidence.json">Machine-readable evidence</a> · <a href="routed.kicad_pcb">Routed PCB</a> ·
<a href="generated/placement_editor.html">Placement review</a> ·
<a href="generated/confidence_report.html">Confidence report</a> ·
<a href="delivery/delivery_manifest.json">Delivery manifest</a> ·
<a href="prototype-positions.csv">Prototype positions (review only)</a></p></details>
<h2>Workflow evidence</h2>
<table><tr><th>Step</th><th>Check result</th><th>Seconds</th><th>Evidence</th></tr>{rows}</table>
<h2>Schematic sheets</h2><ul>{schematic}</ul>
<p>{html.escape(summary.get('error', ''))}</p>
<script>document.querySelectorAll('[data-board-view]').forEach(button => {{
button.addEventListener('click', () => {{
document.querySelectorAll('[data-board-view]').forEach(item => {{
item.setAttribute('aria-pressed', String(item === button)); }});
document.querySelectorAll('.board-view').forEach(panel => {{
panel.hidden = panel.id !== 'board-view-' + button.dataset.boardView; }});
}}); }});</script>"""
        (self.output / "index.html").write_text(page, encoding="utf-8")


def load_return_plan_in_runtime(path: Path, *, cwd: Path, env: dict[str, str]) -> dict:
    """Validate a plan with the same package environment used by the demo CLI."""
    code = (
        "import json, sys; "
        "from circuit_weaver.pcb_return_paths import load_return_plan; "
        "print(json.dumps(load_return_plan(sys.argv[1])))"
    )
    process = subprocess.run(
        [sys.executable, "-c", code, str(path)], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        raise ValueError(f"Return-path plan preflight failed: {detail}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Return-path plan preflight returned invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"connections"}:
        raise ValueError("Return-path plan preflight returned an invalid plan")
    return payload


def load_cleanup_plan_in_runtime(path: Path, *, cwd: Path, env: dict[str, str]) -> dict:
    """Validate a cleanup plan using the same package environment as the demo CLI."""
    code = (
        "import json, sys; "
        "from circuit_weaver.pcb_copper_cleanup import load_cleanup_plan; "
        "print(json.dumps(load_cleanup_plan(sys.argv[1])))"
    )
    process = subprocess.run(
        [sys.executable, "-c", code, str(path)], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        raise ValueError(f"Copper-cleanup plan preflight failed: {detail}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Copper-cleanup plan preflight returned invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"nets", "protected_connections"}:
        raise ValueError("Copper-cleanup plan preflight returned an invalid plan")
    return payload


def load_ground_plane_plan_in_runtime(path: Path, *, cwd: Path, env: dict[str, str]) -> dict:
    """Validate the zone plan in the same source or installed package as the CLI."""
    code = (
        "import json, sys; "
        "from circuit_weaver.pcb_ground_plane import load_ground_plane_plan; "
        "print(json.dumps(load_ground_plane_plan(sys.argv[1])))"
    )
    process = subprocess.run(
        [sys.executable, "-c", code, str(path)], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        raise ValueError(f"Ground-plane plan preflight failed: {detail}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Ground-plane plan preflight returned invalid JSON") from exc
    expected = {"net", "layers", "edge_clearance_mm", "clearance_mm", "min_thickness_mm",
                "thermal_gap_mm", "thermal_spoke_width_mm", "protected_connections"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("Ground-plane plan preflight returned an invalid plan")
    return payload


def load_plane_cleanup_plan_in_runtime(path: Path, *, cwd: Path, env: dict[str, str]) -> dict:
    """Validate the plane cleanup plan with the same package used by the demo CLI."""
    code = (
        "import json, sys; "
        "from circuit_weaver.pcb_plane_cleanup import load_plane_cleanup_plan; "
        "print(json.dumps(load_plane_cleanup_plan(sys.argv[1])))"
    )
    process = subprocess.run(
        [sys.executable, "-c", code, str(path)], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if process.returncode != 0:
        raise ValueError(f"Plane-cleanup plan preflight failed: {(process.stderr or process.stdout).strip()}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Plane-cleanup plan preflight returned invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"net", "protected_connections"}:
        raise ValueError("Plane-cleanup plan preflight returned an invalid plan")
    return payload


def validation_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def collect_mcp_validation_pages(session, spec_json: str) -> tuple[dict, list[dict]]:
    """Retrieve every finding and provenance ID, then verify the complete report."""
    transcript = []
    collected = {}
    header = None
    expected_digest = ""
    descriptors = None
    header_fields = None
    for collection, limit in (("findings", 10), ("evidence_ids", 100)):
        items = []
        offset = 0
        while True:
            arguments = {"spec_json": spec_json, "detail_level": "page", "collection": collection,
                         "offset": offset, "limit": limit, "expected_report_sha256": expected_digest}
            response = await session.call_tool("validate_design", arguments)
            require(not response.isError, f"MCP validation page failed: {response.content}")
            text = "\n".join(block.text for block in response.content if hasattr(block, "text"))
            require(len(text.encode("utf-8")) <= 20000, "MCP validation page exceeds the agent-output budget")
            page = json.loads(text)
            require(page.get("detail_level") == "page" and page.get("collection") == collection
                    and page.get("offset") == offset and page.get("limit") == limit,
                    "MCP validation page does not match the request")
            fixed_fields = {key: value for key, value in page.items()
                            if key not in {"collection", "offset", "limit", "items", "next_offset"}}
            if header is None:
                expected_digest = page.get("report_sha256")
                require(isinstance(expected_digest, str) and re.fullmatch(r"[0-9a-f]{64}", expected_digest),
                        "MCP validation page omits the report digest")
                descriptors = page.get("collections")
                require(isinstance(descriptors, dict) and set(descriptors) == {"findings", "evidence_ids"},
                        "MCP validation page omits collection descriptors")
                header = page
                header_fields = fixed_fields
            require(page.get("report_sha256") == expected_digest and page.get("collections") == descriptors,
                    "MCP validation report changed between pages")
            require(fixed_fields == header_fields, "MCP validation report header changed between pages")
            descriptor = descriptors[collection]
            total = descriptor.get("total")
            require(isinstance(total, int) and not isinstance(total, bool) and total >= 0,
                    "MCP validation collection has an invalid count")
            chunk = page.get("items")
            require(isinstance(chunk, list) and len(chunk) <= limit and offset + len(chunk) <= total,
                    "MCP validation page has an invalid item count")
            items.extend(chunk)
            next_offset = offset + len(chunk) if len(items) < total else None
            require(page.get("next_offset") == next_offset and (next_offset is None or next_offset > offset),
                    "MCP validation page skipped items or made no progress")
            transcript.append({"tool": "validate_design", "arguments": {key: value for key, value in arguments.items()
                                                                          if key != "spec_json"}, "result": page})
            if next_offset is None:
                break
            offset = next_offset
        require(len(items) == total and validation_digest(items) == descriptor.get("sha256"),
                "MCP validation collection is incomplete or has a mismatched digest")
        collected[collection] = items
    order = header.get("category_order")
    require(isinstance(order, list) and all(isinstance(name, str) for name in order)
            and len(set(order)) == len(order), "MCP validation category order is invalid")
    categories = {name: [] for name in order}
    for entry in collected["findings"]:
        require(isinstance(entry, dict) and set(entry) == {"category", "finding"}
                and entry["category"] in categories and isinstance(entry["finding"], dict),
                "MCP validation page contains a malformed finding")
        categories[entry["category"]].append(entry["finding"])
    paging_fields = {"detail_level", "report_sha256", "category_order", "collection", "offset", "limit",
                     "items", "next_offset", "collections"}
    report = {key: value for key, value in header.items() if key not in paging_fields}
    report.update(detail_level="summary", categories=categories, evidence_ids=collected["evidence_ids"])
    require(validation_digest(report) == expected_digest, "MCP reconstructed validation report has a mismatched digest")
    return report, transcript


async def mcp_smoke(spec: Path, output: Path, env: dict) -> dict:
    """Exercise the real JSON-RPC stdio server, including error propagation."""
    import yaml
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    transcript = []
    parameters = StdioServerParameters(command=sys.executable,
                                      args=["-m", "circuit_weaver.mcp_server"], cwd=str(output), env=env)
    with (output / "mcp.stderr.log").open("w", encoding="utf-8") as errors:
        async with stdio_client(parameters, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                inventory = await session.list_tools()
                names = sorted(tool.name for tool in inventory.tools)
                require(set(names) == {"generate_artifacts", "validate_design", "discover_projects",
                                       "research_component", "manufacturing_readiness"}, "MCP inventory mismatch")
                spec_json = json.dumps(yaml.safe_load(spec.read_text(encoding="utf-8")))
                validation_tool = next(tool for tool in inventory.tools if tool.name == "validate_design")
                paged_validation = "page" in validation_tool.inputSchema.get("properties", {}).get(
                    "detail_level", {}).get("enum", [])
                for name, arguments in (
                    ("validate_design", {"spec_json": spec_json}),
                    ("generate_artifacts", {"spec_json": spec_json, "output_dir": str(output / "mcp-generated")}),
                    ("discover_projects", {"root_dir": str(output), "depth": 2}),
                ):
                    if name == "validate_design" and paged_validation:
                        payload, pages = await collect_mcp_validation_pages(session, spec_json)
                        transcript.extend(pages)
                    else:
                        response = await session.call_tool(name, arguments)
                        require(not response.isError, f"MCP {name} failed: {response.content}")
                        payload = json.loads("\n".join(b.text for b in response.content if hasattr(b, "text")))
                        transcript.append({"tool": name, "result": payload})
                    if name in {"validate_design", "generate_artifacts"}:
                        require(payload.get("valid") is True, f"MCP {name} returned invalid design")
                    if name == "validate_design" and not paged_validation:
                        require(len(json.dumps(payload).encode("utf-8")) <= 20000,
                                "MCP validation response exceeds the workflow's tested agent-output budget")
                    if name == "generate_artifacts":
                        require(payload.get("kicad_verified") is True, "MCP generation skipped native KiCad")
                bad = await session.call_tool("validate_design", {"spec_json": "{invalid"})
                require(bad.isError is True, "MCP invalid input did not set isError")
                research = await session.call_tool("research_component", {"query": ""})
                require(research.isError is True, "MCP empty research query did not set isError")
                readiness = await session.call_tool("manufacturing_readiness", {
                    "readiness_path": str(output / "missing-readiness.json")
                })
                require(readiness.isError is True, "MCP missing readiness was treated as manufacturing approval")
    result = {"tools": names, "calls": transcript, "invalid_input_is_error": True,
              "validation_paging_verified": paged_validation,
              "empty_research_is_error": True, "missing_readiness_is_error": True}
    (output / "mcp-transcript.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=REPO / "samples/led_power_indicator/led_power_indicator.yaml")
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory; existing output is refused")
    parser.add_argument("--iterations", type=int, default=1500)
    parser.add_argument("--width", type=float, help="Prototype board width in mm; defaults to design constraint or 80")
    parser.add_argument("--height", type=float,
                        help="Prototype board height in mm; defaults to design constraint or 60")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--return-paths", type=Path,
                        help="Opt in to local return-path shortcuts using a strict JSON connections plan")
    parser.add_argument("--cleanup-plan", type=Path,
                        help="Opt in to native copper cleanup using a strict nets/protected-connections plan")
    parser.add_argument("--ground-plane", type=Path,
                        help="Opt in to native filled ground zones using a strict JSON plane plan")
    parser.add_argument("--plane-cleanup", type=Path,
                        help="Remove redundant plane-covered traces after --ground-plane using a strict JSON plan")
    parser.add_argument("--route-timeout", type=int, default=180)
    parser.add_argument("--clearance", type=float, default=0.127,
                        help="Prototype default clearance in mm (0.127 accommodates the sample USB connector)")
    parser.add_argument("--minimum-drill", type=float,
                        help="Explicit new-project minimum drill in mm; existing rules cannot be overridden")
    parser.add_argument("--mcp-smoke", action="store_true")
    parser.add_argument("--render-3d", action="store_true", help="Require a native assembly-model PNG (KiCad 9+)")
    parser.add_argument("--installed", action="store_true",
                        help="Test this Python's installed package, not checkout src/")
    parser.add_argument("--expected-wheel", type=Path,
                        help="Require the installed package payload to exactly match this wheel")
    parser.add_argument("--native-contracts", type=Path,
                        help="Snapshotted channel/path pad-net contracts to check before placement")
    parser.add_argument("--native-contracts-sha256", help="Expected SHA-256 for --native-contracts")
    args = parser.parse_args()
    if bool(args.native_contracts) != bool(args.native_contracts_sha256):
        parser.error("--native-contracts and --native-contracts-sha256 must be provided together")
    if args.native_contracts is not None and not args.native_contracts.is_file():
        parser.error("--native-contracts does not exist")
    if args.native_contracts is not None and digest(args.native_contracts.resolve()) != args.native_contracts_sha256:
        parser.error("--native-contracts hash does not match --native-contracts-sha256")
    if args.plane_cleanup is not None and args.ground_plane is None:
        parser.error("--plane-cleanup requires --ground-plane in the generated-board workflow")
    for name, value in (("--width", args.width), ("--height", args.height)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"{name} must be a positive finite number")
    if args.expected_wheel is not None and not args.installed:
        parser.error("--expected-wheel requires --installed")
    if args.expected_wheel is not None and not args.expected_wheel.is_file():
        parser.error(f"--expected-wheel does not exist: {args.expected_wheel}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    contract_snapshot = None
    if args.native_contracts is not None:
        contract_snapshot = output / "native-contracts.json"
        shutil.copy2(args.native_contracts.resolve(), contract_snapshot)
        require(
            digest(contract_snapshot) == args.native_contracts_sha256,
            "Copied native contract snapshot does not match the runner hash",
        )
    source_spec = args.spec.resolve()
    spec = output / "design.yaml"
    demo = Demo(output, max(300, args.route_timeout + 120), installed=args.installed)
    summary = {"status": "FAILED", "spec": str(source_spec), "fabrication_ready": False}
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True)
        summary["revision"] = revision.stdout.strip()
        changes = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                                 capture_output=True, text=True, check=True)
        summary["source_dirty"] = bool(changes.stdout.strip())
        package_probe = subprocess.run(
            [sys.executable, "-c", "import circuit_weaver; print(circuit_weaver.__file__)"],
            cwd=demo.cwd, env=demo.env, capture_output=True, text=True, check=True,
        )
        package_root = Path(package_probe.stdout.strip()).parent
        require(not args.installed or not package_root.resolve().is_relative_to(REPO / "src"),
                "Installed-mode probe imported checkout source")
        summary["package_path"] = str(package_root)
        summary["package_mode"] = "installed" if args.installed else "source"
        if args.expected_wheel is not None:
            summary["wheel_payload_verification"] = verify_installed_wheel_payload(
                package_root, args.expected_wheel.resolve()
            )
        source_files = [p for p in package_root.rglob("*")
                        if p.suffix in {".py", ".json", ".step", ".md", ".yaml"}]
        package_hash = hashlib.sha256()
        for path in sorted(source_files):
            package_hash.update(f"{path.relative_to(package_root).as_posix()}:{digest(path)}\n".encode())
        summary["package_source_sha256"] = package_hash.hexdigest()
        summary["runner_sha256"] = digest(Path(__file__))
        initialize_workflow_project(output, cwd=demo.cwd, env=demo.env)
        summary["project_state_root"] = str(output)
        original = digest(source_spec)
        return_plan_source = args.return_paths.resolve() if args.return_paths else None
        return_plan_hash = digest(return_plan_source) if return_plan_source else None
        cleanup_plan_source = args.cleanup_plan.resolve() if args.cleanup_plan else None
        cleanup_plan_hash = digest(cleanup_plan_source) if cleanup_plan_source else None
        ground_plan_source = args.ground_plane.resolve() if args.ground_plane else None
        ground_plan_hash = digest(ground_plan_source) if ground_plan_source else None
        plane_cleanup_source = args.plane_cleanup.resolve() if args.plane_cleanup else None
        plane_cleanup_hash = digest(plane_cleanup_source) if plane_cleanup_source else None
        if return_plan_source is not None:
            try:
                return_plan = load_return_plan_in_runtime(
                    return_plan_source, cwd=demo.cwd, env=demo.env,
                )
            except (OSError, ValueError) as exc:
                raise ValueError(f"invalid --return-paths plan: {exc}") from exc
        else:
            return_plan = None
        if cleanup_plan_source is not None:
            try:
                cleanup_plan = load_cleanup_plan_in_runtime(
                    cleanup_plan_source, cwd=demo.cwd, env=demo.env,
                )
            except (OSError, ValueError) as exc:
                raise ValueError(f"invalid --cleanup-plan: {exc}") from exc
        else:
            cleanup_plan = None
        if ground_plan_source is not None:
            try:
                ground_plan = load_ground_plane_plan_in_runtime(
                    ground_plan_source, cwd=demo.cwd, env=demo.env,
                )
            except (OSError, ValueError) as exc:
                raise ValueError(f"invalid --ground-plane plan: {exc}") from exc
        else:
            ground_plan = None
        if plane_cleanup_source is not None:
            try:
                plane_cleanup_plan = load_plane_cleanup_plan_in_runtime(
                    plane_cleanup_source, cwd=demo.cwd, env=demo.env,
                )
            except (OSError, ValueError) as exc:
                raise ValueError(f"invalid --plane-cleanup plan: {exc}") from exc
        else:
            plane_cleanup_plan = None
        summary.update(prepare_working_spec(source_spec, spec, args.width, args.height))
        width, height = summary["board_size_effective_mm"]
        demo.command("doctor", ["doctor"], json_output=False)
        valid = demo.command("validate", ["validate", str(spec)])
        require(valid.get("valid") is True, "Static design validation failed")
        generated = demo.command("generate", ["generate", str(spec), "--require-kicad",
                                              "-o", str(output / "generated")])
        require(generated.get("kicad_verified") is True, "Generation skipped native KiCad verification")
        root = Path(generated["root_schematic"])
        demo.command("confidence", ["confidence", str(spec), "--run-sims", "--json",
                                    "-o", str(output / "generated/confidence_report.html")])
        build_args = ["build-pcb", str(root), "-o", str(output / "unplaced.kicad_pcb"),
                      "--clearance", str(args.clearance), "--width", str(width), "--height", str(height)]
        if args.minimum_drill is not None:
            build_args.extend(["--minimum-drill", str(args.minimum_drill)])
        built = demo.command("build", build_args)
        require(built.get("fabrication_ready") is False, "Prototype board incorrectly claims fabrication readiness")
        summary["prototype_clearance_mm"] = args.clearance
        summary["prototype_minimum_drill_mm"] = args.minimum_drill
        placement, preflight = place_after_native_contracts(
            demo,
            output / "unplaced.kicad_pcb",
            contract_snapshot,
            args.native_contracts_sha256 if contract_snapshot is not None else None,
            output,
            ["place-pcb", str(output / "unplaced.kicad_pcb"),
             "-o", str(output / "placed.kicad_pcb"),
             "--iterations", str(args.iterations), "--seed", "7",
             "--design", str(spec), "--tidy-references"],
        )
        if preflight is not None:
            summary["native_contract_preflight"] = preflight
        require(placement["placement"]["quality"]["review_required"] is False, "Placement constraints were violated")
        summary["layout_review"] = placement["placement"].get("layout_review", {})
        summary["placement_quality"] = placement["placement"]["quality"]
        summary["board_size_mm"] = [width, height]
        summary["silkscreen"] = placement["placement"].get("silkscreen", {})
        require(summary["silkscreen"].get("unresolved_count") == 0,
                "Native reference cleanup left unresolved collisions")
        require(summary["silkscreen"].get("remaining_conflict_count") == 0,
                "Native silkscreen conflict check did not pass")
        routed_base = output / ("routed-base.kicad_pcb" if any(
            plan is not None for plan in (return_plan, cleanup_plan, ground_plan))
                                else "routed.kicad_pcb")
        routing = demo.command("route", [
            "autoroute", str(output / "placed.kicad_pcb"), "--routed-board", str(routed_base),
            "--attempts", str(args.attempts), "--timeout", str(args.route_timeout),
            "--max-passes", "20", "--optimization-threads", "0",
        ])
        verify_routing(routing, routed_base)
        routed = routed_base
        if return_plan is not None:
            plan_copy = output / "return-paths.json"
            shutil.copy2(return_plan_source, plan_copy)
            require(digest(plan_copy) == return_plan_hash,
                    "Copied return-path plan does not match the input plan")
            refined = output / ("routed-refined.kicad_pcb" if cleanup_plan is not None or ground_plan is not None
                                else "routed.kicad_pcb")
            refinement = demo.command("refine-return-paths", [
                "refine-pcb", str(routed_base), "-o", str(refined), "--connections", str(plan_copy),
            ])
            verify_refinement(refinement, refined, routed_base, len(return_plan["connections"]))
            summary["return_path_plan"] = {"path": plan_copy.name, "source_sha256": return_plan_hash,
                                           "copied_sha256": digest(plan_copy)}
            require(digest(return_plan_source) == return_plan_hash, "Workflow modified the return-path plan")
            summary["return_path_refinement"] = refinement
            routed = refined
        if cleanup_plan is not None:
            cleanup_plan_copy = output / "copper-cleanup.json"
            shutil.copy2(cleanup_plan_source, cleanup_plan_copy)
            require(digest(cleanup_plan_copy) == cleanup_plan_hash,
                    "Copied copper-cleanup plan does not match the input plan")
            cleanup_source = routed
            cleanup_output = output / ("routed-cleaned.kicad_pcb" if ground_plan is not None
                                       else "routed.kicad_pcb")
            cleaned = demo.command("cleanup-copper", [
                "cleanup-pcb", str(routed), "-o", str(cleanup_output), "--plan", str(cleanup_plan_copy),
            ])
            verify_cleanup(cleaned, cleanup_output, routed, len(cleanup_plan["protected_connections"]))
            summary["copper_cleanup_plan"] = {"path": cleanup_plan_copy.name,
                                              "source_sha256": cleanup_plan_hash,
                                              "copied_sha256": digest(cleanup_plan_copy)}
            require(digest(cleanup_plan_source) == cleanup_plan_hash,
                    "Workflow modified the input copper-cleanup plan")
            summary["copper_cleanup"] = cleaned
            routed = cleanup_output
        if ground_plan is not None:
            ground_plan_copy = output / "ground-plane.json"
            shutil.copy2(ground_plan_source, ground_plan_copy)
            require(digest(ground_plan_copy) == ground_plan_hash,
                    "Copied ground-plane plan does not match the input plan")
            ground_source = routed
            ground_output = output / ("routed-grounded.kicad_pcb" if plane_cleanup_plan is not None
                                      else "routed.kicad_pcb")
            grounded = demo.command("ground-plane", [
                "plane-pcb", str(ground_source), "-o", str(ground_output), "--plan", str(ground_plan_copy),
            ])
            verify_ground_plane(grounded, ground_output, ground_source, ground_plan)
            summary["ground_plane_plan"] = {"path": ground_plan_copy.name,
                                           "source_sha256": ground_plan_hash,
                                           "copied_sha256": digest(ground_plan_copy)}
            require(digest(ground_plan_source) == ground_plan_hash,
                    "Workflow modified the input ground-plane plan")
            summary["ground_plane"] = grounded
            routed = ground_output
        if plane_cleanup_plan is not None:
            plane_cleanup_copy = output / "plane-cleanup.json"
            shutil.copy2(plane_cleanup_source, plane_cleanup_copy)
            require(digest(plane_cleanup_copy) == plane_cleanup_hash,
                    "Copied plane-cleanup plan does not match the input plan")
            plane_cleanup_board_source = routed
            plane_cleanup_output = output / "routed.kicad_pcb"
            plane_cleaned = demo.command("plane-cleanup", [
                "plane-cleanup-pcb", str(routed), "-o", str(plane_cleanup_output), "--plan", str(plane_cleanup_copy),
            ])
            verify_plane_cleanup(plane_cleaned, plane_cleanup_output, plane_cleanup_board_source, plane_cleanup_plan)
            summary["plane_cleanup_plan"] = {"path": plane_cleanup_copy.name, "source_sha256": plane_cleanup_hash,
                                             "copied_sha256": digest(plane_cleanup_copy)}
            require(digest(plane_cleanup_source) == plane_cleanup_hash, "Workflow modified the plane-cleanup plan")
            summary["plane_cleanup"] = plane_cleaned
            routed = plane_cleanup_output
        summary["routing_drc"] = routing["drc"]
        summary["drc"] = plane_cleaned["drc"] if plane_cleanup_plan is not None else (
            grounded["drc"] if ground_plan is not None else (
                cleaned["drc"] if cleanup_plan is not None else (
                    refinement["drc"] if return_plan is not None else routing["drc"]))
        )
        delivery = demo.command("assembly-gate", ["export-jlcpcb", str(spec), "--pcb", str(routed),
                                            "-o", str(output / "delivery")], exit_code=(0, 1))
        assembly_summary = verify_blocked_assembly_delivery(delivery, output / "delivery")
        reasons = delivery.get("blocked_reasons", [])
        require(bool(reasons) and all("identity handoff bundle" in r or "no LCSC part number" in r
                                     for r in reasons), f"Unexpected assembly blocker: {reasons}")
        require(not delivery.get("cpl") and not list((output / "delivery").glob("cpl*.csv")),
                "Unapproved assembly export published a CPL")
        summary["assembly_delivery"] = {**delivery, "verified_inventory": assembly_summary}
        refused = demo.command("manufacturing-gate", ["export-gerbers", str(routed),
                                                     "-o", str(output / "manufacturing")], exit_code=1)
        require(refused.get("status") == "blocked", "Unapproved manufacturing export was not blocked")
        require(not (output / "manufacturing").exists(), "Blocked export created a manufacturing directory")
        status = demo.command("status", ["status", str(output / "generated"), "--json"])
        resumed = demo.command("resume", ["resume", str(output / "generated"), "--json"])
        require(status.get("has_manifest") is True and status.get("validation", {}).get("passed") is True,
                "Generation project state was not persisted")
        require(bool(status.get("project_id")) and resumed.get("project_id") == status["project_id"],
                "Resume did not restore the generated project")
        cli = shutil.which("kicad-cli") or next(
            (str(p) for version in ("10.0", "9.0", "8.0")
             if (p := Path(f"C:/Program Files/KiCad/{version}/bin/kicad-cli.exe")).is_file()), None
        )
        require(cli is not None, "KiCad CLI disappeared")
        demo.external("prototype-positions", [str(cli), "pcb", "export", "pos", "--format", "csv",
                                               "--units", "mm", "-o", str(output / "prototype-positions.csv"),
                                               str(routed)])
        summary["previews"] = {}
        summary["preview_appearance"] = "assembly" if args.render_3d else "layers"
        preview_jobs = [("front", "board-preview.svg", "assembly" if args.render_3d else "layers"),
                        ("back", "board-preview-back.svg", "layers")]
        if args.render_3d:
            preview_jobs.append(("front-layers", "board-layers.svg", "layers"))
        for key, filename, appearance in preview_jobs:
            preview = output / filename
            side = "back" if key == "back" else "front"
            payload = demo.command(f"board-preview-{key}", [
                "preview-pcb", str(routed), "-o", str(preview), "--side", side,
                "--kicad-cli-path", str(cli), "--appearance", appearance,
            ])
            verify_preview(payload, routed, preview, side, appearance)
            summary["previews"][key] = payload
        if args.mcp_smoke:
            step = {"name": "mcp", "passed": False}
            demo.steps.append(step)
            started = time.monotonic()
            result = asyncio.run(asyncio.wait_for(mcp_smoke(spec, output, demo.env), timeout=300))
            step.update(passed=True, seconds=round(time.monotonic() - started, 2))
            (output / "mcp.stdout.log").write_text(json.dumps(result, indent=2), encoding="utf-8")
        require(digest(source_spec) == original, "Workflow modified the input design")
        require(digest(spec) == summary["working_spec_sha256"],
                "Workflow modified the copied design specification")
        if return_plan_source is not None:
            require(digest(return_plan_source) == return_plan_hash,
                    "Workflow modified the input return-path plan")
            require(digest(plan_copy) == return_plan_hash,
                    "Copied return-path plan does not match the input plan")
        if cleanup_plan_source is not None:
            require(digest(cleanup_plan_source) == cleanup_plan_hash,
                    "Workflow modified the input copper-cleanup plan")
            require(digest(cleanup_plan_copy) == cleanup_plan_hash,
                    "Copied copper-cleanup plan does not match the input plan")
        if ground_plan_source is not None:
            require(digest(ground_plan_source) == ground_plan_hash,
                    "Workflow modified the input ground-plane plan")
            require(digest(ground_plan_copy) == ground_plan_hash,
                    "Copied ground-plane plan does not match the input plan")
        if plane_cleanup_source is not None:
            require(digest(plane_cleanup_source) == plane_cleanup_hash,
                    "Workflow modified the input plane-cleanup plan")
            require(digest(plane_cleanup_copy) == plane_cleanup_hash,
                    "Copied plane-cleanup plan does not match the input plan")
        verify_routing(routing, routed_base)
        if return_plan is not None:
            verify_refinement(refinement, refined, routed_base, len(return_plan["connections"]))
        if cleanup_plan is not None:
            verify_cleanup(cleaned, cleanup_output, cleanup_source, len(cleanup_plan["protected_connections"]))
        if ground_plan is not None:
            verify_ground_plane(grounded, ground_output, ground_source, ground_plan)
        if plane_cleanup_plan is not None:
            verify_plane_cleanup(plane_cleaned, plane_cleanup_output, plane_cleanup_board_source, plane_cleanup_plan)
        summary.update(status="PASS — prototype workflow validated; manufacturing export correctly blocked",
                       source_unchanged=True, board_sha256=digest(routed))
    except Exception as exc:
        summary["error"] = exception_message(exc)
        print(f"FAILED: {summary['error']}", file=sys.stderr)
    finally:
        demo.report(summary)
    print(f"Evidence: {output / 'index.html'}", flush=True)
    return 0 if summary["status"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
