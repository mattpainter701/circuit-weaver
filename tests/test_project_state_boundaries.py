"""Project-root resolution must respect circuit boundaries and recorded ownership."""

from pathlib import Path

from circuit_weaver.project_state import (
    ProjectState,
    get_project_state_summary,
    project_state_path,
    record_generation_state,
    resolve_project_root,
    save_project_state,
)


def test_unrelated_ancestor_manifest_does_not_own_child_native_board(tmp_path: Path) -> None:
    parent = tmp_path / "stateful-parent"
    child = parent / "python-project"
    child.mkdir(parents=True)
    parent_state = ProjectState(name="Unrelated parent", kind="circuit_weaver")
    save_project_state(parent, parent_state)
    board = child / "existing.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    assert resolve_project_root(board) == child.resolve()
    summary = get_project_state_summary(board)
    assert summary["project_root"] == str(child.resolve())
    assert summary["has_manifest"] is False
    assert project_state_path(parent).is_file()
    assert summary["project_id"] != parent_state.project_id


def test_direct_child_design_spec_is_its_own_boundary(tmp_path: Path) -> None:
    parent = tmp_path / "stateful-parent"
    child = parent / "new-circuit"
    child.mkdir(parents=True)
    save_project_state(parent, ProjectState(name="Unrelated parent", kind="circuit_weaver"))
    (child / "design.yaml").write_text("project: Child\nblocks: []\n", encoding="utf-8")

    assert resolve_project_root(child) == child.resolve()
    summary = get_project_state_summary(child)
    assert summary["project_root"] == str(child.resolve())
    assert summary["has_manifest"] is False


def test_parent_manifest_owns_recorded_generated_output_file_and_directory(tmp_path: Path) -> None:
    parent = tmp_path / "managed-parent"
    output = parent / "output"
    output.mkdir(parents=True)
    source = parent / "design.yaml"
    source.write_text("project: Parent\nblocks: []\n", encoding="utf-8")
    board = output / "prototype.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    record_generation_state(
        parent,
        project_name="Parent",
        spec_path=source,
        output_dir=output,
        phase="generated",
        artifacts=[board],
    )

    assert resolve_project_root(board) == parent.resolve()
    assert resolve_project_root(output) == parent.resolve()
    assert get_project_state_summary(board)["project_root"] == str(parent.resolve())


def test_parent_manifest_owns_checkpointed_native_output(tmp_path: Path) -> None:
    parent = tmp_path / "runner-parent"
    child = parent / "native-output"
    child.mkdir(parents=True)
    board = child / "prototype.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    state = ProjectState(
        name="Runner parent",
        kind="native",
        workflow={
            "runner": {
                "stages": {
                    "build": {
                        "outputs": [{"path": str(board.resolve()), "exists": True}],
                    }
                }
            }
        },
    )
    save_project_state(parent, state)

    assert resolve_project_root(board) == parent.resolve()
    assert get_project_state_summary(board)["project_root"] == str(parent.resolve())


def test_direct_native_board_wins_over_unclaimed_parent_design_boundary(tmp_path: Path) -> None:
    parent = tmp_path / "design-project"
    child = parent / "hardware"
    child.mkdir(parents=True)
    (parent / "design.yaml").write_text("project: Parent\nblocks: []\n", encoding="utf-8")
    save_project_state(parent, ProjectState(name="Parent", kind="circuit_weaver"))
    (child / "board.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")

    assert resolve_project_root(child) == child.resolve()
    assert resolve_project_root(child / "board.kicad_pcb") == child.resolve()


def test_direct_schematic_wins_over_unclaimed_parent_kicad_project(tmp_path: Path) -> None:
    parent = tmp_path / "kicad-project"
    child = parent / "hardware"
    child.mkdir(parents=True)
    (parent / "parent.kicad_pro").write_text("{}", encoding="utf-8")
    (child / "controller.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")

    assert resolve_project_root(child) == child.resolve()
    assert resolve_project_root(child / "controller.kicad_sch") == child.resolve()


