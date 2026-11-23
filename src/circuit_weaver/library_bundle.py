"""Plan and apply a local KiCad library bundle with user-asserted provenance.

This module copies user-authored library assets into a project. It never fetches
files and never claims that a symbol pin map, footprint fit, or SPICE model is
correct. Provenance and license text are user assertions. SPICE subcircuit and
pin-order fields are user-declared metadata and are not checked against the
source model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .footprint_lib import _parse_kicad_sexp
from .project_state import file_sha256

_KINDS = {"symbol_library", "footprint_library", "model", "spice"}
_NICK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SUBCKT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$.-]{0,127}$")
_MODEL_SUFFIXES = {".step", ".stp", ".wrl", ".wrz", ".iges", ".igs", ".obj", ".stl", ".ply"}
_WINDOWS_DEVICE_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
_MAX_TABLE_BYTES = 1024 * 1024
_RECEIPT_DIR = "libraries/.circuit-weaver-receipts"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_digest(value: Any) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "${" in value:
        raise ValueError("destination must be a safe project-relative path using forward slashes")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or Path(value).is_absolute():
        raise ValueError("destination path traversal is not allowed")
    for part in parts:
        _validate_windows_component(part)
    if parts[0] != "libraries" or len(parts) < 2:
        raise ValueError("all bundle destinations must be under project libraries/")
    return "/".join(parts)


def _validate_windows_component(value: str) -> None:
    if value.endswith((".", " ")):
        raise ValueError("path components cannot end with a dot or space")
    if any(ord(char) < 32 or char in '<>:"|?*' for char in value):
        raise ValueError("path component contains a forbidden filename character")
    basename = value.split(".", 1)[0]
    if _WINDOWS_DEVICE_RE.fullmatch(basename):
        raise ValueError("path component uses a reserved Windows device name")


def _validate_destination(project: Path, relative: str, *, require_absent: bool = True) -> Path:
    """Check every existing path component without following symlinks."""
    relative = _safe_relative(relative)
    parts = relative.split("/")
    cursor = project
    for part in parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"symlink destination ancestor is unsupported: {cursor}")
        if cursor.exists():
            if not cursor.is_dir():
                raise ValueError(f"destination ancestor is not a directory: {cursor}")
            resolved = cursor.resolve(strict=True)
            if not resolved.is_relative_to(project):
                raise ValueError(f"destination ancestor escapes project root: {cursor}")
    destination = cursor / parts[-1]
    if destination.is_symlink():
        raise ValueError(f"destination symlink conflict: {relative}")
    if require_absent and destination.exists():
        raise ValueError(f"destination conflict: {relative}")
    return destination


def _snapshot_matches(destination: Path, expected: list[dict[str, str]], is_directory: bool) -> bool:
    try:
        actual = _source_snapshot(destination, is_directory)["files"]
    except (OSError, RuntimeError, ValueError):
        return False
    if is_directory:
        return actual == expected
    return len(actual) == len(expected) == 1 and actual[0]["sha256"] == expected[0]["sha256"]


@contextmanager
def _bundle_lock(project: Path):
    lock_path = project / ".circuit-weaver-library-bundle.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("another library bundle apply is active, or its lock needs review") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _source_snapshot(path: Path, want_dir: bool) -> dict[str, Any]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError(f"symlink sources are not allowed: {path}")
    # Resolve follows symlinked parents even when the final source is a plain
    # file. Reject those aliases before building a source snapshot.
    for ancestor in expanded.absolute().parents:
        if ancestor.is_symlink():
            raise ValueError(f"symlink source ancestors are not allowed: {ancestor}")
    resolved = expanded.resolve(strict=True)
    if want_dir != resolved.is_dir() or (not want_dir and not resolved.is_file()):
        raise ValueError(f"source has the wrong type: {path}")
    if resolved.is_symlink():
        raise ValueError(f"symlink sources are not allowed: {path}")
    files: list[dict[str, str]] = []
    if want_dir:
        for item in sorted(
            resolved.rglob("*"),
            key=lambda p: (p.relative_to(resolved).as_posix().casefold(), p.relative_to(resolved).as_posix()),
        ):
            if item.is_symlink():
                raise ValueError(f"symlinks inside library directories are not allowed: {item}")
            if item.is_file():
                files.append({"path": item.relative_to(resolved).as_posix(), "sha256": file_sha256(item)})
        if not files:
            raise ValueError(f"source library directory is empty: {path}")
    else:
        files.append({"path": resolved.name, "sha256": file_sha256(resolved)})
    return {"path": str(resolved), "is_directory": want_dir, "files": files}


def _table_state(project: Path, filename: str, root_name: str) -> dict[str, Any]:
    path = project / filename
    if not path.exists():
        return {"path": filename, "sha256": None, "text": None}
    if path.is_symlink():
        raise ValueError(f"project table symlink is unsupported: {filename}")
    if not path.is_file() or path.stat().st_size > _MAX_TABLE_BYTES:
        raise ValueError(f"unsupported or oversized project table: {filename}")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        tree = _parse_kicad_sexp(text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"could not parse project table {filename}: {exc}") from exc
    if not tree or tree[0] != root_name:
        raise ValueError(f"unsupported project table root in {filename}")
    # Validate all library entries before preserving and extending this table.
    names: set[str] = set()
    for entry in tree[1:]:
        if not isinstance(entry, list) or not entry or entry[0] != "lib":
            continue
        try:
            fields = [child for child in entry if isinstance(child, list) and child]
            name_fields = [child for child in fields if child[0] == "name"]
            uri_fields = [child for child in fields if child[0] == "uri"]
            type_fields = [child for child in fields if child[0] == "type"]
            if any(len(items) != 1 for items in (name_fields, uri_fields, type_fields)):
                raise ValueError("library entry must have exactly one name, type, and uri")
            malformed = any(
                len(items[0]) != 2 or not isinstance(items[0][1], str)
                for items in (name_fields, uri_fields, type_fields)
            )
            if malformed:
                raise ValueError("malformed library entry field")
            name = name_fields[0][1]
            folded_name = name.casefold()
            if not name or folded_name in names:
                raise ValueError("empty or duplicate library nickname")
            names.add(folded_name)
        except (IndexError, ValueError) as exc:
            raise ValueError(f"unsupported library entry in {filename}: {exc}") from exc
    return {"path": filename, "sha256": _sha(raw), "text": text}


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _existing_table_nicknames(text: str | None) -> set[str]:
    if text is None:
        return set()
    tree = _parse_kicad_sexp(text)
    names: set[str] = set()
    for entry in tree[1:]:
        if isinstance(entry, list) and entry and entry[0] == "lib":
            fields = [child for child in entry if isinstance(child, list) and child and child[0] == "name"]
            if fields and len(fields[0]) == 2 and isinstance(fields[0][1], str):
                names.add(fields[0][1])
    return names


def _root_close_index(text: str) -> int:
    depth = 0
    quoted = False
    escaped = False
    comment = False
    for index, char in enumerate(text):
        if comment:
            if char == "\n":
                comment = False
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == ";":
            comment = True
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unsupported project table format")


def _append_entry(table_state: dict[str, Any], root_name: str, nickname: str, rel: str) -> bytes:
    text = table_state["text"]
    if text is None:
        uri = _quote("${KIPRJMOD}/" + rel)
        return f'({root_name}\n  (lib (name {_quote(nickname)}) (type "KiCad") (uri {uri}))\n)\n'.encode()
    _parse_kicad_sexp(text)
    if nickname.casefold() in {name.casefold() for name in _existing_table_nicknames(text)}:
        raise ValueError(f"project table already contains nickname {nickname!r}")
    close = _root_close_index(text)
    entry_text = f'\n  (lib (name {_quote(nickname)}) (type "KiCad") (uri {_quote("${KIPRJMOD}/" + rel)}))\n'
    return (text[:close] + entry_text + text[close:]).encode("utf-8")


def _normalize_manifest(manifest: dict[str, Any], project: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("assets"), list):
        raise ValueError("manifest must contain version=1 and an assets list")
    assets: list[dict[str, Any]] = []
    table_additions: list[dict[str, Any]] = []
    seen_dest: set[str] = set()
    seen_nick: set[tuple[str, str]] = set()
    for raw in manifest["assets"]:
        if not isinstance(raw, dict) or raw.get("kind") not in _KINDS:
            raise ValueError("each asset must have a supported kind")
        kind = raw["kind"]
        provenance, license_text = raw.get("provenance"), raw.get("license")
        missing_provenance = not isinstance(provenance, str) or not provenance.strip()
        missing_license = not isinstance(license_text, str) or not license_text.strip()
        if missing_provenance or missing_license:
            raise ValueError("each asset requires non-empty provenance and license text")
        nickname = raw.get("nickname")
        if kind in {"symbol_library", "footprint_library"}:
            if not isinstance(nickname, str):
                raise ValueError("library nickname contains unsupported characters")
            _validate_windows_component(nickname)
            if not _NICK_RE.fullmatch(nickname):
                raise ValueError("library nickname contains unsupported characters")
            table_kind = "symbol" if kind == "symbol_library" else "footprint"
            key = (table_kind, nickname.casefold())
            if key in seen_nick:
                raise ValueError(f"duplicate project library nickname: {nickname}")
            seen_nick.add(key)
            if kind == "symbol_library":
                dest = f"libraries/symbols/{nickname}.kicad_sym"
                if Path(str(raw.get("source", ""))).suffix != ".kicad_sym":
                    raise ValueError("symbol_library source must be a .kicad_sym file")
                table_additions.append(
                    {"table": "sym-lib-table", "root": "sym_lib_table", "nickname": nickname, "destination": dest}
                )
                is_dir = False
            else:
                dest = f"libraries/footprints/{nickname}.pretty"
                if not str(raw.get("source", "")).endswith(".pretty"):
                    raise ValueError("footprint_library source must be a .pretty directory")
                table_additions.append(
                    {"table": "fp-lib-table", "root": "fp_lib_table", "nickname": nickname, "destination": dest}
                )
                is_dir = True
        else:
            dest = _safe_relative(raw.get("destination"))
            is_dir = False
            if kind == "model":
                source_suffix = Path(str(raw.get("source", ""))).suffix.casefold()
                destination_suffix = Path(dest).suffix.casefold()
                if source_suffix not in _MODEL_SUFFIXES or destination_suffix not in _MODEL_SUFFIXES:
                    raise ValueError("model assets must use a supported 3D model file extension")
            if kind == "spice":
                subckt, pin_order = raw.get("subckt"), raw.get("pin_order")
                if not isinstance(subckt, str) or not _SUBCKT_RE.fullmatch(subckt):
                    raise ValueError("SPICE staging requires a valid explicit subckt name")
                if (
                    not isinstance(pin_order, list)
                    or not pin_order
                    or any(not isinstance(pin, str) or not pin.strip() for pin in pin_order)
                    or len({pin.casefold() for pin in pin_order}) != len(pin_order)
                ):
                    raise ValueError("SPICE staging requires an explicit unique non-empty pin_order")
        folded_dest = dest.casefold()
        if folded_dest in seen_dest:
            raise ValueError(f"duplicate destination: {dest}")
        seen_dest.add(folded_dest)
        src = raw.get("source")
        if not isinstance(src, str) or not src.strip():
            raise ValueError("asset source must be a local filesystem path")
        source_path = Path(src).expanduser()
        if not source_path.is_absolute():
            raise ValueError("asset source must be an absolute local filesystem path")
        snapshot = _source_snapshot(source_path, is_dir)
        assets.append(
            {
                "kind": kind,
                "nickname": nickname if kind in {"symbol_library", "footprint_library"} else None,
                "source": snapshot,
                "destination": dest,
                "provenance": provenance.strip(),
                "license": license_text.strip(),
                "spice_contract": (
                    {
                        "subckt": raw["subckt"],
                        "pin_order": list(raw["pin_order"]),
                        "claim": "user-declared metadata; source declaration and pin mapping not validated",
                    }
                    if kind == "spice"
                    else None
                ),
            }
        )
    assets.sort(key=lambda item: (item["destination"].casefold(), item["kind"]))
    table_additions.sort(key=lambda item: (item["table"], item["nickname"].casefold()))
    return assets, table_additions


def plan_library_bundle(project_root: str | Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Create a deterministic, JSON-safe dry-run plan for local project assets."""
    project = Path(project_root).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise ValueError("project_root must be a directory")
    assets, additions = _normalize_manifest(manifest, project)
    tables: dict[str, dict[str, Any]] = {}
    for addition in additions:
        tables.setdefault(addition["table"], _table_state(project, addition["table"], addition["root"]))
        existing_names = _existing_table_nicknames(tables[addition["table"]]["text"])
        if addition["nickname"].casefold() in {name.casefold() for name in existing_names}:
            raise ValueError(f"project table already contains nickname {addition['nickname']!r}")
    for asset in assets:
        _validate_destination(project, asset["destination"])
    plan: dict[str, Any] = {
        "status": "planned",
        "project_root": str(project),
        "manifest": {"version": 1, "assets": manifest["assets"]},
        "assets": assets,
        "table_preconditions": {
            name: {"path": state["path"], "sha256": state["sha256"]} for name, state in sorted(tables.items())
        },
        "changes": [
            {
                "action": "copy_directory" if item["source"]["is_directory"] else "copy_file",
                "source": item["source"]["path"],
                "destination": item["destination"],
                "files": len(item["source"]["files"]),
                "sha256": _json_digest(item["source"]["files"]),
            }
            for item in assets
        ]
        + [
            {
                "action": "append_library_entry",
                "table": item["table"],
                "nickname": item["nickname"],
                "uri": "${KIPRJMOD}/" + item["destination"],
            }
            for item in additions
        ],
        "proof": {
            "local_only": True,
            "source_hashes": "sha256",
            "provenance_and_license_are_user_asserted": True,
            "symbol_pin_maps_verified": False,
            "footprint_fit_verified": False,
            "model_correctness_verified": False,
            "spice_file_presence_means_usable_model": False,
            "spice_contract_is_user_declared_metadata": True,
        },
    }
    plan["plan_sha256"] = _json_digest(plan)
    return plan


