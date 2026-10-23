from __future__ import annotations

import json
from pathlib import Path

import pytest

from circuit_weaver.library_bundle import apply_library_bundle, plan_library_bundle


def _manifest(symbol: object, footprint: object) -> dict:
    return {
        "version": 1,
        "assets": [
            {
                "kind": "symbol_library",
                "source": str(symbol),
                "nickname": "VendorParts",
                "provenance": "vendor authored source",
                "license": "CC-BY-4.0",
            },
            {
                "kind": "footprint_library",
                "source": str(footprint),
                "nickname": "VendorFootprints",
                "provenance": "vendor authored source",
                "license": "CC-BY-4.0",
            },
        ],
    }


def _sources(tmp_path):
    symbol = tmp_path / "source" / "parts.kicad_sym"
    symbol.parent.mkdir()
    symbol.write_text('(kicad_symbol_lib (version 20231120) (symbol "Demo"))\n', encoding="utf-8")
    footprint = tmp_path / "source" / "Vendor.pretty"
    footprint.mkdir()
    (footprint / "Demo.kicad_mod").write_text('(footprint "Demo")\n', encoding="utf-8")
    return symbol, footprint


def test_plan_and_apply_preserve_existing_table_entries_and_emit_local_proof(tmp_path):
    symbol, footprint = _sources(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    original = '(sym_lib_table\n  (lib (name "Existing") (type "KiCad") (uri "${KIPRJMOD}/old.kicad_sym"))\n)\n'
    (project / "sym-lib-table").write_text(original, encoding="utf-8")

    plan = plan_library_bundle(project, _manifest(symbol, footprint))
    assert plan["status"] == "planned"
    assert plan["proof"]["local_only"] is True
    assert plan["proof"]["symbol_pin_maps_verified"] is False
    assert len(plan["assets"][0]["source"]["files"][0]["sha256"]) == 64
    assert len(json.dumps(plan)) > 0

    result = apply_library_bundle(project, plan)
    table = (project / "sym-lib-table").read_text(encoding="utf-8")
    fp_table = (project / "fp-lib-table").read_text(encoding="utf-8")
    assert result["status"] == "applied"
    assert '(name "Existing")' in table
    assert '(name "VendorParts")' in table
    assert "${KIPRJMOD}/libraries/symbols/VendorParts.kicad_sym" in table
    assert '(name "VendorFootprints")' in fp_table
    assert (project / "libraries/symbols/VendorParts.kicad_sym").is_file()
    assert (project / "libraries/footprints/VendorFootprints.pretty/Demo.kicad_mod").is_file()
    retry = apply_library_bundle(project, plan)
    assert retry["status"] == "already_applied"
    assert retry["receipt"].endswith(f"{plan['plan_sha256']}.json")
    (project / "fp-lib-table").write_text(fp_table + "; changed after apply\n", encoding="utf-8")
    with pytest.raises(ValueError, match="project table changed since apply"):
        apply_library_bundle(project, plan)


def test_apply_multiple_symbols_to_one_table_and_refuses_modified_retry(tmp_path):
    symbol, footprint = _sources(tmp_path)
    second_symbol = symbol.with_name("other.kicad_sym")
    second_symbol.write_text('(kicad_symbol_lib (symbol "Other"))\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    manifest = _manifest(symbol, footprint)
    manifest["assets"].append(
        {
            "kind": "symbol_library",
            "source": str(second_symbol),
            "nickname": "OtherParts",
            "provenance": "user supplied",
            "license": "CC0",
        }
    )
    plan = plan_library_bundle(project, manifest)

    result = apply_library_bundle(project, plan)
    table = (project / "sym-lib-table").read_text(encoding="utf-8")
    assert result["status"] == "applied"
    assert '(name "VendorParts")' in table
    assert '(name "OtherParts")' in table
    installed = project / "libraries/symbols/OtherParts.kicad_sym"
    original_contents = installed.read_text(encoding="utf-8")
    installed.write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="installed asset changed"):
        apply_library_bundle(project, plan)
    installed.write_text(original_contents, encoding="utf-8")
    second_symbol.write_text("source changed", encoding="utf-8")
    with pytest.raises(ValueError, match="source changed since apply"):
        apply_library_bundle(project, plan)


def test_plan_rejects_destination_conflict_and_traversal(tmp_path):
    symbol, footprint = _sources(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    conflict = project / "libraries/symbols/VendorParts.kicad_sym"
    conflict.parent.mkdir(parents=True)
    conflict.write_text("owned", encoding="utf-8")
    with pytest.raises(ValueError, match="destination conflict"):
        plan_library_bundle(project, _manifest(symbol, footprint))

    traversal = {
        "version": 1,
        "assets": [
            {
                "kind": "model",
                "source": str(symbol),
                "destination": "libraries/../outside.step",
                "provenance": "manual",
                "license": "CC0",
            }
        ],
    }
    with pytest.raises(ValueError, match="traversal"):
        plan_library_bundle(project, traversal)


def test_plan_rejects_relative_sources_and_case_insensitive_table_duplicates(tmp_path):
    symbol, footprint = _sources(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    relative_manifest = _manifest(symbol, footprint)
    relative_manifest["assets"][0]["source"] = "relative.kicad_sym"
    with pytest.raises(ValueError, match="absolute"):
        plan_library_bundle(project, relative_manifest)

    (project / "sym-lib-table").write_text(
        "(sym_lib_table "
        '(lib (name "Existing") (type "KiCad") (uri "${KIPRJMOD}/one.kicad_sym")) '
        '(lib (name "existing") (type "KiCad") (uri "${KIPRJMOD}/two.kicad_sym")))\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate library nickname"):
        plan_library_bundle(project, _manifest(symbol, footprint))


def test_plan_rejects_symlinked_source_ancestor(tmp_path, monkeypatch):
    symbol, footprint = _sources(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    symlink_parent = symbol.parent
    real_is_symlink = Path.is_symlink

    def report_source_parent_as_symlink(path):
        if path == symlink_parent:
            return True
        return real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", report_source_parent_as_symlink)
    with pytest.raises(ValueError, match="symlink source ancestors"):
        plan_library_bundle(project, _manifest(symbol, footprint))


@pytest.mark.parametrize(
    ("destination", "error"),
    [
        ("libraries/models/CON.step", "reserved Windows"),
        ("libraries/models/LPT9.step", "reserved Windows"),
        ("libraries/models/bad?.step", "forbidden filename"),
        ("libraries/models/control\x01.step", "forbidden filename"),
        ("libraries/models/trailing.step.", "end with a dot or space"),
        ("libraries/models/trailing.step ", "end with a dot or space"),
    ],
)
def test_plan_rejects_nonportable_destination_components(tmp_path, destination, error):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "model.step"
    source.write_bytes(b"model")
    manifest = {
        "version": 1,
        "assets": [
            {
                "kind": "model",
                "source": str(source),
                "destination": destination,
                "provenance": "user supplied",
                "license": "CC0",
            }
        ],
    }
    with pytest.raises(ValueError, match=error):
        plan_library_bundle(project, manifest)


@pytest.mark.parametrize("nickname", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "Vendor.", "Vendor "])
def test_plan_rejects_nonportable_library_nicknames(tmp_path, nickname):
    symbol, footprint = _sources(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    manifest = _manifest(symbol, footprint)
    manifest["assets"][0]["nickname"] = nickname
    with pytest.raises(ValueError):
        plan_library_bundle(project, manifest)


def test_apply_rejects_tampered_and_stale_source_plans(tmp_path):
    symbol, footprint = _sources(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    manifest = _manifest(symbol, footprint)
    plan = plan_library_bundle(project, manifest)

    tampered = dict(plan)
    tampered["changes"] = []
    with pytest.raises(ValueError, match="integrity"):
        apply_library_bundle(project, tampered)

    symbol.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        apply_library_bundle(project, plan)
    assert not (project / "libraries").exists()


def test_rollback_removes_staged_assets_when_table_update_fails(tmp_path, monkeypatch):
    import circuit_weaver.library_bundle as bundle

    symbol, footprint = _sources(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    original = '(sym_lib_table (lib (name "Existing") (type "KiCad") (uri "${KIPRJMOD}/old.kicad_sym")))\n'
    (project / "sym-lib-table").write_text(original, encoding="utf-8")
    plan = plan_library_bundle(project, _manifest(symbol, footprint))

    def fail_table(*args, **kwargs):
        raise OSError("injected table write failure")

    monkeypatch.setattr(bundle.os, "replace", fail_table)
    with pytest.raises(OSError, match="injected"):
        apply_library_bundle(project, plan)
    assert (project / "sym-lib-table").read_text(encoding="utf-8") == original
    assert not (project / "libraries").exists()


def test_rollback_restores_first_table_if_second_table_replace_fails(tmp_path, monkeypatch):
    import circuit_weaver.library_bundle as bundle

    symbol, footprint = _sources(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    original_sym = '(sym_lib_table (lib (name "OldSym") (type "KiCad") (uri "${KIPRJMOD}/old.kicad_sym")))\n'
    original_fp = '(fp_lib_table (lib (name "OldFp") (type "KiCad") (uri "${KIPRJMOD}/old.pretty")))\n'
    (project / "sym-lib-table").write_text(original_sym, encoding="utf-8")
    (project / "fp-lib-table").write_text(original_fp, encoding="utf-8")
    plan = plan_library_bundle(project, _manifest(symbol, footprint))
    real_replace = bundle.os.replace
    replace_count = 0

    def fail_second_replace(source_path, destination_path):
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("injected second table write failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(bundle.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="second table"):
        apply_library_bundle(project, plan)
    assert (project / "fp-lib-table").read_text(encoding="utf-8") == original_fp
    assert (project / "sym-lib-table").read_text(encoding="utf-8") == original_sym
    assert not (project / "libraries").exists()
    assert not list(project.glob("*.bundle-*"))


def test_spice_requires_declared_structural_contract(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "model.lib"
    source.write_text(".subckt x a b\n.ends x\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "assets": [
            {
                "kind": "spice",
                "source": str(source),
                "destination": "libraries/spice/x.lib",
                "provenance": "vendor",
                "license": "vendor terms",
            }
        ],
    }
    with pytest.raises(ValueError, match="subckt"):
        plan_library_bundle(project, manifest)
    manifest["assets"][0].update({"subckt": "x", "pin_order": ["a", "b"]})
    plan = plan_library_bundle(project, manifest)
    assert "user-declared metadata" in plan["assets"][0]["spice_contract"]["claim"]
    assert plan["proof"]["spice_file_presence_means_usable_model"] is False
    assert plan["proof"]["provenance_and_license_are_user_asserted"] is True
