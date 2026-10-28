"""Read-only inventory and search for local KiCad library access.

KiCad drawing assets, Circuit Weaver component definitions/builders, and
simulation model bindings are separate capabilities.  This module reports
each independently and never downloads, rewrites, or executes library files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .footprint_lib import KiCadFootprintLibrary, _iter_blocks, _project_library_uri
from .kicad_lib import KiCadLibrary, _project_symbol_path
from .pcb_model_preview import (
    _MODEL_RE,
    _decode_sexpr_string,
    _find_kicad_cli,
    _model_roots,
    _resolve_model_path,
    _sha256,
)

_SYMBOL_NAME_RE = re.compile(r'\(symbol\s+"((?:\\.|[^"\\])*)"')
_MODEL_SUFFIXES = {".step", ".stp", ".wrl", ".wrz", ".iges", ".igs", ".obj", ".stl", ".ply"}
_MAX_MATCHES = 25


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        key = str(resolved).casefold()
        if resolved.is_dir() and key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def _path_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve()).casefold()
    except (OSError, RuntimeError):
        return str(path.expanduser().absolute()).casefold()


def _root_records(
    paths: Iterable[Path], explicit: Iterable[str | Path], environment_names: Iterable[str]
) -> list[dict[str, str]]:
    explicit_keys = {_path_key(Path(path)) for path in explicit}
    environment_paths = {_path_key(Path(os.environ[name])): name for name in environment_names if os.environ.get(name)}
    records = []
    for path in paths:
        key = _path_key(path)
        if key in explicit_keys:
            source = "explicit_argument"
        elif key in environment_paths:
            source = f"environment:{environment_paths[key]}"
        else:
            source = "platform_default"
        records.append({"path": str(path), "source": source})
    return records


def _library_name_conflicts(assets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, str]] = {}
    for asset in assets:
        by_name.setdefault(str(asset["library"]), {})[_path_key(Path(asset["path"]))] = str(asset["path"])
    return [
        {"library": name, "paths": sorted(paths.values())} for name, paths in sorted(by_name.items()) if len(paths) > 1
    ]


def _symbol_assets(
    library: KiCadLibrary,
    project_root: Path | None,
    root_sources: dict[str, str],
) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(library_name: str, path: Path, source: str) -> None:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return
        key = str(resolved).casefold()
        if key in seen or not resolved.exists():
            return
        seen.add(key)
        assets.append({"library": library_name, "path": str(resolved), "source": source})

    if project_root is not None:
        for name in library._iter_project_library_names():
            try:
                path = _project_symbol_path(project_root, name)
            except (OSError, RuntimeError, ValueError):
                continue
            if path is not None:
                add(name, path, "project_table")

    for name, path in library._sym_files.items():
        add(name, path, "explicit_path")
    for name, path in library._sym_dirs.items():
        add(name, path, "explicit_path")
    for name, path in library.global_libraries.items():
        add(name, path, "global_table")

    for root in library.roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.suffix == ".kicad_sym":
                add(entry.stem, entry, root_sources.get(_path_key(root), "local_root"))
            elif entry.is_dir() and entry.suffix == ".kicad_symdir":
                add(entry.stem, entry, root_sources.get(_path_key(root), "local_root"))
    return assets


def _search_symbols(assets: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, str]]:
    needle = query.casefold()
    matches: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for asset in assets:
        path = Path(asset["path"])
        if path.is_dir():
            try:
                symbol_files = sorted(path.glob("*.kicad_sym"))
            except OSError:
                continue
            for symbol_file in symbol_files:
                name = symbol_file.stem
                if needle in name.casefold():
                    key = (asset["library"], name)
                    if key not in seen:
                        seen.add(key)
                        matches.append(
                            {
                                "library": asset["library"],
                                "name": name,
                                "path": str(symbol_file.resolve()),
                                "source": asset["source"],
                            }
                        )
                        if len(matches) >= limit:
                            return matches
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle not in content.casefold():
            continue
        for raw_name in _SYMBOL_NAME_RE.findall(content):
            name = raw_name.replace(r"\"", '"').replace(r"\\", "\\")
            if needle not in name.casefold():
                continue
            key = (asset["library"], name)
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                {
                    "library": asset["library"],
                    "name": name,
                    "path": str(path),
                    "source": asset["source"],
                }
            )
            if len(matches) >= limit:
                return matches
    return matches


def _default_model_roots() -> list[Path]:
    roots: list[Path] = []
    for name in ("KICAD10_3DMODEL_DIR", "KICAD9_3DMODEL_DIR", "KICAD8_3DMODEL_DIR", "KISYS3DMOD"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    for version in ("10.0", "9.0", "8.0"):
        if os.name == "nt":
            roots.append(Path(f"C:/Program Files/KiCad/{version}/share/kicad/3dmodels"))
    if os.name != "nt":
        roots.extend((Path("/usr/share/kicad/3dmodels"), Path("/usr/local/share/kicad/3dmodels")))
        roots.append(Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"))
    return _dedupe_paths(roots)


def _search_models(roots: list[Path], query: str, limit: int) -> list[dict[str, str]]:
    needle = query.casefold()
    matches: list[dict[str, str]] = []
    for root in roots:
        try:
            files = root.rglob("*")
            for path in files:
                if path.is_file() and path.suffix.casefold() in _MODEL_SUFFIXES and needle in path.name.casefold():
                    matches.append({"name": path.name, "path": str(path), "root": str(root)})
                    if len(matches) >= limit:
                        return matches
        except OSError:
            continue
    return matches


def _resolved_symbol_assets(
    symbols: list[dict[str, str]],
    query: str,
    library: KiCadLibrary,
    footprints: KiCadFootprintLibrary,
    model_roots: dict[str, list[Path]],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Resolve assets for a few exact symbol-name matches, without vetting fit."""
    resolved_assets: list[dict[str, Any]] = []
    for symbol in symbols:
        if symbol["name"].casefold() != query.casefold():
            continue
        if len(resolved_assets) >= limit:
            break
        try:
            component = library.get_component(symbol["name"], lib_name=symbol["library"])
            component_error = None
        except (OSError, RuntimeError, ValueError) as exc:
            component = None
            component_error = type(exc).__name__
        footprint_ref = getattr(component, "footprint", None) if component else None
        item: dict[str, Any] = {
            "symbol": symbol["name"],
            "library": symbol["library"],
            "symbol_path": symbol["path"],
            "footprint_reference": footprint_ref,
            "footprint": {"available": False, "path": None, "sha256": None, "pad_count": None},
            "models": [],
            "note": "asset resolution only; does not verify pinout, package fit, or simulation usability",
        }
        if not isinstance(footprint_ref, str) or not footprint_ref:
            item["footprint"]["reason"] = (
                f"symbol parser failed: {component_error}"
                if component_error
                else "symbol has no parsed footprint binding"
            )
            resolved_assets.append(item)
            continue
        try:
            footprint_path = footprints.resolve(footprint_ref)
            if footprint_path is None:
                item["footprint"]["reason"] = "footprint reference is not locally resolvable"
                resolved_assets.append(item)
                continue
            text = footprint_path.read_text(encoding="utf-8")
            item["footprint"] = {
                "available": True,
                "path": str(footprint_path.resolve()),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "pad_count": sum(1 for _ in _iter_blocks(text, ("pad",))),
            }
            for match in _MODEL_RE.finditer(text):
                raw_reference = _decode_sexpr_string(match.group(1))
                model_path = _resolve_model_path(raw_reference, footprint_path.parent, model_roots)
                model_record: dict[str, Any] = {
                    "reference": raw_reference,
                    "available": model_path is not None,
                    "path": str(model_path) if model_path else None,
                    "sha256": _sha256(model_path) if model_path else None,
                }
                item["models"].append(model_record)
        except (OSError, RuntimeError, ValueError) as exc:
            item["footprint"]["reason"] = f"asset could not be read: {type(exc).__name__}"
        resolved_assets.append(item)
    return resolved_assets


