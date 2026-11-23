from __future__ import annotations

from pathlib import Path

from circuit_weaver.schematic_pcb import _copy_schematic_project


def test_schematic_stage_uses_explicit_project_root_for_local_footprints(tmp_path: Path) -> None:
    project = tmp_path / "project"
    generated = project / "output"
    generated.mkdir(parents=True)
    schematic = generated / "design.kicad_sch"
    schematic.write_text("(kicad_sch (version 20250114) (generator test))\n", encoding="utf-8")
    library = project / "footprints" / "Local.pretty"
    library.mkdir(parents=True)
    (library / "A.kicad_mod").write_text("(footprint A)\n", encoding="utf-8")
    (project / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "Local") (type "KiCad") '
        '(uri "${KIPRJMOD}/footprints/Local.pretty")))\n', encoding="utf-8"
    )
    stage = tmp_path / "stage"
    stage.mkdir()

    staged_schematic, snapshots = _copy_schematic_project(schematic, stage, project)

    assert staged_schematic == stage / schematic.name
    assert (stage / "fp-lib-table").is_file()
    assert (stage / "footprints" / "Local.pretty" / "A.kicad_mod").read_text(encoding="utf-8") == "(footprint A)\n"
    assert project / "fp-lib-table" in snapshots
    assert project / "footprints" / "Local.pretty" in snapshots
