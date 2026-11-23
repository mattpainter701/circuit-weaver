"""Bounded staging of explicitly project-local KiCad footprint libraries.

Callers provide the original project directory explicitly; this module never
searches up from a temporary staging folder.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
from pathlib import Path
from typing import Any

from .footprint_lib import _direct_string, _parse_kicad_sexp

_MAX_TABLE_BYTES = 1 * 1024 * 1024
_MAX_TABLE_ENTRIES = 256
_MAX_LIBRARY_FILES = 4_096
_MAX_LIBRARY_BYTES = 64 * 1024 * 1024
_MAX_LIBRARY_ENTRIES = 8_192
_REPARSE_POINT = 0x400
_MAX_STOCK_TABLE_BYTES = 1 * 1024 * 1024
_MAX_STOCK_ASSET_FILES = 4_096
_MAX_STOCK_ASSET_BYTES = 64 * 1024 * 1024


def _sha256(path: Path) -> str:
    return _sha256_limited(path, _MAX_LIBRARY_BYTES)


def _sha256_limited(path: Path, limit: int) -> str:
    return _hash_file_limited(path, limit)[0]


def _hash_file_limited(path: Path, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(min(1024 * 1024, limit - count + 1))
            if not chunk:
                break
            count += len(chunk)
            if count > limit:
                raise ValueError("footprint context file grew beyond its staging byte limit")
            digest.update(chunk)
    return digest.hexdigest(), count


def _stock_footprint_table_candidates(cli: str | Path) -> list[Path]:
    """Find the shipped footprint table alongside the selected KiCad CLI."""
    raw_executable = Path(cli)
    executable = raw_executable
    try:
        executable = executable.resolve(strict=False)
    except (OSError, RuntimeError):
        pass
    candidates: list[Path] = []
    if platform.system() == "Darwin":
        for path in (raw_executable, executable):
            for parent in path.parents:
                if parent.name == "Contents":
                    support = parent / "SharedSupport"
                    candidates.extend((support / "template" / "fp-lib-table", support / "fp-lib-table"))
                    break
    else:
        # Installed CLIs normally live in <prefix>/bin or /usr/bin. Deriving
        # the prefix from the selected executable avoids borrowing another
        # KiCad installation's table.
        for path in (raw_executable, executable):
            parents = list(path.parents)
            for index, parent in enumerate(parents):
                if parent.name.casefold() == "bin" and index + 1 < len(parents):
                    prefix = parents[index + 1]
                    candidates.append(prefix / "share" / "kicad" / "template" / "fp-lib-table")
                    break
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _stock_asset_identity(
    board_bytes: bytes,
    project_root: Path,
    stock_root: Path,
    major: int,
    table_tree: list,
) -> dict[str, Any]:
    """Hash only stock footprint files referenced by this board, within caps."""
    try:
        references: set[tuple[str, str]] = set()
        for match in re.finditer(rb'\(footprint\s+("(?:\\.|[^"\\])*"|[^\s()]+)', board_bytes):
            try:
                token = match.group(1)
                if token.startswith(b'"'):
                    reference = token[1:-1].decode("utf-8").replace('\\"', '"').replace("\\\\", "\\")
                else:
                    reference = token.decode("utf-8")
            except UnicodeError:
                return {"status": "unverified", "reason": "board has a non-UTF-8 footprint reference"}
            if ":" in reference:
                library, footprint = reference.split(":", 1)
                references.add((library, footprint))
                if len(references) > _MAX_STOCK_ASSET_FILES:
                    return {"status": "unverified", "reason": "board exceeds stock footprint inventory count limit"}
            else:
                return {"status": "unverified", "reason": "board has a footprint reference without a library nickname"}
        if b"(module " in board_bytes and not references:
            return {"status": "unverified", "reason": "legacy module footprints are outside the bounded identity scan"}

        project_nicknames: dict[str, str] = {}
        project_table = _read_table(project_root / "fp-lib-table")
        if project_table:
            tree = _parse_kicad_sexp(project_table.decode("utf-8"))
            for item in tree[1:]:
                if isinstance(item, list) and item and item[0] == "lib":
                    uri = _direct_string(item, "uri")
                    nickname = _direct_string(item, "name")
                    if nickname:
                        project_nicknames[nickname.casefold()] = uri

        stock_libraries: dict[str, str] = {}
        expected_variables = {f"KICAD{major}_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR"}
        for item in table_tree[1:]:
            if not isinstance(item, list) or not item or item[0] != "lib":
                continue
            nickname = _direct_string(item, "name")
            kind = _direct_string(item, "type")
            uri = _direct_string(item, "uri")
            if not nickname or kind != "KiCad":
                continue
            match = re.fullmatch(r"\$\{([^}]+)\}/([^/\\:]+\.pretty)", uri)
            if match and match.group(1) in expected_variables:
                if nickname.casefold() in stock_libraries:
                    return {"status": "unverified", "reason": "stock footprint table has duplicate library nicknames"}
                stock_libraries[nickname.casefold()] = match.group(2)

        rows: list[tuple[str, str, str]] = []
        paths: list[tuple[Path, str]] = []
        total_bytes = 0
        unresolved = False
        for library, footprint in sorted(references):
            project_uri = project_nicknames.get(library.casefold())
            if project_uri is not None:
                if project_uri.startswith("${KIPRJMOD}/"):
                    continue
                unresolved = True
                continue
            folder_name = stock_libraries.get(library.casefold())
            if folder_name is None:
                unresolved = True
                continue
            relative_file = f"{folder_name}/{footprint}.kicad_mod"
            candidate = stock_root / relative_file
            library_root = stock_root / folder_name
            if _is_link_or_reparse(library_root) or _is_link_or_reparse(candidate) or not candidate.is_file():
                unresolved = True
                continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(stock_root.resolve(strict=True)):
                return {"status": "unverified", "reason": "stock footprint file escapes its installed library root"}
            if len(paths) >= _MAX_STOCK_ASSET_FILES:
                return {"status": "unverified", "reason": "referenced stock footprint assets exceed inventory limits"}
            digest, size = _hash_file_limited(candidate, _MAX_STOCK_ASSET_BYTES - total_bytes)
            total_bytes += size
            rows.append((library, footprint, digest))
            paths.append((candidate, digest))

        payload = "".join(f"{library}\0{footprint}\0{digest}\n" for library, footprint, digest in rows)
        return {
            "status": "unverified" if unresolved else "verified",
            "reason": "some board stock footprint references could not be resolved to installed assets"
            if unresolved
            else "",
            "file_count": len(rows),
            "total_bytes": total_bytes,
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "files": [
                {"library": library, "footprint": footprint, "sha256": digest} for library, footprint, digest in rows
            ],
            "_paths": paths,
        }
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return {"status": "unverified", "reason": f"stock footprint identity could not be verified: {exc}"}


def stage_installed_footprint_context(
    cli: str | Path,
    major: int,
    staging_root: str | Path,
    *,
    board_bytes: bytes,
    project_root: str | Path,
) -> dict[str, Any]:
    """Stage only board-referenced stock footprints into a private KiCad config.

    The installed table is the provenance source; a filtered table and referenced
    footprint files are staged privately. No user-global config is modified.
    """
    stage = Path(staging_root).resolve(strict=True)
    if not stage.is_dir():
        raise ValueError("staging root must be a directory")
    source: Path | None = None
    data: bytes | None = None
    for candidate in _stock_footprint_table_candidates(cli):
        try:
            if _is_link_or_reparse(candidate) or not candidate.is_file():
                continue
            if candidate.stat().st_size > _MAX_STOCK_TABLE_BYTES:
                return {"status": "invalid", "reason": "installed footprint table exceeds 1 MiB"}
            with candidate.open("rb") as stream:
                content = stream.read(_MAX_STOCK_TABLE_BYTES + 1)
            if len(content) > _MAX_STOCK_TABLE_BYTES:
                return {"status": "invalid", "reason": "installed footprint table grew beyond 1 MiB"}
        except OSError:
            return {"status": "invalid", "reason": "installed footprint table could not be read safely"}
        try:
            tree = _parse_kicad_sexp(content.decode("utf-8"))
        except (UnicodeError, ValueError):
            return {"status": "invalid", "reason": "installed footprint table is not valid KiCad S-expression text"}
        if not tree or tree[0] != "fp_lib_table":
            return {"status": "invalid", "reason": "installed footprint table has an invalid root"}
        source, data = candidate.resolve(strict=True), content
        break
    if source is None or data is None:
        return {
            "status": "unavailable",
            "reason": "selected KiCad installation has no discoverable stock footprint table",
        }

    install_root: Path | None = None
    for candidate in _stock_footprint_table_candidates(cli):
        if candidate == source or candidate.resolve(strict=False) == source:
            if candidate.parts[-4:-1] == ("share", "kicad", "template"):
                install_root = candidate.parents[3]
            elif "SharedSupport" in candidate.parts:
                idx = candidate.parts.index("SharedSupport")
                install_root = Path(*candidate.parts[: idx + 1])
            break
    if install_root is None:
        return {"status": "invalid", "reason": "could not bind installed footprint table to its installation root"}
    stock_root = (
        install_root / "share" / "kicad" / "footprints"
        if platform.system() != "Darwin"
        else install_root / "footprints"
    )
    if not stock_root.is_dir() or _is_link_or_reparse(stock_root):
        return {"status": "invalid", "reason": "selected installation footprint root is missing or unsafe"}
    try:
        tree = _parse_kicad_sexp(data.decode("utf-8"))
        variables: set[str] = set()
        for item in tree[1:]:
            if isinstance(item, list) and item and item[0] == "lib":
                uri = _direct_string(item, "uri")
                variables.update(re.findall(r"\$\{([^}]+)\}", uri))
        allowed = {f"KICAD{major}_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR"}
        if not variables or not variables.issubset(allowed):
            return {
                "status": "invalid",
                "reason": "installed footprint table does not match the selected KiCad major version",
            }
    except (UnicodeError, ValueError, IndexError):
        return {"status": "invalid", "reason": "installed footprint table contains malformed library entries"}

    digest = hashlib.sha256(data).hexdigest()
    asset_identity = _stock_asset_identity(
        board_bytes, Path(project_root).resolve(strict=True), stock_root, major, tree
    )
    footprint_env = (
        f"KICAD{major}_FOOTPRINT_DIR" if f"KICAD{major}_FOOTPRINT_DIR" in variables else "KICAD_FOOTPRINT_DIR"
    )
    library_folders: dict[str, str] = {}
    for item in tree[1:]:
        if not isinstance(item, list) or not item or item[0] != "lib":
            continue
        nickname = _direct_string(item, "name")
        uri = _direct_string(item, "uri")
        match = re.fullmatch(rf"\$\{{{re.escape(footprint_env)}\}}/([^/\\:]+\.pretty)", uri)
        if nickname and match and match.group(1) not in {".", ".."}:
            library_folders[nickname.casefold()] = match.group(1)

    effective_root = stage / "cw-stock-footprints"
    if effective_root.exists() or _is_link_or_reparse(effective_root):
        raise ValueError("staged stock footprint destination already exists")
    effective_root.mkdir()
    copied_paths: list[tuple[Path, str]] = []
    copied_bytes = 0
    used_libraries: set[str] = set()
    for record in asset_identity.get("files", []):
        nickname, footprint, expected = record["library"], record["footprint"], record["sha256"]
        folder = library_folders.get(nickname.casefold())
        if folder is None or Path(footprint).name != footprint or any(sep in footprint for sep in ("/", "\\", ":")):
            asset_identity["status"] = "unverified"
            asset_identity["reason"] = "stock footprint identity cannot be staged safely"
            continue
        source_asset = stock_root / folder / f"{footprint}.kicad_mod"
        destination = effective_root / folder / f"{footprint}.kicad_mod"
        _check_stage_path(stage, destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or _is_link_or_reparse(destination):
            raise ValueError("staged stock footprint file destination already exists")
        remaining = _MAX_STOCK_ASSET_BYTES - copied_bytes
        with source_asset.open("rb") as reader, destination.open("xb") as writer:
            while True:
                chunk = reader.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ValueError("referenced stock footprints grew beyond the staging byte limit")
                writer.write(chunk)
                remaining -= len(chunk)
                copied_bytes += len(chunk)
        if _sha256_limited(destination, _MAX_STOCK_ASSET_BYTES) != expected:
            raise ValueError("staged stock footprint failed hash verification")
        copied_paths.append((destination, expected))
        used_libraries.add(nickname)

    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    entries = []
    for nickname in sorted(used_libraries, key=str.casefold):
        folder = library_folders[nickname.casefold()]
        entries.append(
            f"  (lib (name {quoted(nickname)}) (type \"KiCad\") "
            f"(uri {quoted('${' + footprint_env + '}/' + folder)}))"
        )
    staged_table_bytes = ("(fp_lib_table\n" + "\n".join(entries) + "\n)\n").encode("utf-8")
    config = stage / "kicad-config"
    if config.exists() or _is_link_or_reparse(config):
        raise ValueError("staged KiCad config destination already exists")
    config.mkdir()
    target = config / f"{major}.0" / "fp-lib-table"
    _check_stage_path(stage, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(staged_table_bytes)
    generated_table_sha256 = hashlib.sha256(staged_table_bytes).hexdigest()
    if _sha256_limited(target, _MAX_STOCK_TABLE_BYTES) != generated_table_sha256:
        raise ValueError("staged filtered footprint table failed hash verification")
    return {
        "status": "staged",
        "source": str(source),
        "sha256": digest,
        "stock_root": str(stock_root.resolve(strict=True)),
        "effective_root": str(effective_root.resolve(strict=True)),
        "config_root": str(config),
        "generated_table_sha256": generated_table_sha256,
        "footprint_env": footprint_env,
        "asset_identity": asset_identity,
        "_staged_paths": copied_paths,
    }


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
        not relative
        or "\\" in relative
        or ":" in relative
        or "${" in relative
        or any(part in {"", ".", ".."} for part in parts)
        or Path(relative).is_absolute()
        or not relative.casefold().endswith(".pretty")
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
            relative = uri[len(prefix) :]
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
        return {"status": "absent", "source_hashes": {}, "watch_paths": [str(table)], "library_manifest_sha256": {}}
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


def _copy_library(source: Path, destination: Path, staging: Path, counter: dict[str, int]) -> dict[str, str]:
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


def stage_project_footprint_context(project_root: str | Path, staging_root: str | Path) -> dict[str, Any]:
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
