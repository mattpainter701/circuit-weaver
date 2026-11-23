import json
from pathlib import Path

import pytest

from circuit_weaver.footprint_lib import KiCadFootprintLibrary, _parse_kicad_sexp
from circuit_weaver.schematic_pcb import schematic_to_pcb


def _table(root: Path, entry: str) -> None:
    (root / "fp-lib-table").write_text(f"(fp_lib_table {entry})\n", encoding="utf-8")


def test_literal_kiprjmod_alias_resolves_real_project_library_without_renaming(tmp_path: Path):
    project = tmp_path / "project"
    pretty = project / "beaglebone-ai-rev-a2.pretty"
    pretty.mkdir(parents=True)
    mod = pretty / "BGA153P5.kicad_mod"
    mod.write_text('(footprint "BGA153P5")', encoding="utf-8")
    _table(
        project,
        '(lib (name "beaglebone-ai")(type "KiCad")'
        '(uri "${KIPRJMOD}/beaglebone-ai-rev-a2.pretty")(options "")(descr ""))',
    )

    library = KiCadFootprintLibrary(project_root=project)

    assert library.resolve("beaglebone-ai:BGA153P5") == mod.resolve()
    assert not (project / "beaglebone-ai.pretty").exists()


def test_project_alias_is_authoritative_and_missing_target_does_not_fallback(tmp_path: Path):
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    (project / "fp-lib-table").parent.mkdir(parents=True)
    _table(project, '(lib (name "Alias")(type "KiCad")(uri "${KIPRJMOD}/vendor.pretty"))')
    fake = global_root / "Alias.pretty" / "Part.kicad_mod"
    fake.parent.mkdir(parents=True)
    fake.write_text("global", encoding="utf-8")

    library = KiCadFootprintLibrary(global_root, project_root=project)

    assert library.resolve("Alias:Part") is None


@pytest.mark.parametrize(
    ("lib_type", "uri"),
    [
        ("Git", "${KIPRJMOD}/vendor.pretty"),
        ("KiCad", "${HOME}/vendor.pretty"),
        ("KiCad", "https://example.invalid/vendor.pretty"),
        ("KiCad", "${KIPRJMOD}/../outside.pretty"),
        ("KiCad", "${KIPRJMOD}/sub\\vendor.pretty"),
    ],
)
def test_unsupported_project_library_type_or_uri_fails_closed(
    tmp_path: Path, lib_type: str, uri: str
):
    project = tmp_path / "project"
    project.mkdir()
    _table(project, f'(lib (name "Alias")(type "{lib_type}")(uri "{uri}"))')
    library = KiCadFootprintLibrary(project_root=project)

    with pytest.raises(ValueError, match="unsupported|unsafe"):
        library.resolve("Alias:Part")


