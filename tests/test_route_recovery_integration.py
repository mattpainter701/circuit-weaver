"""Adversarial publication contracts for opt-in native routing recovery."""

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from circuit_weaver import autoroute_recovery
from circuit_weaver.autoroute import _validate_specctra_artifact
from circuit_weaver.autoroute_recovery import autoroute_with_recovery
from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.validator import ValidationIssue


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    import circuit_weaver.autoroute as router
    import circuit_weaver.autoroute_recovery as recovery_module
    import circuit_weaver.kicad_bridge as bridge
    import circuit_weaver.kicad_project_context as context
    import circuit_weaver.pcb_layout as layout
    import circuit_weaver.routing_recovery as native

    source = tmp_path / "source.kicad_pcb"
    source.write_text("original board", encoding="utf-8")
    destination = tmp_path / "accepted.kicad_pcb"
    session = tmp_path / "accepted.ses"
    state = {
        "initial_complete": False,
        "initial_error_factory": None,
        "mutate_session_on_import": False,
        "repair_status": "ok",
        "convert_status": "ok",
        "import_status": "ok",
        "accept_status": "ok",
        "cleanup_native_clean": False,
        "mutate": None,
    }

    def route(board, output, **options):
        if options.get("_allow_partial_nets"):
            Path(output).write_text("partial session", encoding="utf-8")
            if state["mutate"] == "source":
                source.write_text("concurrent edit", encoding="utf-8")
            if state["initial_error_factory"] is not None:
                return state["initial_error_factory"](Path(output))
            return {
                "status": "ok" if state["initial_complete"] else "partial",
                "routing_complete": state["initial_complete"],
            }
        raise AssertionError("Recovery serialization must not reroute the native board")

    def serialize(board, output, **options):
        if state["convert_status"] == "ok":
            Path(output).write_text("complete session", encoding="utf-8")
        return {"status": state["convert_status"], "routing_statistics": "not_run"}

    def import_partial(command, **options):
        assert command == "import_ses"
        if state["mutate_session_on_import"]:
            Path(options["session"]).write_text("altered session", encoding="utf-8")
        if state["import_status"] == "ok":
            Path(options["output"]).write_text("private candidate", encoding="utf-8")
        return {"status": state["import_status"]}

    def repair(board, output, **options):
        Path(output).write_text("recovered candidate", encoding="utf-8")
        return {"status": state["repair_status"], "output_path": str(output)}

    def accept(board, ses, output, **options):
        if state["accept_status"] == "ok":
            Path(output).write_text("accepted board", encoding="utf-8")
        return {
            "status": state["accept_status"],
            "routing_complete": state["accept_status"] == "ok",
            "drc": {"board_sha256": "verified"},
        }

    route_mock = Mock(side_effect=route)
    repair_mock = Mock(side_effect=repair)
    accept_mock = Mock(side_effect=accept)
    monkeypatch.setattr(router, "autoroute_pcb", route_mock)
    monkeypatch.setattr(recovery_module, "_serialize_native_session", Mock(side_effect=serialize))
    monkeypatch.setattr(bridge, "run_kicad", Mock(side_effect=import_partial))
    monkeypatch.setattr(native, "recover_routing", repair_mock)
    # Host-side fresh DRC and router-generated dangling normalization have
    # focused behavioral tests in test_native_router_dangling_cleanup.py.
    # Keep this API workflow fixture isolated from KiCad CLI and filesystem
    # footprint-context staging.
    def clean_partial(source_path, partial_path, **_options):
        if state["cleanup_native_clean"]:
            drc = SimpleNamespace(
                findings=(), blocker_count=0, status="ok",
                raw_report={"unconnected_items": [], "violations": [], "schematic_parity": []},
            )
            return Path(partial_path), drc, {"status": "cleaned", "removed_track_count": 1}
        finding = SimpleNamespace(
            rule_id="CW-DRC-002", severity="blocker", suppressed=False,
        )
        drc = SimpleNamespace(findings=(finding,), blocker_count=1)
        return Path(partial_path), drc, {"status": "not_needed"}

    monkeypatch.setattr(recovery_module, "_clean_router_dangling_tracks", clean_partial)
    monkeypatch.setattr(layout, "import_routing_session", accept_mock)
    monkeypatch.setattr(layout, "_prepare_settings", lambda *args: {})
    monkeypatch.setattr(layout, "_restore_settings", lambda *args: None)
    context_mock = Mock(return_value={"libraries": "original"})
    monkeypatch.setattr(context, "inspect_project_footprint_context", context_mock)
    return source, session, destination, state, route_mock, repair_mock, accept_mock, context_mock


