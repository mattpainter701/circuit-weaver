"""Validate and package evidence for a silicon shuttle review.

This is an artifact-completeness check only. It does not perform foundry sign-off,
submit a design, or establish provider acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .silicon_reports import inspect_silicon_report

_REQUIRED_ROLES = {
    "layout", "pin_power_map", "drc", "lvs", "timing",
    "provider_precheck", "bringup_plan",
}
_VERIFY_ROLES = {"drc", "lvs", "timing", "provider_precheck"}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def _artifact_path(raw: Any, role: str, root: Path) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"role {role!r} must have a non-empty relative path")
    # Archive names are portable POSIX paths, even when the caller runs on Windows.
    name = raw.replace("\\", "/")
    posix = PurePosixPath(name)
    parts = name.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if ("\\" in raw or posix.is_absolute() or ":" in name or
            any(p in ("", ".", "..") or p.endswith((".", " ")) or p.split(".")[0].upper() in reserved for p in parts)):
        raise ValueError(f"unsafe artifact path for role {role!r}: {raw!r}")
    path = root.joinpath(*posix.parts)
    # Resolve and reject symlinks in the path chain (including a symlinked file).
    cursor = root
    for part in posix.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"symlink artifact path is not allowed: {raw!r}")
    resolved = path.resolve(strict=True)
    root = root.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"artifact path must resolve to a file under the manifest directory: {raw!r}")
    return posix.as_posix(), resolved


def _has_physical_geometry(layout: Any, root: Any) -> bool:
    """Inspect each cell once, stopping at the first non-label physical shape."""
    pending = [root]
    visited: set[int] = set()
    while pending:
        cell = pending.pop()
        index = cell.cell_index()
        if index in visited:
            continue
        visited.add(index)
        for layer_index in layout.layer_indexes():
            for shape in cell.shapes(layer_index).each():
                if shape.is_box() or shape.is_polygon() or shape.is_path():
                    box = shape.bbox()
                    if box.width() > 0 and box.height() > 0:
                        return True
        for instance in cell.each_inst():
            child = layout.cell(instance.cell_index)
            if child is not None:
                pending.append(child)
    return False


def _stream_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def review_silicon_handoff(
    manifest: str | os.PathLike[str], *, bundle: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Review a schema-1 handoff manifest and optionally write a deterministic ZIP.

    The manifest is a JSON file; artifact paths are relative to its directory. Every artifact
    includes its SHA-256. A complete review may be packaged; incomplete reviews
    never create or modify the requested bundle.
    """
    unresolved_manifest = Path(manifest).expanduser().absolute()
    if unresolved_manifest.is_symlink():
        raise ValueError("manifest must be a regular JSON file, not a symlink")
    manifest_path = unresolved_manifest.resolve(strict=True)
    if not manifest_path.is_file():
        raise ValueError("manifest must be a regular JSON file, not a symlink")
    manifest_dir = manifest_path.parent
    try:
        manifest_obj = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest is not valid UTF-8 JSON: {exc}") from exc
    manifest = manifest_obj
    if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest.get("schema") != 1:
        raise ValueError("manifest schema must be integer 1")
    if manifest.get("provider") is not None and not isinstance(manifest.get("provider"), str):
        raise ValueError("manifest provider must be a string when selected")
    if manifest.get("run") is not None and not isinstance(manifest.get("run"), str):
        raise ValueError("manifest run must be a string when selected")
    if manifest.get("declared_top") is not None and not isinstance(manifest.get("declared_top"), str):
        raise ValueError("manifest declared_top must be a string when selected")
    provider = (manifest.get("provider") or "").strip()
    run = (manifest.get("run") or "").strip()
    declared_top = (manifest.get("declared_top") or "").strip()

    roles = manifest.get("roles")
    if not isinstance(roles, list):
        raise ValueError("manifest roles must be a list")
    blockers: list[str] = []
    role_items: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    role_hashes: dict[str, str] = {}
    files: list[tuple[str, Path, str]] = []
    unavailable: set[str] = set()
    for item in roles:
        if not isinstance(item, dict) or not isinstance(item.get("role"), str):
            raise ValueError("each role entry must be an object with a role name")
        role = item["role"]
        if role in role_items:
            raise ValueError(f"duplicate role: {role}")
        role_items[role] = item
        raw_path = item.get("path")
        if isinstance(raw_path, str):
            folded = raw_path.casefold()
            if folded in paths:
                raise ValueError(f"duplicate or case-colliding artifact paths: {paths[folded]!r} and {raw_path!r}")
            paths[folded] = raw_path
        try:
            name, path = _artifact_path(raw_path, role, manifest_dir)
        except FileNotFoundError:
            blockers.append(f"role {role}: artifact file is missing: {item.get('path')!r}")
            unavailable.add(role)
            continue
        expected = item.get("sha256")
        if not isinstance(expected, str) or not _SHA.fullmatch(expected):
            blockers.append(f"role {role}: missing or invalid SHA-256")
            expected = ""
        actual = _stream_digest(path)
        role_hashes[role] = actual
        if expected and actual != expected:
            blockers.append(f"role {role}: SHA-256 mismatch (expected {expected}, got {actual})")
        if not path.stat().st_size:
            blockers.append(f"role {role}: artifact is empty")
        elif role in _VERIFY_ROLES:
            try:
                text = path.read_text(encoding="utf-8")
                text = re.sub(r"(?s)<!--.*?-->|/\*.*?\*/", "", text)
                meaningful = [line.strip() for line in text.splitlines()
                              if line.strip() and not line.strip().startswith(("#", "//", ";", "--", "/*", "*"))]
                if not meaningful:
                    blockers.append(f"role {role}: report is empty or contains comments only")
            except UnicodeDecodeError:
                blockers.append(f"role {role}: verification report must be readable UTF-8 text")
        files.append((name, path, actual))

    additional = manifest.get("required_roles", [])
    if not isinstance(additional, list) or any(not isinstance(r, str) or not r for r in additional):
        raise ValueError("required_roles must be a list of non-empty role names")
    required = _REQUIRED_ROLES | set(additional)
    if not provider:
        blockers.append("target provider: select the shuttle provider")
    if not run:
        blockers.append("target run: select the shuttle run")
    if not declared_top:
        blockers.append("target declared_top: select the expected physical GDS top cell")
    if not ({"integration_spec", "package_bond_map"} & role_items.keys()):
        blockers.append("missing role: integration_spec or package_bond_map")
    for role in sorted(required):
        if role not in role_items:
            blockers.append(f"missing role: {role}")

    layout = role_items.get("layout")
    layout_hash = ""
    if layout:
        try:
            layout_path = _artifact_path(layout.get("path"), "layout", manifest_dir)[1]
            layout_hash = _stream_digest(layout_path)
        except (ValueError, OSError):
            pass
    if layout:
        if layout.get("scope") == "core_macro":
            blockers.append(
                "role layout: core_macro is not a shuttle physical top; provide a full top-level layout "
                "or shuttle wrapper"
            )
        elif layout.get("scope") not in {"top_level", "shuttle_wrapper"}:
            blockers.append(
                "role layout: scope must be 'top_level' or 'shuttle_wrapper' for the selected provider profile"
            )
        else:
            try:
                import klayout.db as kdb  # type: ignore[import-not-found]
                physical = kdb.Layout()
                physical.read(str(_artifact_path(layout.get("path"), "layout", manifest_dir)[1]))
                if not declared_top:
                    blockers.append("target declared_top: select the expected physical GDS top cell")
                    cell = None
                else:
                    cell = physical.cell(declared_top)
                if cell is None:
                    if declared_top:
                        blockers.append(f"role layout: declared top {declared_top!r} is absent from GDS")
                elif cell not in physical.top_cells():
                    blockers.append(f"role layout: declared top {declared_top!r} is not a GDS root/top cell")
                else:
                    if not _has_physical_geometry(physical, cell):
                        blockers.append("role layout: declared physical top has no geometry")
            except ImportError:
                blockers.append(
                    "role layout: KLayout Python module is unavailable; cannot verify declared physical top"
                )
            except Exception as exc:
                blockers.append(
                    f"role layout: KLayout could not inspect GDS top/geometry ({type(exc).__name__}: {exc})"
                )
    checks = manifest.get("verification", {})
    if not isinstance(checks, dict):
        raise ValueError("verification must be an object")
    report_modes: dict[str, str] = {}
    for check in sorted(_VERIFY_ROLES):
        record = checks.get(check)
        if not isinstance(record, dict):
            blockers.append(f"verification {check}: missing pass record")
            continue
        if record.get("status") != "pass":
            blockers.append(f"verification {check}: status must be 'pass'")
        if record.get("layout_sha256") != layout_hash or not layout_hash:
            blockers.append(f"verification {check}: must bind to the current layout SHA-256")
        if provider and run and (record.get("provider") != provider or record.get("run") != run):
            blockers.append(f"verification {check}: must bind to this provider and run")
        if record.get("role") != check or check not in role_items:
            blockers.append(f"verification {check}: must reference the {check!r} report role")
        elif record.get("report_sha256") != role_hashes.get(check):
            blockers.append(f"verification {check}: report_sha256 must match the current {check!r} artifact")
        elif check in role_items and check not in unavailable:
            try:
                report_modes[check] = inspect_silicon_report(
                    check, _artifact_path(role_items[check].get("path"), check, manifest_dir)[1], declared_top
                )
            except (ValueError, OSError) as exc:
                blockers.append(f"verification {check}: report inspection failed ({exc})")

    status = "incomplete" if blockers else "ready_for_provider_review"
    result: dict[str, Any] = {
        "status": status,
        "provider": provider or None,
        "run": run or None,
        "declared_top": declared_top or None,
        "blockers": blockers,
        "report_modes": report_modes,
        "scope": "artifact completeness only; no provider acceptance or independent foundry sign-off implied",
    }
    if status != "ready_for_provider_review" or bundle is None:
        return result

    destination = Path(bundle).expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.resolve() in {p for _, p, _ in files} | {manifest_path}:
        raise ValueError("bundle destination would overwrite an input artifact")
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    members = [(name, path, digest) for name, path, digest in files]
    archive_names = [n.casefold() for n, _, _ in members] + ["handoff_manifest.json"]
    if len(archive_names) != len(set(archive_names)):
        raise ValueError("artifact archive path collides with handoff_manifest.json")
    archive_set = set(archive_names)
    for name in archive_set:
        parts = name.split("/")
        if any("/".join(parts[:idx]) in archive_set for idx in range(1, len(parts))):
            raise ValueError("artifact archive paths collide as a file and directory")
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, source, expected_digest in sorted(members, key=lambda item: item[0].casefold()):
                info = zipfile.ZipInfo(name, _ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                digest = hashlib.sha256()
                with source.open("rb") as stream, archive.open(info, "w") as target:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        target.write(chunk)
                if digest.hexdigest() != expected_digest:
                    raise ValueError(f"artifact changed while packaging: {name}")
            info = zipfile.ZipInfo("handoff_manifest.json", _ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, manifest_bytes)
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    bundle_hash = _stream_digest(destination)
    result.update({"bundle": str(destination), "bundle_sha256": bundle_hash, "artifact_count": len(files)})
    return result
