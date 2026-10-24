"""Bounded staging of explicitly project-local KiCad footprint libraries.

Callers provide the original project directory explicitly; this module never
searches up from a temporary staging folder.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .footprint_lib import _direct_string, _parse_kicad_sexp

_MAX_TABLE_BYTES = 1 * 1024 * 1024
_MAX_TABLE_ENTRIES = 256
_MAX_LIBRARY_FILES = 4_096
_MAX_LIBRARY_BYTES = 64 * 1024 * 1024
_MAX_LIBRARY_ENTRIES = 8_192
_REPARSE_POINT = 0x400


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(chunk)
            if count > _MAX_LIBRARY_BYTES:
                raise ValueError("footprint context file grew beyond the staging byte limit")
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        stat = path.lstat()
    except FileNotFoundError:
        return False
    return bool(getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT)


def _check_stage_path(staging: Path, target: Path) -> None:
    try:
        relative = target.relative_to(staging)
    except ValueError as exc:
        raise ValueError("staged footprint destination escapes its temporary project") from exc
    cursor = staging
    if _is_link_or_reparse(cursor):
        raise ValueError(f"staging root is a symlink or reparse point: {cursor}")
    for part in relative.parts:
        cursor /= part
        if _is_link_or_reparse(cursor):
            raise ValueError(f"staging destination has a symlink or reparse ancestor: {cursor}")


def footprint_library_tree_sha256(path: str | Path) -> str:
    """Hash file names and contents in one bounded `.pretty` tree."""
    raw_root = Path(path)
    if _is_link_or_reparse(raw_root):
        raise ValueError("footprint tree root is a symlink or reparse point")
    root = raw_root.resolve(strict=True)
    if not root.is_dir() or root.suffix.casefold() != ".pretty":
        raise ValueError("footprint tree hash requires an existing .pretty directory")
    rows: list[tuple[str, str]] = []
    total_bytes = 0
    entries = 0
    for item in root.rglob("*"):
        entries += 1
        if entries > _MAX_LIBRARY_ENTRIES or _is_link_or_reparse(item):
            raise ValueError("footprint library contains too many entries or a symlink/reparse point")
        if not item.is_file():
            continue
        total_bytes += item.stat().st_size
        if len(rows) >= _MAX_LIBRARY_FILES or total_bytes > _MAX_LIBRARY_BYTES:
            raise ValueError("project-local footprint library exceeds staging size limits")
        if not item.resolve(strict=True).is_relative_to(root):
            raise ValueError(f"footprint library file escapes its declared .pretty root: {item}")
        rows.append((item.relative_to(root).as_posix(), _sha256(item)))
    if not rows:
        raise ValueError("project-local footprint library is empty")
    payload = "".join(f"{name}\0{digest}\n" for name, digest in sorted(rows))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _checked_local_library(project: Path, relative: str) -> Path:
    parts = relative.split("/")
    if (
        not relative or "\\" in relative or ":" in relative or "${" in relative
        or any(part in {"", ".", ".."} for part in parts)
        or Path(relative).is_absolute() or not relative.casefold().endswith(".pretty")
    ):
        raise ValueError(f"unsupported project-local footprint URI: {relative!r}")
    candidate = project.joinpath(*parts)
    cursor = project
    for part in parts:
        cursor = cursor / part
        if _is_link_or_reparse(cursor):
            raise ValueError(f"symlink in project footprint-library path: {cursor}")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"unresolved KiCad footprint library: {relative!r}") from exc
    if not resolved.is_relative_to(project) or not resolved.is_dir():
        raise ValueError(f"project footprint library is missing or escapes project: {relative!r}")
    if _is_link_or_reparse(resolved):
        raise ValueError(f"project footprint library is a symlink or reparse point: {relative!r}")
    return resolved


def _read_table(table: Path) -> bytes | None:
    if _is_link_or_reparse(table):
        raise ValueError(f"project fp-lib-table is a symlink or reparse point: {table}")
    if not table.exists():
        return None
    if not table.is_file() or table.stat().st_size > _MAX_TABLE_BYTES:
        raise ValueError("project fp-lib-table is not regular or exceeds 1 MiB")
    with table.open("rb") as stream:
        content = stream.read(_MAX_TABLE_BYTES + 1)
    if len(content) > _MAX_TABLE_BYTES:
        raise ValueError("project fp-lib-table exceeds 1 MiB")
    return content


def _parse_local_references(project: Path, table_bytes: bytes) -> list[tuple[str, Path]]:
    try:
        tree = _parse_kicad_sexp(table_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"could not parse project fp-lib-table: {exc}") from exc
    if not tree or tree[0] != "fp_lib_table":
        raise ValueError("invalid project fp-lib-table root")
    references: list[tuple[str, Path]] = []
    nicknames: set[str] = set()
    table_entries = 0
    for item in tree[1:]:
        if not isinstance(item, list) or not item or item[0] != "lib":
            continue
        table_entries += 1
        if table_entries > _MAX_TABLE_ENTRIES:
            raise ValueError("project fp-lib-table exceeds 256 library entries")
        nickname = _direct_string(item, "name")
        kind = _direct_string(item, "type")
        uri = _direct_string(item, "uri")
        if not nickname or not kind or not uri:
            raise ValueError("project fp-lib-table library entry lacks a nickname, type, or URI")
        folded_nickname = nickname.casefold()
        if folded_nickname in nicknames:
            raise ValueError(f"project fp-lib-table has duplicate library nickname: {nickname!r}")
        nicknames.add(folded_nickname)
        prefix = "${KIPRJMOD}/"
        if "${KIPRJMOD}" in uri and not uri.startswith(prefix):
            raise ValueError(f"malformed project-local footprint URI: {uri!r}")
        if uri.startswith(prefix):
            if kind != "KiCad":
                raise ValueError(f"unsupported project-local footprint library type: {kind!r}")
            relative = uri[len(prefix):]
            references.append((relative, _checked_local_library(project, relative)))
    return references


def _inventory_library(source: Path, counter: dict[str, int]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for item in source.rglob("*"):
        counter["entries"] += 1
        if counter["entries"] > _MAX_LIBRARY_ENTRIES or _is_link_or_reparse(item):
            raise ValueError("project-local footprint library has too many entries or contains a link")
        if not item.is_file():
            continue
        counter["files"] += 1
        counter["bytes"] += item.stat().st_size
        if counter["files"] > _MAX_LIBRARY_FILES or counter["bytes"] > _MAX_LIBRARY_BYTES:
            raise ValueError("project-local footprint libraries exceed staging size limits")
        if not item.resolve(strict=True).is_relative_to(source):
            raise ValueError(f"footprint library file escapes its declared .pretty root: {item}")
        hashes[item.relative_to(source).as_posix()] = _sha256(item)
    if not hashes:
        raise ValueError(f"project-local footprint library is empty: {source}")
    return hashes


def inspect_project_footprint_context(project_root: str | Path) -> dict[str, Any]:
    """Return a bounded read-only dependency inventory for workflow checkpoints."""
    project = Path(project_root).resolve(strict=True)
    if not project.is_dir():
        raise ValueError("project_root must be a directory")
    table = project / "fp-lib-table"
    table_bytes = _read_table(table)
    if table_bytes is None:
        return {"status": "absent", "source_hashes": {}, "watch_paths": [str(table)],
                "library_manifest_sha256": {}}
    references = _parse_local_references(project, table_bytes)
    source_hashes = {str(table): hashlib.sha256(table_bytes).hexdigest()}
    library_manifest_hashes: dict[str, str] = {}
    counter = {"entries": 0, "files": 0, "bytes": 0}
    seen: dict[str, str] = {}
    for relative, source in references:
        folded = relative.casefold()
        previous = seen.get(folded)
        if previous is not None and previous != relative:
            raise ValueError(f"project fp-lib-table has case-ambiguous local URI paths: {previous!r}, {relative!r}")
        if previous is not None:
            continue
        seen[folded] = relative
        files = _inventory_library(source, counter)
        source_hashes.update({str(source / name): digest for name, digest in files.items()})
        payload = "".join(f"{name}\0{files[name]}\n" for name in sorted(files))
        library_manifest_hashes[str(source)] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if _read_table(table) != table_bytes:
        raise ValueError("project fp-lib-table changed while it was inventoried")
    for directory, expected in library_manifest_hashes.items():
        if footprint_library_tree_sha256(directory) != expected:
            raise ValueError(f"project footprint library changed while it was inventoried: {directory}")
    return {
        "status": "available",
        "source_hashes": source_hashes,
        "watch_paths": sorted(set(source_hashes) | set(library_manifest_hashes)),
        "library_manifest_sha256": library_manifest_hashes,
        "library_tree_paths": sorted(library_manifest_hashes),
        "library_count": len(library_manifest_hashes),
        "file_count": counter["files"],
        "total_bytes": counter["bytes"],
    }


def _copy_library(
    source: Path, destination: Path, staging: Path, counter: dict[str, int]
) -> dict[str, str]:
    hashes = _inventory_library(source, counter)
    for relative_name, digest in hashes.items():
        item = source / Path(relative_name)
        relative = Path(relative_name)
        target = destination / relative
        _check_stage_path(staging, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or _is_link_or_reparse(target):
            raise ValueError(f"staging footprint file destination already exists: {target}")
        # Bound bytes actually copied, including concurrent source growth.
        # The inventory size alone cannot bound a later copy2/read-until-EOF.
        with item.open("rb") as stream, target.open("xb") as staged:
            while True:
                remaining = _MAX_LIBRARY_BYTES - counter.get("copied_bytes", 0)
                chunk = stream.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ValueError("footprint context grew beyond the aggregate staging byte limit")
                staged.write(chunk)
                counter["copied_bytes"] = counter.get("copied_bytes", 0) + len(chunk)
        if _sha256(target) != digest:
            raise ValueError(f"copied footprint library file failed hash verification: {item}")
    if _inventory_library(source, {"entries": 0, "files": 0, "bytes": 0}) != hashes:
        raise ValueError(f"project footprint library changed while staging: {source}")
    return hashes


def stage_project_footprint_context(
    project_root: str | Path, staging_root: str | Path
) -> dict[str, Any]:
    """Copy fp-lib-table and bounded contained .pretty assets into a fresh stage.

    KiCad's global/variable-based table entries are retained verbatim and their
    global libraries are not copied. Only literal ``${KIPRJMOD}/...pretty``
    assets are staged, preserving each URI's relative layout. Callers should
    snapshot the returned source hashes around native execution.
    """
    project = Path(project_root).resolve(strict=True)
    raw_staging = Path(staging_root)
    if _is_link_or_reparse(raw_staging):
        raise ValueError("staging root is a symlink or reparse point")
    staging = raw_staging.resolve(strict=True)
    if not project.is_dir() or not staging.is_dir() or project == staging:
        raise ValueError("project and fresh staging roots must be distinct directories")
    inventory = inspect_project_footprint_context(project)
    if inventory["status"] == "absent":
        target = staging / "fp-lib-table"
        _check_stage_path(staging, target)
        if target.exists() or _is_link_or_reparse(target):
            raise ValueError("staging fp-lib-table conflicts with absent source project table")
        return inventory

    table = project / "fp-lib-table"
    table_bytes = _read_table(table)
    if table_bytes is None:
        raise ValueError("project fp-lib-table disappeared after inventory")
    references = _parse_local_references(project, table_bytes)
    target_table = staging / "fp-lib-table"
    _check_stage_path(staging, target_table)
    if target_table.exists() or _is_link_or_reparse(target_table):
        raise ValueError("staging fp-lib-table destination already exists")
    with target_table.open("xb") as stream:
        stream.write(table_bytes)
    if _sha256(target_table) != inventory["source_hashes"][str(table)]:
        raise ValueError("staged project fp-lib-table failed hash verification")

    counter = {"entries": 0, "files": 0, "bytes": 0}
    copied: dict[str, str] = {}
    for relative, source in references:
        folded = relative.casefold()
        previous = copied.get(folded)
        if previous is not None and previous != relative:
            raise ValueError(f"project fp-lib-table has case-ambiguous local URI paths: {previous!r}, {relative!r}")
        if previous is not None:
            continue
        copied[folded] = relative
        destination = staging.joinpath(*relative.split("/"))
        _check_stage_path(staging, destination)
        if destination.exists() or _is_link_or_reparse(destination):
            raise ValueError(f"staging footprint destination already exists: {relative}")
        _copy_library(source, destination, staging, counter)
    if inspect_project_footprint_context(project) != inventory:
        raise ValueError("project footprint context changed during staging")
    return {
        **inventory,
        "status": "staged",
        "library_count": len(copied),
        "file_count": counter["files"],
        "total_bytes": counter["bytes"],
    }