@pytest.mark.parametrize("invalid", ["missing", "dsn", "same-output", "no-board-output"])
def test_recovery_rejects_invalid_native_input_before_tools(recovery, invalid):
    source, ses, dest, _, route, repair, accept, _ = recovery
    if invalid == "missing":
        source.unlink()
    elif invalid == "dsn":
        source = source.with_suffix(".dsn")
        source.write_text("dsn", encoding="utf-8")
    elif invalid == "same-output":
        dest = source
    else:
        dest = None
    result = autoroute_with_recovery(source, ses, routed_board_path=dest)
    assert result["status"] == "error"
    route.assert_not_called()
    repair.assert_not_called()
    accept.assert_not_called()
    assert not ses.exists()


@pytest.mark.parametrize("failure", ["import", "repair", "convert", "source", "library"])
def test_private_candidate_failures_never_publish(recovery, failure):
    source, ses, dest, state, _, _, accept, context = recovery
    if failure in {"import", "repair", "convert"}:
        state[f"{failure}_status"] = "partial" if failure == "repair" else "error"
    elif failure == "source":
        state["mutate"] = "source"
    else:
        context.side_effect = [{"libraries": "original"}, {"libraries": "changed"}]
    result = autoroute_with_recovery(source, ses, routed_board_path=dest)
    assert result["status"] != "ok"
    assert result["routing_complete"] is False
    assert not dest.exists()
    assert not ses.exists()
    accept.assert_not_called()


def test_final_native_rejection_retains_session_but_never_publishes_board(recovery):
    source, ses, dest, state, _, _, accept, _ = recovery
    state["accept_status"] = "error"
    result = autoroute_with_recovery(source, ses, routed_board_path=dest)
    assert result["status"] == "error"
    assert result["routing_complete"] is False
    assert ses.read_text(encoding="utf-8") == "complete session"
    assert not dest.exists()
    assert accept.call_args.kwargs["expected_hashes"][source]


def test_complete_initial_route_bypasses_repair_but_keeps_native_gate(recovery):
    source, ses, dest, state, route, repair, accept, _ = recovery
    state["initial_complete"] = True
    result = autoroute_with_recovery(source, ses, routed_board_path=dest)
    assert result["status"] == "ok"
    assert dest.exists()
    assert route.call_count == 1
    repair.assert_not_called()
    accept.assert_called_once()


def _initial_native_open_rejection(session_path: Path, *, drc=None, **overrides):
    return {
        "status": "error",
        "message": "Imported routing failed KiCad DRC; board was not published",
        "output_kind": "specctra_session",
        "output_path": str(session_path),
        "routing_complete": False,
        "requires_kicad_drc": True,
        "stats": {"incomplete": 0},
        "artifact": {"valid": True, "kind": "ses", "path": str(session_path)},
        "board_import": {
            "status": "error",
            "message": "Imported routing failed KiCad DRC; board was not published",
            "drc": drc or {
                "status": "ok",
                "passed": False,
                "blocker_count": 1,
                "findings": [{
                    "rule_id": "CW-DRC-002",
                    "severity": "blocker",
                    "level": "error",
                    "detection_confidence": "verified",
                    "suppressed": False,
                    "confirmed_blocker": True,
                }],
            },
        },
        **overrides,
    }