@pytest.mark.skip_category("platform")
def test_project_symlink_cannot_escape_project_root(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside.pretty"
    project.mkdir()
    outside.mkdir()
    (outside / "Part.kicad_mod").write_text("outside", encoding="utf-8")
    _table(project, '(lib (name "Alias")(type "KiCad")(uri "${KIPRJMOD}/vendor.pretty"))')
    try:
        (project / "vendor.pretty").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")

    with pytest.raises(ValueError, match="escapes project root"):
        KiCadFootprintLibrary(project_root=project).resolve("Alias:Part")


def test_global_roots_keep_existing_behavior_without_project_mapping(tmp_path: Path):
    root = tmp_path / "global"
    mod = root / "Alias.pretty" / "Part.kicad_mod"
    mod.parent.mkdir(parents=True)
    mod.write_text("global", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    _table(project, '(lib (name "Other")(type "KiCad")(uri "${KIPRJMOD}/other.pretty"))')

    assert KiCadFootprintLibrary(root, project_root=project).resolve("Alias:Part") == mod
    assert KiCadFootprintLibrary(root).resolve("Alias:Part") == mod


def test_duplicate_project_alias_fails_closed(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _table(
        project,
        '(lib (name "Alias")(type "KiCad")(uri "${KIPRJMOD}/a.pretty")) '
        '(lib (name "Alias")(type "KiCad")(uri "${KIPRJMOD}/b.pretty"))',
    )

    with pytest.raises(ValueError, match="duplicate footprint library nickname"):
        KiCadFootprintLibrary(project_root=project).resolve("Alias:Part")



def test_table_parser_treats_parentheses_and_escaped_quotes_as_string_data():
    parsed = _parse_kicad_sexp(
        '(fp_lib_table ; a comment with ( unmatched paren\n'
        ' (lib (name "Alias") (type "KiCad") '
        '(uri "${KIPRJMOD}/vendor.pretty") (descr "contains (parens) and \\"quotes\\"")))'
    )

    lib = next(item for item in parsed[1:] if isinstance(item, list) and item[0] == "lib")
    descriptions = [item for item in lib if isinstance(item, list) and item[0] == "descr"]
    assert descriptions[0][1] == 'contains (parens) and "quotes"'


def test_table_parser_rejects_unterminated_quotes_and_malformed_scalar_fields(tmp_path: Path):
    with pytest.raises(ValueError, match="unterminated quoted string"):
        _parse_kicad_sexp('(fp_lib_table (lib (name "Alias)))')

    project = tmp_path / "project"
    project.mkdir()
    _table(project, '(lib (name (nested "Alias"))(type "KiCad")(uri "${KIPRJMOD}/a.pretty"))')
    with pytest.raises(ValueError, match="malformed 'name' field"):
        KiCadFootprintLibrary(project_root=project).resolve("Alias:Part")


def _mock_public_workflow(monkeypatch, tmp_path: Path, *, mapped_path: str, global_alias: bool):
    import shutil
    from types import SimpleNamespace

    import circuit_weaver.schematic_pcb as module

    project = tmp_path / "source-project"
    project.mkdir()
    schematic = project / "board.kicad_sch"
    schematic.write_text('(kicad_sch)', encoding="utf-8")
    _table(project, f'(lib (name "Alias")(type "KiCad")(uri "${{KIPRJMOD}}/{mapped_path}"))')
    local_pretty = project / mapped_path
    global_root = tmp_path / "global"
    footprint_root = global_root / "Alias.pretty" if global_alias else local_pretty
    footprint_root.mkdir(parents=True)
    mod = footprint_root / "Part.kicad_mod"
    mod.write_text(
        '(footprint "Part" (fp_rect (start -1 -1) (end 1 1) (layer "F.CrtYd")) '
        '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")) '
        '(pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")))',
        encoding="utf-8",
    )
    output = tmp_path / "out" / "board.kicad_pcb"
    monkeypatch.setattr(module, "_kicad_cli_path", lambda: Path("kicad-cli"))
    monkeypatch.setattr(module, "find_kicad_python", lambda _path=None: "kicad-python")
    monkeypatch.setattr(module.KiCadFootprintLibrary, "_default_roots", staticmethod(lambda root: [global_root]))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:4] == ["sch", "export", "netlist"]:
            netlist = Path(command[command.index("--output") + 1])
            netlist.write_text(
                '<export><components><comp ref="U1"><value>Part</value><footprint>Alias:Part</footprint>'
                '<libsource lib="Lib" part="Part"/><units><unit name="A"><pins>'
                '<pin num="1"/><pin num="2"/></pins></unit></units></comp></components>'
                '<nets></nets></export>', encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stderr="", stdout="KiCad 10.0")
        request = json.loads(kwargs["input"])
        Path(request["board"]).write_text("mock board", encoding="utf-8")
        from circuit_weaver.pcb_stackup import expected_copper_layer_names

        count = request["copper_layers"]
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=(
                'CW_SCHEMATIC_PCB_RESULT=' + json.dumps({
                    "status": "ok", "footprints": 1, "pads": 2,
                    "copper_layer_count": count,
                    "copper_layer_names": list(expected_copper_layer_names(count)),
                })
            ),
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        module, "run_drc",
        lambda *args, **kwargs: SimpleNamespace(status="ok", findings=(), failure_reason=""),
    )
    monkeypatch.setattr(
        module, "_publish_staged_transaction",
        lambda staging, publications: [shutil.copy2(source, destination) for source, destination in publications],
    )
    return schematic, output, mod, calls


def test_public_schematic_to_pcb_resolves_source_projects_literal_alias(tmp_path: Path, monkeypatch):
    schematic, output, expected_mod, calls = _mock_public_workflow(
        monkeypatch, tmp_path, mapped_path="vendor-board.pretty", global_alias=False
    )

    result = schematic_to_pcb(schematic, output)

    assert result["status"] == "ok"
    assert result["counts"]["footprints"] == 1
    assert output.is_file()
    assert len(calls) == 2
    assert expected_mod.exists()


def test_public_schematic_to_pcb_does_not_use_global_lookalike_for_missing_explicit_alias(
    tmp_path: Path, monkeypatch
):
    schematic, output, global_mod, calls = _mock_public_workflow(
        monkeypatch, tmp_path, mapped_path="missing-project.pretty", global_alias=True
    )

    result = schematic_to_pcb(schematic, output)

    assert result["status"] == "error"
    assert "unresolved KiCad footprint" in result["message"]
    assert global_mod.is_file()
    assert not output.exists()
    assert not calls  # reject the missing explicit library before exporting or publishing
