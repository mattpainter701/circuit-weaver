from __future__ import annotations

import hashlib
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


def _symbol_with_footprint(path: Path, name: str, footprint: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""(kicad_symbol_lib (version 20211014) (generator "kicad_symbol_editor")
  (symbol "{name}"
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1 1))))
    (property "Value" "{name}" (at 0 0 0) (effects (font (size 1 1))))
    (property "Footprint" "{footprint}" (at 0 0 0) (effects (font (size 1 1))))
  )
)
""",
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


def test_library_access_reports_footprint_provenance_from_selected_physical_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    local_root = tmp_path / "explicit-footprints"
    local_footprint = local_root / "RF_Module.pretty" / "AD9361.kicad_mod"
    local_footprint.parent.mkdir(parents=True)
    local_footprint.write_text('(footprint "AD9361")', encoding="utf-8")
    global_library = tmp_path / "global" / "RF_Module.pretty"
    global_footprint = global_library / "AD9361.kicad_mod"
    global_footprint.parent.mkdir(parents=True)
    global_footprint.write_text('(footprint "AD9361-global")', encoding="utf-8")
    global_table = tmp_path / "roaming" / "kicad" / "10.0" / "fp-lib-table"
    global_table.parent.mkdir(parents=True)
    global_table.write_text(
        "(fp_lib_table (version 7) " f'(lib (name "RF_Module") (type "KiCad") (uri "{global_library.as_posix()}")))',
        encoding="utf-8",
    )

    report = audit_library_access("AD9361", footprint_roots=[local_root])

    footprint_match = report["matches"]["footprints"][0]
    assert footprint_match["path"] == str(local_footprint)
    assert footprint_match["source"] == "local_root"


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


def test_spice_audit_accepts_multiline_subckt_declaration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    spice_dir = tmp_path / "spice_models"
    model = spice_dir / "vendor" / "model.lib"
    model.parent.mkdir(parents=True)
    model.write_text(
        "* UTF-8 model\n.subckt amp noninv inv\n+ vplus vminus out params: gain=1\n.ends amp\n",
        encoding="utf-8",
    )
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
    assert binding["explicit_pin_contract_available"] is True
    assert binding["pin_order"] == ["1", "2", "3", "4", "5"]


@pytest.mark.parametrize(
    ("model_bytes", "subckt", "pin_order", "reason_fragment"),
    [
        (b".subckt amp a b\n.ends amp\n.subckt AMP c d\n.ends amp\n", "amp", ["1", "2"], "ambiguous"),
        (b".subckt amp a a\n.ends amp\n", "amp", ["1", "2"], "duplicate pins"),
        (b".subckt amp a b\n.ends amp\n", "amp", ["1", "1"], "pin_order contains duplicate"),
        (b".subckt\n.ends\n", "amp", ["1"], "malformed .subckt"),
        (b".subckt amp a\n.ends amp\n", "amp", ["1", "2"], "pin count"),
        (b".subckt amp a b\n.ends amp\n\n+ orphan\n", "amp", ["1", "2"], "orphan continuation"),
        (b"\xff\xfe", "amp", ["1"], "valid UTF-8"),
    ],
)
def test_spice_audit_does_not_confirm_malformed_or_ambiguous_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_bytes: bytes,
    subckt: str,
    pin_order: list[str],
    reason_fragment: str,
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    spice_dir = tmp_path / "spice_models"
    model = spice_dir / "model.lib"
    model.parent.mkdir(parents=True)
    model.write_bytes(model_bytes)
    (spice_dir / "manifest.json").write_text(
        json.dumps(
            {
                "MODEL-1": {
                    "spice_status": "ok",
                    "spice_file": "model.lib",
                    "subckt": subckt,
                    "pin_order": pin_order,
                }
            }
        ),
        encoding="utf-8",
    )

    binding = audit_library_access("MODEL-1", spice_model_dir=spice_dir)["spice_model_binding"]

    assert binding["available"] is False
    assert binding["file_available"] is True
    assert binding["explicit_pin_contract_available"] is False
    assert reason_fragment in binding["reason"]


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
    _symbol_with_footprint(symbols / "RF.kicad_sym", "ADF4351", "RF_Generic:LFCSP-32")
    footprints = tmp_path / "footprints"
    model = tmp_path / "models" / "LFCSP-32.step"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"generic 3d asset")
    footprint = footprints / "RF_Generic.pretty" / "LFCSP-32.kicad_mod"
    footprint.parent.mkdir(parents=True)
    footprint_bytes = (
        f'(footprint "LFCSP-32" (pad "1" smd rect) (pad "2" smd rect) (model "{model.as_posix()}"))'.replace(
            " (pad", "\r\n  (pad"
        ).encode("utf-8")
    )
    footprint.write_bytes(footprint_bytes)
    report = audit_library_access("ADF4351", symbol_roots=[symbols], footprint_roots=[footprints])

    assert report["matches"]["symbols"][0]["name"] == "ADF4351"
    assert report["matches"]["footprints"] == []
    asset = report["resolved_symbol_assets"][0]
    assert asset["footprint"]["available"] is True
    assert asset["footprint"]["path"] == str(footprint.resolve())
    assert asset["footprint"]["pad_count"] == 2
    assert asset["footprint"]["sha256"] == hashlib.sha256(footprint_bytes).hexdigest()
    assert asset["models"][0]["available"] is True
    assert asset["models"][0]["path"] == str(model.resolve())
    assert asset["models"][0]["sha256"]
    assert "does not verify" in asset["note"]


def test_duplicate_symbol_nickname_assets_follow_the_selected_symbol_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    local_symbols = tmp_path / "local-symbols"
    global_symbols = tmp_path / "global-symbols"
    _symbol_with_footprint(local_symbols / "RF.kicad_sym", "AD9361", "RFParts:LocalPart")
    global_symbol = _symbol_with_footprint(global_symbols / "RF.kicad_sym", "AD9361", "RFParts:GlobalPart")
    global_table = tmp_path / "roaming" / "kicad" / "10.0" / "sym-lib-table"
    global_table.parent.mkdir(parents=True)
    global_table.write_text(
        "(sym_lib_table (version 7) " f'(lib (name "RF") (type "KiCad") (uri "{global_symbol.as_posix()}")))',
        encoding="utf-8",
    )
    footprints = tmp_path / "footprints"
    for name in ("LocalPart", "GlobalPart"):
        footprint = footprints / "RFParts.pretty" / f"{name}.kicad_mod"
        footprint.parent.mkdir(parents=True, exist_ok=True)
        footprint.write_text(f'(footprint "{name}")', encoding="utf-8")

    # A stale nickname cache must not replace the symbol definition selected
    # from the global table with a different same-nickname local definition.
    monkeypatch.setattr(
        KiCadLibrary,
        "get_component",
        lambda self, symbol_name, lib_name=None: SimpleNamespace(footprint="RFParts:LocalPart"),
    )
    report = audit_library_access("AD9361", symbol_roots=[local_symbols], footprint_roots=[footprints])

    assert report["conflicts"]["symbol_library_name_collisions"]
    asset = report["resolved_symbol_assets"][0]
    assert asset["symbol_path"] == str(global_symbol.resolve())
    assert asset["footprint_reference"] == "RFParts:GlobalPart"
    assert asset["footprint"]["path"].endswith("GlobalPart.kicad_mod")


def test_unambiguous_extended_symbol_keeps_resolved_inherited_footprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_kicad_env(monkeypatch, tmp_path)
    symbol_root = tmp_path / "symbols"
    symdir = symbol_root / "Sensors.kicad_symdir"
    _symbol_with_footprint(symdir / "Parent.kicad_sym", "Parent", "RF_Generic:DerivedPart")
    child = symdir / "DerivedPart.kicad_sym"
    child.write_text(
        """(kicad_symbol_lib (version 20211014) (generator "kicad_symbol_editor")
  (symbol "DerivedPart" (extends "Parent")
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1 1))))
    (property "Value" "DerivedPart" (at 0 0 0) (effects (font (size 1 1))))
  )
)
""",
        encoding="utf-8",
    )
    footprint_root = tmp_path / "footprints"
    footprint = footprint_root / "RF_Generic.pretty" / "DerivedPart.kicad_mod"
    footprint.parent.mkdir(parents=True)
    footprint.write_text('(footprint "DerivedPart")', encoding="utf-8")

    report = audit_library_access("DerivedPart", symbol_roots=[symbol_root], footprint_roots=[footprint_root])

    asset = report["resolved_symbol_assets"][0]
    assert asset["symbol_path"] == str(child.resolve())
    assert asset["footprint_reference"] == "RF_Generic:DerivedPart"
    assert asset["footprint"]["available"] is True
    assert asset["footprint"]["path"] == str(footprint.resolve())


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
