"""T249 authoritative schematic-to-PCB handoff contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import circuit_weaver.drc_runner as drc_runner
import circuit_weaver.pcb_handoff as pcb_handoff
from circuit_weaver.component_db import ComponentDef, PinDef
from circuit_weaver.design_ir import DesignIR
from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.evidence import EvidenceLedger, EvidenceSource
from circuit_weaver.footprint_lib import FootprintGeometry, KiCadFootprintLibrary
from circuit_weaver.identity import (
    IdentityHandoffBlocked,
    IdentityHandoffBundle,
    IdentityReconciliation,
    PinPadMap,
    build_identity_record,
    build_identity_source_assertion,
    reconcile_identity_assertions,
)
from circuit_weaver.pcb_constraints import PcbConstraintConflictError, compile_pcb_constraints
from circuit_weaver.pcb_contracts import (
    PcbArtifactKind,
    PcbConstraint,
    drc_validation_issue,
    inspect_pcb_artifact,
)
from circuit_weaver.pcb_handoff import PcbHandoffError, approve_placements, generate_authoritative_board

FOOTPRINT = """(footprint "TEST_2PAD"
  (version 20240108)
  (generator pcbnew)
  (layer "F.Cu")
  (fp_rect (start -2 -1) (end 2 1) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)"""


@pytest.fixture(autouse=True)
def _clean_staged_drc(monkeypatch: pytest.MonkeyPatch):
    def clean(board, *, evidence_ledger, constraints=(), approved_overrides=None, timeout=120):
        del constraints, approved_overrides, timeout
        payload = Path(board).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        evidence_id = evidence_ledger.record(
            subject_ref="tool:drc",
            claim=json.dumps(
                {"board_sha256": digest, "kicad_version": "test", "observed_findings": 0},
                sort_keys=True,
                separators=(",", ":"),
            ),
            kind="tool_result",
            source=EvidenceSource(
                doc_id="kicad-cli-test",
                content_hash=digest,
                extraction_method="test-clean-drc",
            ),
            confidence="verified",
            freshness="current",
        )
        return DrcResult(
            status="ok",
            board=str(board),
            board_sha256=digest,
            tool_version="test",
            evidence_id=evidence_id,
            raw_report={
                "kicad_version": "test",
                "violations": [],
                "unconnected_items": [],
                "schematic_parity": [],
            },
        )

    monkeypatch.setattr(pcb_handoff, "run_drc", clean)


def _library(tmp_path: Path) -> KiCadFootprintLibrary:
    pretty = tmp_path / "Test.pretty"
    pretty.mkdir(parents=True)
    (pretty / "TEST_2PAD.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    return KiCadFootprintLibrary(tmp_path)


def _component() -> ComponentDef:
    return ComponentDef(
        mpn="ACME-2P",
        source_mpn="ACME-2P",
        source_ref="U1",
        source_manufacturer="Acme",
        value="ACME-2P",
        footprint="Test:TEST_2PAD",
        pins=[PinDef("1", "SIG", "bidirectional", "L"), PinDef("2", "GND", "power_in", "B")],
        pin_nets={"1": "SIG"},
        power_pins={"2": "GND"},
    )


def _identity():
    return build_identity_record(
        status="resolved",
        manufacturer="Acme",
        mpn="ACME-2P",
        package_suffix="2P",
        symbol_ref="Acme:ACME-2P",
        footprint_ref="Test:TEST_2PAD",
        symbol_pins=("1", "2"),
        footprint_pads=("1", "2"),
        pin_pad_map=(PinPadMap("1", "1"), PinPadMap("2", "2")),
        evidence_ids=("EV-DATASHEET-0123456789ab",),
    )


def _assertion(identity, family: str, document: str):
    return build_identity_source_assertion(
        source_family=family,
        source_uri=f"https://example.test/{document}",
        source_doc_id=document,
        identity=identity,
        evidence_ids=("EV-DATASHEET-0123456789ab",),
    )


def _bundle(*, independent: bool = True) -> IdentityHandoffBundle:
    identity = _identity()
    assertions = [_assertion(identity, "manufacturer", "datasheet")]
    if independent:
        assertions.append(_assertion(identity, "footprint", "library"))
    return IdentityHandoffBundle(
        assertions=tuple(assertions),
        reconciliation=reconcile_identity_assertions(assertions),
        manufacturer="Acme",
        mpn="ACME-2P",
        package_suffix="2P",
        symbol_ref="Acme:ACME-2P",
        footprint_ref="Test:TEST_2PAD",
    )


def _forged_single_source_bundle() -> IdentityHandoffBundle:
    identity = _identity()
    assertions = (_assertion(identity, "manufacturer", "datasheet"),)
    evidence_ids = tuple(sorted({value for item in assertions for value in item.evidence_ids}))
    payload = {
        "state": "agree",
        "source_state": "agree",
        "assertion_ids": [item.id for item in assertions],
        "evidence_ids": list(evidence_ids),
        "missing_coverage": [],
        "disagreements": [],
        "approval_id": None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    forged = IdentityReconciliation(
        id=f"IRC-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:12]}",
        state="agree",
        source_state="agree",
        assertion_ids=tuple(payload["assertion_ids"]),
        evidence_ids=evidence_ids,
    )
    return IdentityHandoffBundle(
        assertions=assertions,
        reconciliation=forged,
        manufacturer="Acme",
        mpn="ACME-2P",
        package_suffix="2P",
        symbol_ref="Acme:ACME-2P",
        footprint_ref="Test:TEST_2PAD",
    )


def _approval(placements, approval_id="PLA-0123456789ab"):
    return approve_placements(
        placements,
        approval_id=approval_id,
        approved_at="2026-01-01T00:00:00Z",
        expires_at="2100-01-01T00:00:00Z",
    )


def _constraints():
    return (
        PcbConstraint.create(
            klass="clearance",
            target="net:SIG",
            params={"minimum": {"value": 0.2, "unit": "mm"}},
            origin="user",
            evidence_ids=("EV-DATASHEET-0123456789ab",),
        ),
    )


def _golden_constraints():
    return compile_pcb_constraints(
        DesignIR(),
        fab_profile="jlcpcb",
        fab_profile_evidence_id="EV-DATASHEET-abcdef012345",
        user_constraints=[
            {
                "klass": "length",
                "target": "net:SIG",
                "params": {"maximum": {"value": 20, "unit": "mm"}},
                "evidence_ids": ["EV-CALCULATION-0123456789ab"],
            },
            {
                "klass": "keepout",
                "target": "net:SIG",
                "params": {"copper_exclusion": {"value": 1, "unit": "mm"}},
                "evidence_ids": ["EV-CALCULATION-0123456789ab"],
            },
        ],
    )


def test_authoritative_handoff_emits_real_pads_nets_geometry_and_provenance(tmp_path: Path) -> None:
    output = tmp_path / "out"
    placements = {"U1": {"x_mm": 20, "y_mm": 15, "rotation_deg": 90, "layer": "F.Cu"}}
    result = generate_authoritative_board(
        [_component()],
        placements,
        {"U1": _bundle()},
        output,
        project_name="RealBoard",
        placement_approval=_approval(placements),
        board_constraints=_constraints(),
        footprint_library=_library(tmp_path / "libs"),
    )

    board_path = Path(result.board_path)
    board = board_path.read_text(encoding="utf-8")
    inspection = inspect_pcb_artifact(board_path)
    manifest = json.loads(Path(result.board_manifest_path).read_text(encoding="utf-8"))
    evidence = json.loads(Path(result.evidence_manifest_path).read_text(encoding="utf-8"))
    rules = Path(result.board_rules_path).read_text(encoding="utf-8")
    readiness = json.loads(Path(result.manufacturing_readiness_path).read_text(encoding="utf-8"))

    assert inspection.kind is PcbArtifactKind.AUTHORITATIVE
    assert result.pad_count == 2
    assert '(footprint "Test:TEST_2PAD"' in board
    assert '(pad "1"' in board and '(net 2 "SIG")' in board
    assert '(pad "2"' in board and '(net 1 "GND")' in board
    assert "placement_preview" not in board
    assert manifest["components"][0]["geometry"] == {
        "height_mm": 2.0,
        "source": "courtyard",
        "width_mm": 4.0,
    }
    assert manifest["board_provenance_evidence_id"] == result.board_provenance_evidence_id
    provenance = next(item for item in evidence["records"] if item["id"] == result.board_provenance_evidence_id)
    drc_evidence = next(item for item in evidence["records"] if item["id"] == result.drc_evidence_id)
    assert provenance["subject_ref"] == "tool:pcb_handoff"
    assert provenance["kind"] == "tool_result"
    assert "PLA-0123456789ab" in provenance["claim"]
    assert result.identity_guard_ids[0] in provenance["claim"]
    assert drc_evidence["subject_ref"] == "tool:drc" and drc_evidence["kind"] == "tool_result"
    assert manifest["drc"]["passed"] is True
    assert readiness == manifest["manufacturing_readiness"]
    assert readiness["state"] == "needs_review"
    assert readiness["blockers"] == ["routing_incomplete"]
    assert manifest["board_constraint_ids"] == [_constraints()[0].id]
    assert _constraints()[0].id in rules


def test_authoritative_rerun_preserves_uuid_and_reports_semantic_change(tmp_path: Path) -> None:
    library = _library(tmp_path / "libs")
    output = tmp_path / "out"
    kwargs = {
        "components": [_component()],
        "identity_handoffs": {"U1": _bundle()},
        "output_dir": output,
        "project_name": "StableBoard",
        "footprint_library": library,
        "board_constraints": _constraints(),
    }
    first_placements = {"U1": (20, 15, 0, "top")}
    first = generate_authoritative_board(
        approved_placements=first_placements,
        placement_approval=_approval(first_placements, "PLA-stable-1"),
        **kwargs,
    )
    first_manifest = json.loads(Path(first.board_manifest_path).read_text(encoding="utf-8"))
    second_placements = {"U1": (21, 15, 0, "top")}
    second = generate_authoritative_board(
        approved_placements=second_placements,
        placement_approval=_approval(second_placements, "PLA-stable-2"),
        **kwargs,
    )
    second_manifest = json.loads(Path(second.board_manifest_path).read_text(encoding="utf-8"))

    assert first_manifest["components"][0]["uuid"] == second_manifest["components"][0]["uuid"]
    assert second_manifest["semantic_changes"] == [{"action": "moved", "reference": "U1"}]


def test_t247_guard_blocks_before_any_authoritative_pad_render_or_file_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_render(*_args, **_kwargs):
        pytest.fail("pad renderer must not run before T247 passes")

    monkeypatch.setattr(pcb_handoff, "_render_authoritative_footprint", forbidden_render)
    output = tmp_path / "out"
    placements = {"U1": (20, 15, 0, "top")}
    with pytest.raises(IdentityHandoffBlocked):
        generate_authoritative_board(
            [_component()],
            placements,
            {"U1": _bundle(independent=False)},
            output,
            project_name="BlockedBoard",
            placement_approval=_approval(placements, "PLA-blocked"),
            board_constraints=_constraints(),
            footprint_library=_library(tmp_path / "libs"),
        )

    assert not output.exists()


def test_forged_single_source_agreement_is_recomputed_before_any_pad_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_render(*_args, **_kwargs):
        pytest.fail("pad renderer must not run before corroboration is recomputed")

    monkeypatch.setattr(pcb_handoff, "_render_authoritative_footprint", forbidden_render)
    output = tmp_path / "out"
    placements = {"U1": (20, 15, 0, "top")}

    with pytest.raises(ValueError, match="does not match its source assertions"):
        generate_authoritative_board(
            [_component()],
            placements,
            {"U1": _forged_single_source_bundle()},
            output,
            project_name="ForgedIdentityBoard",
            placement_approval=_approval(placements, "PLA-forged"),
            board_constraints=_constraints(),
            footprint_library=_library(tmp_path / "libs"),
        )

    assert not output.exists()


def test_authoritative_handoff_refuses_heuristic_geometry_before_board_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = _library(tmp_path / "libs")
    monkeypatch.setattr(
        library,
        "geometry",
        lambda _footprint: FootprintGeometry(
            5.0,
            5.0,
            "heuristic",
            "0" * 64,
            evidence_kind="heuristic",
            confidence="heuristic",
        ),
    )
    output = tmp_path / "out"
    placements = {"U1": (20, 15, 0, "top")}

    with pytest.raises(PcbHandoffError, match="refuses heuristic or placeholder"):
        generate_authoritative_board(
            [_component()],
            placements,
            {"U1": _bundle()},
            output,
            project_name="PlaceholderGeometryBoard",
            placement_approval=_approval(placements, "PLA-placeholder"),
            board_constraints=_constraints(),
            footprint_library=library,
        )

    assert not output.exists()


def test_four_layer_handoff_emits_internal_copper_layers(tmp_path: Path) -> None:
    placements = {"U1": (20, 15, 0, "top")}
    result = generate_authoritative_board(
        [_component()],
        placements,
        {"U1": _bundle()},
        tmp_path / "out",
        project_name="FourLayer",
        placement_approval=_approval(placements, "PLA-four"),
        board_constraints=_constraints(),
        footprint_library=_library(tmp_path / "libs"),
        copper_layers=4,
    )
    board = Path(result.board_path).read_text(encoding="utf-8")
    assert '(4 "In1.Cu" power)' in board
    assert '(6 "In2.Cu" power)' in board


def test_stale_or_mismatched_placement_approval_and_missing_constraints_fail_closed(tmp_path: Path) -> None:
    placements = {"U1": (20, 15, 0, "top")}
    base = {
        "components": [_component()],
        "approved_placements": placements,
        "identity_handoffs": {"U1": _bundle()},
        "output_dir": tmp_path / "out",
        "project_name": "ApprovalGate",
        "footprint_library": _library(tmp_path / "libs"),
    }
    stale = approve_placements(
        placements,
        approval_id="PLA-stale",
        approved_at="2000-01-01T00:00:00Z",
        expires_at="2100-01-01T00:00:00Z",
    )
    stale = pcb_handoff.PlacementApproval(
        stale.id,
        stale.placement_sha256,
        "2000-01-01T00:00:00Z",
        "2001-01-01T00:00:00Z",
    )
    with pytest.raises(pcb_handoff.PcbHandoffError, match="stale"):
        generate_authoritative_board(
            placement_approval=stale,
            board_constraints=_constraints(),
            **base,
        )
    with pytest.raises(pcb_handoff.PcbHandoffError, match="compiled board constraints"):
        generate_authoritative_board(
            placement_approval=_approval(placements),
            board_constraints=(),
            **base,
        )

    assert not (tmp_path / "out").exists()


def test_constraint_conflict_blocks_before_footprint_preflight_or_board_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = compile_pcb_constraints(
        DesignIR(),
        fab_profile="jlcpcb",
        fab_profile_evidence_id="EV-DATASHEET-abcdef012345",
        user_constraints=[
            {
                "klass": "clearance",
                "target": "net_class:Default",
                "params": {"minimum": {"value": 0.05, "unit": "mm"}},
                "evidence_ids": ["EV-CALCULATION-0123456789ab"],
            }
        ],
    )
    monkeypatch.setattr(
        pcb_handoff,
        "_preflight_component",
        lambda *_args, **_kwargs: pytest.fail("footprint preflight must follow the conflict gate"),
    )
    placements = {"U1": (20, 15, 0, "top")}
    output = tmp_path / "out"

    with pytest.raises(PcbConstraintConflictError):
        generate_authoritative_board(
            [_component()],
            placements,
            {"U1": _bundle()},
            output,
            project_name="Conflict",
            placement_approval=_approval(placements),
            board_constraints=compilation,
            footprint_library=_library(tmp_path / "libs"),
        )

    assert not output.exists()


def test_failing_drc_preserves_every_last_known_good_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "out"
    placements = {"U1": (20, 15, 0, "top")}
    first = generate_authoritative_board(
        [_component()],
        placements,
        {"U1": _bundle()},
        output,
        project_name="Transactional",
        placement_approval=_approval(placements, "PLA-first"),
        board_constraints=_constraints(),
        footprint_library=_library(tmp_path / "libs"),
    )
    artifact_paths = [
        Path(first.board_path),
        Path(first.board_rules_path),
        Path(first.board_manifest_path),
        Path(first.evidence_manifest_path),
        Path(first.drc_report_path),
        Path(first.drc_findings_path),
        Path(first.manufacturing_readiness_path),
    ]
    before = {path: path.read_bytes() for path in artifact_paths}

    def blocked(board, *, evidence_ledger, **_kwargs):
        digest = hashlib.sha256(Path(board).read_bytes()).hexdigest()
        evidence_id = evidence_ledger.record(
            subject_ref="tool:drc",
            claim=json.dumps({"board_sha256": digest, "observed_findings": 1}, sort_keys=True),
            kind="tool_result",
            source=EvidenceSource(content_hash=digest, extraction_method="test-blocked-drc"),
            confidence="verified",
            freshness="current",
        )
        finding = drc_validation_issue(
            rule_number=1,
            message="clearance violation",
            severity="blocker",
            evidence_ids=(evidence_id,),
            observed_value="0.1 mm",
            expected_constraint="0.2 mm",
            safest_next_action="increase clearance",
        )
        return DrcResult(
            status="ok",
            board=str(board),
            board_sha256=digest,
            tool_version="test",
            evidence_id=evidence_id,
            findings=(finding,),
            raw_report={"violations": [{}]},
        )

    monkeypatch.setattr(pcb_handoff, "run_drc", blocked)
    moved = {"U1": (21, 15, 0, "top")}
    with pytest.raises(pcb_handoff.PcbDrcBlocked, match="unapproved blocker"):
        generate_authoritative_board(
            [_component()],
            moved,
            {"U1": _bundle()},
            output,
            project_name="Transactional",
            placement_approval=_approval(moved, "PLA-second"),
            board_constraints=_constraints(),
            footprint_library=_library(tmp_path / "libs2"),
        )

    assert {path: path.read_bytes() for path in artifact_paths} == before


def test_drc_operational_failure_or_stale_hash_publishes_no_partial_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placements = {"U1": (20, 15, 0, "top")}
    base = {
        "components": [_component()],
        "approved_placements": placements,
        "identity_handoffs": {"U1": _bundle()},
        "project_name": "NoPartial",
        "placement_approval": _approval(placements),
        "board_constraints": _constraints(),
        "footprint_library": _library(tmp_path / "libs"),
    }
    for name, result in (
        ("failed", DrcResult(status="failed", board="staged", failure_reason="parser drift")),
        (
            "stale",
            DrcResult(
                status="ok",
                board="staged",
                board_sha256="0" * 64,
                evidence_id="EV-TOOL_RESULT-0123456789ab",
                raw_report={},
            ),
        ),
    ):
        output = tmp_path / name
        monkeypatch.setattr(pcb_handoff, "run_drc", lambda *_args, _result=result, **_kwargs: _result)
        with pytest.raises(pcb_handoff.PcbDrcBlocked):
            generate_authoritative_board(output_dir=output, **base)
        assert not (output / "NoPartial.kicad_pcb").exists()
        assert not (output / "NoPartial_board_manifest.json").exists()


def test_interrupted_multi_file_publish_restores_prior_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    output = tmp_path / "output"
    staging.mkdir()
    output.mkdir()
    sources = [staging / "one", staging / "two", staging / "three"]
    destinations = [output / "one", output / "two", output / "three"]
    for index, (source, destination) in enumerate(zip(sources, destinations), start=1):
        source.write_text(f"new-{index}", encoding="utf-8")
        destination.write_text(f"old-{index}", encoding="utf-8")
    real_replace = Path.replace

    def interrupted(source: Path, destination: Path):
        if Path(destination).name == "two":
            raise OSError("injected interruption")
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(pcb_handoff.PcbHandoffTransactionError):
        pcb_handoff._publish_staged_transaction(staging, tuple(zip(sources, destinations)))

    assert [path.read_text(encoding="utf-8") for path in destinations] == [
        "old-1",
        "old-2",
        "old-3",
    ]


@pytest.mark.parametrize("copper_layers", [2, 4])
@pytest.mark.skip_category("optional-tool")
def test_authoritative_golden_round_trips_through_kicad_cli(
    tmp_path: Path,
    copper_layers: int,
) -> None:
    cli = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if not cli:
        pytest.skip("KiCad CLI is not available")
    placements = {"U1": (20, 15, 0, "top")}
    result = generate_authoritative_board(
        [_component()],
        placements,
        {"U1": _bundle()},
        tmp_path / "out",
        project_name=f"Golden{copper_layers}Layer",
        placement_approval=_approval(placements, f"PLA-golden-{copper_layers}"),
        board_constraints=_golden_constraints(),
        footprint_library=_library(tmp_path / "libs"),
        copper_layers=copper_layers,
    )
    load_target = tmp_path / f"load-{copper_layers}.kicad_pcb"
    pcb_help = subprocess.run(
        [cli, "pcb", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    upgrade_available = any(
        line.strip().startswith("upgrade ")
        for line in (pcb_help.stdout + pcb_help.stderr).splitlines()
    )
    if upgrade_available:
        shutil.copy2(result.board_path, load_target)
        completed = subprocess.run(
            [cli, "pcb", "upgrade", "--force", str(load_target)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    else:
        kicad_python = os.environ.get("KICAD_PYTHON") or shutil.which(
            "python3",
            path=os.defpath,
        )
        assert kicad_python, "KiCad lacks pcb upgrade and no system Python is available"
        completed = subprocess.run(
            [
                kicad_python,
                "-c",
                (
                    "import pcbnew, sys; "
                    "board = pcbnew.LoadBoard(sys.argv[1]); "
                    "assert board is not None; "
                    "pcbnew.SaveBoard(sys.argv[2], board)"
                ),
                str(result.board_path),
                str(load_target),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert load_target.stat().st_size > 0
    actual_drc = drc_runner.run_drc(
        load_target,
        evidence_ledger=EvidenceLedger(),
        constraints=_golden_constraints().constraints,
    )
    assert actual_drc.status == "ok"
    assert actual_drc.passed
    assert all(type(finding).__name__ == "ValidationIssue" for finding in actual_drc.findings)


def test_authoritative_manifest_reports_unrendered_constraint_ids(tmp_path: Path) -> None:
    output = tmp_path / "out"
    placements = {"U1": {"x_mm": 20, "y_mm": 15, "rotation_deg": 90, "layer": "F.Cu"}}
    requested = (*_constraints(), PcbConstraint.create(
        klass="impedance",
        target="net:SIG",
        params={"target": {"value": 50, "unit": "ohm"}, "mode": "single-ended"},
        origin="user",
        evidence_ids=("EV-DATASHEET-0123456789ab",),
    ))
    result = generate_authoritative_board(
        [_component()], placements, {"U1": _bundle()}, output, project_name="PartialRules",
        placement_approval=_approval(placements), board_constraints=requested,
        footprint_library=_library(tmp_path / "libs"),
    )
    manifest = json.loads(Path(result.board_manifest_path).read_text(encoding="utf-8"))
    rules = Path(result.board_rules_path).read_text(encoding="utf-8")
    accounting = manifest["board_constraint_rendering"]
    assert accounting["status"] == "partial"
    assert accounting["requested_ids"] == [item.id for item in requested]
    assert accounting["emitted_ids"] == [requested[0].id]
    assert accounting["unemitted_ids"] == [requested[1].id]
    assert "requested intent only" in rules
    assert requested[1].id not in "\\n".join(
        line for line in rules.splitlines() if line.startswith('(rule ')
    )


def test_authoritative_manifest_preserves_unverified_ddr_requirements(tmp_path: Path) -> None:
    memory = replace(
        _component(),
        description="DDR3 memory device",
        pin_nets={"1": "DQ0", "2": "GND"},
    )
    compilation = compile_pcb_constraints(
        DesignIR(),
        components=[memory],
        fab_profile="jlcpcb",
        fab_profile_evidence_id="EV-DATASHEET-abcdef012345",
    )
    assert compilation.ready
    assert compilation.unverified_requirements[0]["bus_type"] == "ddr3"
    assert compilation.unverified_requirements[0]["generated_hard_constraints"] == []
    assert compilation.to_dict()["unverified_requirements"][0]["verification_status"] == "not_verified"

    placements = {"U1": {"x_mm": 20, "y_mm": 15, "rotation_deg": 90, "layer": "F.Cu"}}
    result = generate_authoritative_board(
        [memory], placements, {"U1": _bundle()}, tmp_path / "out", project_name="UnverifiedDDR",
        placement_approval=_approval(placements), board_constraints=compilation,
        footprint_library=_library(tmp_path / "libs"),
    )
    manifest = json.loads(Path(result.board_manifest_path).read_text(encoding="utf-8"))
    requirement = manifest["unverified_board_requirements"][0]
    assert requirement["verification_status"] == "not_verified"
    assert requirement["bus_type"] == "ddr3"
    assert requirement["nets"] == ["DQ0"]
    assert requirement["generated_hard_constraints"] == []
