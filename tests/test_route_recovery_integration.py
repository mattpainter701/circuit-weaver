"""Adversarial publication contracts for opt-in native routing recovery."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from circuit_weaver.autoroute import _validate_specctra_artifact
from circuit_weaver.autoroute_recovery import autoroute_with_recovery


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
        "repair_status": "ok",
        "convert_status": "ok",
        "import_status": "ok",
        "accept_status": "ok",
        "mutate": None,
    }

    def route(board, output, **options):
        if options.get("_allow_partial_nets"):
            Path(output).write_text("partial session", encoding="utf-8")
            if state["mutate"] == "source":
                source.write_text("concurrent edit", encoding="utf-8")
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
        if state["import_status"] == "ok":
            Path(options["output"]).write_text("private candidate", encoding="utf-8")
        return {"status": state["import_status"]}

    def repair(board, output, **options):
        Path(output).write_text("recovered candidate", encoding="utf-8")
        return {"status": state["repair_status"], "output_path": str(output)}

    def accept(board, ses, output, **options):
        if state["accept_status"] == "ok":
            Path(output).write_text("accepted board", encoding="utf-8")
        return {"status": state["accept_status"], "drc": {"board_sha256": "verified"}}

    route_mock = Mock(side_effect=route)
    repair_mock = Mock(side_effect=repair)
    accept_mock = Mock(side_effect=accept)
    monkeypatch.setattr(router, "autoroute_pcb", route_mock)
    monkeypatch.setattr(recovery_module, "_serialize_native_session", Mock(side_effect=serialize))
    monkeypatch.setattr(bridge, "run_kicad", Mock(side_effect=import_partial))
    monkeypatch.setattr(native, "recover_routing", repair_mock)
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