def test_unrelated_ancestor_state_and_kicad_project_do_not_claim_child_board(tmp_path: Path) -> None:
    parent = tmp_path / "aggregator"
    child = parent / "independent-board"
    child.mkdir(parents=True)
    (parent / "routed-noopt.kicad_pro").write_text("{}", encoding="utf-8")
    parent_state = ProjectState(name="Unrelated aggregate", kind="native")
    save_project_state(parent, parent_state)
    board = child / "existing.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    summary = get_project_state_summary(board)

    assert resolve_project_root(board) == child.resolve()
    assert summary["project_root"] == str(child.resolve())
    assert summary["project_id"] != parent_state.project_id


def test_unclaimed_generated_directory_stops_at_nearest_state_ancestor(tmp_path: Path) -> None:
    aggregate = tmp_path / "unrelated-aggregate"
    aggregate.mkdir()
    aggregate_spec = aggregate / "design.yaml"
    aggregate_spec.write_text("project: Aggregate\nblocks: []\n", encoding="utf-8")
    aggregate_board = aggregate / "unrelated.kicad_pcb"
    aggregate_board.write_text("(kicad_pcb unrelated)", encoding="utf-8")
    aggregate_manifest = save_project_state(aggregate, ProjectState(name="Aggregate"))

    parent = aggregate / "parent-project"
    case = parent / "case"
    case.mkdir(parents=True)
    parent_manifest = save_project_state(parent, ProjectState(name="Parent"))
    case_state = ProjectState(name="Case")
    save_project_state(case, case_state)
    ancestor_bytes = {
        path: path.read_bytes()
        for path in (aggregate_spec, aggregate_board, aggregate_manifest, parent_manifest)
    }

    generated = case / "generated"
    assert resolve_project_root(generated) == generated.resolve()

    case_state.workflow["generate"] = {"output_dir": str(generated)}
    save_project_state(case, case_state)
    assert resolve_project_root(generated) == case.resolve()
    assert {path: path.read_bytes() for path in ancestor_bytes} == ancestor_bytes


def test_canonical_output_child_inherits_parent_project_boundary(tmp_path: Path) -> None:
    for parent_kind in ("state", "design"):
        parent = tmp_path / parent_kind
        output = parent / "output"
        output.mkdir(parents=True)
        if parent_kind == "state":
            save_project_state(parent, ProjectState(name="Parent", kind="circuit_weaver"))
        else:
            (parent / "design.yaml").write_text("project: Parent\nblocks: []\n", encoding="utf-8")
        board = output / "board.kicad_pcb"
        board.write_text("(kicad_pcb)", encoding="utf-8")

        assert resolve_project_root(output) == parent.resolve()
        assert resolve_project_root(board) == parent.resolve()


def test_generation_state_uses_explicit_parent_design_source_root(tmp_path: Path) -> None:
    parent = tmp_path / "generated-project"
    output = parent / "output"
    output.mkdir(parents=True)
    source = parent / "design.yaml"
    source.write_text("project: Parent\nblocks: []\n", encoding="utf-8")

    state_path = record_generation_state(
        output,
        project_name="Parent",
        spec_path=source,
        output_dir=output,
        phase="generated",
        artifacts=[],
    )

    assert state_path == project_state_path(parent)
    assert not project_state_path(output).exists()


def test_explicit_project_directory_wins_over_child_state_but_source_file_matches_it(tmp_path: Path) -> None:
    parent = tmp_path / "explicit-parent"
    output = parent / "output"
    output.mkdir(parents=True)
    source = parent / "design.yaml"
    source.write_text("project: Parent\nblocks: []\n", encoding="utf-8")
    (parent / "existing.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    child_state = ProjectState(
        name="Generated child",
        kind="circuit_weaver",
        status="generated",
        workflow={
            "generate": {
                "status": "generated",
                "source": str(source.resolve()),
                "output_dir": str(output.resolve()),
            }
        },
    )
    save_project_state(output, child_state)

    directory_summary = get_project_state_summary(parent)
    source_summary = get_project_state_summary(source)

    assert directory_summary["project_root"] == str(parent.resolve())
    assert directory_summary["has_manifest"] is False
    assert source_summary["project_root"] == str(output.resolve())
    assert source_summary["project_id"] == child_state.project_id
