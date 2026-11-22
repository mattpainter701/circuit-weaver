"""Project library table paths must remain inside their owning project."""

from pathlib import Path

import pytest

from circuit_weaver.footprint_lib import _project_library_uri
from circuit_weaver.kicad_lib import _project_symbol_path


@pytest.mark.parametrize(
    ("table_name", "root_name", "resolver"),
    [
        ("fp-lib-table", "fp_lib_table", _project_library_uri),
        ("sym-lib-table", "sym_lib_table", _project_symbol_path),
    ],
)
@pytest.mark.skip_category("platform")
def test_project_library_table_rejects_symlink_escape(tmp_path: Path, table_name, root_name, resolver):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / ("external.kicad_sym" if "sym" in table_name else "External.pretty")
    if outside.suffix == ".pretty":
        outside.mkdir()
    else:
        outside.write_text("(kicad_symbol_lib)", encoding="utf-8")
    link = project / ("alias.kicad_sym" if "sym" in table_name else "Alias.pretty")
    try:
        link.symlink_to(outside, target_is_directory=outside.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")
    uri = "${KIPRJMOD}/alias.kicad_sym" if "sym" in table_name else "${KIPRJMOD}/Alias.pretty"
    (project / table_name).write_text(
        f'({root_name} (version 7) (lib (name "Escape") (type "KiCad") (uri "{uri}")))',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes project root"):
        resolver(project, "Escape")


@pytest.mark.parametrize(
    ("table_name", "root_name", "resolver"),
    [
        ("fp-lib-table", "fp_lib_table", _project_library_uri),
        ("sym-lib-table", "sym_lib_table", _project_symbol_path),
    ],
)
def test_project_library_table_rejects_malformed_sexp(tmp_path: Path, table_name, root_name, resolver):
    project = tmp_path / "project"
    project.mkdir()
    (project / table_name).write_text(f"({root_name} (lib (name \\\"broken\\\"))", encoding="utf-8")

    with pytest.raises(ValueError, match="unterminated"):
        resolver(project, "broken")
