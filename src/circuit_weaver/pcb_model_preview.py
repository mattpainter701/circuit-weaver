"""Optional native KiCad 3D rendering for assembled PCB previews.

The renderer always operates on an isolated copy of the board and its
project/rule sidecars.  That matters because KiCad may write project state
while loading a board, and relative 3D model paths would otherwise change
meaning when the board is staged elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
import zlib
from pathlib import Path
from typing import Any

_MODEL_RE = re.compile(r'\(model\s+"((?:\\.|[^"\\])*)"')
_VARIABLE_RE = re.compile(r"\$\{([^}]+)\}")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHUNK_HEADER_SIZE = 8
_PNG_CRC_SIZE = 4


def _bundled_model(relative_path: str) -> Path | None:
    """Resolve only an exact, hash-verified package model absent upstream.

    This is an inventory of dimensioned native STEP assets, never a generic
    body substitute. User-defined model aliases do not consult this inventory.
    """
    root = Path(__file__).with_name("model_assets")
    registry_path = root / "inventory.json"
    if not registry_path.is_file():
        return None
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(registry, dict) or not isinstance(registry.get("models"), dict):
        return None
    record = registry["models"].get(relative_path)
    if not isinstance(record, dict) or not isinstance(record.get("file"), str):
        return None
    candidate = (root / record["file"]).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        return None
    return candidate if _sha256(candidate) == record.get("sha256") else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_kicad_cli(explicit: str | Path | None) -> Path | None:
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        return candidate.resolve() if candidate.is_file() else None

    discovered = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if discovered:
        return Path(discovered).resolve()

    candidates: list[Path] = []
    if os.name == "nt":
        for version in ("10.0", "9.0", "8.0"):
            candidates.append(Path(f"C:/Program Files/KiCad/{version}/bin/kicad-cli.exe"))
    else:
        candidates.extend((Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli")))
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


def _decode_sexpr_string(value: str) -> str:
    return value.replace(r"\"", '"').replace(r"\\", "\\")


def _encode_sexpr_string(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r'\"')


def _model_roots(cli_path: Path, cli_major: int) -> dict[str, list[Path]]:
    bin_dir = cli_path.parent
    roots_by_variable: dict[str, list[Path]] = {}
    for version in {8, 9, 10, cli_major}:
        variable = f"KICAD{version}_3DMODEL_DIR"
        roots: list[Path] = []
        if version == cli_major:
            roots.extend(
                (
                    bin_dir.parent / "share" / "kicad" / "3dmodels",
                    bin_dir.parent / "share" / "3dmodels",
                    bin_dir.parent.parent / "share" / "kicad" / "3dmodels",
                )
            )
            if os.name != "nt":
                roots.extend(
                    (
                        Path("/usr/share/kicad/3dmodels"),
                        Path("/usr/local/share/kicad/3dmodels"),
                        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"),
                    )
                )
        if os.name == "nt":
            roots.append(Path(f"C:/Program Files/KiCad/{version}.0/share/kicad/3dmodels"))

        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root).casefold()
            if key not in seen:
                seen.add(key)
                unique.append(root)
        roots_by_variable[variable] = unique

    # Legacy KiCad boards used KISYS3DMOD. Bind it to the CLI installation's
    # own library tree; never search unrelated custom roots for this alias.
    roots_by_variable["KISYS3DMOD"] = roots_by_variable.get(f"KICAD{cli_major}_3DMODEL_DIR", [])
    return roots_by_variable


def _resolve_model_path(raw_path: str, board_dir: Path, roots: dict[str, list[Path]]) -> Path | None:
    value = raw_path.replace("\\", "/")
    value = value.replace("${KIPRJMOD}", str(board_dir).replace("\\", "/"))
    while variable := _VARIABLE_RE.search(value):
        name = variable.group(1)
        suffix = value[variable.end() :].lstrip("/")
        configured_root = os.environ.get(name)
        if configured_root is not None:
            if not configured_root:
                return None
            candidate_root = Path(configured_root).expanduser()
            if not candidate_root.is_absolute():
                candidate_root = board_dir / candidate_root
            candidate = candidate_root / suffix
            if not candidate.is_file():
                # KiCad may explicitly export its own standard library root.
                # Treat that exactly like built-in discovery, while keeping
                # incomplete user libraries authoritative.
                is_standard_root = (
                    re.fullmatch(r"KICAD(?:8|9|10)_3DMODEL_DIR|KISYS3DMOD", name) is not None
                    and any(candidate_root.resolve() == root.resolve() for root in roots.get(name, []))
                )
                candidate = _bundled_model(suffix) if is_standard_root else None
                if candidate is None:
                    return None
            value = str(candidate)
            continue

        # Only well-known, versioned KiCad aliases may use built-in roots.
        # Unset user aliases must fail instead of binding to a different model
        # library that happens to contain a file with the same relative name.
        recognized = re.fullmatch(r"KICAD(?:8|9|10)_3DMODEL_DIR|KISYS3DMOD", name)
        if not recognized:
            return None
        candidates = [root / suffix for root in roots.get(name, [])]
        candidate = next((path for path in candidates if path.is_file()), None)
        if candidate is None:
            candidate = _bundled_model(suffix)
        if candidate is None:
            return None
        value = str(candidate)

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = board_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_file() else None


def _model_inventory(
    board_text: str, board_dir: Path, roots: dict[str, list[Path]]
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for match in _MODEL_RE.finditer(board_text):
        raw_path = _decode_sexpr_string(match.group(1))
        resolved = _resolve_model_path(raw_path, board_dir, roots)
        record: dict[str, Any] = {"source_path": raw_path, "resolved_path": str(resolved) if resolved else None}
        if resolved:
            record["sha256"] = _sha256(resolved)
            asset_root = Path(__file__).with_name("model_assets").resolve()
            if resolved.is_relative_to(asset_root):
                record["origin"] = "bundled_dimensioned_parametric_model"
                provenance = asset_root / "inventory.json"
                record["provenance_path"] = str(provenance)
                record["provenance_sha256"] = _sha256(provenance)
        else:
            record["reason"] = "model path could not be resolved to an existing file"
        inventory.append(record)
    return inventory


def _rewrite_model_paths(board_text: str, inventory: list[dict[str, Any]]) -> str:
    iterator = iter(inventory)

    def replacement(match: re.Match[str]) -> str:
        record = next(iterator)
        resolved = record.get("resolved_path")
        if not resolved:
            return match.group(0)
        portable = str(resolved).replace("\\", "/")
        return match.group(0).replace(match.group(1), _encode_sexpr_string(portable), 1)

    return _MODEL_RE.sub(replacement, board_text)


def _source_files(board: Path) -> list[Path]:
    candidates = [board, board.with_suffix(".kicad_pro"), board.with_suffix(".kicad_dru")]
    candidates.extend((board.parent / "fp-lib-table", board.parent / "sym-lib-table"))
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate.is_file():
            resolved = candidate.resolve()
            if resolved not in seen:
                result.append(resolved)
                seen.add(resolved)
    return result


def _hash_snapshot(paths: list[Path]) -> dict[str, str]:
    return {str(path): _sha256(path) for path in paths if path.is_file()}


def _result(status: str, reason: str, **details: Any) -> dict[str, Any]:
    message = details.pop("message", None) or reason.replace("_", " ").capitalize()
    return {"status": status, "reason": reason, "message": message, **details}


def _validate_png(payload: bytes) -> tuple[bool, str, int | None, int | None]:
    """Check PNG signature, chunk framing/CRCs, dimensions, IDAT and IEND."""
    if not payload.startswith(_PNG_SIGNATURE):
        return False, "PNG signature is missing", None, None

    offset = len(_PNG_SIGNATURE)
    width: int | None = None
    height: int | None = None
    seen_idat = False
    seen_iend = False
    chunk_index = 0
    while offset < len(payload):
        if len(payload) - offset < _PNG_CHUNK_HEADER_SIZE + _PNG_CRC_SIZE:
            return False, "PNG chunk header or CRC is truncated", width, height
        chunk_length = struct.unpack_from(">I", payload, offset)[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_end = offset + _PNG_CHUNK_HEADER_SIZE + chunk_length
        crc_end = chunk_end + _PNG_CRC_SIZE
        if crc_end > len(payload):
            return False, f"PNG {chunk_type!r} chunk is truncated", width, height
        chunk_data = payload[offset + _PNG_CHUNK_HEADER_SIZE : chunk_end]
        expected_crc = struct.unpack_from(">I", payload, chunk_end)[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            return False, f"PNG {chunk_type!r} CRC is invalid", width, height
        if chunk_index == 0:
            if chunk_type != b"IHDR" or chunk_length != 13:
                return False, "PNG must begin with a 13-byte IHDR chunk", None, None
            width, height = struct.unpack_from(">II", chunk_data)
            if width <= 0 or height <= 0:
                return False, "PNG dimensions must be positive", width, height
        elif chunk_type == b"IHDR":
            return False, "PNG contains a duplicate IHDR chunk", width, height
        if chunk_type == b"IDAT":
            seen_idat = True
        if chunk_type == b"IEND":
            if chunk_length != 0:
                return False, "PNG IEND chunk must be empty", width, height
            if not seen_idat:
                return False, "PNG has no IDAT image data", width, height
            if crc_end != len(payload):
                return False, "PNG contains trailing bytes after IEND", width, height
            seen_iend = True
            break
        offset = crc_end
        chunk_index += 1

    if not seen_iend:
        return False, "PNG IEND chunk is missing", width, height
    return True, "PNG structure is valid", width, height


def _publish_png(staged_png: Path, output: Path, *, overwrite: bool) -> str | None:
    """Publish validated bytes atomically without truncating an existing file."""
    publish_staging = output.with_name(f".{output.stem}.cw-stage-{uuid.uuid4().hex}{output.suffix}")
    try:
        shutil.copyfile(staged_png, publish_staging)
        if overwrite:
            os.replace(publish_staging, output)
        else:
            try:
                os.link(publish_staging, output)
            except FileExistsError:
                return f"Output appeared while rendering; refusing to replace it without overwrite: {output}"
            publish_staging.unlink()
    except OSError as exc:
        return f"Could not atomically publish preview {output}: {exc}"
    finally:
        try:
            if publish_staging.exists():
                publish_staging.unlink()
        except OSError:
            pass
    return None


def render_board_model(
    board_path: str | Path,
    output_png: str | Path,
    *,
    kicad_cli: str | Path | None = None,
    side: str = "front",
    timeout_seconds: float = 120,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Render real KiCad 3D models to a transparent PNG.

    KiCad 9 and newer expose ``pcb render``. KiCad 8 and older return an
    explicit ``unavailable`` result. Every source file is read from its
    original location, but KiCad only receives an isolated staged copy.
    """
    try:
        board = Path(board_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return _result("failed", "board_not_found", message=str(exc))
    if not board.is_file() or board.suffix.lower() != ".kicad_pcb":
        return _result("failed", "invalid_board_path", board_path=str(board))
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        return _result("failed", "invalid_timeout", timeout_seconds=timeout_seconds)
    if not isinstance(overwrite, bool):
        return _result("failed", "invalid_overwrite", overwrite=overwrite)

    normalized_side = {"front": "top", "top": "top", "back": "bottom", "bottom": "bottom"}.get(
        str(side).lower()
    )
    if normalized_side is None:
        return _result(
            "failed", "invalid_side", side=side, supported_sides=["front", "top", "back", "bottom"]
        )

    try:
        output = Path(output_png).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return _result("failed", "invalid_output_path", message=str(exc))
    if output.suffix.lower() != ".png":
        return _result("failed", "invalid_output_extension", output_path=str(output), required_extension=".png")
    source_paths = _source_files(board)
    if output in source_paths:
        return _result("failed", "output_conflicts_with_source", output_path=str(output))
    if output.exists() and not overwrite:
        return _result("failed", "output_exists", output_path=str(output), overwrite_required=True)

    cli = _find_kicad_cli(kicad_cli)
    if cli is None:
        return _result("unavailable", "kicad_cli_not_found", source_sha256=_sha256(board))

    source_sha256 = _sha256(board)
    try:
        version_run = subprocess.run(
            [str(cli), "version"], capture_output=True, text=True, timeout=min(float(timeout_seconds), 15), check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result("unavailable", "kicad_cli_version_probe_failed", source_sha256=source_sha256, message=str(exc))
    version_text = (version_run.stdout or version_run.stderr or "").strip()
    version_match = re.search(r"\b(\d+)\.(\d+)(?:\.\d+)?", version_text)
    if version_run.returncode != 0 or not version_match:
        return _result(
            "unavailable", "kicad_cli_version_unknown", source_sha256=source_sha256,
            cli=str(cli), evidence=version_text[-1000:],
        )
    major = int(version_match.group(1))
    if major < 9:
        return _result(
            "unavailable", "native_3d_render_requires_kicad_9_or_newer",
            source_sha256=source_sha256, cli=str(cli), cli_version=version_text,
            evidence=f"KiCad {major} does not expose `pcb render`; 2D SVG preview remains available.",
        )

    try:
        capability = subprocess.run(
            [str(cli), "pcb", "render", "--help"], capture_output=True, text=True,
            timeout=min(float(timeout_seconds), 15), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result(
            "unavailable", "native_3d_render_probe_failed", source_sha256=source_sha256,
            cli=str(cli), cli_version=version_text, message=str(exc),
        )
    capability_text = f"{capability.stdout or ''}\n{capability.stderr or ''}"
    if capability.returncode != 0 or "pcb render" not in capability_text.lower():
        return _result(
            "unavailable", "native_3d_render_not_supported", source_sha256=source_sha256,
            cli=str(cli), cli_version=version_text, evidence=capability_text.strip()[-1000:],
        )

    try:
        board_bytes = board.read_bytes()
        board_text = board_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return _result("failed", "board_read_failed", source_sha256=source_sha256, message=str(exc))
    staged_board_sha256 = hashlib.sha256(board_bytes).hexdigest()

    inventory = _model_inventory(board_text, board.parent, _model_roots(cli, major))
    resolved_models = [record for record in inventory if record.get("resolved_path")]
    missing_models = [record for record in inventory if not record.get("resolved_path")]
    model_coverage = {
        "expected": len(inventory),
        "resolved": len(resolved_models),
        "missing": len(missing_models),
        "models": inventory,
    }
    if missing_models:
        return _result(
            "failed", "3d_model_files_missing", source_sha256=source_sha256,
            cli=str(cli), cli_version=version_text, model_coverage=model_coverage,
        )
    if not inventory:
        return _result(
            "failed", "board_has_no_3d_model_references", source_sha256=source_sha256,
            cli=str(cli), cli_version=version_text, model_coverage=model_coverage,
        )

    source_paths = list(dict.fromkeys(
        source_paths + [Path(record["resolved_path"]) for record in inventory]
        + [Path(record["provenance_path"]) for record in inventory if record.get("provenance_path")]
    ))
    if output in source_paths:
        return _result(
            "failed", "output_conflicts_with_source", source_sha256=source_sha256,
            output_path=str(output), model_coverage=model_coverage,
        )
    source_hashes = _hash_snapshot(source_paths)
    source_sha256 = source_hashes.get(str(board), source_sha256)
    model_hash_mismatch = any(
        source_hashes.get(str(record["resolved_path"])) != record.get("sha256")
        for record in resolved_models
    ) or any(
        source_hashes.get(record["provenance_path"]) != record["provenance_sha256"]
        for record in resolved_models if record.get("provenance_path")
    )
    if source_sha256 != staged_board_sha256 or model_hash_mismatch:
        return _result(
            "failed", "source_changed_during_resolution", source_sha256=source_sha256,
            source_board_read_sha256=staged_board_sha256, source_hashes=source_hashes,
            cli=str(cli), cli_version=version_text, model_coverage=model_coverage,
        )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _result("failed", "output_directory_unavailable", source_sha256=source_sha256, message=str(exc))

    with tempfile.TemporaryDirectory(prefix="cw-pcb-model-preview-") as temp_name:
        staging = Path(temp_name)
        staged_board = staging / board.name
        try:
            staged_board.write_text(_rewrite_model_paths(board_text, inventory), encoding="utf-8", newline="\n")
            for source in source_paths:
                if source == board:
                    continue
                destination = staging / source.name
                is_project_sidecar = source.suffix.lower() in {".kicad_pro", ".kicad_dru"}
                is_library_table = source.name in {"fp-lib-table", "sym-lib-table"}
                if is_project_sidecar or is_library_table:
                    shutil.copy2(source, destination)
        except OSError as exc:
            return _result(
                "failed", "staging_failed", source_sha256=source_sha256,
                cli=str(cli), cli_version=version_text, model_coverage=model_coverage, message=str(exc),
            )

        staged_png = staging / "render.png"
        command = [
            str(cli), "pcb", "render", "--output", str(staged_png), "--width", "1600", "--height", "1200",
            "--side", normalized_side, "--background", "transparent", "--quality", "high", str(staged_board),
        ]
        try:
            render = subprocess.run(
                command, capture_output=True, text=True, timeout=float(timeout_seconds), check=False
            )
        except subprocess.TimeoutExpired as exc:
            return _result(
                "failed", "render_timeout", source_sha256=source_sha256,
                cli=str(cli), cli_version=version_text, model_coverage=model_coverage,
                timeout_seconds=float(timeout_seconds), message=str(exc),
            )
        except OSError as exc:
            return _result(
                "failed", "render_process_failed", source_sha256=source_sha256,
                cli=str(cli), cli_version=version_text, model_coverage=model_coverage, message=str(exc),
            )

        cli_evidence = f"{render.stdout or ''}\n{render.stderr or ''}".strip()[-2000:]
        if render.returncode != 0:
            return _result(
                "failed", "native_3d_render_failed", source_sha256=source_sha256,
                cli=str(cli), cli_version=version_text, model_coverage=model_coverage,
                returncode=render.returncode, evidence=cli_evidence,
            )
        try:
            png_bytes = staged_png.read_bytes()
        except OSError as exc:
            return _result(
                "failed", "render_output_missing", source_sha256=source_sha256,
                cli=str(cli), cli_version=version_text, model_coverage=model_coverage, message=str(exc),
            )
        png_valid, png_reason, png_width, png_height = _validate_png(png_bytes)
        if not png_valid:
            return _result(
                "failed", "render_output_invalid_png", source_sha256=source_sha256,
                cli=str(cli), cli_version=version_text, model_coverage=model_coverage,
                output_size_bytes=len(png_bytes), evidence=png_reason,
            )

        final_hashes = _hash_snapshot(source_paths)
        if final_hashes != source_hashes:
            return _result(
                "failed", "source_changed_during_render", source_sha256=source_sha256,
                cli=str(cli), cli_version=version_text, model_coverage=model_coverage,
                source_hashes_before=source_hashes, source_hashes_after=final_hashes,
            )

        if output.exists():
            if not overwrite:
                return _result(
                    "failed", "output_exists", source_sha256=source_sha256,
                    cli=str(cli), cli_version=version_text, model_coverage=model_coverage,
                    output_path=str(output), overwrite_required=True,
                )
        publish_error = _publish_png(staged_png, output, overwrite=overwrite)
        if publish_error:
            return _result(
                "failed", "output_publish_failed", source_sha256=source_sha256,
                cli=str(cli), cli_version=version_text, model_coverage=model_coverage, message=publish_error,
            )

        return _result(
            "ok", "rendered", source_sha256=source_sha256, source_hashes=source_hashes,
            cli=str(cli), cli_version=version_text, side=normalized_side,
            output_path=str(output), png_sha256=hashlib.sha256(png_bytes).hexdigest(),
            output_size_bytes=len(png_bytes), png_width=png_width, png_height=png_height,
            model_coverage=model_coverage,
            evidence=cli_evidence,
        )
