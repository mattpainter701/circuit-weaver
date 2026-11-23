"""Read-only local footprint asset checks use product library resolution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from benchmarks.four_layer_acceptance.asset_checks import check_footprint_assets
from circuit_weaver import footprint_lib


def _item(reference: str, footprint: str) -> SimpleNamespace:
    return SimpleNamespace(reference=reference, footprint=footprint, dnp=False)


def _disable_host_library_tables(monkeypatch) -> None:
    monkeypatch.setattr(footprint_lib, "_global_library_table_entries", lambda *_args: {})
    monkeypatch.setattr(
        footprint_lib.KiCadFootprintLibrary,
        "_default_roots",
        staticmethod(lambda _root: []),
    )


def test_two_assets_and_bare_sot_footprint_are_qualified_and_hashed(tmp_path: Path, monkeypatch) -> None:
    _disable_host_library_tables(monkeypatch)
    resistor = tmp_path / "Resistor_SMD.pretty" / "R_0402_1005Metric.kicad_mod"
    sot = tmp_path / "Package_TO_SOT_SMD.pretty" / "SOT-23-5.kicad_mod"
    sot6 = tmp_path / "Package_TO_SOT_SMD.pretty" / "SOT-23-6.kicad_mod"
    resistor.parent.mkdir(parents=True)
    sot.parent.mkdir(parents=True)
    resistor.write_text('(footprint "R_0402_1005Metric")\n', encoding="utf-8")
    sot.write_text('(footprint "SOT-23-5")\n', encoding="utf-8")
    sot6.write_text('(footprint "SOT-23-6")\n', encoding="utf-8")

    result = check_footprint_assets(
        [
            _item("R1", "Resistor_SMD:R_0402_1005Metric"),
            _item("U1", "SOT-23-5"),
            _item("U2", "SOT-23-6"),
        ],
        roots=[tmp_path],
    )

    assert result["status"] == "passed"
    checks = {row["reference"]: row for row in result["checks"]}
    assert checks["R1"]["declared_footprint"] == "Resistor_SMD:R_0402_1005Metric"
    assert checks["R1"]["normalized_footprint"] == "Resistor_SMD:R_0402_1005Metric"
    assert checks["R1"]["path"] == str(resistor.resolve())
    assert checks["R1"]["sha256"] == hashlib.sha256(resistor.read_bytes()).hexdigest()
    assert checks["R1"]["source"]["kind"] == "local_kicad_root"
    assert checks["R1"]["exists"] is True
    assert checks["U1"]["normalized_footprint"] == "Package_TO_SOT_SMD:SOT-23-5"
    assert checks["U1"]["path"] == str(sot.resolve())
    assert checks["U2"]["normalized_footprint"] == "Package_TO_SOT_SMD:SOT-23-6"
    assert checks["U2"]["path"] == str(sot6.resolve())


def test_qualified_missing_file_fails_when_local_root_is_available(tmp_path: Path, monkeypatch) -> None:
    _disable_host_library_tables(monkeypatch)
    tmp_path.mkdir(exist_ok=True)

    result = check_footprint_assets([_item("U404", "MissingLib:MissingPart")], roots=[tmp_path])

    assert result["status"] == "failed"
    row = result["checks"][0]
    assert row["check"] == "footprint_asset_resolution"
    assert row["status"] == "failed"
    assert row["normalized_footprint"] == "MissingLib:MissingPart"
    assert row["path"] == str((tmp_path / "MissingLib.pretty" / "MissingPart.kicad_mod").resolve())
    assert row["exists"] is False
    assert row["sha256"] is None


def test_asset_source_names_the_second_matching_root(tmp_path: Path, monkeypatch) -> None:
    _disable_host_library_tables(monkeypatch)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    footprint = second_root / "Resistor_SMD.pretty" / "R_0402_1005Metric.kicad_mod"
    footprint.parent.mkdir(parents=True)
    footprint.write_text('(footprint "R_0402_1005Metric")\n', encoding="utf-8")

    result = check_footprint_assets(
        [_item("R7", "Resistor_SMD:R_0402_1005Metric")],
        roots=[first_root, second_root],
    )

    assert result["status"] == "passed"
    assert result["checks"][0]["source"] == {
        "kind": "local_kicad_root",
        "root": str(second_root.resolve()),
    }


def test_no_local_root_or_project_context_is_unverified_without_resolving(tmp_path: Path, monkeypatch) -> None:
    _disable_host_library_tables(monkeypatch)
    monkeypatch.setattr(
        footprint_lib.KiCadFootprintLibrary,
        "resolve",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("resolver must not run without local roots or library context")
        ),
    )

    result = check_footprint_assets([_item("U1", "Package_TO_SOT_SMD:SOT-23-5")], roots=[])

    assert result["status"] == "unverified"
    assert result["roots"] == []
    assert result["checks"][0]["status"] == "unverified"
    assert "no local KiCad" in result["checks"][0]["reason"]
    assert result["checks"][0]["path"] is None


def test_missing_explicit_project_nickname_does_not_fall_back_to_global_root(tmp_path: Path, monkeypatch) -> None:
    _disable_host_library_tables(monkeypatch)
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    project.mkdir()
    global_candidate = global_root / "Alias.pretty" / "Part.kicad_mod"
    global_candidate.parent.mkdir(parents=True)
    global_candidate.write_text("global lookalike", encoding="utf-8")
    (project / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "Alias") (type "KiCad") '
        '(uri "${KIPRJMOD}/vendor.pretty") (options "") (descr "")))\n',
        encoding="utf-8",
    )

    result = check_footprint_assets(
        [_item("U1", "Alias:Part")],
        roots=[global_root],
        project_root=project,
    )

    assert result["status"] == "failed"
    row = result["checks"][0]
    assert row["status"] == "failed"
    assert row["source"]["kind"] == "project_library_table"
    assert row["path"] == str((project / "vendor.pretty" / "Part.kicad_mod").resolve())
    assert row["path"] != str(global_candidate.resolve())
    assert row["exists"] is False
