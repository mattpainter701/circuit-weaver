"""Install Circuit Weaver skills without losing user customizations.

Each installed skill carries a small provenance manifest.  On later releases we
compare every managed file with the hash recorded at installation time:

* pristine managed files are upgraded automatically;
* user-modified or user-deleted managed files produce a conflict;
* files that were not installed by Circuit Weaver are never touched;
* ``force=True`` resolves managed-file conflicts in favour of the release, and
  ``backup=True`` preserves each replaced file beside the original.

The known hashes below migrate the last manifest-less release.  They allow a
pristine older installation to receive this release while still treating an
unknown file as a user customization.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

_MANIFEST_NAME = ".circuit-weaver-install.json"
_MANIFEST_SCHEMA = 2
_SUPPORTED_MANIFEST_SCHEMAS = {1, _MANIFEST_SCHEMA}
_WINDOWS_INVALID_MANAGED_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_MANAGED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

# SHA-256 of SKILL.md in the final manifest-less release.  Keep these entries
# when renaming a skill so an existing pristine directory can be migrated.
_LEGACY_PRISTINE_SKILL_MD_HASHES: dict[str, frozenset[str]] = {
    "bom": frozenset({"7523c4b99c0ccacef3f6003896dac4056759dad42e2fae5fb5a74ee89e969ebe"}),
    "circuit-weaver": frozenset({"9101f17ee49d57ff404b2d04c1c52fb81faa406e4dba85ec3eff181b3ece18c4"}),
    "design-wizard": frozenset({"c65ee32eb9ef5e7e8ce37c7fffb97bc402a135274964685830452e9326cdf31e"}),
    "digikey": frozenset({"1f1e71285f643be2f28e59eb0dd6861554d4556b34ae51c0380ecda1e0950bd7"}),
    "ee": frozenset({"418ecb211843b87d732fe7acf13046936cf470544f55db7aed43340f7278dcd7"}),
    "jlcpcb": frozenset({"29ed3bb70f7b8c5baf04ded9b399036d9e5f65a3479994e4d1ceb850702c9271"}),
    "kicad": frozenset({"599a780e09fb33a44eda86c98a6094a0ad9bb31fdaec91da7d7120dbb8567d44"}),
    "lcsc": frozenset({"4442d83e7c4783304508aba8e8ed102e125988de98096ee6dc1be4072fd0fe13"}),
    "mouser": frozenset({"8ef95c172b05f4f02cad2674aebacc9643dfd538e31eb86b62373e9d115a2417"}),
    "pcbway": frozenset({"bb6e29ae3730ff1c0af17d382c2a321ef4ea6d556c3abd510c87e131a3edfe1b"}),
    "vivado": frozenset({"8284088875671db14dbaaa6e33afd20a8aadd9d585d561e0aacac73594abc8f7"}),
}

_LEGACY_SKILL_DIR_ALIASES: dict[str, tuple[str, ...]] = {
    "design-wizard": ("design_wizard",),
}
_LEGACY_SKILL_NAME_ALIASES = {"design_wizard": "design-wizard"}


class _UnsafeManagedPath(ValueError):
    """Raised when provenance points outside its installed skill directory."""


def _managed_target(skill_dir: Path, relative: str) -> Path:
    """Return a contained managed-file path or reject unsafe provenance.

    Manifest paths use portable POSIX separators.  Validate their lexical form
    before constructing a host path, then resolve existing symlinks and require
    the result to remain strictly below the installed skill directory.
    """
    if not relative or "\x00" in relative or "\\" in relative:
        raise _UnsafeManagedPath(f"invalid managed path {relative!r}")

    windows_path = PureWindowsPath(relative)
    if windows_path.drive or windows_path.root or relative.startswith("/"):
        raise _UnsafeManagedPath(f"absolute or drive-qualified managed path {relative!r}")

    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _UnsafeManagedPath(f"non-canonical managed path {relative!r}")
    for part in parts:
        if (
            any(ord(char) < 32 or ord(char) == 127 for char in part)
            or any(char in _WINDOWS_INVALID_MANAGED_CHARS for char in part)
            or part.endswith((".", " "))
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_MANAGED_NAMES
        ):
            raise _UnsafeManagedPath(f"non-portable managed path {relative!r}")

    target = skill_dir.joinpath(*parts)
    try:
        root = skill_dir.resolve(strict=False)
        resolved = target.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _UnsafeManagedPath(f"managed path escapes skill directory: {relative!r}") from exc
    if resolved == root:
        raise _UnsafeManagedPath(f"managed path resolves to skill directory: {relative!r}")
    return target


def _manifest_target(skill_dir: Path) -> Path:
    """Return the reserved provenance path without following a symlink."""
    manifest = _managed_target(skill_dir, _MANIFEST_NAME)
    if manifest.is_symlink():
        raise _UnsafeManagedPath("provenance manifest path must not be a symlink")
    if manifest.exists() and not manifest.is_file():
        raise _UnsafeManagedPath("provenance manifest path must be a regular file")
    return manifest


def _build_platform_paths(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Path], dict[str, Path], dict[str, tuple[Path, ...]]]:
    """Build platform discovery and destination paths from the environment."""
    env = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home

    claude_root = Path(env.get("CLAUDE_CONFIG_DIR", user_home / ".claude")).expanduser()

    opencode_override = env.get("OPENCODE_CONFIG_DIR")
    if opencode_override:
        opencode_root = Path(opencode_override).expanduser()
        opencode_skills = opencode_root / "skills"
    else:
        xdg_root = Path(env.get("XDG_CONFIG_HOME", user_home / ".config")).expanduser()
        opencode_root = xdg_root / "opencode"
        # OpenCode and current Codex both discover the Agent Skills standard
        # location, so use one copy by default and avoid duplicate skill names.
        opencode_skills = user_home / ".agents" / "skills"

    kilo_root = Path(env.get("KILO_CONFIG_DIR", user_home / ".kilo")).expanduser()
    agents_root = user_home / ".agents"

    detect_dirs = {
        "claude": claude_root,
        "codex": agents_root,
        "opencode": opencode_root,
        "kilo": kilo_root,
    }
    skill_dirs = {
        "claude": claude_root / "skills",
        "codex": agents_root / "skills",
        "opencode": opencode_skills,
        "kilo": kilo_root / "skills",
    }
    detect_aliases = {
        # Older Codex releases created ~/.codex.  Detect that installation but
        # always write skills to the current ~/.agents/skills location.
        "codex": (user_home / ".codex",),
    }
    return detect_dirs, skill_dirs, detect_aliases


_PLATFORM_DETECT_DIRS, _PLATFORM_SKILL_DIRS, _PLATFORM_DETECT_ALIASES = _build_platform_paths()


def detect_platforms() -> list[str]:
    """Return platforms whose primary or compatibility detection directory exists."""
    detected: list[str] = []
    for platform, detect_dir in _PLATFORM_DETECT_DIRS.items():
        candidates = (detect_dir, *_PLATFORM_DETECT_ALIASES.get(platform, ()))
        if any(candidate.exists() for candidate in candidates):
            detected.append(platform)
    return detected


def _find_skills_source() -> tuple[Path | None, str]:
    """Find the repository or wheel-bundled canonical skills directory."""
    try:
        repo_skills = Path(__file__).resolve().parent.parent.parent / "skills"
        if repo_skills.is_dir():
            return repo_skills, "repo"
    except OSError:
        pass

    try:
        bundled_skills = Path(__file__).resolve().parent / "_bundled_skills"
        if bundled_skills.is_dir():
            return bundled_skills, "bundled"
    except OSError:
        pass

    return None, "none"


def _file_hash(path: Path) -> str | None:
    """Return a SHA-256 hex digest, or ``None`` when the file is unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _backup_path(target: Path, *, now: datetime | None = None) -> Path:
    """Return a non-colliding timestamped backup filename for ``target``."""
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    candidate = target.with_suffix(target.suffix + f".bak.{timestamp}")
    counter = 1
    while candidate.exists():
        candidate = target.with_suffix(target.suffix + f".bak.{timestamp}.{counter}")
        counter += 1
    return candidate


