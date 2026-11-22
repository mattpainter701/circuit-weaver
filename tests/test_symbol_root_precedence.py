"""Explicit and project symbol sources must outrank same-name globals."""

from pathlib import Path

import pytest

from circuit_weaver import kicad_lib


def _symbol(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    symbol = path / "Shared.kicad_sym"
    symbol.write_text('(kicad_symbol_lib (version 20211014) (symbol "Duplicate"))', encoding="utf-8")
    return symbol


@pytest.mark.parametrize("source_mode", ["constructor", "environment"])
def test_explicit_symbol_root_precedes_same_nickname_global_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_mode: str
) -> None:
    for name in ("KICAD_SYMBOL_DIR", "KICAD10_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR"):
        monkeypatch.delenv(name, raising=False)
    explicit_root = tmp_path / "explicit"
    global_root = tmp_path / "global"
    stock_root = tmp_path / "stock"
    explicit_file = _symbol(explicit_root)
    global_file = _symbol(global_root)
    _symbol(stock_root)
    monkeypatch.setattr(kicad_lib, "_KICAD_LIB_PATHS", {"Windows": [stock_root]})
    monkeypatch.setattr(kicad_lib, "_global_library_table_entries", lambda *_args: {"Shared": global_file})
    monkeypatch.setattr(
        kicad_lib,
        "parse_kicad_sym_file",
        lambda filename: {"Duplicate": {"source": str(Path(filename).resolve())}},
    )
    if source_mode == "environment":
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(explicit_root))
        library = kicad_lib.KiCadLibrary()
    else:
        library = kicad_lib.KiCadLibrary(roots=[explicit_root])

    symbol = library.get_symbol_data("Duplicate", "Shared")

    assert symbol == {"source": str(explicit_file.resolve())}


def test_project_symbol_table_still_overrides_explicit_and_global_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit_root = tmp_path / "explicit"
    global_root = tmp_path / "global"
    _symbol(explicit_root)
    global_file = _symbol(global_root)
    project = tmp_path / "project"
    project_file = _symbol(project / "symbols")
    (project / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) '
        '(lib (name "Shared") (type "KiCad") (uri "${KIPRJMOD}/symbols/Shared.kicad_sym")))',
        encoding="utf-8",
    )
    monkeypatch.setattr(kicad_lib, "_global_library_table_entries", lambda *_args: {"Shared": global_file})
    monkeypatch.setattr(
        kicad_lib,
        "parse_kicad_sym_file",
        lambda filename: {"Duplicate": {"source": str(Path(filename).resolve())}},
    )
    library = kicad_lib.KiCadLibrary(roots=[explicit_root], project_root=project)

    symbol = library.get_symbol_data("Duplicate", "Shared")

    assert symbol == {"source": str(project_file.resolve())}
