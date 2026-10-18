import hashlib
import json
from pathlib import Path

import pytest

from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.pcb_copper_cleanup import cleanup_copper, load_cleanup_plan

CONNECTION = {"from_ref": "J1", "from_pad": "1", "to_ref": "C1", "to_pad": "2"}
PLAN = {"nets": ["GND"], "protected_connections": [CONNECTION]}


@pytest.mark.parametrize("content", [
    "{}", '{"nets":[],"protected_connections":[]}',
    '{"nets":["GND","GND"],"protected_connections":[]}',
    '{"nets":["GND"],"protected_connections":[],"extra":true}',
    '{"nets":["GND"],"protected_connections":[],"nets":["GND"]}',
    json.dumps({"nets": ["GND"], "protected_connections": [
        CONNECTION, {**CONNECTION, "max_length_mm": 1.0},
    ]}),
    '{"nets":["GND"],"protected_connections":[{"from_ref":"J1","from_pad":"1",'
    '"to_ref":"C1","to_pad":"2","net":"GND"}]}',
])
def test_cleanup_plan_is_strict(tmp_path, content):
    plan = tmp_path / "plan.json"
    plan.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        load_cleanup_plan(plan)


def test_cleanup_plan_normalizes_unique_nets_and_accepts_bounded_connections(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"nets": [" GND "], "protected_connections": [
        {**CONNECTION, "max_length_mm": 8},
    ]}), encoding="utf-8")

    assert load_cleanup_plan(plan) == {
        "nets": ["GND"],
        "protected_connections": [{**CONNECTION, "max_length_mm": 8.0}],
    }


def _drc(path: Path, *, fail=False):
    return DrcResult(status="error" if fail else "ok", board=str(path),
                     board_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                     failure_reason="failed" if fail else "")


def _report():
    return {
        "all_pads_connected": True,
        "protected_paths_preserved": True,
        "unselected_copper_preserved": True,
        "original_footprints_and_text_preserved": True,
        "copper_changes_limited_to_selected_nets": True,
        "locked_copper_preserved": True,
        "protected_connections": [{**CONNECTION, "status": "connected", "protected_path_preserved": True}],
    }


def test_cleanup_publishes_only_fully_verified_output_and_restores_project_settings(tmp_path, monkeypatch):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source board")
    source.with_suffix(".kicad_pro").write_bytes(b"project")
    source.with_suffix(".kicad_dru").write_bytes(b"rules")
    destination = tmp_path / "cleaned.kicad_pcb"
    original = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    candidate = b"cleaned board with fewer copper items"

    def worker(operation, **kwargs):
        assert operation == "cleanup_copper"
        assert kwargs["nets"] == ["GND"]
        assert kwargs["connections"] == [CONNECTION]
        Path(kwargs["output"]).write_bytes(candidate)
        return {"status": "ok", "cleanup": _report(),
                "silkscreen": {"unresolved_count": 0, "remaining_conflict_count": 0}}

    monkeypatch.setattr("circuit_weaver.pcb_copper_cleanup.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_copper_cleanup.run_drc", lambda path, **_: _drc(path))

    result = cleanup_copper(source, destination, ["GND"], [CONNECTION])

    assert result["status"] == "ok"
    assert result["output_kind"] == "cleaned_kicad_board"
    assert result["fabrication_ready"] is False
    assert destination.read_bytes() == candidate
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"project"
    assert destination.with_suffix(".kicad_dru").read_bytes() == b"rules"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name in original} == original
    assert result["source_sha256"] == hashlib.sha256(original["input.kicad_pcb"]).hexdigest()


@pytest.mark.parametrize("failure", ["input_drc", "output_drc", "stale_output_drc", "silkscreen", "missing_flag",
                                      "false_flag", "missing_row", "bad_row"])
