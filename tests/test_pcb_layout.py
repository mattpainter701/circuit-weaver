import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.kicad_bridge import run_kicad
from circuit_weaver.pcb_layout import import_routing_session, place_pcb
from circuit_weaver.placement_optimizer import ComponentPlacement, _cost_connectivity


def test_real_pad_cost_distinguishes_pin_orientation():
    a = ComponentPlacement("U1", 10, 10, net_pad_offsets={"SIG": [(2, 0)]})
    b = ComponentPlacement("R1", 16, 10, net_pad_offsets={"SIG": [(-2, 0)]})
    aligned = _cost_connectivity([a, b], {})
    reversed_pin = _cost_connectivity([replace(a, rotation=180), b], {})
    assert aligned < reversed_pin
    rotated = replace(a, rotation=90)
    vertical = replace(b, x=10, y=4, rotation=90)
    assert _cost_connectivity([rotated, vertical], {}) == pytest.approx(aligned)


@pytest.mark.parametrize("failure", ["drc", "worker", "changed_board", "new_project", "changed_session"])
def test_failed_import_preserves_complete_previous_output(tmp_path, monkeypatch, failure):
    source, session, destination = (tmp_path / n for n in ("source.kicad_pcb", "input.ses", "out.kicad_pcb"))
    source.write_bytes(b"source")
    session.write_bytes(b"session")
    project = source.with_suffix(".kicad_pro")
    if failure != "new_project":
        project.write_bytes(b"source settings")
    destination.write_bytes(b"previous board")
    destination.with_suffix(".kicad_pro").write_bytes(b"previous settings")

    def worker(operation, **kwargs):
        if failure == "worker":
            return {"status": "error", "message": "failed import"}
        Path(kwargs["output"]).write_bytes(b"staged board")
        if failure == "changed_board":
            source.write_bytes(b"new source")
        if failure == "changed_session":
            session.write_bytes(b"new session")
        if failure == "new_project":
            project.write_bytes(b"concurrent project")
        return {"status": "ok"}

    monkeypatch.setattr("circuit_weaver.pcb_layout.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_layout.run_drc", lambda *a, **k: DrcResult(
        status="error" if failure == "drc" else "ok", board=str(a[0]), failure_reason="failed DRC",
    ))
    result = import_routing_session(source, session, destination, overwrite=True)
    assert result["status"] == "error"
    assert destination.read_bytes() == b"previous board"
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"previous settings"
    assert not list(tmp_path.glob(".cw-import-*"))


def test_import_rejects_board_changed_since_routing_started(tmp_path, monkeypatch):
    source = tmp_path / "board.kicad_pcb"
    source.write_bytes(b"new board")
    def unexpected(*args, **kwargs):
        pytest.fail("Stale input reached KiCad")
    monkeypatch.setattr("circuit_weaver.pcb_layout.run_kicad", unexpected)
    result = import_routing_session(source, tmp_path / "input.ses", tmp_path / "out.kicad_pcb",
                                    expected_hashes={source: hashlib.sha256(b"old board").hexdigest()})
    assert result["status"] == "error"
    assert "changed" in result["message"]