def _spice_binding(query: str, spice_model_dir: Path | None) -> dict[str, Any]:
    if spice_model_dir is None:
        return {
            "available": False,
            "file_available": False,
            "explicit_pin_contract_available": False,
            "reason": "no project SPICE model directory was supplied",
        }
    manifest_path = spice_model_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "available": False,
            "file_available": False,
            "explicit_pin_contract_available": False,
            "reason": "project SPICE manifest is absent or unreadable",
        }
    entry = (
        next(
            (value for mpn, value in manifest.items() if str(mpn).casefold() == query.casefold()),
            None,
        )
        if isinstance(manifest, dict)
        else None
    )
    if not isinstance(entry, dict) or entry.get("spice_status") != "ok":
        return {
            "available": False,
            "file_available": False,
            "explicit_pin_contract_available": False,
            "manifest": str(manifest_path),
            "reason": "no successful model entry for query",
        }
    filename = entry.get("spice_file")
    if not isinstance(filename, str) or not filename.strip():
        return {
            "available": False,
            "file_available": False,
            "explicit_pin_contract_available": False,
            "manifest": str(manifest_path),
            "reason": "unsafe model filename in manifest",
        }
    model_root = spice_model_dir.resolve()
    try:
        requested_path = Path(filename).expanduser()
        model_path = (requested_path if requested_path.is_absolute() else model_root / requested_path).resolve()
        contained = model_path.is_relative_to(model_root)
        file_available = contained and model_path.is_file()
    except (OSError, RuntimeError, ValueError):
        file_available = False
        model_path = None
    if not file_available or model_path is None:
        return {
            "available": False,
            "file_available": False,
            "explicit_pin_contract_available": False,
            "manifest": str(manifest_path),
            "reason": "model file is missing or outside model directory",
        }
    from .spice_fetcher import resolve_spice_models

    resolved = resolve_spice_models(manifest_path)
    contract = next(
        (value for mpn, value in resolved.items() if str(mpn).casefold() == query.casefold()),
        None,
    )
    if not isinstance(contract, dict):
        return {
            "available": False,
            "file_available": True,
            "explicit_pin_contract_available": False,
            "path": str(model_path),
            "manifest": str(manifest_path),
            "reason": "model file is present but lacks a validated explicit subckt/pin_order contract",
        }
    return {
        "available": True,
        "file_available": True,
        "explicit_pin_contract_available": True,
        "contract_validation": "structural only; does not verify datasheet pin functions or electrical behavior",
        "path": contract["path"],
        "subckt": contract["subckt"],
        "pin_order": contract["pin_order"],
        "manifest": str(manifest_path),
    }