def test_zero_router_incomplete_with_verified_native_open_rejection_enters_private_recovery(recovery):
    source, ses, dest, state, route, repair, accept, _ = recovery
    state["initial_error_factory"] = _initial_native_open_rejection

    result = autoroute_with_recovery(source, ses, routed_board_path=dest)

    assert result["status"] == "ok"
    assert result["routing_complete"] is True
    assert result["recovery_trigger"]["kind"] == "native_open_drc_after_router_reported_zero_incomplete"
    assert result["recovery_trigger"]["router_incomplete"] == 0
    assert result["recovery_trigger"]["session_sha256"]
    assert result["recovery_trigger"]["native_drc"]["findings"][0]["rule_id"] == "CW-DRC-002"
    assert route.call_count == 1
    repair.assert_called_once()
    accept.assert_called_once()
    assert dest.read_text(encoding="utf-8") == "accepted board"
    assert ses.read_text(encoding="utf-8") == "complete session"


def test_zero_router_incomplete_with_known_dangling_warning_enters_fresh_cleanup_and_recovery(recovery):
    source, ses, dest, state, route, repair, accept, _ = recovery

    def initial_error(path):
        return _initial_native_open_rejection(path, drc={
            "status": "ok",
            "passed": False,
            "blocker_count": 1,
            "findings": [
                {
                    "rule_id": "CW-DRC-002",
                    "severity": "blocker",
                    "level": "error",
                    "detection_confidence": "verified",
                    "suppressed": False,
                    "confirmed_blocker": True,
                    "net": "SIGNAL_A",
                },
                {
                    "rule_id": "CW-DRC-841",
                    "severity": "major",
                    "level": "warning",
                    "detection_confidence": "verified",
                    "suppressed": False,
                    "confirmed_blocker": False,
                    "net": "SIGNAL_B",
                    "message": "Track has unconnected end",
                },
            ],
        })

    state["initial_error_factory"] = initial_error
    result = autoroute_with_recovery(source, ses, routed_board_path=dest)

    assert result["status"] == "ok"
    assert result["routing_complete"] is True
    trigger = result["recovery_trigger"]
    assert trigger["kind"] == "native_open_and_dangling_drc_after_router_reported_zero_incomplete"
    assert "dangling-track warnings" in trigger["message"]
    assert trigger["native_drc"]["blocker_count"] == 1
    assert trigger["native_drc"]["finding_count"] == 2
    assert trigger["native_drc"]["dangling_warning_count"] == 1
    assert route.call_count == 1
    repair.assert_called_once()
    accept.assert_called_once()
    assert dest.read_text(encoding="utf-8") == "accepted board"


def test_warning_only_complete_router_result_cleans_then_serializes_without_link_recovery(recovery):
    source, ses, dest, state, route, repair, accept, _ = recovery
    state["cleanup_native_clean"] = True

    def initial_error(path):
        return _initial_native_open_rejection(path, drc={
            "status": "ok",
            "passed": False,
            "blocker_count": 0,
            "findings": [{
                "rule_id": "CW-DRC-841",
                "severity": "major",
                "level": "warning",
                "detection_confidence": "verified",
                "suppressed": False,
                "confirmed_blocker": False,
                "message": "Track has unconnected end",
            }],
        })

    state["initial_error_factory"] = initial_error
    result = autoroute_with_recovery(source, ses, routed_board_path=dest)

    assert result["status"] == "ok"
    assert result["routing_complete"] is True
    assert result["recovery_trigger"]["kind"] == "native_dangling_drc_after_router_reported_zero_incomplete"
    assert result["recovery_trigger"]["native_drc"]["blocker_count"] == 0
    assert result["recovery_trigger"]["native_drc"]["dangling_warning_count"] == 1
    assert route.call_count == 1
    repair.assert_not_called()
    accept.assert_called_once()
    assert dest.read_text(encoding="utf-8") == "accepted board"
    assert ses.read_text(encoding="utf-8") == "complete session"