def test_original_board_is_never_replaced(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    source.write_bytes(b"original")
    assert place_pcb(source, source, overwrite=True)["status"] == "error"
    assert import_routing_session(source, tmp_path / "input.ses", source, overwrite=True)["status"] == "error"
    assert source.read_bytes() == b"original"


@pytest.mark.parametrize("stdout", ['CW_KICAD_RESULT=[]', 'CW_KICAD_RESULT={"status":"invalid"}', 'unexpected stdout'])
def test_worker_malformed_output_is_a_structured_error(tmp_path, monkeypatch, stdout):
    from types import SimpleNamespace

    monkeypatch.setattr("circuit_weaver.kicad_bridge.find_kicad_python", lambda *a: "python")
    monkeypatch.setattr("circuit_weaver.kicad_bridge.subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=stdout, stderr=""))
    board = tmp_path / "example.kicad_pcb"
    board.write_bytes(b"board")
    assert run_kicad("inspect", board=str(board))["status"] == "error"


def test_native_worker_cannot_modify_original_project(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    board = tmp_path / "input.kicad_pcb"
    project = board.with_suffix(".kicad_pro")
    board.write_bytes(b"board")
    project.write_bytes(b"settings")
    monkeypatch.setattr("circuit_weaver.kicad_bridge.find_kicad_python", lambda *a: "python")
    def worker(*args, **kwargs):
        request = json.loads(kwargs["input"])
        copy = Path(request["board"])
        assert copy != board
        copy.write_bytes(b"mutated by native library")
        copy.with_suffix(".kicad_pro").write_bytes(b"updated UI paths")
        return SimpleNamespace(returncode=0, stdout='CW_KICAD_RESULT={"status":"ok"}', stderr="")
    monkeypatch.setattr("circuit_weaver.kicad_bridge.subprocess.run", worker)
    assert run_kicad("inspect", board=str(board))["status"] == "ok"
    assert board.read_bytes() == b"board"
    assert project.read_bytes() == b"settings"


def _mock_place_attempt(monkeypatch, outcomes, *, source=None):
    calls = []

    def attempt(board_path, output_path, **kwargs):
        index = len(calls)
        calls.append((kwargs["seed"], kwargs["iterations"], kwargs["tidy_references"]))
        Path(output_path).write_bytes(f"candidate-{index}".encode())
        project = Path(board_path).with_suffix(".kicad_pro")
        if project.is_file():
            Path(output_path).with_suffix(".kicad_pro").write_bytes(project.read_bytes())
        if source is not None and index == 0 and outcomes[index].get("change_source"):
            Path(board_path).write_bytes(b"concurrent source update")
        return outcomes[index]

    monkeypatch.setattr("circuit_weaver.pcb_layout._place_pcb_once", attempt)
    return calls


def _candidate(status, unresolved, remaining, *, payload="candidate"):
    return {"status": status, "output_path": payload,
            "placement": {"silkscreen": {"unresolved_count": unresolved,
                                           "remaining_conflict_count": remaining}},
            "drc": {"board": payload, "status": "ok"}}


def test_tidy_placement_retries_and_publishes_first_native_clean_candidate(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    calls = _mock_place_attempt(monkeypatch, [
        _candidate("error", 1, 1), _candidate("error", 0, 1), _candidate("ok", 0, 0),
    ])

    result = place_pcb(source, destination, iterations=321, seed=7, tidy_references=True)

    assert result["status"] == "ok"
    assert destination.read_bytes() == b"candidate-2"
    assert result["output_path"] == str(destination.resolve())
    assert result["drc"]["board"] == str(destination.resolve())
    assert result["selected_seed"] == 9
    assert result["selected_attempt"] == 3
    assert result["requested_seed"] == 7
    assert result["iterations_per_attempt"] == 321
    assert [row["seed"] for row in result["placement_attempts"]] == [7, 8, 9]
    assert [row["iterations"] for row in result["placement_attempts"]] == [321, 321, 321]
    assert calls == [(7, 321, True), (8, 321, True), (9, 321, True)]
    assert not list(tmp_path.glob(".cw-place-retries-*"))


def test_tidy_placement_exhaustion_never_publishes_candidate(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    calls = _mock_place_attempt(monkeypatch, [
        _candidate("error", 1, 0), _candidate("ok", 0, 1), _candidate("error", 0, 2),
    ])

    result = place_pcb(source, destination, seed=11, tidy_references=True)

    assert result["status"] == "error"
    assert "not published" in result["message"]
    assert len(result["placement_attempts"]) == len(calls) == 3
    assert result["best_candidate"]["placement"]["silkscreen"]["unresolved_count"] == 1
    assert not destination.exists()
    assert not list(tmp_path.glob(".cw-place-retries-*"))


def test_tidy_exhaustion_preserves_existing_output_bundle(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    destination.write_bytes(b"old board")
    project = destination.with_suffix(".kicad_pro")
    project.write_bytes(b"old project")
    _mock_place_attempt(monkeypatch, [
        _candidate("error", 1, 0), _candidate("ok", 0, 1), _candidate("error", 0, 2),
    ])

    result = place_pcb(source, destination, seed=11, tidy_references=True, overwrite=True)

    assert result["status"] == "error"
    assert destination.read_bytes() == b"old board"
    assert project.read_bytes() == b"old project"


def test_tidy_retry_requires_complete_native_silkscreen_report(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    incomplete = _candidate("ok", 0, 0)
    incomplete["placement"]["silkscreen"].pop("remaining_conflict_count")
    _mock_place_attempt(monkeypatch, [incomplete, incomplete, incomplete])

    result = place_pcb(source, destination, tidy_references=True)

    assert result["status"] == "error"
    assert len(result["placement_attempts"]) == 3
    assert not destination.exists()


@pytest.mark.parametrize("kwargs, message", [
    ({"tidy_references": None}, "tidy_references must be a boolean"),
    ({"seed": None, "tidy_references": False}, "seed must be an integer"),
    ({"seed": "7", "tidy_references": True}, "seed must be an integer"),
])
def test_place_rejects_invalid_retry_inputs_before_dispatch(tmp_path, monkeypatch, kwargs, message):
    def unexpected(*args, **kw):
        pytest.fail("Invalid arguments reached placement")
    monkeypatch.setattr("circuit_weaver.pcb_layout._place_pcb_once", unexpected)
    result = place_pcb(tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb", **kwargs)
    assert result == {"status": "error", "message": message}


def test_tidy_retry_fails_fast_on_unrelated_placement_error(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    calls = _mock_place_attempt(monkeypatch, [
        {"status": "error", "message": "native DRC failed"},
        _candidate("ok", 0, 0),
    ])

    result = place_pcb(source, destination, tidy_references=True)

    assert result["status"] == "error"
    assert len(calls) == 1
    assert result["best_candidate"]["message"] == "native DRC failed"


def test_missing_native_counts_are_json_finite_and_diagnostic(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    incomplete = _candidate("ok", 0, 0)
    incomplete["placement"]["silkscreen"]["remaining_conflict_count"] = float("inf")
    _mock_place_attempt(monkeypatch, [incomplete, incomplete, incomplete])

    result = place_pcb(source, destination, tidy_references=True)

    assert result["status"] == "error"
    assert result["placement_attempts"][0]["remaining_conflict_count"] is None
    assert result["placement_attempts"][0]["rejection_reason"] == "native_silkscreen_count_invalid"
    import json
    json.dumps(result, allow_nan=False)


def test_tidy_search_aborts_if_source_changes_between_attempts(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    outcomes = [_candidate("error", 1, 0, payload="first"), _candidate("ok", 0, 0)]
    outcomes[0]["change_source"] = True
    calls = _mock_place_attempt(monkeypatch, outcomes, source=source)

    result = place_pcb(source, destination, tidy_references=True)

    assert result["status"] == "error"
    assert "changed during processing" in result["message"]
    assert len(calls) == 1
    assert not destination.exists()


def test_tidy_false_keeps_single_attempt_path(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    calls = _mock_place_attempt(monkeypatch, [_candidate("ok", 9, 9)])

    result = place_pcb(source, destination, seed=4, tidy_references=False)

    assert result["status"] == "ok"
    assert calls == [(4, 5000, False)]
    assert "placement_attempts" not in result


def test_tidy_retry_preserves_existing_output_on_rejected_candidates(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    source.with_suffix(".kicad_pro").write_bytes(b"source project")
    destination.write_bytes(b"previous board")
    destination.with_suffix(".kicad_pro").write_bytes(b"previous project")
    calls = _mock_place_attempt(monkeypatch, [
        _candidate("error", 1, 0), _candidate("ok", 0, 0),
    ])

    rejected = place_pcb(source, destination, seed=1, tidy_references=True, overwrite=False)
    assert rejected["status"] == "error"
    assert calls == []
    assert destination.read_bytes() == b"previous board"

    accepted = place_pcb(source, destination, seed=1, tidy_references=True, overwrite=True)
    assert accepted["status"] == "ok"
    assert destination.read_bytes() == b"candidate-1"
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"source project"
