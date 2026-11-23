from pathlib import Path

import pytest

from circuit_weaver import project_workflow as workflow


def _project(tmp_path):
    board = tmp_path / "prototype.kicad_pcb"
    board.write_text("native board fixture")
    library = tmp_path / "footprints" / "Local.pretty"
    library.mkdir(parents=True)
    (library / "A.kicad_mod").write_text("original footprint")
    (tmp_path / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "Local") (type "KiCad") '
        '(uri "${KIPRJMOD}/footprints/Local.pretty")))'
    )
    return board, library


@pytest.mark.parametrize("stage", ["place", "route"])
def test_shared_stage_tracks_library_contents_and_membership(tmp_path, stage):
    _, library = _project(tmp_path)
    paths = workflow._stage_inputs(tmp_path, stage, [])
    before = workflow._snapshot(tmp_path, paths)
    assert library in paths
    (library / "B.kicad_mod").write_text("new footprint during native operation")
    assert workflow._snapshot(tmp_path, paths) != before


def test_absent_optional_library_table_is_watched_without_blocking_standalone_stage(tmp_path):
    board = tmp_path / "prototype.kicad_pcb"
    board.write_text("native board fixture")
    output = tmp_path / "placed.kicad_pcb"

    def action():
        output.write_text("placed board fixture")
        return {"status": "ok"}

    result = workflow.run_stage(tmp_path, "place", action, inputs=[board], outputs=[output])
    assert result["ok"], result
    state = workflow._runner(workflow._load(tmp_path))
    watched = state["stages"]["place"]["inputs"]
    assert any(row["path"] == str(tmp_path / "fp-lib-table") and not row["exists"] for row in watched)


@pytest.mark.parametrize("change", ["new_table", "new_asset", "changed_asset"])
def test_standalone_stage_rejects_library_context_drift(tmp_path, change):
    if change == "new_table":
        board = tmp_path / "prototype.kicad_pcb"
        board.write_text("native board fixture")
        library = None
    else:
        board, library = _project(tmp_path)
    output = tmp_path / "placed.kicad_pcb"

    def action():
        if change == "new_table":
            (tmp_path / "fp-lib-table").write_text("(fp_lib_table)")
        elif change == "new_asset":
            (library / "B.kicad_mod").write_text("new footprint")
        else:
            (library / "A.kicad_mod").write_text("changed footprint")
        output.write_text("candidate board")
        return {"status": "ok"}

    result = workflow.run_stage(tmp_path, "place", action, inputs=[board], outputs=[output])
    assert not result["ok"]
    assert result["error"]["code"] == "input_changed", result


def test_build_command_receives_project_root_for_generated_schematic(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    (output / "design.kicad_sch").write_text("schematic fixture")
    observed = {}

    def fake_cli(stage, command, root, timeout):
        observed["command"] = command
        target = Path(command[command.index("--output") + 1])
        target.write_text("native board fixture")
        return {"status": "ok", "output_path": str(target)}

    monkeypatch.setattr(workflow, "_cli", fake_cli)
    workflow._execute_stage(tmp_path, "build", {"board_width_mm": 20, "board_height_mm": 20}, [])
    command = observed["command"]
    assert command[command.index("--project-root") + 1] == str(tmp_path)