@pytest.mark.parametrize(
    "warning_mutation",
    [
        "wrong-level",
        "wrong-severity",
        "unverified-open",
        "unverified-warning",
        "suppressed",
        "confirmed-blocker",
        "unknown-rule",
        "extra-finding",
        "blocker-count-mismatch",
        "no-findings",
        "warning-only-drc-passed",
    ],
)
def test_mixed_open_and_dangling_rejection_requires_exact_verified_finding_inventory(recovery, warning_mutation):
    source, ses, dest, state, route, repair, accept, _ = recovery

    def initial_error(path):
        drc = {
            "status": "ok",
            "passed": False,
            "blocker_count": 1,
            "findings": [
                {
                    "rule_id": "CW-DRC-002",
                    "severity": "blocker",
                    "level": "error",
                    "detection_confidence": "verified",
                    "suppressed": False,
                    "confirmed_blocker": True,
                },
                {
                    "rule_id": "CW-DRC-841",
                    "severity": "major",
                    "level": "warning",
                    "detection_confidence": "verified",
                    "suppressed": False,
                    "confirmed_blocker": False,
                },
            ],
        }
        if warning_mutation == "wrong-level":
            drc["findings"][1]["level"] = "error"
        elif warning_mutation == "wrong-severity":
            drc["findings"][1]["severity"] = "minor"
        elif warning_mutation == "unverified-open":
            drc["findings"][0]["detection_confidence"] = "single_source"
        elif warning_mutation == "unverified-warning":
            drc["findings"][1]["detection_confidence"] = "single_source"
        elif warning_mutation == "suppressed":
            drc["findings"][1]["suppressed"] = True
        elif warning_mutation == "confirmed-blocker":
            drc["findings"][1]["confirmed_blocker"] = True
        elif warning_mutation == "unknown-rule":
            drc["findings"][1]["rule_id"] = "CW-DRC-999"
        elif warning_mutation == "extra-finding":
            drc["findings"].append({"rule_id": "CW-DRC-841", "severity": "info", "level": "info"})
        elif warning_mutation == "blocker-count-mismatch":
            drc["blocker_count"] = 2
        elif warning_mutation == "no-findings":
            drc["blocker_count"] = 0
            drc["findings"] = []
        elif warning_mutation == "warning-only-drc-passed":
            drc["blocker_count"] = 0
            drc["findings"].pop(0)
            drc["passed"] = True
        return _initial_native_open_rejection(path, drc=drc)

    state["initial_error_factory"] = initial_error
    result = autoroute_with_recovery(source, ses, routed_board_path=dest)

    assert result["status"] == "error"
    assert result["routing_complete"] is False
    assert result.get("recovery_trigger") is None
    repair.assert_not_called()
    accept.assert_not_called()
    assert not dest.exists()
    assert not ses.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        "missing-board-import",
        "wrong-board-import-message",
        "wrong-drc-status",
        "drc-passed",
        "no-findings",
        "blocker-count-mismatch",
        "other-rule",
        "suppressed",
        "nonzero-incomplete",
        "boolean-incomplete",
        "float-incomplete",
        "missing-incomplete",
        "wrong-session-kind",
        "wrong-session-path",
        "invalid-artifact",
        "wrong-artifact-kind",
        "missing-findings",
        "malformed-finding",
        "warning-finding",
        "unconfirmed-finding",
        "generic-import-error-message",
    ],
)
def test_ambiguous_initial_import_error_never_enters_recovery(recovery, mutate):
    source, ses, dest, state, route, repair, accept, _ = recovery

    def initial_error(path):
        report = _initial_native_open_rejection(path)
        if mutate == "missing-board-import":
            report.pop("board_import")
        elif mutate == "wrong-board-import-message":
            report["board_import"]["message"] = "Import failed for another reason"
        elif mutate == "wrong-drc-status":
            report["board_import"]["drc"]["status"] = "error"
        elif mutate == "drc-passed":
            report["board_import"]["drc"]["passed"] = True
        elif mutate == "no-findings":
            report["board_import"]["drc"]["findings"] = []
            report["board_import"]["drc"]["blocker_count"] = 0
        elif mutate == "blocker-count-mismatch":
            report["board_import"]["drc"]["blocker_count"] = 2
        elif mutate == "other-rule":
            report["board_import"]["drc"]["findings"][0]["rule_id"] = "CW-DRC-001"
        elif mutate == "suppressed":
            report["board_import"]["drc"]["findings"][0]["suppressed"] = True
        elif mutate == "nonzero-incomplete":
            report["stats"]["incomplete"] = 1
        elif mutate == "boolean-incomplete":
            report["stats"]["incomplete"] = False
        elif mutate == "float-incomplete":
            report["stats"]["incomplete"] = 0.0
        elif mutate == "missing-incomplete":
            report["stats"].pop("incomplete")
        elif mutate == "wrong-session-kind":
            report["output_kind"] = "kicad_pcb"
        elif mutate == "wrong-session-path":
            report["artifact"]["path"] = str(path.with_name("other.ses"))
        elif mutate == "invalid-artifact":
            report["artifact"]["valid"] = False
        elif mutate == "wrong-artifact-kind":
            report["artifact"]["kind"] = "dsn"
        elif mutate == "missing-findings":
            report["board_import"]["drc"].pop("findings")
        elif mutate == "malformed-finding":
            report["board_import"]["drc"]["findings"][0] = None
        elif mutate == "warning-finding":
            report["board_import"]["drc"]["findings"][0]["severity"] = "warning"
        elif mutate == "unconfirmed-finding":
            report["board_import"]["drc"]["findings"][0]["confirmed_blocker"] = False
        elif mutate == "generic-import-error-message":
            report["board_import"]["message"] = "Could not import SES"
        return report

    state["initial_error_factory"] = initial_error
    result = autoroute_with_recovery(source, ses, routed_board_path=dest)

    assert result["status"] == "error"
    assert result["routing_complete"] is False
    repair.assert_not_called()
    accept.assert_not_called()
    assert not dest.exists()
    assert not ses.exists()