def _source_files(skill_dir: Path) -> dict[str, Path]:
    """Return every distributable file in a skill, keyed by POSIX relative path."""
    files: dict[str, Path] = {}
    ignored_parts = {"__pycache__", ".pytest_cache"}
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(skill_dir)
        if relative.name == _MANIFEST_NAME or any(part in ignored_parts for part in relative.parts):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        files[relative.as_posix()] = path
    return files


def _read_manifest(skill_dir: Path, skill_name: str) -> dict[str, str] | None:
    """Read a valid provenance manifest and return its managed-file hashes."""
    path = _manifest_target(skill_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema") not in _SUPPORTED_MANIFEST_SCHEMAS or payload.get("skill") != skill_name:
        return None
    files = payload.get("files")
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        return None
    for relative in files:
        _managed_target(skill_dir, relative)
    return dict(files)


def _package_version() -> str:
    try:
        from . import __version__

        return __version__
    except ImportError:
        pass
    try:
        return importlib_metadata.version("circuit-weaver")
    except importlib_metadata.PackageNotFoundError:
        return "source-checkout"


def _source_fingerprint(hashes: Mapping[str, str]) -> str:
    material = "".join(f"{name}\0{digest}\n" for name, digest in sorted(hashes.items()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _write_manifest(skill_dir: Path, skill_name: str, hashes: Mapping[str, str]) -> None:
    manifest = _manifest_target(skill_dir)
    for relative in hashes:
        _managed_target(skill_dir, relative)
    payload = {
        "schema": _MANIFEST_SCHEMA,
        "skill": skill_name,
        "package_version": _package_version(),
        "python_executable": str(Path(os.sys.executable).resolve()),
        "python_version": os.sys.version.split()[0],
        "package_location": str(Path(__file__).resolve().parent),
        "cli_command": [str(Path(os.sys.executable).resolve()), "-m", "circuit_weaver"],
        "mcp_command": [str(Path(os.sys.executable).resolve()), "-m", "circuit_weaver.mcp_server"],
        "source_fingerprint": _source_fingerprint(hashes),
        "files": dict(sorted(hashes.items())),
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=skill_dir,
            prefix=".circuit-weaver-install.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())

        # Recheck after writing the private temporary file.  A destination
        # symlink is never followed by os.replace, and an unsafe one present at
        # this boundary is reported instead of silently accepted.
        manifest = _manifest_target(skill_dir)
        os.replace(temporary, manifest)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _legacy_baseline(
    skill_name: str,
    existing_dir: Path,
    source_hashes: Mapping[str, str],
) -> dict[str, str] | None:
    """Recognize the previous manifest-less release without trusting unknown files."""
    if existing_dir.is_symlink():
        return None
    try:
        installed_md = _managed_target(existing_dir, "SKILL.md")
    except _UnsafeManagedPath:
        return None
    installed_md_hash = _file_hash(installed_md)
    recognized_hashes = set(_LEGACY_PRISTINE_SKILL_MD_HASHES.get(skill_name, ()))
    if "SKILL.md" in source_hashes:
        recognized_hashes.add(source_hashes["SKILL.md"])
    if installed_md_hash not in recognized_hashes:
        return None

    # A known SKILL.md alone does not establish ownership of other files.
    # Migrate only when every existing distributable file is an exact current
    # source match; unknown or customized files remain untouched as conflicts.
    for path in existing_dir.rglob("*"):
        if path.is_symlink():
            return None
        if not path.is_file() or path.name == _MANIFEST_NAME:
            continue
        relative = path.relative_to(existing_dir).as_posix()
        if relative == "SKILL.md":
            continue
        if relative not in source_hashes or _file_hash(path) != source_hashes[relative]:
            return None

    baseline = {"SKILL.md": installed_md_hash}
    for relative, source_hash in source_hashes.items():
        if relative == "SKILL.md":
            continue
        try:
            destination = _managed_target(existing_dir, relative)
        except _UnsafeManagedPath:
            return None
        destination_hash = _file_hash(destination)
        if destination_hash == source_hash:
            baseline[relative] = source_hash
    return baseline


def _copy_skill(
    src_dir: Path,
    dest_platform_dir: Path,
    skill_name: str,
    *,
    force: bool = False,
    backup: bool = False,
    dry_run: bool = False,
    legacy_source_dir: Path | None = None,
) -> dict[str, Any]:
    """Install or safely upgrade one skill and return a structured outcome."""
    src_skill = src_dir / skill_name
    dest_skill = dest_platform_dir / skill_name
    if not src_skill.is_dir():
        return {"status": "missing_source", "reason": "source directory missing", "dest": str(dest_skill)}

    source_files = _source_files(src_skill)
    if "SKILL.md" not in source_files:
        return {"status": "missing_source", "reason": "source SKILL.md missing", "dest": str(dest_skill)}
    source_hashes = {relative: _file_hash(path) for relative, path in source_files.items()}
    if any(value is None for value in source_hashes.values()):
        return {"status": "missing_source", "reason": "source file unreadable", "dest": str(dest_skill)}
    clean_source_hashes: dict[str, str] = {key: value for key, value in source_hashes.items() if value is not None}

    legacy_dir: Path | None = None
    if legacy_source_dir is not None and legacy_source_dir.is_symlink():
        return {
            "status": "conflict",
            "reason": "legacy skill directory is a symlink; resolve manually",
            "dest": str(dest_skill / "SKILL.md"),
            "conflicts": [str(legacy_source_dir)],
        }
    if legacy_source_dir is not None and legacy_source_dir.exists():
        if dest_skill.exists():
            return {
                "status": "conflict",
                "reason": "both canonical and legacy skill directories exist; resolve manually",
                "dest": str(dest_skill / "SKILL.md"),
                "conflicts": [str(legacy_source_dir)],
            }
        legacy_dir = legacy_source_dir
    for alias in _LEGACY_SKILL_DIR_ALIASES.get(skill_name, ()):
        if legacy_dir is not None:
            break
        candidate = dest_platform_dir / alias
        if not candidate.exists():
            continue
        if dest_skill.exists():
            return {
                "status": "conflict",
                "reason": f"both canonical '{skill_name}' and legacy '{alias}' directories exist; resolve manually",
                "dest": str(dest_skill / "SKILL.md"),
                "conflicts": [str(candidate)],
            }
        legacy_dir = candidate
        break

    existing_dir = legacy_dir or dest_skill
    destination_exists = existing_dir.exists()
    try:
        prior_hashes = _read_manifest(existing_dir, skill_name) if destination_exists else None
    except _UnsafeManagedPath as exc:
        return {
            "status": "conflict",
            "reason": f"provenance manifest contains an unsafe managed path: {exc}",
            "dest": str(existing_dir / "SKILL.md"),
            "conflicts": [str(existing_dir / _MANIFEST_NAME)],
        }
    if prior_hashes is None and destination_exists:
        prior_hashes = _legacy_baseline(skill_name, existing_dir, clean_source_hashes)
        if prior_hashes is None and not force:
            return {
                "status": "conflict",
                "reason": (
                    "destination has no recognized Circuit Weaver provenance; " "use --force to overwrite managed files"
                ),
                "dest": str(existing_dir / "SKILL.md"),
                "conflicts": [str(existing_dir / "SKILL.md")],
            }
        prior_hashes = prior_hashes or {}
    else:
        prior_hashes = prior_hashes or {}

    # Preflight every path before reading managed files or applying any action.
    # This also rejects a lexically safe path whose existing symlink chain
    # resolves outside the installed skill directory.
    try:
        managed_targets = {
            relative: _managed_target(existing_dir, relative)
            for relative in set(clean_source_hashes) | set(prior_hashes)
        }
    except _UnsafeManagedPath as exc:
        return {
            "status": "conflict",
            "reason": f"managed file path is unsafe: {exc}",
            "dest": str(existing_dir / "SKILL.md"),
            "conflicts": [str(existing_dir / _MANIFEST_NAME)],
        }

    actions: list[tuple[str, str]] = []
    conflicts: list[str] = []
    for relative, source_hash in clean_source_hashes.items():
        target = managed_targets[relative]
        destination_hash = _file_hash(target)
        if destination_hash == source_hash:
            continue
        if destination_hash is None:
            if relative in prior_hashes and not force:
                conflicts.append(f"{relative} (managed file was deleted)")
            else:
                actions.append(("copy", relative))
            continue
        if force or prior_hashes.get(relative) == destination_hash:
            actions.append(("copy", relative))
        else:
            conflicts.append(relative)

    for relative, previous_hash in prior_hashes.items():
        if relative in clean_source_hashes:
            continue
        target = managed_targets[relative]
        destination_hash = _file_hash(target)
        if destination_hash is None:
            continue
        if force or destination_hash == previous_hash:
            actions.append(("remove", relative))
        else:
            conflicts.append(f"{relative} (removed upstream, customized locally)")

    if conflicts and not force:
        return {
            "status": "conflict",
            "reason": "managed files differ from their installed hashes: " + ", ".join(conflicts),
            "dest": str(existing_dir / "SKILL.md"),
            "conflicts": conflicts,
        }

    migrating = legacy_dir is not None
    status = "installed" if actions or not destination_exists or migrating else "unchanged"
    if dry_run:
        return {
            "status": status,
            "reason": f"would migrate {legacy_dir.name} to {skill_name}" if migrating else None,
            "dest": str(dest_skill / "SKILL.md"),
            "actions": actions,
        }

    dest_platform_dir.mkdir(parents=True, exist_ok=True)
    if migrating:
        legacy_dir.rename(dest_skill)
    dest_skill.mkdir(parents=True, exist_ok=True)

    for action, relative in actions:
        # Recheck immediately before each operation to narrow the opportunity
        # for a concurrent symlink swap after the preflight above.
        target = _managed_target(dest_skill, relative)
        if target.exists() and force and backup:
            backup_target = _backup_path(target)
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_target)
        if action == "copy":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_files[relative], target)
        else:
            target.unlink()

    _write_manifest(dest_skill, skill_name, clean_source_hashes)
    return {
        "status": status,
        "reason": f"migrated legacy directory to {skill_name}" if migrating else None,
        "dest": str(dest_skill / "SKILL.md"),
        "actions": actions,
    }


def install_skills(
    platforms: list[str] | None = None,
    skills: list[str] | None = None,
    *,
    force: bool = False,
    backup: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install canonical skills to selected platforms.

    ``platforms=None`` and ``platforms=['all']`` intentionally mean all
    supported platforms.  Explicit destinations are created when absent.
    Conflicts return ``status='error'`` so the command exits non-zero.
    """
    result: dict[str, Any] = {
        "status": "ok",
        "platforms_detected": detect_platforms(),
        "platforms_installed": [],
        "skills_skipped": [],  # Backward-compatible alias for conflict reporting.
        "skills_conflicted": [],
        "source": "none",
        "dry_run": dry_run,
        "warnings": [],
        "message": "",
    }

    if backup and not force:
        result["status"] = "error"
        result["message"] = "--backup requires --force"
        return result

    skills_source, source_type = _find_skills_source()
    result["source"] = source_type
    if not skills_source:
        result["status"] = "error"
        result["message"] = "No skills source found (not in git clone, not in PyPI wheel)"
        return result

    selected_platforms = list(_PLATFORM_SKILL_DIRS) if not platforms or "all" in platforms else list(platforms)
    invalid_platforms = [name for name in selected_platforms if name not in _PLATFORM_SKILL_DIRS]
    if invalid_platforms:
        result["warnings"].append(f"Unknown platforms ignored: {', '.join(invalid_platforms)}")
    selected_platforms = [name for name in selected_platforms if name in _PLATFORM_SKILL_DIRS]
    if not selected_platforms:
        result["status"] = "error"
        result["message"] = "No valid platforms selected"
        return result

    available_skills = sorted(
        directory.name
        for directory in skills_source.iterdir()
        if directory.is_dir() and (directory / "SKILL.md").is_file()
    )
    if skills:
        requested_skills: list[str] = []
        for name in skills:
            normalized = _LEGACY_SKILL_NAME_ALIASES.get(name, name)
            if normalized != name:
                result["warnings"].append(f"Skill '{name}' was renamed to '{normalized}'")
            if normalized not in requested_skills:
                requested_skills.append(normalized)
        invalid_skills = [name for name in requested_skills if name not in available_skills]
        if invalid_skills:
            result["warnings"].append(f"Skills not found: {', '.join(invalid_skills)}")
        skills_to_install = [name for name in requested_skills if name in available_skills]
    else:
        skills_to_install = available_skills
    if not skills_to_install:
        result["status"] = "error"
        result["message"] = f"No valid skills to install. Available: {', '.join(available_skills)}"
        return result

    any_failure = False
    for platform in selected_platforms:
        platform_skill_dir = _PLATFORM_SKILL_DIRS[platform]
        installed: list[str] = []
        unchanged: list[str] = []
        conflicted: list[str] = []
        failed: list[str] = []

        for skill_name in skills_to_install:
            outcome = _copy_skill(
                skills_source,
                platform_skill_dir,
                skill_name,
                force=force,
                backup=backup,
                dry_run=dry_run,
                legacy_source_dir=(
                    _PLATFORM_DETECT_ALIASES.get("codex", ())[0] / "skills" / skill_name
                    if platform == "codex" and _PLATFORM_DETECT_ALIASES.get("codex")
                    else None
                ),
            )
            status = outcome["status"]
            if status == "installed":
                installed.append(skill_name)
            elif status == "unchanged":
                unchanged.append(skill_name)
            elif status == "conflict":
                any_failure = True
                conflicted.append(skill_name)
                entry = {
                    "platform": platform,
                    "skill": skill_name,
                    "reason": outcome.get("reason") or "conflict",
                    "dest": outcome.get("dest", ""),
                    "conflicts": outcome.get("conflicts", []),
                }
                result["skills_conflicted"].append(entry)
                result["skills_skipped"].append(entry)
            else:
                any_failure = True
                failed.append(skill_name)

        result["platforms_installed"].append(
            {
                "platform": platform,
                "skills_installed": installed,
                "skills_unchanged": unchanged,
                "skills_conflicted": conflicted,
                "path": str(platform_skill_dir),
            }
        )
        if failed:
            result["warnings"].append(f"{platform}: failed to install {', '.join(failed)}")

    total_installed = sum(len(item["skills_installed"]) for item in result["platforms_installed"])
    total_unchanged = sum(len(item["skills_unchanged"]) for item in result["platforms_installed"])
    if any_failure:
        result["status"] = "error"
        conflict_count = len(result["skills_conflicted"])
        if conflict_count:
            result["message"] = (
                f"Installed {total_installed} skills; found {conflict_count} conflict(s). "
                "Resolve them or re-run with --force (and optionally --backup)."
            )
        else:
            result["message"] = f"Installed {total_installed} skills with failures; see warnings"
    else:
        prefix = "Would install" if dry_run else "Installed"
        unchanged_note = f" ({total_unchanged} already up to date)" if total_unchanged else ""
        result["message"] = (
            f"{prefix} {total_installed} skills to " f"{len(selected_platforms)} platform(s){unchanged_note}"
        )
    return result