def test_cleanup_rejection_preserves_existing_output(tmp_path, monkeypatch, failure):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")
    destination = tmp_path / "out.kicad_pcb"
    destination.write_bytes(b"previous output")
    destination.with_suffix(".kicad_pro").write_bytes(b"previous project")

    def worker(operation, **kwargs):
        Path(kwargs["output"]).write_bytes(b"candidate")
        report = _report()
        if failure == "missing_flag":
            report.pop("copper_changes_limited_to_selected_nets")
        elif failure == "false_flag":
            report["unselected_copper_preserved"] = False
        elif failure == "missing_row":
            report["protected_connections"] = []
        elif failure == "bad_row":
            report["protected_connections"][0]["protected_path_preserved"] = False
        return {"status": "ok", "cleanup": report,
                "silkscreen": {"unresolved_count": int(failure == "silkscreen"),
                               "remaining_conflict_count": 0}}

    calls = 0

    def drc(path, **kwargs):
        nonlocal calls
        calls += 1
        result = _drc(path, fail=failure == "input_drc" or (failure == "output_drc" and calls == 2))
        if failure == "stale_output_drc" and calls == 2:
            return DrcResult(status="ok", board=str(path), board_sha256="0" * 64)
        return result

    monkeypatch.setattr("circuit_weaver.pcb_copper_cleanup.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_copper_cleanup.run_drc", drc)

    result = cleanup_copper(source, destination, ["GND"], [CONNECTION], overwrite=True)

    assert result["status"] == "error"
    assert destination.read_bytes() == b"previous output"
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"previous project"
    assert not list(tmp_path.glob(".cw-copper-cleanup-*"))


def test_cleanup_detects_source_changes_during_input_drc_before_worker(tmp_path, monkeypatch):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")
    destination = tmp_path / "out.kicad_pcb"
    destination.write_bytes(b"previous")

    def drc(path, **kwargs):
        source.with_suffix(".kicad_pro").write_bytes(b"unexpected project settings")
        return _drc(path)

    def unexpected_worker(*args, **kwargs):
        pytest.fail("Source change reached the native cleanup worker")

    monkeypatch.setattr("circuit_weaver.pcb_copper_cleanup.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.pcb_copper_cleanup.run_kicad", unexpected_worker)

    result = cleanup_copper(source, destination, ["GND"], [CONNECTION], overwrite=True)

    assert result["status"] == "error"
    assert "settings changed during processing" in result["message"]
    assert destination.read_bytes() == b"previous"


def test_cleanup_rejects_in_place_and_non_board_outputs(tmp_path):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source")

    assert cleanup_copper(source, source, ["GND"], [CONNECTION])["status"] == "error"
    assert cleanup_copper(source, tmp_path / "out.ses", ["GND"], [CONNECTION])["status"] == "error"


def test_cleanup_api_rejects_duplicate_protected_pairs_before_native_tools(tmp_path, monkeypatch):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"not reached")
    calls = []
    monkeypatch.setattr("circuit_weaver.pcb_copper_cleanup.run_drc", lambda *a, **k: calls.append("drc"))
    monkeypatch.setattr("circuit_weaver.pcb_copper_cleanup.run_kicad", lambda *a, **k: calls.append("worker"))

    result = cleanup_copper(source, tmp_path / "out.kicad_pcb", ["GND"], [
        {**CONNECTION, "max_length_mm": 1.0}, CONNECTION,
    ])

    assert result["status"] == "error"
    assert "duplicates a directed endpoint pair" in result["message"]
    assert calls == []


def test_native_worker_state_filter_distinguishes_selected_unselected_and_locked_copper():
    from collections import Counter

    from circuit_weaver._kicad_worker import _copper_state_for_nets

    rows = Counter({
        json.dumps({"net": "GND", "locked": False}): 1,
        json.dumps({"net": "VCC", "locked": False}): 1,
        json.dumps({"net": "GND", "locked": True}): 1,
    })

    assert len(_copper_state_for_nets(rows, ["GND"])) == 2
    assert len(_copper_state_for_nets(rows, ["GND"], include_selected=False)) == 1
    assert len(_copper_state_for_nets(rows, set(), include_selected=False, locked=True)) == 1


def test_cleanup_cli_rejects_invalid_plan_as_json_error(tmp_path, monkeypatch, capsys):
    from circuit_weaver import dispatcher

    plan = tmp_path / "plan.json"
    plan.write_text('{"nets":["GND"],"protected_connections":[],"unknown":true}', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["circuit-weaver", "cleanup-pcb", str(tmp_path / "board.kicad_pcb"),
                                     "-o", str(tmp_path / "out.kicad_pcb"), "--plan", str(plan)])

    with pytest.raises(SystemExit) as exc:
        dispatcher.main()

    assert exc.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "error"
    assert "only 'nets' and 'protected_connections'" in output["message"]