def _curated_component_support(query: str) -> dict[str, Any]:
    from .component_db import BUILTIN_REGISTRY
    from .ic_data import get_all_ics
    from .subcircuits.base import get_default_registry

    builtins = {mpn.casefold(): mpn for mpn in BUILTIN_REGISTRY.all_mpns()}
    ic_records = get_all_ics()
    ic_data = {mpn.casefold(): (mpn, record) for mpn, record in ic_records.items()}
    key = query.casefold()
    sources: list[str] = []
    matched_mpn = None
    record: dict[str, Any] = {}
    builtin_mpn = builtins.get(key)
    if builtin_mpn:
        sources.append("builtin_component_registry")
        matched_mpn = builtin_mpn
        component = BUILTIN_REGISTRY.get(builtin_mpn)
        if component:
            record = {"category": component.category, "footprint": component.footprint}
    ic_match = ic_data.get(key)
    if ic_match:
        matched_mpn = matched_mpn or ic_match[0]
        sources.append("ic_data")
        record.setdefault("topology", ic_match[1].get("topology"))
    topology = record.get("topology")
    template = get_default_registry().get(topology) if topology else None
    normalized_query = re.sub(r"[^a-z0-9]", "", key)
    candidate_mpns: list[str] = []
    if len(normalized_query) >= 5 and not sources:
        candidates = {
            mpn
            for mpn in (*builtins.values(), *(item[0] for item in ic_data.values()))
            if re.sub(r"[^a-z0-9]", "", mpn.casefold()).startswith(normalized_query)
        }
        candidate_mpns = sorted(candidates, key=str.casefold)[:5]
    return {
        "available": bool(sources),
        "mpn": matched_mpn,
        "sources": sources,
        "candidate_mpns": candidate_mpns,
        "record_summary": {key: value for key, value in record.items() if value},
        "functional_template_support": {
            "available": template is not None,
            "topology": topology,
            "template_type": template.template_type if template is not None else None,
        },
    }


