from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import circuit_weaver.kicad_lib as kicad_lib_module
from circuit_weaver.footprint_lib import KiCadFootprintLibrary
from circuit_weaver.kicad_lib import KiCadLibrary
from circuit_weaver.library_access import audit_library_access


def _clean_kicad_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("circuit_weaver.kicad_lib.platform.system", lambda: "UnlistedOS")
    monkeypatch.setattr("circuit_weaver.footprint_lib.platform.system", lambda: "UnlistedOS")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "roaming"))
    for name in (
        "KICAD_SYMBOL_DIR",
        "KICAD10_SYMBOL_DIR",
        "KICAD9_SYMBOL_DIR",
        "KICAD8_SYMBOL_DIR",
        "KICAD_FOOTPRINT_DIR",
        "KICAD10_FOOTPRINT_DIR",
        "KICAD9_FOOTPRINT_DIR",
        "KICAD8_FOOTPRINT_DIR",
        "KICAD10_3DMODEL_DIR",
        "KICAD9_3DMODEL_DIR",
        "KICAD8_3DMODEL_DIR",
        "KISYS3DMOD",
    ):
        monkeypatch.delenv(name, raising=False)


def _symbol(path: Path, name: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'(kicad_symbol_lib (version 20211014) (generator kicad_symbol_editor) (symbol "{name}"))',
        encoding="utf-8",
    )
    return path


def test_add_path_indexes_kicad10_symdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    root = tmp_path / "symbols"
    symbol_path = _symbol(root / "Radio.kicad_symdir" / "AD9361.kicad_sym", "AD9361")
    monkeypatch.setattr(kicad_lib_module, "parse_kicad_sym_file", lambda _: {"AD9361": {"pins": []}})

    library = KiCadLibrary(roots=[])
    library.add_path(root)

    assert library.get_symbol_data("AD9361", "Radio") == {"pins": []}
    assert library._sym_dirs["Radio"] == root.resolve() / "Radio.kicad_symdir"
    assert symbol_path.is_file()


def test_project_sym_lib_table_resolves_only_contained_kiprjmod_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    symbol_path = _symbol(project / "symbols" / "radio.kicad_sym", "AD9361")
    (project / "sym-lib-table").write_text(
        "(sym_lib_table (version 7) "
        '(lib (name "ProjectRadio") (type "KiCad") (uri "${KIPRJMOD}/symbols/radio.kicad_sym")))',
        encoding="utf-8",
    )
    monkeypatch.setattr(kicad_lib_module, "parse_kicad_sym_file", lambda _: {"AD9361": {"pins": []}})

    library = KiCadLibrary(roots=[], project_root=project)

    assert library.get_symbol_data("AD9361", "ProjectRadio") == {"pins": []}
    assert library.global_libraries == {}
    assert symbol_path.is_file()


def test_project_sym_lib_table_rejects_path_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    escaped = _symbol(tmp_path / "outside.kicad_sym", "AD9361")
    (project / "sym-lib-table").write_text(
        "(sym_lib_table (version 7) " '(lib (name "Escape") (type "KiCad") (uri "${KIPRJMOD}/../outside.kicad_sym")))',
        encoding="utf-8",
    )
    library = KiCadLibrary(roots=[], project_root=project)

    with pytest.raises(ValueError, match="unsafe project symbol library URI"):
        library.get_symbol_data("AD9361", "Escape")
    assert escaped.is_file()


def test_versioned_symbol_root_is_discovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    root = tmp_path / "kicad10-symbols"
    _symbol(root / "RF.kicad_sym", "AD9361")
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(root))

    assert KiCadLibrary().roots == [root.resolve()]


def test_global_symbol_table_and_nested_default_table_are_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    roaming = tmp_path / "roaming"
    settings = roaming / "kicad" / "10.0"
    template = tmp_path / "templates" / "sym-lib-table"
    symbol = _symbol(tmp_path / "custom" / "radio.kicad_sym", "AD9361")
    template.parent.mkdir(parents=True)
    template.write_text(
        f'(sym_lib_table (version 7) (lib (name "UserRadio") (type "KiCad") (uri "{symbol.as_posix()}")))',
        encoding="utf-8",
    )
    settings.mkdir(parents=True)
    (settings / "sym-lib-table").write_text(
        f'(sym_lib_table (version 7) (lib (name "Default") (type "Table") (uri "{template.as_posix()}")))',
        encoding="utf-8",
    )
    monkeypatch.setattr(kicad_lib_module, "parse_kicad_sym_file", lambda _: {"AD9361": {"pins": []}})

    library = KiCadLibrary(roots=[])

    assert library.get_symbol_data("AD9361", "UserRadio") == {"pins": []}
    assert library.global_libraries["UserRadio"] == symbol
    assert symbol.is_file()