def apply_library_bundle(project_root: str | Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Apply a plan transactionally, or confirm an exact prior apply by receipt."""
    project = Path(project_root).expanduser().resolve(strict=True)
    with _bundle_lock(project):
        return _apply_library_bundle_locked(project, plan)


def _already_applied(project: Path, plan: dict[str, Any], receipt_path: Path) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("existing bundle receipt is unreadable; refusing to retry") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("version") != 1
        or receipt.get("plan_sha256") != plan.get("plan_sha256")
    ):
        raise ValueError("existing bundle receipt does not match this plan")
    expected_sources = {asset["destination"]: asset["source"] for asset in plan.get("assets", [])}
    if receipt.get("source_hashes") != {
        destination: {"is_directory": source["is_directory"], "files": source["files"]}
        for destination, source in expected_sources.items()
    }:
        raise ValueError("bundle receipt source hashes do not match this plan")
    for destination, source in expected_sources.items():
        destination_path = _validate_destination(project, destination, require_absent=False)
        if not _snapshot_matches(
            destination_path,
            source["files"],
            source["is_directory"],
        ):
            raise ValueError(f"installed asset changed since apply: {destination}")
        try:
            source_matches = _source_snapshot(Path(source["path"]), source["is_directory"]) == source
        except (OSError, RuntimeError, ValueError):
            source_matches = False
        if not source_matches:
            raise ValueError(f"source changed since apply: {source['path']}")
    expected_tables = receipt.get("table_hashes")
    if not isinstance(expected_tables, dict) or set(expected_tables) != set(plan["table_preconditions"]):
        raise ValueError("bundle receipt lacks table hashes")
    for filename, expected_hash in expected_tables.items():
        if filename not in {"sym-lib-table", "fp-lib-table"} or not isinstance(expected_hash, str):
            raise ValueError("bundle receipt has invalid table hashes")
        table = project / filename
        if table.is_symlink() or not table.is_file() or _sha(table.read_bytes()) != expected_hash:
            raise ValueError(f"project table changed since apply: {filename}")
    return {
        "status": "already_applied",
        "project_root": str(project),
        "plan_sha256": plan["plan_sha256"],
        "written": sorted([*expected_sources, *expected_tables]),
        "proof": plan["proof"],
        "receipt": str(receipt_path.relative_to(project).as_posix()),
    }


def _apply_library_bundle_locked(project: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Apply a fresh plan transactionally, restoring touched state on failure."""
    if not isinstance(plan, dict) or plan.get("status") != "planned" or plan.get("project_root") != str(project):
        raise ValueError("plan does not target this project")
    claimed = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if not isinstance(claimed, str) or _json_digest(unsigned) != claimed:
        raise ValueError("plan integrity check failed")
    receipt_rel = f"{_RECEIPT_DIR}/{claimed}.json"
    receipt_path = _validate_destination(project, receipt_rel, require_absent=False)
    if receipt_path.exists():
        return _already_applied(project, plan, receipt_path)
    fresh = plan_library_bundle(project, plan.get("manifest"))
    if fresh != plan:
        raise ValueError("plan is stale or differs from the current sources/project")

    changed: list[Path] = []
    old_tables: dict[Path, bytes | None] = {}
    created_dirs: set[Path] = set()
    table_temps: set[Path] = set()
    written_table_hashes: dict[str, str] = {}
    try:
        for asset in plan["assets"]:
            source = Path(asset["source"]["path"])
            destination = _validate_destination(project, asset["destination"])
            # Recheck content immediately before copying.
            current = _source_snapshot(source, asset["source"]["is_directory"])
            if current != asset["source"]:
                raise ValueError(f"source changed during apply: {source}")
            relative_parent = Path(*asset["destination"].split("/")[:-1])
            parent_chain = [
                project.joinpath(*relative_parent.parts[:index]) for index in range(1, len(relative_parent.parts) + 1)
            ]
            for directory in parent_chain:
                _validate_destination(project, asset["destination"], require_absent=True)
                if directory.exists():
                    continue
                directory.mkdir()
                created_dirs.add(directory)
            destination = _validate_destination(project, asset["destination"], require_absent=True)
            changed.append(destination)
            if asset["source"]["is_directory"]:
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
            if not _snapshot_matches(destination, asset["source"]["files"], asset["source"]["is_directory"]):
                raise ValueError(f"copied source changed during apply: {source}")
        additions: list[dict[str, Any]] = []
        for raw in plan["manifest"]["assets"]:
            if raw["kind"] == "symbol_library":
                additions.append(
                    {
                        "table": "sym-lib-table",
                        "root": "sym_lib_table",
                        "nickname": raw["nickname"],
                        "destination": f"libraries/symbols/{raw['nickname']}.kicad_sym",
                    }
                )
            elif raw["kind"] == "footprint_library":
                additions.append(
                    {
                        "table": "fp-lib-table",
                        "root": "fp_lib_table",
                        "nickname": raw["nickname"],
                        "destination": f"libraries/footprints/{raw['nickname']}.pretty",
                    }
                )
        table_groups: dict[str, list[dict[str, Any]]] = {}
        for item in additions:
            table_groups.setdefault(item["table"], []).append(item)
        for filename, items in sorted(table_groups.items()):
            root_name = items[0]["root"]
            state = _table_state(project, filename, root_name)
            expected_state = plan["table_preconditions"][filename]
            if state["sha256"] != expected_state["sha256"]:
                raise ValueError(f"project table changed during apply: {filename}")
            table_path = project / filename
            old_tables[table_path] = table_path.read_bytes() if table_path.exists() else None
            text = state["text"]
            for item in sorted(items, key=lambda entry: entry["nickname"].casefold()):
                state["text"] = text
                text = _append_entry(state, root_name, item["nickname"], item["destination"]).decode("utf-8")
                state["text"] = text
            handle = tempfile.NamedTemporaryFile(prefix=table_path.name + ".bundle-", dir=project, delete=False)
            temp = Path(handle.name)
            handle.close()
            table_temps.add(temp)
            table_bytes = text if isinstance(text, bytes) else text.encode("utf-8")
            temp.write_bytes(table_bytes)
            os.replace(temp, table_path)
            table_temps.discard(temp)
            changed.append(table_path)
            table_hash = _sha(table_bytes)
            if table_path.is_symlink() or _sha(table_path.read_bytes()) != table_hash:
                raise ValueError(f"project table changed during apply: {filename}")
            written_table_hashes[filename] = table_hash

        for asset in plan["assets"]:
            source = _source_snapshot(Path(asset["source"]["path"]), asset["source"]["is_directory"])
            if source != asset["source"]:
                raise ValueError(f"source changed during apply: {asset['source']['path']}")
        source_hashes = {
            asset["destination"]: {
                "is_directory": asset["source"]["is_directory"],
                "files": asset["source"]["files"],
            }
            for asset in plan["assets"]
        }
        table_hashes = written_table_hashes
        receipt_rel = f"{_RECEIPT_DIR}/{claimed}.json"
        receipt_path = _validate_destination(project, receipt_rel)
        receipt_parent = receipt_path.parent
        if not receipt_parent.exists():
            receipt_parent.mkdir(parents=True)
            created_dirs.add(receipt_parent)
        receipt_path = _validate_destination(project, receipt_rel)
        receipt = {
            "version": 1,
            "plan_sha256": claimed,
            "source_hashes": source_hashes,
            "table_hashes": table_hashes,
        }
        handle = tempfile.NamedTemporaryFile(prefix="bundle-receipt-", dir=receipt_parent, delete=False)
        temp = Path(handle.name)
        handle.close()
        table_temps.add(temp)
        temp.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, receipt_path)
        table_temps.discard(temp)
        changed.append(receipt_path)
    except Exception:
        for temp in table_temps:
            temp.unlink(missing_ok=True)
        for path in reversed(changed):
            if path in old_tables:
                if path.is_symlink() or path.parent.resolve(strict=True) != project:
                    continue
                prior = old_tables[path]
                if prior is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(prior)
            else:
                try:
                    rel = path.relative_to(project).as_posix()
                    safe_path = _validate_destination(project, rel, require_absent=False)
                except (ValueError, OSError, RuntimeError):
                    continue
                if safe_path.is_dir():
                    shutil.rmtree(safe_path, ignore_errors=True)
                else:
                    safe_path.unlink(missing_ok=True)
        for path in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
        raise
    return {
        "status": "applied",
        "project_root": str(project),
        "plan_sha256": claimed,
        "written": [str(path.relative_to(project).as_posix()) for path in changed],
        "proof": plan["proof"],
    }


__all__ = ["plan_library_bundle", "apply_library_bundle"]
