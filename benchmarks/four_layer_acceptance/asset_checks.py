"""Read-only local KiCad footprint asset checks for acceptance manifests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from circuit_weaver.footprint_lib import KiCadFootprintLibrary
from circuit_weaver.primitives import qualify_footprint


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _applicable_context(library: KiCadFootprintLibrary, nickname: str) -> bool:
    return bool(library.roots or nickname in library.global_libraries or nickname in library.project_libraries)


def _source_for(
    library: KiCadFootprintLibrary,
    normalized: str,
    resolved_path: Path | None,
) -> dict[str, str] | None:
    nickname = normalized.split(":", 1)[0] if ":" in normalized else ""
    if nickname in library.project_libraries:
        return {
            "kind": "project_library_table",
            "nickname": nickname,
            "library_root": str(library.project_libraries[nickname]),
        }
    if nickname in library.global_libraries:
        return {
            "kind": "global_library_table",
            "nickname": nickname,
            "library_root": str(library.global_libraries[nickname]),
        }
    for root in library.roots:
        candidate = root / f"{nickname}.pretty"
        if resolved_path is not None:
            try:
                if candidate.resolve() == resolved_path.resolve().parent:
                    return {"kind": "local_kicad_root", "root": str(root)}
            except OSError:
                pass
    if library.roots:
        return {"kind": "local_kicad_root", "root": str(library.roots[0])}
    return None


def _candidate_path(library: KiCadFootprintLibrary, normalized: str) -> Path | None:
    if ":" not in normalized:
        return None
    nickname, footprint_name = normalized.split(":", 1)
    if nickname in library.project_libraries:
        return library.project_libraries[nickname] / f"{footprint_name}.kicad_mod"
    if nickname in library.global_libraries:
        return library.global_libraries[nickname] / f"{footprint_name}.kicad_mod"
    if library.roots:
        return library.roots[0] / f"{nickname}.pretty" / f"{footprint_name}.kicad_mod"
    return None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_footprint_assets(
    physical_items: Iterable[Any],
    *,
    roots: Iterable[str | Path] | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve declared footprints from physical assembly items without native tools.

    When ``roots`` is supplied it replaces automatic KiCad install-root
    discovery for deterministic callers; product global and project library
    table behavior remains delegated to :class:`KiCadFootprintLibrary`.
    """
    project = Path(project_root).expanduser().resolve(strict=False) if project_root is not None else None
    library = KiCadFootprintLibrary(project_root=project)
    if roots is not None:
        explicit_roots = []
        for raw in roots:
            candidate = Path(raw).expanduser()
            if candidate.is_dir():
                explicit_roots.append(candidate.resolve())
        library._roots = explicit_roots

    checks: list[dict[str, Any]] = []
    resolution_cache: dict[str, tuple[str, Path | None, str | None]] = {}
    for item in physical_items:
        if bool(_value(item, "dnp", False)):
            continue
        reference = str(_value(item, "reference", _value(item, "ref", "")) or "")
        declared = str(_value(item, "footprint", "") or "").strip()
        row: dict[str, Any] = {
            "check": "footprint_asset_resolution",
            "target": reference or None,
            "reference": reference or None,
            "declared_footprint": declared or None,
            "normalized_footprint": None,
            "status": "unverified",
            "source": None,
            "path": None,
            "sha256": None,
            "exists": False,
        }
        if not declared:
            row["reason"] = "physical manifest item has no declared footprint"
            checks.append(row)
            continue

        normalized = qualify_footprint(declared)
        row["normalized_footprint"] = normalized
        nickname = normalized.split(":", 1)[0] if ":" in normalized else ""
        applicable = _applicable_context(library, nickname)
        project_table_present = bool(project and (project / "fp-lib-table").is_file())
        if not applicable and not project_table_present:
            row["reason"] = "no local KiCad footprint roots or matching library-table context are available"
            checks.append(row)
            continue

        if normalized not in resolution_cache:
            try:
                resolved = library.resolve(normalized)
                if resolved is None:
                    if applicable:
                        resolution_cache[normalized] = ("failed", _candidate_path(library, normalized), None)
                    else:
                        resolution_cache[normalized] = (
                            "unverified",
                            None,
                            "project footprint table has no matching nickname and no local roots are available",
                        )
                else:
                    path = Path(resolved).resolve(strict=False)
                    if not path.is_file():
                        resolution_cache[normalized] = ("failed", path, None)
                    else:
                        try:
                            sha256 = _hash_file(path)
                        except OSError:
                            resolution_cache[normalized] = ("unverified", path, None)
                        else:
                            resolution_cache[normalized] = ("passed", path, sha256)
            except (OSError, RuntimeError) as exc:
                resolution_cache[normalized] = ("unverified", None, f"{type(exc).__name__}: {exc}")
            except ValueError as exc:
                # The product resolver rejects unsafe/malformed explicit
                # library context instead of falling back to another nickname.
                resolution_cache[normalized] = ("failed", _candidate_path(library, normalized), str(exc))

        status, path, detail = resolution_cache[normalized]
        row["status"] = status
        row["source"] = _source_for(library, normalized, path)
        if path is not None:
            row["path"] = str(path)
            row["exists"] = path.is_file()
            row["sha256"] = detail if status == "passed" else None
        if status == "failed":
            row["reason"] = detail or "declared qualified footprint file was not found in local libraries"
        elif status == "unverified":
            row["reason"] = detail or "local footprint could not be read for hashing"
        checks.append(row)

    statuses = {str(row.get("status")) for row in checks}
    if "failed" in statuses:
        overall = "failed"
    elif not checks or "unverified" in statuses:
        overall = "unverified"
    else:
        overall = "passed"
    return {
        "status": overall,
        "scope": "local KiCad footprint file existence and SHA-256 only",
        "roots": [str(path) for path in library.roots],
        "project_root": str(project) if project is not None else None,
        "checks": checks,
        "claims": ["No electrical, symbol-library, native-model, or fabrication qualification is made."],
    }