def test_global_footprint_table_resolves_custom_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    roaming = tmp_path / "roaming" / "kicad" / "10.0"
    pretty = tmp_path / "custom" / "RF_Module.pretty"
    footprint = pretty / "AD9361.kicad_mod"
    footprint.parent.mkdir(parents=True)
    footprint.write_text('(footprint "AD9361")', encoding="utf-8")
    roaming.mkdir(parents=True)
    (roaming / "fp-lib-table").write_text(
        "(fp_lib_table (version 7) " f'(lib (name "UserRF") (type "KiCad") (uri "{pretty.as_posix()}")))',
        encoding="utf-8",
    )

    library = KiCadFootprintLibrary()

    assert library.resolve("UserRF:AD9361") == footprint
    assert library.global_libraries["UserRF"] == pretty


def test_library_access_searches_project_footprints_with_project_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    project = tmp_path / "project"
    pretty = project / "vendor.pretty"
    mod = pretty / "PROJECT_RF_PART.kicad_mod"
    mod.parent.mkdir(parents=True)
    mod.write_text('(footprint "PROJECT_RF_PART")', encoding="utf-8")
    (project / "fp-lib-table").write_text(
        "(fp_lib_table (version 7) " '(lib (name "ProjectRF") (type "KiCad") (uri "${KIPRJMOD}/vendor.pretty")))',
        encoding="utf-8",
    )

    report = audit_library_access("PROJECT_RF_PART", project_root=project)

    assert report["matches"]["footprints"][0]["path"] == str(mod)
    assert report["matches"]["footprints"][0]["source"] == "project_table"


def test_library_access_separates_kicad_assets_from_functional_and_spice_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    symbols = tmp_path / "symbols"
    footprints = tmp_path / "footprints"
    models = tmp_path / "models"
    _symbol(symbols / "RF.kicad_sym", "TEST_RF_PART")
    mod = footprints / "RF_Module.pretty" / "TEST_RF_PART.kicad_mod"
    mod.parent.mkdir(parents=True)
    mod.write_text('(footprint "TEST_RF_PART")', encoding="utf-8")
    step = models / "RF_Module" / "TEST_RF_PART.step"
    step.parent.mkdir(parents=True)
    step.write_bytes(b"step-model")

    report = audit_library_access(
        "TEST_RF_PART",
        symbol_roots=[symbols],
        footprint_roots=[footprints],
        model_roots=[models],
        match_limit=25,
    )

    assert report["read_only"] is True
    assert report["matches"]["symbols"][0]["name"] == "TEST_RF_PART"
    assert report["matches"]["footprints"][0]["reference"] == "RF_Module:TEST_RF_PART"
    assert report["matches"]["models_3d"][0]["path"] == str(step)
    assert report["curated_support"]["available"] is False
    assert report["spice_model_binding"]["available"] is False


def test_spice_audit_distinguishes_present_legacy_file_from_explicit_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    spice_dir = tmp_path / "spice_models"
    model = spice_dir / "vendor" / "legacy.lib"
    model.parent.mkdir(parents=True)
    model.write_text(".subckt legacy vin vout\n.ends\n", encoding="utf-8")
    (spice_dir / "manifest.json").write_text(
        json.dumps({"MODEL-1": {"spice_status": "ok", "spice_file": "vendor/legacy.lib"}}),
        encoding="utf-8",
    )

    report = audit_library_access("MODEL-1", spice_model_dir=spice_dir)

    binding = report["spice_model_binding"]
    assert binding["available"] is False
    assert binding["file_available"] is True
    assert binding["explicit_pin_contract_available"] is False
    assert binding["path"] == str(model.resolve())
    assert "lacks a validated explicit" in binding["reason"]