def test_dangling_cleanup_uses_fresh_local_library_stage_for_each_pass(tmp_path, monkeypatch):
    project = tmp_path / "project"
    library = project / "libraries" / "Local.pretty"
    library.mkdir(parents=True)
    (library / "Part.kicad_mod").write_text("(footprint test)\n", encoding="utf-8")
    (project / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "Local") (type "KiCad") '
        '(uri "${KIPRJMOD}/libraries/Local.pretty")))\n',
        encoding="utf-8",
    )
    source = project / "source.kicad_pcb"
    source.write_bytes(b"immutable authored board")
    source.with_suffix(".kicad_pro").write_text("{}\n", encoding="utf-8")
    partial = tmp_path / "partial.kicad_pcb"
    partial.write_bytes(b"router partial")
    work = tmp_path / "work"
    work.mkdir()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    warning_sequence = iter(("stub-1", "stub-2", None))

    def drc(board, **_kwargs):
        warning_id = next(warning_sequence)
        violations = [] if warning_id is None else [{
            "type": "track_dangling",
            "severity": "warning",
            "items": [{"uuid": warning_id, "pos": {"x": 10.0, "y": 0.0}}],
        }]
        findings = [
            ValidationIssue(
                "CW-DRC-002", message=f"open {index}", severity="blocker",
                detection_confidence="verified", rule_id="CW-DRC-002",
            )
            for index in range(2)
        ]
        if warning_id is not None:
            findings.append(ValidationIssue(
                "CW-DRC-841", level="warning", message="Track has unconnected end",
                severity="major", detection_confidence="verified", rule_id="CW-DRC-841",
            ))
        raw = {
            "violations": violations,
            "unconnected_items": [{"items": [{"uuid": f"open-{index}"}]} for index in range(2)],
            "schematic_parity": [],
        }
        return DrcResult(
            "ok", str(board), hashlib.sha256(Path(board).read_bytes()).hexdigest(),
            findings=tuple(findings), raw_report=raw,
        )

    staged_parents = []

    def cleanup(_operation, **kwargs):
        output = Path(kwargs["output"])
        staged_parents.append(output.parent)
        assert (output.parent / "fp-lib-table").read_bytes() == (project / "fp-lib-table").read_bytes()
        assert (output.parent / "libraries" / "Local.pretty" / "Part.kicad_mod").is_file()
        output.write_bytes(b"candidate " + str(len(staged_parents)).encode())
        uuid = kwargs["warning_rows"][0]["items"][0]["uuid"]
        return {
            "status": "ok",
            "router_dangling_cleanup": {
                "status": "candidate_only_requires_fresh_drc",
                "removed_tracks": [{"uuid": uuid}],
                "source_copper_exact_subset_preserved": True,
                "saved_removed_tracks_verified": True,
            },
        }

    monkeypatch.setattr("circuit_weaver.drc_runner.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.kicad_bridge.run_kicad", cleanup)
    result, final_drc, summary = autoroute_recovery._clean_router_dangling_tracks(
        source,
        partial,
        work=work,
        python=None,
        deadline=time.monotonic() + 60,
        source_snapshots={source: source_hash},
    )

    assert result.name == "partial-cleaned-02.kicad_pcb"
    assert staged_parents == [
        work / "native-dangling-cleanup" / "step-01",
        work / "native-dangling-cleanup" / "step-02",
    ]
    assert len(set(staged_parents)) == 2
    assert all((parent / "fp-lib-table").is_file() for parent in staged_parents)
    assert final_drc.blocker_count == 2
    assert summary["removed_track_count"] == 2
    assert source.read_bytes() == b"immutable authored board"


