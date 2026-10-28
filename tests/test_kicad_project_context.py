"""Bounded KiCad context staging and malformed-source regressions."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from circuit_weaver.kicad_project_context import (
    footprint_library_tree_sha256,
    inspect_project_footprint_context,
    stage_project_footprint_context,
)


def _table(uri: str) -> str:
    return f'''(fp_lib_table
  (lib (name "Local") (type "KiCad") (uri "{uri}"))
  (lib (name "Global") (type "KiCad") (uri "${{KICAD9_FOOTPRINT_DIR}}/Connector_Generic.pretty"))
)\n'''


def test_only_declared_contained_local_pretty_is_staged_and_verified(tmp_path: Path) -> None:
    project = tmp_path / "project"
    library = project / "libraries" / "Local.pretty"
    library.mkdir(parents=True)
    (library / "A.kicad_mod").write_bytes(b"local footprint")
    (library / "notes.txt").write_bytes(b"associated local file")
    table = project / "fp-lib-table"
    table.write_text(_table("${KIPRJMOD}/libraries/Local.pretty"), encoding="utf-8")
    stage = tmp_path / "isolated"
    stage.mkdir()

    report = stage_project_footprint_context(project, stage)

    assert report["status"] == "staged"
    assert report["library_count"] == 1
    assert report["file_count"] == 2
    assert (stage / "fp-lib-table").read_bytes() == table.read_bytes()
    for name in ("A.kicad_mod", "notes.txt"):
        source = library / name
        staged = stage / "libraries" / "Local.pretty" / name
        assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(staged.read_bytes()).digest()
    assert not (stage / "Connector_Generic.pretty").exists()
    assert table.read_text(encoding="utf-8") == _table("${KIPRJMOD}/libraries/Local.pretty")


@pytest.mark.parametrize("uri", [
    "${KIPRJMOD}/../outside.pretty",
    "${KIPRJMOD}/libraries/not-a-footprint-dir",
    "${KIPRJMOD}/libraries//Local.pretty",
])
def test_unsafe_or_unsupported_project_relative_assets_fail_closed(tmp_path: Path, uri: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "fp-lib-table").write_text(_table(uri), encoding="utf-8")
    stage = tmp_path / "isolated"
    stage.mkdir()

    with pytest.raises(ValueError):
        stage_project_footprint_context(project, stage)


def test_absent_table_is_explicit_and_does_not_copy_project_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    stage = tmp_path / "isolated"
    project.mkdir()
    stage.mkdir()

    result = inspect_project_footprint_context(project)

    assert result["status"] == "absent"
    assert result["watch_paths"] == [str(project / "fp-lib-table")]
    assert result["source_hashes"] == {}
    assert stage_project_footprint_context(project, stage)["status"] == "absent"
    assert list(stage.iterdir()) == []


def test_tree_manifest_detects_new_file_in_declared_library(tmp_path: Path) -> None:
    project = tmp_path / "project"
    library = project / "libraries" / "Local.pretty"
    library.mkdir(parents=True)
    (library / "A.kicad_mod").write_text("original content", encoding="utf-8")
    table = project / "fp-lib-table"
    table.write_text(_table("${KIPRJMOD}/libraries/Local.pretty"), encoding="utf-8")
    before = inspect_project_footprint_context(project)
    (library / "B.kicad_mod").write_text("new content", encoding="utf-8")
    after = inspect_project_footprint_context(project)

    assert before["library_manifest_sha256"] != after["library_manifest_sha256"]
    assert footprint_library_tree_sha256(library) != next(iter(before["library_manifest_sha256"].values()))


@pytest.mark.parametrize("limit", ["_MAX_TABLE_ENTRIES", "_MAX_LIBRARY_FILES", "_MAX_LIBRARY_BYTES"])
def test_inventory_enforces_resource_caps_before_copying(tmp_path, monkeypatch, limit):
    from circuit_weaver import kicad_project_context as context
    project = tmp_path / "project"
    library = project / "Local.pretty"
    library.mkdir(parents=True)
    (library / "A.kicad_mod").write_bytes(b"footprint bytes")
    (project / "fp-lib-table").write_text(_table("${KIPRJMOD}/Local.pretty"))
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(context, limit, 0)
    with pytest.raises(ValueError):
        stage_project_footprint_context(project, stage)
    assert list(stage.iterdir()) == []


def test_duplicate_library_nickname_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "A") (type "KiCad") (uri "/global.pretty"))'
        ' (lib (name "a") (type "KiCad") (uri "/other.pretty")))'
    )
    with pytest.raises(ValueError, match="duplicate"):
        inspect_project_footprint_context(project)


def test_staging_rejects_reparse_ancestor_without_writing_through_it(tmp_path, monkeypatch):
    from circuit_weaver import kicad_project_context as context
    project = tmp_path / "project"
    library = project / "libraries" / "Local.pretty"
    library.mkdir(parents=True)
    (library / "A.kicad_mod").write_bytes(b"footprint")
    (project / "fp-lib-table").write_text(_table("${KIPRJMOD}/libraries/Local.pretty"))
    stage = tmp_path / "stage"
    stage.mkdir()
    real_check = context._is_link_or_reparse
    monkeypatch.setattr(context, "_is_link_or_reparse",
                        lambda path: path == stage / "libraries" or real_check(path))
    with pytest.raises(ValueError, match="reparse"):
        stage_project_footprint_context(project, stage)
    assert not (stage / "libraries").exists()
    assert (library / "A.kicad_mod").read_bytes() == b"footprint"


def test_copy_byte_limit_survives_stale_inventory(tmp_path, monkeypatch):
    from circuit_weaver import kicad_project_context as context
    source = tmp_path / "Local.pretty"
    source.mkdir()
    (source / "A.kicad_mod").write_bytes(b"grew past inventory")
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(context, "_MAX_LIBRARY_BYTES", 8)
    # A source that grows after inventory must not turn a bounded copy into an
    # unbounded copy-until-EOF. Simulate that stale inventory explicitly.
    monkeypatch.setattr(context, "_inventory_library", lambda *args: {"A.kicad_mod": "stale"})
    with pytest.raises(ValueError, match="byte limit"):
        context._copy_library(source, stage / "Local.pretty", stage,
                              {"entries": 0, "files": 1, "bytes": 1})
    assert sum(path.stat().st_size for path in stage.rglob("*") if path.is_file()) <= 8
