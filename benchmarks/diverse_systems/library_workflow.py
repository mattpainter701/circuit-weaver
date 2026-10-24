#!/usr/bin/env python3
"""Exercise local symbol/footprint bundle staging and project-library lookup.

This benchmark consumes user-selected local KiCad sources only. It records
their hashes and user-asserted provenance/license statements, applies and
replays Circuit Weaver's library bundle plan, then resolves the staged symbol
and real footprint geometry through the project-aware library APIs. It does
not download libraries, create models, run KiCad native validation, or claim
electrical correctness or footprint fit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from circuit_weaver.footprint_lib import KiCadFootprintLibrary  # noqa: E402
from circuit_weaver.kicad_lib import KiCadLibrary  # noqa: E402
from circuit_weaver.library_bundle import apply_library_bundle, plan_library_bundle  # noqa: E402
from circuit_weaver.project_state import file_sha256  # noqa: E402


def _source_files(path: Path) -> list[dict[str, str]]:
    if path.is_file():
        return [{"path": path.name, "sha256": file_sha256(path)}]
    rows = []
    for item in sorted(path.rglob("*"), key=lambda item: (
        item.relative_to(path).as_posix().casefold(), item.relative_to(path).as_posix()
    )):
        if item.is_symlink():
            raise ValueError(f"Symlinks are not supported in local library sources: {item}")
        if item.is_file():
            rows.append({"path": item.relative_to(path).as_posix(), "sha256": file_sha256(item)})
    if not rows:
        raise ValueError(f"Local footprint library contains no files: {path}")
    return rows


def _resolve_source(value: str | Path, *, suffix: str, is_directory: bool) -> Path:
    selected = Path(value).expanduser()
    if selected.is_symlink() or any(parent.is_symlink() for parent in selected.absolute().parents):
        raise ValueError(f"Symlink library sources are not supported: {selected}")
    source = selected.resolve(strict=True)
    if source.suffix.casefold() != suffix.casefold():
        raise ValueError(f"Expected a local {suffix} source: {source}")
    if is_directory != source.is_dir() or (not is_directory and not source.is_file()):
        raise ValueError(f"Local library source has the wrong file type: {source}")
    return source


def _resolve_footprint_source(value: str | Path, footprint_name: str) -> tuple[Path, str, Path]:
    selected = Path(value).expanduser()
    if selected.is_symlink() or any(parent.is_symlink() for parent in selected.absolute().parents):
        raise ValueError(f"Symlink library sources are not supported: {selected}")
    source = selected.resolve(strict=True)
    if source.is_dir() and source.suffix.casefold() == ".pretty":
        return source, source.stem, source
    if source.is_file() and source.suffix.casefold() == ".kicad_mod":
        if source.parent.suffix.casefold() != ".pretty" or source.stem != footprint_name:
            raise ValueError("A selected .kicad_mod must be inside a .pretty directory and match footprint_name")
        return source, source.parent.stem, source.parent
    raise ValueError("Footprint source must be a local .pretty directory or a selected .kicad_mod inside one")


def _assert_disjoint(output: Path, sources: list[Path]) -> None:
    for source in sources:
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("New output directory must be separate from each selected source")


def run_library_workflow(
    *,
    symbol_source: str | Path,
    symbol_name: str,
    footprint_source: str | Path,
    footprint_name: str,
    output: str | Path,
    symbol_provenance: str,
    symbol_license: str,
    footprint_provenance: str,
    footprint_license: str,
) -> dict[str, Any]:
    """Stage local CAD sources, replay the plan, and resolve actual assets.

    Provenance and license strings are retained as user assertions. ``output``
    must be a new directory whose parent already exists; source libraries are
    only read and copied. The returned report is also written into that project.
    """
    symbol_path = _resolve_source(symbol_source, suffix=".kicad_sym", is_directory=False)
    footprint_path, footprint_nickname, footprint_scope = _resolve_footprint_source(
        footprint_source, footprint_name
    )
    for label, value in (("symbol_name", symbol_name), ("footprint_name", footprint_name),
                         ("symbol_provenance", symbol_provenance), ("symbol_license", symbol_license),
                         ("footprint_provenance", footprint_provenance), ("footprint_license", footprint_license)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be nonempty text")

    output_path = Path(output).expanduser().absolute()
    if not output_path.parent.is_dir():
        raise ValueError("Output parent directory must already exist")
    if output_path.exists() or output_path.is_symlink():
        raise ValueError("Output path must be a new, absent directory")
    output_path = output_path.resolve(strict=False)
    _assert_disjoint(output_path, [symbol_path, footprint_scope])

    symbol_nickname = symbol_path.stem
    source_records = {
        "symbol_library": _source_files(symbol_path),
        "footprint_library": _source_files(footprint_path),
    }
    output_path.mkdir(exist_ok=False)
    selected_sources = output_path / "selected-sources"
    symbol_snapshot = selected_sources / "symbols" / symbol_path.name
    footprint_snapshot = selected_sources / "footprints" / f"{footprint_nickname}.pretty"
    footprint_snapshot.parent.mkdir(parents=True, exist_ok=True)
    if footprint_path.is_dir():
        shutil.copytree(footprint_path, footprint_snapshot)
    else:
        footprint_snapshot.mkdir(parents=True)
        shutil.copy2(footprint_path, footprint_snapshot / footprint_path.name)
    symbol_snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(symbol_path, symbol_snapshot)
    bundle_footprint_path = footprint_snapshot
    manifest = {
        "version": 1,
        "assets": [
            {"kind": "symbol_library", "source": str(symbol_snapshot), "nickname": symbol_nickname,
             "provenance": symbol_provenance, "license": symbol_license},
            {"kind": "footprint_library", "source": str(bundle_footprint_path), "nickname": footprint_nickname,
             "provenance": footprint_provenance, "license": footprint_license},
        ],
    }
    (output_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    plan = plan_library_bundle(output_path, manifest)
    (output_path / "plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    applied = apply_library_bundle(output_path, plan)
    replay = apply_library_bundle(output_path, plan)
    if applied.get("status") != "applied" or replay.get("status") != "already_applied":
        raise RuntimeError("Library bundle did not complete its apply and replay lifecycle")

    staged_symbol = output_path / "libraries" / "symbols" / f"{symbol_nickname}.kicad_sym"
    staged_footprints = output_path / "libraries" / "footprints" / f"{footprint_nickname}.pretty"
    symbol_library = KiCadLibrary(roots=[], project_root=output_path)
    symbol_data = symbol_library.get_symbol_data(symbol_name, lib_name=symbol_nickname)
    if not isinstance(symbol_data, dict):
        raise ValueError(f"Project-local symbol did not resolve: {symbol_nickname}:{symbol_name}")

    footprint_library = KiCadFootprintLibrary(root=[], project_root=output_path)
    footprint_reference = f"{footprint_nickname}:{footprint_name}"
    resolved_footprint = footprint_library.resolve(footprint_reference)
    expected_footprint = staged_footprints / f"{footprint_name}.kicad_mod"
    if resolved_footprint is None or resolved_footprint.resolve() != expected_footprint.resolve():
        raise ValueError(f"Project-local footprint did not resolve to its staged source: {footprint_reference}")
    geometry = footprint_library.geometry(footprint_reference)
    if geometry.source not in {"courtyard", "pads"} or geometry.confidence != "verified":
        raise ValueError("Resolved footprint did not provide measurable library geometry")

    staged_hashes = {
        "symbol_library": _source_files(staged_symbol),
        "footprint_library": _source_files(staged_footprints),
    }
    if staged_hashes != source_records:
        raise ValueError("Staged library hashes differ from the selected local sources")
    if (_source_files(symbol_path) != source_records["symbol_library"]
            or _source_files(footprint_path) != source_records["footprint_library"]):
        raise ValueError("Selected local library sources changed during the workflow")

    native_output = output_path / "native-validation" / "symbol-svg"
    native_symbol_command = [
        "kicad-cli", "sym", "export", "svg", "--symbol", symbol_name,
        "--output", str(native_output), str(staged_symbol),
    ]
    native_footprint_code = (
        "import pcbnew,sys; fp=pcbnew.FootprintLoad(sys.argv[1],sys.argv[2]); "
        "raise SystemExit(0 if fp is not None else 1)"
    )
    return {
        "schema_version": 1,
        "status": "resolved_local_assets",
        "project_root": str(output_path),
        "manifest_path": str(output_path / "manifest.json"),
        "plan_path": str(output_path / "plan.json"),
        "selected_sources": str(selected_sources),
        "lifecycle": {
            "plan_status": plan["status"], "plan_sha256": plan["plan_sha256"],
            "apply_status": applied["status"], "replay_status": replay["status"],
            "replay_receipt": replay.get("receipt"),
        },
        "assets": {
            "symbol": {
                "nickname": symbol_nickname, "name": symbol_name,
                "source": str(symbol_path), "destination": str(staged_symbol),
                "source_sha256": source_records["symbol_library"][0]["sha256"],
                "staged_sha256": staged_hashes["symbol_library"][0]["sha256"],
                "provenance_user_assertion": symbol_provenance.strip(),
                "license_user_assertion": symbol_license.strip(),
                "resolved": True, "pin_count_from_library_parser": len(symbol_data.get("pins", [])),
                "property_names_from_library_parser": sorted(symbol_data.get("properties", {})),
            },
            "footprint": {
                "nickname": footprint_nickname, "name": footprint_name,
                "reference": footprint_reference,
                "source": str(footprint_path), "destination": str(staged_footprints),
                "source_files": source_records["footprint_library"],
                "staged_files": staged_hashes["footprint_library"],
                "provenance_user_assertion": footprint_provenance.strip(),
                "license_user_assertion": footprint_license.strip(),
                "resolved": True,
                "geometry": {
                    "width_mm": geometry.width_mm, "height_mm": geometry.height_mm,
                    "center_x_mm": geometry.center_x_mm, "center_y_mm": geometry.center_y_mm,
                    "source": geometry.source, "confidence": geometry.confidence,
                    "content_sha256": geometry.content_hash,
                },
            },
        },
        "native_validation": {
            "status": "not_run",
            "commands_for_later": [
                {"purpose": "KiCad CLI symbol SVG export", "argv": native_symbol_command,
                 "output_directory_must_be_created": True},
                {"purpose": "KiCad pcbnew footprint load", "argv": [
                    "<kicad-python>", "-c", native_footprint_code,
                    str(staged_footprints), footprint_name,
                ]},
            ],
        },
        "claims": {
            "source_assets_were_copied_without_editing": True,
            "provenance_and_license_are_user_assertions": True,
            "resolution_uses_project_aware_kicad_apis": True,
            "native_kicad_validation_was_run": False,
            "symbol_pin_semantics_verified": False,
            "footprint_fit_verified": False,
            "models_or_functional_templates_created": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol-source", required=True, help="Local .kicad_sym file")
    parser.add_argument("--symbol-name", required=True, help="Exact symbol name inside the selected library")
    parser.add_argument("--symbol-provenance", required=True, help="User assertion describing symbol source")
    parser.add_argument("--symbol-license", required=True, help="User assertion for symbol license")
    parser.add_argument(
        "--footprint-source", required=True,
        help="Local .pretty directory, or one .kicad_mod inside a .pretty directory",
    )
    parser.add_argument("--footprint-name", required=True, help="Exact .kicad_mod basename without extension")
    parser.add_argument("--footprint-provenance", required=True, help="User assertion describing footprint source")
    parser.add_argument("--footprint-license", required=True, help="User assertion for footprint license")
    parser.add_argument("--output", required=True, help="New project output directory; it must not exist")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_library_workflow(
            symbol_source=args.symbol_source, symbol_name=args.symbol_name,
            symbol_provenance=args.symbol_provenance, symbol_license=args.symbol_license,
            footprint_source=args.footprint_source, footprint_name=args.footprint_name,
            footprint_provenance=args.footprint_provenance, footprint_license=args.footprint_license,
            output=args.output,
        )
        output_path = Path(report["project_root"])
        report_path = output_path / "library-workflow-report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "report": str(report_path)}, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