def test_family_query_suggests_curated_mpn_without_claiming_exact_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)

    report = audit_library_access("DAC8552")
    support = report["curated_support"]

    assert support["available"] is False
    assert support["mpn"] is None
    assert "DAC8552IDGK" in support["candidate_mpns"]


def test_spice_audit_reports_structural_contract_without_claiming_electrical_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    spice_dir = tmp_path / "spice_models"
    model = spice_dir / "vendor" / "model.lib"
    model.parent.mkdir(parents=True)
    model.write_text(".subckt amp noninv inv vplus vminus out\n.ends amp\n", encoding="utf-8")
    (spice_dir / "manifest.json").write_text(
        json.dumps(
            {
                "MODEL-1": {
                    "spice_status": "ok",
                    "spice_file": "vendor/model.lib",
                    "subckt": "amp",
                    "pin_order": ["1", "2", "3", "4", "5"],
                }
            }
        ),
        encoding="utf-8",
    )

    binding = audit_library_access("MODEL-1", spice_model_dir=spice_dir)["spice_model_binding"]

    assert binding["available"] is True
    assert binding["file_available"] is True
    assert binding["explicit_pin_contract_available"] is True
    assert binding["subckt"] == "amp"
    assert binding["pin_order"] == ["1", "2", "3", "4", "5"]
    assert "does not verify datasheet" in binding["contract_validation"]


def test_library_access_caps_search_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    symbols = tmp_path / "symbols"
    for index in range(7):
        _symbol(symbols / f"RF{index}.kicad_symdir" / f"TEST_RF_{index}.kicad_sym", f"TEST_RF_{index}")

    report = audit_library_access("TEST_RF", symbol_roots=[symbols], match_limit=3)

    assert len(report["matches"]["symbols"]) == 3
    assert report["limits"]["match_limit_per_kind"] == 3


def test_exact_symbol_resolves_generic_footprint_and_model_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    symbols = tmp_path / "symbols"
    _symbol(symbols / "RF.kicad_sym", "ADF4351")
    footprints = tmp_path / "footprints"
    model = tmp_path / "models" / "LFCSP-32.step"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"generic 3d asset")
    footprint = footprints / "RF_Generic.pretty" / "LFCSP-32.kicad_mod"
    footprint.parent.mkdir(parents=True)
    footprint.write_text(
        f'(footprint "LFCSP-32" (pad "1" smd rect) (pad "2" smd rect) (model "{model.as_posix()}"))',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        KiCadLibrary,
        "get_component",
        lambda self, symbol_name, lib_name=None: SimpleNamespace(footprint="RF_Generic:LFCSP-32"),
    )

    report = audit_library_access("ADF4351", symbol_roots=[symbols], footprint_roots=[footprints])

    assert report["matches"]["symbols"][0]["name"] == "ADF4351"
    assert report["matches"]["footprints"] == []
    asset = report["resolved_symbol_assets"][0]
    assert asset["footprint"]["available"] is True
    assert asset["footprint"]["path"] == str(footprint.resolve())
    assert asset["footprint"]["pad_count"] == 2
    assert asset["footprint"]["sha256"]
    assert asset["models"][0]["available"] is True
    assert asset["models"][0]["path"] == str(model.resolve())
    assert asset["models"][0]["sha256"]
    assert "does not verify" in asset["note"]


def test_library_access_reports_duplicate_symbol_and_footprint_nicknames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    symbol_roots = [tmp_path / "symbols-a", tmp_path / "symbols-b"]
    footprint_roots = [tmp_path / "footprints-a", tmp_path / "footprints-b"]
    for root in symbol_roots:
        _symbol(root / "RF.kicad_sym", "TEST_RF_PART")
    for root in footprint_roots:
        mod = root / "RF_Module.pretty" / "TEST_RF_PART.kicad_mod"
        mod.parent.mkdir(parents=True)
        mod.write_text('(footprint "TEST_RF_PART")', encoding="utf-8")

    report = audit_library_access(
        "TEST_RF_PART",
        symbol_roots=symbol_roots,
        footprint_roots=footprint_roots,
    )

    assert report["conflicts"]["symbol_library_name_collisions"][0]["library"] == "RF"
    assert report["conflicts"]["footprint_nickname_collisions"][0]["library"] == "RF_Module"
