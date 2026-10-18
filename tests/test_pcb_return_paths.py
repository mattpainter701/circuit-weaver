import hashlib
import json
from pathlib import Path

import pytest

from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.pcb_return_paths import load_return_plan, refine_return_paths

CONNECTION = {"from_ref": "J1", "from_pad": "1", "to_ref": "C1", "to_pad": "2"}


@pytest.mark.parametrize("value", [
    {}, {"connections": []}, {"connections": [None]},
    {"connections": [{**CONNECTION, "net": "GND"}]},
    {"connections": [{**CONNECTION, "to_pad": 2}]},
    {"connections": [CONNECTION], "overwrite": True},
    {"connections": [{**CONNECTION, "max_length_mm": True}]},
    {"connections": [{**CONNECTION, "max_length_mm": float("nan")}]},
])
def test_return_plan_is_strict(tmp_path, value):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_return_plan(plan)


def test_load_return_plan_accepts_only_explicit_pad_pairs(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"connections": [CONNECTION]}), encoding="utf-8")
    assert load_return_plan(plan) == {"connections": [CONNECTION]}


def test_load_return_plan_accepts_finite_length_limit_and_rejects_duplicate_keys(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"connections": [{**CONNECTION, "max_length_mm": 8.0}]}), encoding="utf-8")
    assert load_return_plan(plan)["connections"][0]["max_length_mm"] == 8.0
    plan.write_text('{"connections":[],"connections":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        load_return_plan(plan)


def _drc(path: Path, *, fail=False):
    data = path.read_bytes()
    return DrcResult(status="error" if fail else "ok", board=str(path),
                     board_sha256=hashlib.sha256(data).hexdigest(), failure_reason="failed" if fail else "")


def test_refinement_preserves_inputs_and_publishes_only_verified_board(tmp_path, monkeypatch):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"native source board")
    source.with_suffix(".kicad_pro").write_bytes(b"project settings")
    source.with_suffix(".kicad_dru").write_bytes(b"rules")
    destination = tmp_path / "refined.kicad_pcb"
    original = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    board_bytes = b"same tracks plus return shortcuts"

    def worker(operation, **kwargs):
        assert operation == "refine_returns"
        assert kwargs["connections"] == [CONNECTION]
        Path(kwargs["output"]).write_bytes(board_bytes)
        return {"status": "ok", "refinement": {
                    "connections": [{**CONNECTION, "status": "routed", "planar_length_mm": 4.0}],
                    "original_copper_preserved": True,
                    "original_footprints_and_text_preserved": True,
                },
                "silkscreen": {"unresolved_count": 0, "remaining_conflict_count": 0}}

    monkeypatch.setattr("circuit_weaver.pcb_return_paths.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_return_paths.run_drc", lambda path, **_: _drc(path))
    result = refine_return_paths(source, destination, [CONNECTION])

    assert result["status"] == "ok"
    assert result["output_kind"] == "refined_kicad_board"
    assert result["fabrication_ready"] is False
    assert destination.read_bytes() == board_bytes
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"project settings"
    assert destination.with_suffix(".kicad_dru").read_bytes() == b"rules"
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.name in original} == original
    assert result["source_sha256"] == hashlib.sha256(original["input.kicad_pcb"]).hexdigest()


@pytest.mark.parametrize("settings_change", ["create", "delete", "modify"])
def test_input_drc_settings_changes_are_detected_before_worker_or_publication(
    tmp_path, monkeypatch, settings_change
):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")
    settings = source.with_suffix(".kicad_pro" if settings_change != "delete" else ".kicad_dru")
    if settings_change in {"delete", "modify"}:
        settings.write_bytes(b"original settings")
    destination = tmp_path / "out.kicad_pcb"
    destination.write_bytes(b"previous board")
    destination.with_suffix(".kicad_pro").write_bytes(b"previous project")

    def drc(path, **kwargs):
        if settings_change == "create":
            settings.write_bytes(b"created during input DRC")
        elif settings_change == "delete":
            settings.unlink()
        else:
            settings.write_bytes(b"modified during input DRC")
        return _drc(path)

    def unexpected_worker(*args, **kwargs):
        pytest.fail("Changed source project settings reached the native refinement worker")

    monkeypatch.setattr("circuit_weaver.pcb_return_paths.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.pcb_return_paths.run_kicad", unexpected_worker)

    result = refine_return_paths(source, destination, [CONNECTION], overwrite=True)

    assert result["status"] == "error"
    assert "settings changed during processing" in result["message"]
    assert destination.read_bytes() == b"previous board"
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"previous project"
    assert not list(tmp_path.glob(".cw-returns-*"))


@pytest.mark.parametrize("failure", [
    "input_drc", "output_drc", "silkscreen", "worker", "missing_flags", "false_flags", "missing_connection",
    "bad_connection_status", "changed_pro", "changed_dru",
])
def test_refinement_failure_does_not_replace_existing_output(tmp_path, monkeypatch, failure):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")
    destination = tmp_path / "out.kicad_pcb"
    destination.write_bytes(b"previous")
    destination.with_suffix(".kicad_pro").write_bytes(b"previous project")

    def worker(operation, **kwargs):
        if failure == "worker":
            return {"status": "error", "message": "native refusal"}
        Path(kwargs["output"]).write_bytes(b"candidate")
        if failure in {"changed_pro", "changed_dru"}:
            source.with_suffix(".kicad_" + failure.removeprefix("changed_")).write_bytes(b"concurrent settings edit")
        report = {"connections": [{**CONNECTION, "status": "routed", "planar_length_mm": 4.0}],
                  "original_copper_preserved": True,
                  "original_footprints_and_text_preserved": True}
        if failure == "missing_flags":
            report.pop("original_copper_preserved")
        if failure == "false_flags":
            report["original_footprints_and_text_preserved"] = False
        if failure == "missing_connection":
            report["connections"] = []
        if failure == "bad_connection_status":
            report["connections"][0]["status"] = "error"
        return {"status": "ok", "refinement": report,
                "silkscreen": {"unresolved_count": 1 if failure == "silkscreen" else 0,
                               "remaining_conflict_count": 0}}

    calls = 0

    def drc(path, **kwargs):
        nonlocal calls
        calls += 1
        return _drc(path, fail=failure == "input_drc" or (failure == "output_drc" and calls == 2))

    monkeypatch.setattr("circuit_weaver.pcb_return_paths.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_return_paths.run_drc", drc)
    result = refine_return_paths(source, destination, [CONNECTION], overwrite=True)

    assert result["status"] == "error"
    assert destination.read_bytes() == b"previous"
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"previous project"
    assert not list(tmp_path.glob(".cw-returns-*"))


def test_refinement_rejects_in_place_and_non_board_output(tmp_path):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")
    assert refine_return_paths(source, source, [CONNECTION])["status"] == "error"
    assert refine_return_paths(source, tmp_path / "out.ses", [CONNECTION])["status"] == "error"


def test_cli_reports_invalid_return_plan_as_json_error(tmp_path, monkeypatch, capsys):
    from circuit_weaver import dispatcher

    plan = tmp_path / "plan.json"
    plan.write_text('{"connections":[],"unknown":true}', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["circuit-weaver", "refine-pcb", str(tmp_path / "board.kicad_pcb"),
                                     "-o", str(tmp_path / "out.kicad_pcb"), "--connections", str(plan)])
    with pytest.raises(SystemExit) as exc:
        dispatcher.main()
    assert exc.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "error"
    assert "only 'connections'" in output["message"]
