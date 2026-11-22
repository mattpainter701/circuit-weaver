"""Exact-byte, isolated KiCad Gerber and drill export helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


class GerberExportError(RuntimeError):
    """Native Gerber/drill export failed or produced an invalid artifact set."""


def _required_manufacturing_layers(files: list[Path], output: Path) -> set[str]:
    jobs = [path for path in files if path.name.casefold().endswith(".gbrjob")]
    if len(jobs) != 1:
        raise GerberExportError("KiCad Gerber export must produce exactly one .gbrjob manifest")
    try:
        manifest = json.loads(jobs[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GerberExportError(f"KiCad Gerber job manifest is invalid: {exc}") from exc
    general = manifest.get("GeneralSpecs") if isinstance(manifest, dict) else None
    layer_count = general.get("LayerNumber") if isinstance(general, dict) else None
    if isinstance(layer_count, bool) or not isinstance(layer_count, int) or layer_count < 2:
        raise GerberExportError("Gerber job manifest has no valid copper layer count")
    attributes = manifest.get("FilesAttributes")
    if not isinstance(attributes, list):
        raise GerberExportError("Gerber job manifest has no file attributes")
    by_function: dict[str, str] = {}
    for row in attributes:
        if not isinstance(row, dict):
            raise GerberExportError("Gerber job manifest contains an invalid file record")
        name, function = row.get("Path"), row.get("FileFunction")
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise GerberExportError("Gerber job manifest contains an unsafe file path")
        if not isinstance(function, str) or not function:
            continue
        target = output / name
        if not target.is_file():
            raise GerberExportError(f"Gerber job manifest references missing file {name!r}")
        if target.stat().st_size == 0:
            raise GerberExportError(f"Gerber job manifest references empty file {name!r}")
        by_function[function] = name

    required: set[str] = set()
    for index in range(1, layer_count + 1):
        copper_functions = [name for name in by_function if name.startswith(f"Copper,L{index},")]
        if not copper_functions:
            raise GerberExportError(f"Gerber export is missing copper layer L{index}")
        if index == 1 and not any(name.endswith(",Top") for name in copper_functions):
            raise GerberExportError("Gerber export is missing front copper")
        if index == layer_count and not any(name.endswith(",Bot") for name in copper_functions):
            raise GerberExportError("Gerber export is missing back copper")
        required.update(by_function[name] for name in copper_functions)
    if "Profile" not in by_function:
        raise GerberExportError("Gerber export is missing the board outline/profile layer")
    required.add(by_function["Profile"])
    drills = [path for path in files if path.suffix.casefold() == ".drl"]
    if not drills:
        raise GerberExportError("KiCad export did not produce drill files")
    if any(path.stat().st_size == 0 for path in drills):
        raise GerberExportError("KiCad export produced an empty drill file")
    return required


def export_gerbers_archive(
    board: str | Path,
    output_dir: str | Path,
    *,
    kicad_cli: str | Path,
    expected_board_sha256: str,
    timeout: int = 300,
) -> dict[str, object]:
    """Export one immutable board snapshot and archive only this run's outputs.

    Native exports run in a fresh temporary directory, so stale Gerbers already
    present in ``output_dir`` cannot leak into the delivery ZIP.
    """
    source = Path(board).resolve()
    if not source.is_file() or source.suffix.casefold() != ".kicad_pcb":
        raise GerberExportError("Gerber export requires an existing .kicad_pcb file")
    try:
        board_bytes = source.read_bytes()
    except OSError as exc:
        raise GerberExportError(f"cannot read input board: {exc}") from exc
    board_sha256 = hashlib.sha256(board_bytes).hexdigest()
    if board_sha256 != expected_board_sha256:
        raise GerberExportError("board changed after manufacturing evidence was checked")

    sidecars: dict[str, bytes] = {}
    for suffix in (".kicad_pro", ".kicad_dru"):
        path = source.with_suffix(suffix)
        if path.is_file():
            sidecars[path.name] = path.read_bytes()

    destination = Path(output_dir).expanduser().resolve()
    archive_name = f"{source.stem}_gerbers.zip"
    with tempfile.TemporaryDirectory(prefix="cw-gerber-export-") as temporary:
        stage = Path(temporary)
        staged_board = stage / source.name
        staged_board.write_bytes(board_bytes)
        for name, payload in sidecars.items():
            (stage / name).write_bytes(payload)
        if hashlib.sha256(staged_board.read_bytes()).hexdigest() != board_sha256:
            raise GerberExportError("private board snapshot does not match the approved board hash")

        native_output = stage / "native-output"
        native_output.mkdir()
        for export_kind in ("gerbers", "drill"):
            try:
                result = subprocess.run(
                    [
                        str(kicad_cli), "pcb", "export", export_kind,
                        "-o", str(native_output), str(staged_board),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise GerberExportError(f"KiCad {export_kind} export failed: {exc}") from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise GerberExportError(f"KiCad {export_kind} export failed: {detail[:1000]}")
            if hashlib.sha256(staged_board.read_bytes()).hexdigest() != board_sha256:
                raise GerberExportError("KiCad export changed the private board snapshot")

        files = sorted(path for path in native_output.iterdir() if path.is_file())
        required_layers = _required_manufacturing_layers(files, native_output)
        archive_stage = stage / archive_name
        with zipfile.ZipFile(archive_stage, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
        expected_members = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        if not required_layers.issubset(expected_members):
            raise GerberExportError("Gerber ZIP is missing required copper or outline files")

        destination.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, destination / path.name)
        archive_destination = destination / archive_name
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{archive_name}.", suffix=".tmp", dir=destination
        )
        os.close(fd)
        temporary_archive = Path(temporary_name)
        try:
            shutil.copyfile(archive_stage, temporary_archive)
            with zipfile.ZipFile(temporary_archive, "r") as archive:
                if set(archive.namelist()) != set(expected_members):
                    raise GerberExportError("Gerber ZIP does not contain exactly the current export files")
                for name, expected_hash in expected_members.items():
                    if hashlib.sha256(archive.read(name)).hexdigest() != expected_hash:
                        raise GerberExportError(f"Gerber ZIP read-back verification failed for {name!r}")
            os.replace(temporary_archive, archive_destination)
        finally:
            temporary_archive.unlink(missing_ok=True)

    return {
        "board_sha256": board_sha256,
        "zip": str(destination / archive_name),
        "files": [str(destination / path.name) for path in files],
        "file_count": len(files),
    }
