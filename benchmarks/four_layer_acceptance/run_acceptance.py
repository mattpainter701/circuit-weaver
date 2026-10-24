#!/usr/bin/env python3
"""Serial, evidence-oriented acceptance runner for the four-layer corpus.

The safe default performs only spec compilation and independent contract checks.
Native workflow execution is opt-in and always records its narrower evidence
separately from electrical or product acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import os
import platform
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CORPUS = Path(__file__).resolve().parent / "corpus.py"
CONTROLS = Path(__file__).resolve().parent / "controls.py"
ASSET_CHECKS = Path(__file__).resolve().parent / "asset_checks.py"
PHASES = ("preflight", "validate", "generate", "build", "place", "route", "preview")
STATUSES = {"passed", "failed", "unverified", "resource_blocked", "not_run"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    )


def hash_file(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def load_cases(module_path: Path = CORPUS) -> list[dict[str, Any]]:
    if not module_path.is_file():
        raise FileNotFoundError(f"benchmark corpus module is missing: {module_path}")
    spec = importlib.util.spec_from_file_location("four_layer_acceptance_cases", module_path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load benchmark corpus module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, "build_cases", None)
    if not callable(factory):
        raise ValueError("corpus must expose build_cases()")
    cases = factory()
    if not isinstance(cases, list) or not cases:
        raise ValueError("build_cases() must return a nonempty list")
    ids = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("each case must be a dictionary")
        case_id = case.get("case_id")
        if (
            not isinstance(case_id, str)
            or not case_id.strip()
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in case_id)
        ):
            raise ValueError(f"invalid case_id: {case_id!r}")
        if not isinstance(case.get("spec"), dict):
            raise ValueError(f"case {case_id} must contain a spec object")
        ids.append(case_id)
    if len(ids) != len(set(ids)):
        raise ValueError("case_id values must be unique")
    return cases


def _component_nets(component: Any) -> dict[str, str]:
    nets = dict(getattr(component, "power_pins", {}) or {})
    nets.update(getattr(component, "pin_nets", {}) or {})
    return {str(pin): str(net) for pin, net in nets.items() if net}


def _footprint_asset_checks(physical_items: list[Any], project_root: str | Path | None) -> dict[str, Any]:
    if not ASSET_CHECKS.is_file():
        return {
            "status": "unverified",
            "scope": "local KiCad footprint file existence and SHA-256 only",
            "roots": [],
            "project_root": str(project_root) if project_root else None,
            "checks": [],
            "reason": "footprint asset checker is unavailable",
        }
    spec = importlib.util.spec_from_file_location("four_layer_acceptance_asset_checks", ASSET_CHECKS)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load footprint asset checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    check = getattr(module, "check_footprint_assets", None)
    if not callable(check):
        raise ValueError("footprint asset checker does not expose check_footprint_assets")
    result = check(physical_items, project_root=project_root)
    if not isinstance(result, dict) or not isinstance(result.get("checks"), list):
        raise ValueError("footprint asset checker returned malformed evidence")
    return result


def _preflight(case: dict[str, Any]) -> dict[str, Any]:
    """Compile spec and compare declarations with compiled connectivity."""
    from circuit_weaver.assembly_manifest import build_assembly_manifest
    from circuit_weaver.design_loader import compile_design_ir

    compiled = compile_design_ir(case["spec"], enrich_parts=False, use_kicad=False)
    copper_layers = case.get("copper_layers")
    ground_plane = case.get("ground_plane")
    valid_ground_layers = {"In1.Cu", "In2.Cu"}
    config_checks = [
        {
            "check": "stackup",
            "actual": copper_layers,
            "status": "passed" if copper_layers == 4 and type(copper_layers) is int else "failed",
        },
        {
            "check": "inner_ground_plane_declaration",
            "actual": ground_plane.get("layers") if isinstance(ground_plane, dict) else None,
            "status": "passed"
            if isinstance(ground_plane, dict)
            and isinstance(ground_plane.get("layers"), list)
            and bool(ground_plane["layers"])
            and set(ground_plane["layers"]) <= valid_ground_layers
            and ground_plane.get("net") == "GND"
            else "failed",
        },
    ]
    components = list(compiled.components)
    manifest = build_assembly_manifest(components)
    physical_items = [item for item in manifest.items if not item.dnp]
    physical_components = list(manifest.prepared_components)
    footprint_assets = _footprint_asset_checks(physical_items, case.get("project_root"))
    comp_by_ref: dict[str, Any] = {}
    duplicate_refs = []
    for comp in physical_components:
        ref = str(getattr(comp, "source_ref", "") or getattr(comp, "ref", ""))
        if ref and ref in comp_by_ref:
            duplicate_refs.append(ref)
        if ref:
            comp_by_ref[ref] = comp
    nets_to_pads: dict[str, list[dict[str, str]]] = {}
    for comp in physical_components:
        ref = str(getattr(comp, "source_ref", "") or getattr(comp, "ref", ""))
        for pin, net in _component_nets(comp).items():
            nets_to_pads.setdefault(net, []).append({"ref": ref, "pad": pin})
    # Generated series straps and bypass capacitors are separate physical
    # manifest items. Include their authored two-terminal net mapping so the
    # contracts follow signal paths through filters rather than treating the
    # primary IC's raw output as the filtered output.
    for item in manifest.items:
        if item.dnp:
            continue
        if item.source_kind == "strap":
            support_pads = (("1", item.net1), ("2", item.net2))
        elif item.source_kind == "bypass":
            # The generated capacitor convention used by the corpus maps the
            # signal side to pad 2 and return side to pad 1.
            support_pads = (("2", item.net1), ("1", item.net2))
        else:
            continue
        for pad, net in support_pads:
            if net:
                nets_to_pads.setdefault(str(net), []).append({"ref": item.reference, "pad": pad})
    expected = case.get("expectations") if isinstance(case.get("expectations"), dict) else {}
    required_nets = expected.get("required_nets", [])
    required_endpoints = expected.get("required_endpoints", [])
    required_components = expected.get("required_components", [])
    checks = list(config_checks)
    checks.extend(row for row in footprint_assets.get("checks", []) if isinstance(row, dict))
    for net in required_nets if isinstance(required_nets, list) else []:
        present = net in nets_to_pads and len(nets_to_pads[net]) >= 2
        checks.append(
            {
                "check": "required_net_connectivity",
                "target": net,
                "status": "passed" if present else "failed",
                "endpoint_count": len(nets_to_pads.get(net, [])),
            }
        )
    for endpoint in required_endpoints if isinstance(required_endpoints, list) else []:
        if not isinstance(endpoint, dict):
            checks.append({"check": "required_endpoint", "status": "failed", "reason": "malformed expectation"})
            continue
        ref, pad, net = (str(endpoint.get(k, "")) for k in ("ref", "pad", "net"))
        comp = comp_by_ref.get(ref)
        actual = (
            _component_nets(comp).get(pad)
            if comp is not None
            else next(
                (
                    candidate_net
                    for candidate_net, rows in nets_to_pads.items()
                    if any(row.get("ref") == ref and row.get("pad") == pad for row in rows)
                ),
                None,
            )
        )
        ok = actual == net
        checks.append(
            {
                "check": "required_endpoint",
                "target": f"{ref}.{pad}",
                "expected_net": net,
                "actual_net": actual,
                "status": "passed" if ok else "failed",
            }
        )
    for expectation in required_components if isinstance(required_components, list) else []:
        ref = str(expectation.get("ref", "")) if isinstance(expectation, dict) else ""
        comp = comp_by_ref.get(ref)
        expected_ic = str(expectation.get("ic", "")) if isinstance(expectation, dict) else ""
        actual_ic = (
            str(
                (
                    getattr(comp, "ic", "")
                    or getattr(comp, "source_mpn", "")
                    or getattr(comp, "mpn", "")
                    or getattr(comp, "value", "")
                )
            )
            if comp is not None
            else ""
        )
        ok = comp is not None and (not expected_ic or actual_ic.casefold() == expected_ic.casefold())
        checks.append(
            {
                "check": "required_component",
                "target": ref,
                "expected_ic": expected_ic,
                "actual_ic": actual_ic or None,
                "status": "passed" if ok else "failed",
            }
        )
    required_supports = expected.get("required_supports", [])
    for expectation in required_supports if isinstance(required_supports, list) else []:
        if not isinstance(expectation, dict):
            checks.append({"check": "required_support", "status": "failed", "reason": "malformed expectation"})
            continue
        signature = {key: expectation.get(key) for key in ("owner_ref", "role", "source_kind", "net1", "net2")}
        matches = [
            item for item in manifest.items if all(getattr(item, key) == value for key, value in signature.items())
        ]
        checks.append(
            {
                "check": "required_support",
                "target": signature,
                "status": "passed" if len(matches) == 1 else "failed",
                "match_count": len(matches),
                "resolved_reference": matches[0].reference if len(matches) == 1 else None,
            }
        )
    required_parts = expected.get("required_library_parts", [])
    resolved_part_ids = {
        str(getattr(comp, name, "") or "").casefold()
        for comp in components
        for name in ("ic", "mpn", "source_mpn", "value")
    }
    from circuit_weaver.primitives import qualify_footprint

    manifest_footprints = {
        qualify_footprint(str(getattr(item, "footprint", "") or "")).casefold()
        for item in physical_items
        if getattr(item, "footprint", "")
    }
    for part in required_parts if isinstance(required_parts, list) else []:
        part_text = str(part)
        part_key = part_text.casefold()
        matched_by = "compiled_part" if part_key in resolved_part_ids else None
        if matched_by is None and ":" in part_text and qualify_footprint(part_text).casefold() in manifest_footprints:
            matched_by = "manifest_footprint"
        present = matched_by is not None
        checks.append(
            {
                "check": "compiled_part_declaration",
                "target": part,
                "status": "passed" if present else "failed",
                "matched_by": matched_by,
            }
        )
    support_count = sum(item.source_kind in {"strap", "bypass"} and not item.dnp for item in manifest.items)
    compiled_pin_count = sum(len(getattr(comp, "pins", []) or []) for comp in physical_components) + support_count * 2
    area = None
    constraints = expected.get("board_constraints", {})
    if isinstance(constraints, dict):
        width = constraints.get("width_mm")
        height = constraints.get("height_mm")
        if (
            isinstance(width, (int, float))
            and not isinstance(width, bool)
            and isinstance(height, (int, float))
            and not isinstance(height, bool)
        ):
            area = float(width) * float(height)
    declared_max = 499
    if declared_max is not None:
        ok = len(physical_items) <= declared_max
        checks.append(
            {
                "check": "component_limit",
                "actual": len(physical_items),
                "limit": declared_max,
                "status": "passed" if ok else "failed",
            }
        )
    if duplicate_refs:
        checks.append({"check": "unique_references", "duplicates": sorted(set(duplicate_refs)), "status": "failed"})
    missing = sorted(set(required_nets if isinstance(required_nets, list) else []) - set(nets_to_pads))
    failed = any(row.get("status") == "failed" for row in checks)
    return {
        "status": "failed" if failed else "passed",
        "checks": checks,
        "metrics": {
            "component_count": len(physical_items),
            "pad_count": None,
            "compiled_pin_count": compiled_pin_count,
            "net_count": len(nets_to_pads),
            "native_board_net_count": None,
            "board_area_mm2": None,
            "declared_board_area_mm2": area,
        },
        "connectivity": {
            "compiled_net_count": len(nets_to_pads),
            "required_nets_missing": missing,
            "net_endpoint_counts": {net: len(rows) for net, rows in sorted(nets_to_pads.items())},
        },
        "assembly": {
            "manifest_item_count": len(physical_items),
            "footprint_missing_count": sum(not item.footprint for item in physical_items),
            "footprint_missing_refs": sorted(item.reference for item in physical_items if not item.footprint),
        },
        "footprint_asset_resolution": footprint_assets,
        "repair_actions": list(compiled.repair_actions),
        "library_footprint_and_native_model_resolution": "unverified",
        "required_library_declaration_count": len(required_parts) if isinstance(required_parts, list) else 0,
        "library_access_evidence": (
            "manifest footprint files resolved; symbol libraries and native models remain unverified"
            if footprint_assets.get("status") == "passed"
            else footprint_assets.get("reason") or "some local footprint assets are missing or unverified"
        ),
        "reason": (
            "footprint checks cover manifest assets only; symbol libraries, board pads, "
            "and native 3D models remain outside preflight scope"
        ),
    }


def _run_negative_controls(case: dict[str, Any]) -> list[dict[str, Any]]:
    declared = case.get("controls", [])
    if not declared:
        return []
    if not CONTROLS.is_file():
        return [
            {
                "control_id": item.get("control_id"),
                "kind": item.get("kind"),
                "expected_result": item.get("expected_result"),
                "status": "unverified",
                "reason": "negative-control executor is unavailable",
            }
            for item in declared
            if isinstance(item, dict)
        ]
    spec = importlib.util.spec_from_file_location("four_layer_acceptance_controls", CONTROLS)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load negative-control executor")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    execute = getattr(module, "run_negative_controls", None)
    if not callable(execute):
        raise ValueError("negative-control executor does not expose run_negative_controls")
    result = execute(case, _preflight)
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise ValueError("negative-control executor returned malformed evidence")
    return result


def _stage_outputs(project: Path, stage: str) -> list[Path]:
    names = {
        "validate": [],
        "generate": [],
        "build": ["prototype.kicad_pcb"],
        "place": ["placed.kicad_pcb"],
        "route": ["routed.kicad_pcb", "route-quality.json"],
        "preview": ["preview.svg"],
    }.get(stage, [])
    return [project / name for name in names]


def _workflow_case(case: dict[str, Any], outdir: Path, through: str) -> dict[str, Any]:
    from circuit_weaver import project_workflow

    project = outdir / case["case_id"] / "project"
    project.parent.mkdir(parents=True, exist_ok=True)
    created = project_workflow.start_project(project, case["spec"], name=case.get("title") or case["case_id"])
    if not created.get("ok"):
        raise RuntimeError(f"could not create fresh project: {created}")
    board = case.get("expectations", {}).get("board_constraints", {})
    width, height = board.get("width_mm"), board.get("height_mm")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in (width, height)):
        raise ValueError("full workflow requires explicit positive corpus board width_mm and height_mm")
    plane_raw = case.get("ground_plane") or {"net": "GND", "layers": ["In1.Cu"]}
    plane = {
        "net": plane_raw.get("net", "GND"),
        "layers": plane_raw.get("layers", ["In1.Cu"]),
        "protected_connections": [],
        "edge_clearance_mm": 0.5,
        "clearance_mm": 0.25,
        "min_thickness_mm": 0.2,
        "thermal_gap_mm": 0.25,
        "thermal_spoke_width_mm": 0.3,
    }
    profile = case.get("prototype_profile", {})
    if not isinstance(profile, dict):
        raise ValueError("prototype_profile must be an object")
    clearance = profile.get("clearance_mm", 0.127)
    minimum_drill = profile.get("minimum_drill_mm", 0.2)
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
        for value in (clearance, minimum_drill)
    ):
        raise ValueError("prototype_profile clearance_mm and minimum_drill_mm must be positive finite values")
    options = {
        "copper_layers": 4,
        "ground_plane": plane,
        "board_width_mm": width,
        "board_height_mm": height,
        "clearance_mm": clearance,
        "minimum_drill_mm": minimum_drill,
        "iterations": 750,
        "route_timeout": 600,
        "route_max_passes": 16,
        "route_attempts": 1,
        "route_optimization_threads": 0,
        "appearance": "assembly",
    }
    route_contracts = _route_contracts(case)
    if route_contracts:
        options["route_contracts"] = route_contracts
    stages = list(PHASES[1 : PHASES.index(through) + 1])
    plan = project_workflow.plan_workflow(project, stages=stages, options=options)
    phaserows = []
    if not plan.get("ok"):
        for stage in stages:
            phaserows.append(_phase(stage, "failed", inputs={}, outputs={}, detail=plan))
        return {"phases": phaserows, "project_dir": str(project)}
    run = project_workflow.run_workflow(project, stages=stages, options=options)
    stage_map = {}
    try:
        state = project_workflow.load_project_state(project)
        stage_map = (state.workflow.get("runner", {}).get("stages", {}) if state else {}) or {}
    except Exception:
        pass
    # Only count an individual phase complete when the workflow recorded it and
    # its outputs still exist. Helper process exit status is not route evidence.
    for stage in stages:
        checkpoint = stage_map.get(stage, {}) if isinstance(stage_map, dict) else {}
        output_paths = _stage_outputs(project, stage)
        recorded = checkpoint.get("outputs", []) if isinstance(checkpoint, dict) else []
        if recorded:
            output_hashes = {
                str(item.get("path")): hash_file(Path(item.get("path", "")))
                for item in recorded
                if isinstance(item, dict) and item.get("exists")
            }
        else:
            output_hashes = {str(p): hash_file(p) for p in output_paths}
        has_outputs = any(output_hashes.values()) or (
            stage in {"validate", "generate"} and checkpoint.get("status") == "completed"
        )
        if stage in {"validate", "generate"} and has_outputs and not output_hashes:
            output_hashes = {"workflow_checkpoint": canonical_hash(checkpoint)}
        status = "not_run" if not checkpoint else _phase_status(checkpoint, stage, output_hashes)
        stage_inputs = checkpoint.get("inputs", []) if isinstance(checkpoint, dict) else []
        input_hashes = {str(item.get("path")): item.get("sha256") for item in stage_inputs if isinstance(item, dict)}
        input_hashes.update({"spec_sha256": canonical_hash(case["spec"]), "options_sha256": canonical_hash(options)})
        detail = {
            "checkpoint_status": checkpoint.get("status"),
            "workflow_result": run,
            "checkpoint_result": checkpoint.get("result"),
            "error": checkpoint.get("error"),
        }
        phaserows.append(_phase(stage, status, inputs=input_hashes, outputs=output_hashes, detail=detail))
    results = {stage: (row.get("result") if isinstance(row, dict) else None) for stage, row in stage_map.items()}
    preview = results.get("preview") if isinstance(results.get("preview"), dict) else {}
    route_result = results.get("route") if isinstance(results.get("route"), dict) else {}
    plane_result = route_result.get("ground_plane") if isinstance(route_result, dict) else None
    plane_after = plane_result.get("after_route") if isinstance(plane_result, dict) else None
    stackup_result = next(
        (
            value.get("copper_layers")
            for value in (results.get("route"), results.get("place"), results.get("build"))
            if isinstance(value, dict) and isinstance(value.get("copper_layers"), dict)
        ),
        None,
    )
    plane_continuity, ground_pads = _ground_plane_evidence(plane_after, plane)
    routed_board_hash = hash_file(project / "routed.kicad_pcb")
    drc_proof = _native_drc_evidence(route_result, routed_board_hash)
    gates = {
        "native_drc": "passed" if drc_proof.get("status") == "passed" else "unverified",
        "route_connectivity_contracts": "passed"
        if _route_quality_complete(route_result.get("route_quality"))
        else "unverified",
        "four_layer_stackup": "passed" if _four_layer_stackup_complete(stackup_result) else "unverified",
        "ground_plane_continuity": "passed" if plane_continuity else "unverified",
        "ground_pad_connections": "passed" if ground_pads else "unverified",
        "modeled_assembly": "passed" if _coverage_complete(preview) else "unverified",
    }
    execution_status = (
        "passed"
        if run.get("status") == "completed"
        else "failed"
        if any(phase.get("status") == "failed" for phase in phaserows)
        else "resource_blocked"
        if any(phase.get("status") == "resource_blocked" for phase in phaserows)
        else "unverified"
    )
    return {
        "phases": phaserows,
        "project_dir": str(project),
        "gates": gates,
        "workflow_execution_status": execution_status,
        "gate_evidence": {
            "native_drc": drc_proof,
            "four_layer_stackup": stackup_result,
            "route_connectivity": route_result.get("route_quality"),
            "ground_plane": plane_after,
            "modeled_assembly": {
                "component_coverage": preview.get("component_coverage"),
                "model_coverage": preview.get("model_coverage"),
            },
        },
        "native_preview_coverage": {
            "component_coverage": preview.get("component_coverage"),
            "model_coverage": preview.get("model_coverage"),
        },
        "native_metrics": {
            "pad_count": None,
            "native_board_net_count": None,
            "board_area_mm2": None,
            "reason": "workflow checkpoints do not expose these native inventory totals",
        },
    }


def _native_drc_evidence(route_result: dict[str, Any], routed_board_hash: str | None) -> dict[str, Any]:
    drc = route_result.get("drc")
    verification = route_result.get("verification")
    if not isinstance(drc, dict) or not isinstance(verification, dict):
        return {"status": "unverified", "reason": "route checkpoint has no native DRC result"}
    blocker_count = drc.get("blocker_count")
    findings = drc.get("findings")
    wellformed_findings = isinstance(findings, list) and all(isinstance(item, dict) for item in findings)
    blocker_findings = (
        [item for item in findings if item.get("confirmed_blocker") is True or item.get("severity") == "blocker"]
        if wellformed_findings
        else None
    )
    passed = (
        drc.get("status") == "ok"
        and drc.get("passed") is True
        and type(blocker_count) is int
        and blocker_count == 0
        and wellformed_findings
        and blocker_findings == []
        and isinstance(routed_board_hash, str)
        and drc.get("board_sha256") == routed_board_hash
        and verification.get("kicad_drc") == "passed"
        and verification.get("connection_completeness") == "verified"
        and route_result.get("routing_complete") is True
    )
    return {
        "status": "passed" if passed else "unverified",
        "board_sha256": drc.get("board_sha256"),
        "routed_board_sha256": routed_board_hash,
        "blocker_count": blocker_count,
        "findings_count": len(findings) if isinstance(findings, list) else None,
        "blocker_findings_count": len(blocker_findings) if blocker_findings is not None else None,
        "verification": verification,
        "routing_complete": route_result.get("routing_complete"),
    }


def _route_contracts(case: dict[str, Any]) -> dict[str, Any] | None:
    expectations = case.get("expectations", {})
    endpoints = expectations.get("required_endpoints", []) if isinstance(expectations, dict) else []
    by_net: dict[str, list[dict[str, str]]] = {}
    for item in endpoints:
        if isinstance(item, dict) and item.get("net") and item.get("ref") and item.get("pad"):
            by_net.setdefault(str(item["net"]), []).append({"ref": str(item["ref"]), "pad": str(item["pad"])})
    routes = {}
    for net, rows in sorted(by_net.items()):
        normalized_net = net.upper()
        if normalized_net.startswith(
            ("GND", "AGND", "DGND", "PGND", "VDD", "VCC", "VBAT", "VBUS", "VIN", "VSS", "DAC_REF")
        ):
            continue
        unique = list({(row["ref"], row["pad"]): row for row in rows}.values())
        if len(unique) >= 2:
            routes[f"contract_{net}"] = {"net": net, "source": "AUTHORED", "from": unique[0], "to": unique[1]}
    return {"routes": routes} if routes else None


def _four_layer_stackup_complete(stackup: Any) -> bool:
    return (
        isinstance(stackup, dict)
        and type(stackup.get("count")) is int
        and stackup.get("count") == 4
        and stackup.get("ordered_names") == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    )


def _coverage_complete(preview: dict[str, Any]) -> bool:
    components = preview.get("component_coverage")
    models = preview.get("model_coverage")
    return (
        isinstance(components, dict)
        and isinstance(models, dict)
        and type(components.get("expected")) is int
        and components["expected"] > 0
        and components.get("represented") == components["expected"]
        and components.get("missing") == 0
        and type(models.get("expected")) is int
        and models["expected"] > 0
        and models.get("resolved") == models["expected"]
        and models.get("missing") == 0
    )


def _ground_plane_evidence(report: Any, plan: dict[str, Any]) -> tuple[bool, bool]:
    if not isinstance(report, dict) or report.get("status") != "verified":
        return False, False
    layers, zones, anchors = plan.get("layers"), report.get("zones"), report.get("connected_copper")
    if not isinstance(layers, list) or not layers or not isinstance(zones, list) or not isinstance(anchors, list):
        return False, False
    if (
        report.get("saved_fill_geometry_verified") is not True
        or report.get("zone_definitions_preserved") is not True
        or report.get("copper_layers_preserved") is not True
        or type(report.get("zone_count")) is not int
        or report["zone_count"] != len(zones)
        or len(zones) != len(layers)
    ):
        return False, False
    continuity = True
    unconnected = report.get("unconnected_pads_by_net")
    pads_connected = isinstance(unconnected, dict) and unconnected.get(plan.get("net")) == []
    for layer in layers:
        matching = [
            zone
            for zone in zones
            if isinstance(zone, dict) and zone.get("net") == plan.get("net") and zone.get("layer_name") == layer
        ]
        if len(matching) != 1:
            return False, False
        zone = matching[0]
        anchor = [item for item in anchors if isinstance(item, dict) and item.get("id") == zone.get("id")]
        if (
            not zone.get("id")
            or len(anchor) != 1
            or type(anchor[0].get("connected_copper_count")) is not int
            or anchor[0]["connected_copper_count"] <= 0
        ):
            pads_connected = False
        area, islands, digest = zone.get("area_mm2"), zone.get("island_count"), zone.get("polygon_sha256")
        if (
            isinstance(area, bool)
            or not isinstance(area, (int, float))
            or not math.isfinite(area)
            or area <= 0
            or type(islands) is not int
            or islands != 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest.casefold())
        ):
            continuity = False
    return continuity, pads_connected


def _phase(name: str, status: str, *, inputs: dict[str, Any], outputs: dict[str, Any], detail: Any) -> dict[str, Any]:
    return {
        "phase": name,
        "status": status,
        "started_at": None,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "input_hashes": inputs,
        "output_hashes": outputs,
        "detail": detail,
    }


def _phase_status(checkpoint: dict[str, Any], stage: str, output_hashes: dict[str, Any]) -> str:
    if checkpoint.get("status") == "failed":
        error = checkpoint.get("error")
        message = json.dumps(error, ensure_ascii=False).casefold()
        if any(
            token in message for token in ("unavailable", "not found", "not installed", "missing tool", "filenotfound")
        ):
            return "unverified"
        return "failed"
    if checkpoint.get("status") != "completed":
        return "unverified"
    if stage == "route":
        result = checkpoint.get("result")
        quality = result.get("route_quality") if isinstance(result, dict) else None
        if not _route_quality_complete(quality):
            return "unverified"
    if not output_hashes or any(value is None for value in output_hashes.values()):
        return "unverified"
    return "passed"


def _route_quality_complete(quality: Any) -> bool:
    routes = quality.get("routes") if isinstance(quality, dict) else None
    return (
        isinstance(quality, dict)
        and quality.get("status") == "verified"
        and isinstance(routes, dict)
        and bool(routes)
        and all(
            isinstance(route, dict)
            and route.get("status") == "verified"
            and ("open_count" not in route or route.get("open_count") == 0)
            and not route.get("opens")
            and route.get("connected") is not False
            for route in routes.values()
        )
    )


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def run(args: argparse.Namespace) -> int:
    engine_mode = getattr(args, "engine_mode", "source")
    if engine_mode not in {"source", "installed"}:
        raise ValueError("engine mode must be 'source' or 'installed'")
    _configure_engine_path(engine_mode)
    engine = importlib.import_module("circuit_weaver")
    engine_path = Path(engine.__file__).resolve()
    if engine_mode == "installed" and engine_path.is_relative_to((REPO / "src").resolve()):
        raise RuntimeError("installed mode resolved Circuit Weaver from the checkout source tree")
    cases = load_cases()
    wanted = set(args.case or [])
    known = {case["case_id"] for case in cases}
    unknown = wanted - known
    if unknown:
        raise ValueError(f"unknown case IDs: {', '.join(sorted(unknown))}")
    if args.through not in PHASES:
        raise ValueError(f"--through must be one of {', '.join(PHASES)}")
    selected = [case for case in cases if not wanted or case["case_id"] in wanted]
    outdir = args.output.expanduser().resolve()
    if outdir.exists():
        raise FileExistsError(f"refusing to reuse existing output directory: {outdir}")
    outdir.mkdir(parents=True, exist_ok=True)
    evidence_sources = (
        "src/circuit_weaver/design_loader.py",
        "src/circuit_weaver/project_workflow.py",
        "src/circuit_weaver/board_review.py",
        "src/circuit_weaver/route_quality.py",
        "src/circuit_weaver/assembly_manifest.py",
        "src/circuit_weaver/pcb_stackup.py",
        "src/circuit_weaver/pcb_ground_plane.py",
    )
    source = {
        "engine_mode": engine_mode,
        "product_module_path": str(engine_path),
        "product_version": getattr(engine, "__version__", None),
        "repo": str(REPO),
        "revision": _git("rev-parse", "HEAD"),
        "worktree_dirty": bool(_git("status", "--porcelain")),
        "runner_sha256": hash_file(Path(__file__)),
        "corpus_sha256": hash_file(CORPUS),
        "controls_sha256": hash_file(CONTROLS),
        "implementation_sha256": {rel: hash_file(REPO / rel) for rel in evidence_sources},
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }
    report_path = outdir / "acceptance-report.json"
    report = {
        "schema_version": 1,
        "suite": "four_layer_acceptance",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "requested_through": args.through,
        "mode": "preflight_only" if args.through == "preflight" else "native_workflow",
        "source": source,
        "cases": [],
        "summary": {},
        "gaps": _gap_inventory(),
    }
    _write_atomic(report_path, report)
    exit_code = 0
    for case in selected:
        phases = []
        start = time.monotonic()
        spec_hash = canonical_hash(case["spec"])
        row = {
            "case_id": case["case_id"],
            "title": case.get("title"),
            "family": case.get("family"),
            "requested_through": args.through,
            "comparison_fixture_reuse": case.get("comparison_fixture_reuse"),
            "declared_component_target": case.get("component_count_target"),
            "prototype_profile": case.get(
                "prototype_profile",
                {"clearance_mm": 0.127, "minimum_drill_mm": 0.2, "scope": "prototype fixture assumption"},
            ),
            "copper_layers": case.get("copper_layers"),
            "ground_plane": case.get("ground_plane"),
            "declared_limits": case.get("declared_limits"),
            "controls": [
                {
                    "control_id": control.get("control_id"),
                    "kind": control.get("kind"),
                    "expected_result": control.get("expected_result"),
                    "status": "unverified",
                    "reason": "pending positive preflight",
                }
                for control in case.get("controls", [])
                if isinstance(control, dict)
            ],
            "spec_sha256": spec_hash,
            "phases": phases,
            "gates": {
                key: "unverified"
                for key in (
                    "native_drc",
                    "four_layer_stackup",
                    "ground_plane_continuity",
                    "ground_pad_connections",
                    "modeled_assembly",
                    "route_connectivity_contracts",
                    "electrical_function",
                    "timing",
                    "impedance",
                    "thermal",
                )
            },
        }
        phases.append(_phase("preflight", "not_run", inputs={"spec_sha256": spec_hash}, outputs={}, detail=None))
        phases[0]["status"] = "running"
        row["acceptance_status"] = "running"
        report["cases"] = [existing for existing in report["cases"] if existing["case_id"] != row["case_id"]] + [row]
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        report["summary"] = _summarize(report["cases"])
        _write_atomic(report_path, report)
        try:
            started = datetime.now(timezone.utc).isoformat()
            result = _preflight(case)
            phases[0] = {
                **phases[0],
                "status": result["status"],
                "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "detail": result,
                "output_hashes": {"preflight_result_sha256": canonical_hash(result)},
            }
            row["metrics"] = result["metrics"]
            row["connectivity"] = result["connectivity"]
            row["assembly"] = result["assembly"]
            if result["status"] == "passed":
                row["controls"] = [
                    {
                        "control_id": item.get("control_id"),
                        "kind": item.get("kind"),
                        "expected_result": item.get("expected_result"),
                        "status": "running",
                    }
                    for item in case.get("controls", [])
                    if isinstance(item, dict)
                ]
                report["cases"] = [
                    existing for existing in report["cases"] if existing["case_id"] != row["case_id"]
                ] + [row]
                report["updated_at"] = datetime.now(timezone.utc).isoformat()
                _write_atomic(report_path, report)
                row["controls"] = _run_negative_controls(case)
                if any(control.get("status") == "undetected" for control in row["controls"]):
                    exit_code = 1
            if result["status"] == "failed":
                exit_code = 1
        except (ImportError, ModuleNotFoundError) as exc:
            phases[0] = {
                **phases[0],
                "status": "unverified",
                "detail": {"reason": "required tool or module unavailable", "error": str(exc)},
            }
        except Exception as exc:
            phases[0] = {
                **phases[0],
                "status": "failed",
                "detail": {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                },
            }
            exit_code = 1
        if args.through != "preflight" and phases[0]["status"] == "passed":
            for phase in PHASES[1 : PHASES.index(args.through) + 1]:
                phases.append(_phase(phase, "running", inputs={"spec_sha256": spec_hash}, outputs={}, detail=None))
            report["updated_at"] = datetime.now(timezone.utc).isoformat()
            report["cases"] = [existing for existing in report["cases"] if existing["case_id"] != row["case_id"]] + [
                row
            ]
            _write_atomic(report_path, report)
            try:
                workflow = _workflow_case(case, outdir, args.through)
                phases[1:] = workflow["phases"]
                row["project_dir"] = workflow["project_dir"]
                row["gates"].update(workflow.get("gates", {}))
                row["gate_evidence"] = workflow.get("gate_evidence", {})
                row["native_preview_coverage"] = workflow.get("native_preview_coverage")
                row["native_metrics"] = workflow.get("native_metrics")
                row["workflow_execution_status"] = workflow.get("workflow_execution_status", "unverified")
                if any(p["status"] == "failed" for p in workflow["phases"]):
                    exit_code = 1
            except Exception as exc:
                error_status = (
                    "unverified" if isinstance(exc, (ImportError, ModuleNotFoundError, FileNotFoundError)) else "failed"
                )
                for phase in phases[1:]:
                    if phase.get("status") == "running":
                        phase.update(
                            {
                                "status": error_status,
                                "finished_at": datetime.now(timezone.utc).isoformat(),
                                "detail": {"error_type": type(exc).__name__, "error": str(exc)},
                            }
                        )
                row["workflow_execution_status"] = error_status
                if error_status == "failed":
                    exit_code = 1
        else:
            for phase in PHASES[1 : PHASES.index(args.through) + 1] if args.through != "preflight" else []:
                phases.append(
                    _phase(phase, "not_run", inputs={}, outputs={}, detail={"reason": "preflight did not pass"})
                )
        row["elapsed_seconds"] = round(time.monotonic() - start, 3)
        cad_keys = (
            "native_drc",
            "route_connectivity_contracts",
            "four_layer_stackup",
            "ground_plane_continuity",
            "ground_pad_connections",
            "modeled_assembly",
        )
        product_keys = ("electrical_function", "timing", "impedance", "thermal")
        row["cad_acceptance_status"] = _cad_acceptance_status(row, cad_keys)
        row["product_qualification_status"] = _gate_group_status(row["gates"], product_keys)
        row["acceptance_status"] = _acceptance_status(row)
        report["cases"] = [existing for existing in report["cases"] if existing["case_id"] != row["case_id"]] + [row]
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        report["summary"] = _summarize(report["cases"])
        report["gaps"] = _gap_inventory(report["cases"])
        _write_atomic(report_path, report)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["updated_at"] = report["finished_at"]
    _write_atomic(report_path, report)
    return exit_code


def _acceptance_status(row: dict[str, Any]) -> str:
    statuses = [p.get("status") for p in row.get("phases", [])]
    if "failed" in statuses:
        return "failed"
    if "resource_blocked" in statuses:
        return "resource_blocked"
    if any(item.get("status") == "undetected" for item in row.get("controls", []) if isinstance(item, dict)):
        return "failed"
    if row.get("requested_through") == "preflight":
        return "unverified"
    if statuses and all(s == "passed" for s in statuses) and all(v == "passed" for v in row.get("gates", {}).values()):
        return "passed"
    return "unverified"


def _gate_group_status(gates: dict[str, Any], keys: tuple[str, ...]) -> str:
    statuses = [gates.get(key, "unverified") for key in keys]
    if "failed" in statuses:
        return "failed"
    return "passed" if statuses and all(status == "passed" for status in statuses) else "unverified"


def _cad_acceptance_status(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    phase_statuses = [phase.get("status") for phase in row.get("phases", []) if isinstance(phase, dict)]
    if "resource_blocked" in phase_statuses:
        return "resource_blocked"
    if "failed" in phase_statuses or any(
        item.get("status") == "undetected" for item in row.get("controls", []) if isinstance(item, dict)
    ):
        return "failed"
    return _gate_group_status(row.get("gates", {}), keys)


def _summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        status: sum(row.get("acceptance_status") == status for row in rows)
        for status in sorted((STATUSES - {"not_run"}) | {"running"})
    }


def _gap_inventory(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "product": ["electrical_function", "timing", "impedance", "thermal"],
        "circuit": [
            "native DRC zero blockers",
            "four-layer stackup evidence",
            "ground-plane continuity",
            "ground-pad connections",
            "native modeled assembly coverage",
        ],
        "resource": [
            "peak RSS within pinned 2.5 GiB cap and 2 GiB floor",
            "runtime completion under external job guard",
        ],
        "case_evidence": [
            {
                "case_id": row.get("case_id"),
                "status": row.get("acceptance_status"),
                "missing_or_unverified": [k for k, v in row.get("gates", {}).items() if v != "passed"],
                "unverified_controls": [
                    item.get("control_id")
                    for item in row.get("controls", [])
                    if isinstance(item, dict) and item.get("status") == "unverified"
                ],
                "undetected_controls": [
                    item.get("control_id")
                    for item in row.get("controls", [])
                    if isinstance(item, dict) and item.get("status") == "undetected"
                ],
                "resource_status": row.get("resource_status", "unverified"),
            }
            for row in (rows or [])
        ],
    }


def _git(*args: str) -> str:
    import subprocess

    try:
        result = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _configure_engine_path(mode: str) -> None:
    repo_path = str(REPO.resolve())
    source_path = str((REPO / "src").resolve())
    sys.path[:] = [item for item in sys.path if not item or Path(item).resolve() != Path(source_path)]
    if repo_path not in sys.path:
        sys.path.insert(0, repo_path)
    if mode == "source":
        sys.path.insert(0, source_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", help="case_id to run; may be repeated")
    parser.add_argument(
        "--through",
        choices=PHASES,
        default="preflight",
        help="last phase to execute (default: preflight; native phases are serial and expensive)",
    )
    parser.add_argument(
        "--engine-mode",
        choices=("source", "installed"),
        default="source",
        help="product import target; installed mode rejects checkout-source imports",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("four_layer_acceptance_results"),
        help="fresh output directory; must not already contain files",
    )
    args = parser.parse_args(argv)
    if args.through not in PHASES:
        parser.error("invalid phase")
    try:
        return run(args)
    except Exception as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    raise SystemExit(main())
