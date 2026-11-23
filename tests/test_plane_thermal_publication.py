"""Public thermal repair must fail closed and preserve previously published CAD."""
import copy
import hashlib
from pathlib import Path

import pytest

from circuit_weaver import pcb_layout as layout
from circuit_weaver import project_workflow as workflow
from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.validator import ValidationIssue


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _thermal_drc(board, *, mixed=False, stale=False, clean=False):
    issue = ValidationIssue(code="thermal", ref="J1", mpn="", message="thermal",
                            rule_id="CW-DRC-787", severity="blocker")
    rows = [{"type": "starved_thermal", "severity": "error",
             "items": [{"uuid": "pad"}, {"uuid": "zone"}]}]
    if mixed:
        rows.append({"type": "clearance", "severity": "error", "items": []})
    return DrcResult(status="ok", board=str(board), board_sha256="0" * 64 if stale else _hash(board),
                     findings=() if clean else (issue,), raw_report={
                         "violations": [] if clean else rows, "unconnected_items": [], "schematic_parity": []})


def _verified_worker():
    return {"status": "ok", "plane_stitching": {
        "status": "candidate_only_requires_full_drc", "net": "GND", "added_vias": 1,
        **{key: True for key in ("source_identity_preserved", "original_footprints_and_text_preserved",
                                "original_board_geometry_preserved", "existing_copper_preserved",
                                "saved_added_vias_verified")},
    }, "zone_refill": {
        "status": "verified", "saved_fill_geometry_verified": True, "zone_definitions_preserved": True,
        "copper_stackup_preserved": True, "unconnected_ground_pads": [], "unconnected_pads_by_net": {"GND": []},
        "rows": [{"id": name, "net": "GND", "island_count": 0, "area_mm2": 10.0} for name in ("in1", "in2")],
        "connected_copper": [{"id": name, "connected_copper_count": 1} for name in ("in1", "in2")],
    }}


@pytest.mark.parametrize("failure", ["mixed_fault", "stale_drc", "native_failure", "changed_copper",
                                     "unconnected_plane", "missing_fill", "final_drc", "final_hash", "final_warning"])
def test_thermal_repair_failure_never_replaces_previous_outputs(tmp_path, monkeypatch, failure):
    source, ses, out = [tmp_path / name for name in ("source.kicad_pcb", "route.ses", "out.kicad_pcb")]
    source.write_bytes(b"source")
    source.with_suffix(".kicad_pro").write_bytes(b"authored rules")
    ses.write_bytes(b"complete session")
    out.write_bytes(b"previous accepted board")
    out.with_suffix(".kicad_pro").write_bytes(b"previous accepted rules")
    calls = []

    def worker(operation, **kwargs):
        calls.append(operation)
        target = Path(kwargs["output"])
        target.write_bytes(operation.encode())
        if operation == "import_ses":
            return {"status": "ok", "zone_count": 2, "zone_refill": _verified_worker()["zone_refill"]}
        assert operation == "repair_plane_thermals"
        assert kwargs["expected_board_sha256"] == _hash(Path(kwargs["board"]))
        result = copy.deepcopy(_verified_worker())
        if failure == "native_failure":
            return {"status": "error", "message": "no legal via"}
        if failure == "changed_copper":
            result["plane_stitching"]["existing_copper_preserved"] = False
        if failure == "unconnected_plane":
            result["zone_refill"]["unconnected_ground_pads"] = ["pad"]
        if failure == "missing_fill":
            result["zone_refill"].pop("saved_fill_geometry_verified")
        return result

    def drc(board, **kwargs):
        repaired = Path(board).name == "plane-thermal-repaired.kicad_pcb"
        result = _thermal_drc(board, mixed=failure == "mixed_fault", stale=(failure == "stale_drc"
                            or (repaired and failure == "final_hash")),
                            clean=repaired and failure != "final_drc")
        if repaired and failure == "final_warning":
            result.raw_report["violations"].append({"type": "clearance", "severity": "warning", "items": []})
        return result

    monkeypatch.setattr(layout, "run_kicad", worker)
    monkeypatch.setattr(layout, "run_drc", drc)
    result = layout.import_routing_session(source, ses, out, overwrite=True, repair_plane_thermals=True)
    assert result["status"] == "error"
    assert out.read_bytes() == b"previous accepted board"
    assert out.with_suffix(".kicad_pro").read_bytes() == b"previous accepted rules"
    assert source.read_bytes() == b"source" and source.with_suffix(".kicad_pro").read_bytes() == b"authored rules"
    assert ("repair_plane_thermals" in calls) is (failure not in {"mixed_fault", "stale_drc"})


@pytest.mark.parametrize("value", [1, "true", None])
def test_public_thermal_repair_requires_explicit_boolean(tmp_path, value):
    source, ses, out = [tmp_path / name for name in ("source.kicad_pcb", "route.ses", "out.kicad_pcb")]
    result = layout.import_routing_session(source, ses, out, repair_plane_thermals=value)
    assert result["status"] == "error" and "boolean" in result["message"]


@pytest.mark.parametrize("options", [
    {"ground_plane_repair_thermals": True},
    {"ground_plane_repair_thermals": "true"},
    {"ground_plane_repair_thermals": True, "copper_layers": 2},
])
def test_workflow_cannot_silently_ignore_inapplicable_thermal_repair(options):
    with pytest.raises(workflow.WorkflowError, match="ground_plane_repair_thermals"):
        workflow._normalize_options(options)