def audit_library_access(
    query: str | None = "",
    *,
    project_root: str | Path | None = None,
    symbol_roots: Iterable[str | Path] | None = None,
    footprint_roots: Iterable[str | Path] | None = None,
    model_roots: Iterable[str | Path] | None = None,
    spice_model_dir: str | Path | None = None,
    match_limit: int = _MAX_MATCHES,
) -> dict[str, Any]:
    """Inspect local library access and independent curated/model support.

    ``query`` is a symbol name, footprint name/library reference, or MPN.
    Search results are intentionally bounded and source-labelled. No network,
    KiCad executable, or file writes are used.
    """
    query = str(query or "").strip()
    limit = max(1, min(int(match_limit), 100))
    project = Path(project_root).expanduser().resolve() if project_root else None
    library = KiCadLibrary(roots=list(symbol_roots or []), project_root=project)
    symbol_root_records = _root_records(
        library.roots,
        symbol_roots or [],
        ("KICAD_SYMBOL_DIR", "KICAD10_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR"),
    )
    symbol_root_sources = {_path_key(Path(item["path"])): item["source"] for item in symbol_root_records}
    symbol_assets = _symbol_assets(library, project, symbol_root_sources)

    footprint_library = KiCadFootprintLibrary(root=footprint_roots, project_root=project)
    footprint_root_records = _root_records(
        footprint_library.roots,
        footprint_roots or [],
        ("KICAD_FOOTPRINT_DIR", "KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR"),
    )
    footprint_assets: list[dict[str, str]] = []
    for root_record in footprint_root_records:
        root = Path(root_record["path"])
        for path in root.glob("*.pretty"):
            footprint_assets.append({"library": path.stem, "path": str(path)})
    for libraries in (footprint_library.project_libraries, footprint_library.global_libraries):
        footprint_assets.extend({"library": name, "path": str(path)} for name, path in libraries.items())

    selected_models = _dedupe_paths(
        [Path(path) for path in model_roots] if model_roots is not None else _default_model_roots()
    )
    model_root_records = _root_records(
        selected_models,
        model_roots or [],
        ("KICAD10_3DMODEL_DIR", "KICAD9_3DMODEL_DIR", "KICAD8_3DMODEL_DIR", "KISYS3DMOD"),
    )
    cli_path = _find_kicad_cli(None)
    cli_version = (
        re.search(r"(?:^|[/\\])(\d+)\.0[/\\]bin[/\\]kicad-cli", str(cli_path), re.IGNORECASE) if cli_path else None
    )
    resolution_model_roots = _model_roots(cli_path, int(cli_version.group(1))) if cli_path and cli_version else {}
    symbols = _search_symbols(symbol_assets, query, limit) if query else []
    footprints: list[dict[str, str]] = []
    if query:
        for reference in footprint_library.find(query, limit=limit):
            resolved = footprint_library.resolve(reference)
            if resolved is None:
                continue
            nickname = reference.partition(":")[0]
            source = "global_table" if nickname in footprint_library.global_libraries else "local_root"
            if project is not None:
                try:
                    project_library = _project_library_uri(project, nickname)
                except (OSError, RuntimeError, ValueError):
                    project_library = None
                if project_library is not None and resolved.is_relative_to(project_library.resolve()):
                    source = "project_table"
            item = {"reference": reference, "path": str(resolved), "source": source}
            if item not in footprints:
                footprints.append(item)
            if len(footprints) >= limit:
                break

    spice_dir = (
        Path(spice_model_dir).expanduser().resolve()
        if spice_model_dir
        else (project / "spice_models" if project else None)
    )
    report: dict[str, Any] = {
        "query": query,
        "read_only": True,
        "discovery": {
            "symbol_roots": symbol_root_records,
            "symbol_global_libraries": [
                {"nickname": name, "path": str(path)} for name, path in sorted(library.global_libraries.items())
            ],
            "footprint_roots": footprint_root_records,
            "footprint_global_libraries": [
                {"nickname": name, "path": str(path)}
                for name, path in sorted(footprint_library.global_libraries.items())
            ],
            "footprint_project_libraries": [
                {"nickname": name, "path": str(path)}
                for name, path in sorted(footprint_library.project_libraries.items())
            ],
            "model_roots": model_root_records,
            "root_notes": [
                "KiCad global/private table and environment roots are read-only; "
                "unsupported variables or missing paths are omitted.",
                "Absent versioned KiCad environment paths are optional; fixed platform defaults are also searched.",
                "File presence does not verify electrical correctness or simulation usability.",
            ],
        },
        "inventory": {
            "symbol_library_count": len(symbol_assets),
            "footprint_library_count": len(
                {_path_key(path) for root in footprint_library.roots for path in root.glob("*.pretty")}
                | {_path_key(path) for path in footprint_library.global_libraries.values()}
                | {_path_key(path) for path in footprint_library.project_libraries.values()}
            ),
            "model_root_count": len(selected_models),
        },
        "matches": {
            "symbols": symbols,
            "footprints": footprints,
            "models_3d": _search_models(selected_models, query, limit) if query else [],
        },
        "resolved_symbol_assets": (
            _resolved_symbol_assets(symbols, query, library, footprint_library, resolution_model_roots)
            if query and symbols
            else []
        ),
        "conflicts": {
            "symbol_library_name_collisions": _library_name_conflicts(symbol_assets),
            "footprint_nickname_collisions": _library_name_conflicts(footprint_assets),
        },
        "curated_support": (_curated_component_support(query) if query else {"available": False, "reason": "no query"}),
        "spice_model_binding": (
            _spice_binding(query, spice_dir)
            if query
            else {
                "available": False,
                "file_available": False,
                "explicit_pin_contract_available": False,
                "reason": "no query",
            }
        ),
        "limits": {"match_limit_per_kind": limit},
    }
    return report
