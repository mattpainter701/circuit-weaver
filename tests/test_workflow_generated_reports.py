from __future__ import annotations

import json
from pathlib import Path

import pytest

from circuit_weaver import project_workflow as workflow
from circuit_weaver.project_state import load_project_state

REPORT_SETS = [
    ("ground-plane.json",),
    ("route-quality.json",),
    ("ground-plane.json", "route-quality.json"),
]


def _bare_board(root: Path, reports: tuple[str, ...]) -> Path:
    root.mkdir()
    board = root / "prototype.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    for name in reports:
        (root / name).write_text(json.dumps({"status": "verified"}), encoding="utf-8")
    return board


@pytest.mark.parametrize("reports", REPORT_SETS)
def test_generated_reports_do_not_become_specs_for_bare_board_open_plan_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reports: tuple[str, ...]
) -> None:
    root = tmp_path / "bare-board"
    board = _bare_board(root, reports)
    monkeypatch.setattr(workflow, "_native_footprint_context_inputs", lambda _project: [])

    assert not workflow._has_spec(root)
    assert not workflow._project_has_spec(root)
    with pytest.raises(workflow.WorkflowError, match="No design spec") as error:
        workflow._spec_path(root)
    assert error.value.code == "spec_missing"

    opened = workflow.open_project(root)
    assert opened["ok"] is True
    assert opened["summary"]["kind"] == "native"
    assert opened["summary"]["read_only"] is True

    planned = workflow.plan_workflow(root, stages=["place"])
    assert planned["ok"] is True, planned
    assert {Path(row["path"]) for row in planned["plan"]["inputs"]} == {board.resolve()}

    def fake_stage(project: Path, stage: str, options: dict, prior: list[str]):
        assert stage == "place"
        assert options == {}
        output = project / "placed.kicad_pcb"
        output.write_text("(kicad_pcb placed)", encoding="utf-8")
        return {"status": "ok"}, [str(output)]

    monkeypatch.setattr(workflow, "_execute_stage", fake_stage)
    resumed = workflow.resume_workflow(root)
    assert resumed["ok"] is True, resumed
    assert resumed["stages"] == ["place"]
    assert (root / "placed.kicad_pcb").is_file()


def test_place_command_does_not_pass_generated_report_as_design(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "bare-board"
    _bare_board(root, ("ground-plane.json", "route-quality.json"))
    commands = []

    def fake_cli(stage: str, args: list[str], project: Path, timeout: int):
        assert stage == "place"
        commands.append(args)
        output = Path(args[args.index("--output") + 1])
        output.write_text("(kicad_pcb placed)", encoding="utf-8")
        return {"status": "ok"}

    monkeypatch.setattr(workflow, "_cli", fake_cli)
    result, outputs = workflow._execute_stage(root, "place", {}, [])

    assert result["status"] == "ok"
    assert len(commands) == 1
    assert "--design" not in commands[0]
    assert outputs == [str(root / "placed.kicad_pcb")]


def test_legitimate_json_spec_is_selected_and_design_yaml_has_precedence(tmp_path: Path) -> None:
    root = tmp_path / "json-design"
    _bare_board(root, ("ground-plane.json", "route-quality.json"))
    json_spec = root / "assembly-intent.json"
    json_spec.write_text(json.dumps({"project": "valid JSON design"}), encoding="utf-8")

    assert workflow._spec_path(root) == json_spec
    assert workflow._has_spec(root)
    assert {Path(path) for path in workflow._stage_inputs(root, "validate", [])} == {json_spec.resolve()}

    canonical = root / "design.yaml"
    canonical.write_text("project: canonical\nblocks: []\n", encoding="utf-8")
    assert workflow._spec_path(root) == canonical
    assert {Path(path) for path in workflow._stage_inputs(root, "validate", [])} == {canonical.resolve()}


def test_adoption_keeps_generated_reports_as_project_inputs_not_design_specs(tmp_path: Path) -> None:
    root = tmp_path / "adopt-bare-board"
    _bare_board(root, ("ground-plane.json", "route-quality.json"))

    adopted = workflow.adopt_project(root)

    assert adopted["ok"] is True, adopted
    state = load_project_state(root)
    assert state is not None
    assert state.kind == "native"
    assert not any(source["kind"] == "design_spec" for source in state.sources)
    assert {source["path"] for source in state.sources} >= {"ground-plane.json", "route-quality.json"}


def test_generated_reports_alone_do_not_create_a_circuit_project(tmp_path: Path) -> None:
    root = tmp_path / "reports-only"
    root.mkdir()
    for filename in ("ground-plane.json", "route-quality.json"):
        (root / filename).write_text('{"status":"verified"}', encoding="utf-8")

    adopted = workflow.adopt_project(root)

    assert adopted["ok"] is False
    assert adopted["error"]["code"] == "unsupported_target"
    assert load_project_state(root) is None