def test_validated_initial_ses_hash_must_stay_stable_through_quarantine_import(recovery):
    source, ses, dest, state, _, repair, accept, _ = recovery
    state["initial_error_factory"] = _initial_native_open_rejection
    state["mutate_session_on_import"] = True

    result = autoroute_with_recovery(source, ses, routed_board_path=dest)

    assert result["status"] == "error"
    assert "Validated private SES changed" in result["message"]
    repair.assert_not_called()
    accept.assert_not_called()
    assert not dest.exists()
    assert not ses.exists()


@pytest.mark.parametrize(
    "change,private_valid",
    [
        (None, True),
        ("extra-net", False),
        ("wrong-design", False),
        ("wrong-base", False),
    ],
)
def test_private_partial_semantics_relax_only_missing_nets(tmp_path, change, private_valid):
    dsn = tmp_path / "board.dsn"
    dsn.write_text(
        """(pcb board (resolution mil 1000) (unit mil)
      (structure (layer F.Cu (type signal))
        (boundary (path pcb 0 0 0 100 0 100 100 0 100 0 0)))
      (placement (component resistor (place R1 10 10 front 0)))
      (library (image resistor))
      (network (net GND (pins R1-1 R2-1)) (net VCC (pins R1-2 R2-2))))""",
        encoding="utf-8",
    )
    text = """(session board (base_design board.dsn) (placement)
      (routes (resolution mil 1000) (library_out)
        (network_out (net GND (wire (path F.Cu 10 10 10 20 20))))))"""
    if change == "extra-net":
        text = text.replace("net GND", "net INTRUDER")
    elif change == "wrong-design":
        text = text.replace("session board", "session unrelated")
    elif change == "wrong-base":
        text = text.replace("base_design board.dsn", "base_design unrelated.dsn")
    ses = tmp_path / "partial.ses"
    ses.write_text(text, encoding="utf-8")
    assert _validate_specctra_artifact(dsn, "dsn")["valid"]
    assert not _validate_specctra_artifact(ses, "ses", source_dsn=dsn)["valid"]
    assert (
        _validate_specctra_artifact(
            ses,
            "ses",
            source_dsn=dsn,
            allow_partial_nets=True,
        )["valid"]
        is private_valid
    )
