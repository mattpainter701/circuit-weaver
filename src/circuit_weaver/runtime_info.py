"""Read-only runtime and installation provenance diagnostics."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment,misc]


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _package_version() -> str:
    try:
        from . import __version__

        return __version__
    except ImportError:
        try:
            return metadata.version("circuit-weaver")
        except metadata.PackageNotFoundError:
            return "source-checkout"


def _distribution_version() -> str | None:
    try:
        return metadata.version("circuit-weaver")
    except metadata.PackageNotFoundError:
        return None


def _kicad_python_candidate() -> tuple[str | None, str | None]:
    """Find a configured/known KiCad Python, without importing pcbnew or spawning it."""
    configured = [os.getenv("CIRCUIT_WEAVER_KICAD_PYTHON"), os.getenv("KICAD_PYTHON")]
    known = [
        *(f"C:/Program Files/KiCad/{version}/bin/python.exe" for version in ("10.0", "9.0", "8.0")),
        "/usr/bin/python3",
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
    ]
    for candidate in dict.fromkeys(str(value) for value in configured if value):
        resolved = shutil.which(candidate)
        if resolved:
            return str(Path(resolved).resolve()), "configured_path"
        path = Path(os.path.expandvars(os.path.expanduser(candidate.strip().strip('"'))))
        if path.is_file():
            return str(path.resolve()), "configured_path"
    for candidate in known:
        resolved = shutil.which(candidate)
        if resolved:
            return str(Path(resolved).resolve()), "known_install_path"
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve()), "known_install_path"
    try:
        pcbnew_spec = importlib.util.find_spec("pcbnew")
    except (ImportError, ValueError):
        pcbnew_spec = None
    if pcbnew_spec is not None:
        return str(Path(sys.executable).resolve()), "current_interpreter_pcbnew_discovered"
    return None, None


def _native_tools() -> dict[str, Any]:
    """Discover optional native tool paths without invoking any executable."""
    from .autoroute import _find_freerouting_command, _find_freerouting_jar, _find_java, _find_kicad_cli

    kicad_python, kicad_python_source = _kicad_python_candidate()
    kicad_cli = _find_kicad_cli()
    java = _find_java()
    freerouting_command = _find_freerouting_command()
    freerouting_jar = _find_freerouting_jar()
    return {
        "discovery": "path_only_no_version_probe",
        "kicad_python": {
            "path": kicad_python,
            "candidate_source": kicad_python_source if kicad_python else None,
            "found": kicad_python is not None,
            "pcbnew_verified": False,
        },
        "kicad_cli": {"path": kicad_cli, "available": kicad_cli is not None},
        "java": {"path": java, "available": java is not None},
        "freerouting": {
            "command": freerouting_command,
            "jar_path": str(freerouting_jar) if freerouting_jar is not None else None,
            "available": freerouting_command is not None,
        },
    }


def _skill_installations() -> list[dict[str, Any]]:
    """Describe known skill copies and compare them to this import's bundle."""
    from .skill_installer import (
        _MANIFEST_NAME,
        _build_platform_paths,
        _find_skills_source,
        _managed_target,
        _UnsafeManagedPath,
    )

    source, _ = _find_skills_source()
    canonical: dict[str, str] = {}
    if source is not None:
        for skill_dir in sorted(source.iterdir()):
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file():
                canonical[skill_dir.name] = _sha256(skill_dir / "SKILL.md") or ""

    _, destinations, aliases = _build_platform_paths()
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for platform, root in destinations.items():
        candidate_roots = [root, *(Path(alias) / "skills" for alias in aliases.get(platform, ()))]
        for candidate_root in candidate_roots:
            if not candidate_root.is_dir():
                continue
            for skill_name, expected_hash in canonical.items():
                skill_dir = candidate_root / skill_name
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    continue
                key = (platform, str(skill_dir.resolve(strict=False)))
                if key in seen:
                    continue
                seen.add(key)
                installed_hash = _sha256(skill_file)
                manifest_path = skill_dir / _MANIFEST_NAME
                manifest: dict[str, Any] = {}
                try:
                    value = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        manifest = value
                except (OSError, json.JSONDecodeError):
                    pass
                file_drift: list[str] = []
                managed_files = manifest.get("files", {})
                if isinstance(managed_files, dict):
                    for relative, installed_digest in managed_files.items():
                        if not isinstance(relative, str) or not isinstance(installed_digest, str):
                            continue
                        try:
                            candidate = _managed_target(skill_dir, relative)
                        except _UnsafeManagedPath:
                            file_drift.append(relative)
                            continue
                        if _sha256(candidate) != installed_digest:
                            file_drift.append(relative)
                drift = []
                if installed_hash != expected_hash:
                    drift.append("skill_content")
                if manifest and manifest.get("package_version") != _package_version():
                    drift.append("package_version")
                if manifest and manifest.get("python_executable") != str(Path(sys.executable).resolve()):
                    drift.append("python_executable")
                if file_drift:
                    drift.append("managed_files")
                records.append(
                    {
                        "platform": platform,
                        "path": str(skill_dir),
                        "installed": True,
                        "provenance": "manifest" if manifest else "legacy_or_unmanaged",
                        "package_version": manifest.get("package_version"),
                        "python_executable": manifest.get("python_executable"),
                        "skill_sha256": installed_hash,
                        "expected_skill_sha256": expected_hash,
                        "file_drift": sorted(file_drift),
                        "drift": sorted(set(drift)),
                    }
                )
    return records


def _audit_mcp_configs() -> list[dict[str, Any]]:
    """Read known JSON client configs and report matching server entries safely."""
    home = Path.home()
    roots = {
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")),
        "codex": home / ".codex",
        "opencode": Path(os.environ.get("OPENCODE_CONFIG_DIR", home / ".config" / "opencode")),
    }
    candidates = {
        "claude": [
            roots["claude"] / "config.json",
            roots["claude"] / "claude_desktop_config.json",
            home / ".claude.json",
        ],
        "codex": [roots["codex"] / "config.toml", roots["codex"] / "mcp.json"],
        "opencode": [roots["opencode"] / "opencode.json", roots["opencode"] / "config.json"],
    }
    reports: list[dict[str, Any]] = []
    for client, paths in candidates.items():
        for path in paths:
            if not path.is_file():
                continue
            try:
                if path.suffix.lower() == ".toml":
                    if tomllib is None:
                        continue
                    payload = tomllib.loads(path.read_text(encoding="utf-8"))
                elif path.suffix.lower() == ".json":
                    payload = json.loads(path.read_text(encoding="utf-8"))
                else:
                    continue
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            servers = (
                {}
                if not isinstance(payload, dict)
                else (
                    payload.get("mcpServers", {})
                    if "mcpServers" in payload
                    else (payload.get("mcp_servers", payload.get("mcp", {})))
                )
            )
            if not isinstance(servers, dict):
                continue
            for name, value in servers.items():
                if not isinstance(value, dict):
                    continue
                serialized = json.dumps(value, ensure_ascii=False).lower()
                if (
                    "circuit-weaver" not in str(name).lower()
                    and "circuit_weaver" not in serialized
                    and "circuit-weaver" not in serialized
                ):
                    continue
                command = value.get("command")
                args = value.get("args", [])
                if isinstance(command, list):
                    args = command[1:] + (args if isinstance(args, list) else [])
                    command = command[0] if command else None
                safe_args: list[str] = []
                redact_next = False
                if isinstance(args, list):
                    for arg in args:
                        value = str(arg)
                        lowered = value.lower()
                        normalized = re.sub(r"[^a-z0-9]", "", lowered)
                        sensitive = any(
                            marker in normalized
                            for marker in ("token", "secret", "password", "apikey", "accesskey", "key")
                        )
                        if redact_next:
                            safe_args.append("[redacted]")
                            redact_next = sensitive and "=" not in value
                        elif sensitive:
                            safe_args.append("[redacted]")
                            redact_next = "=" not in value
                        else:
                            safe_args.append(value)
                reports.append(
                    {
                        "client": client,
                        "config_path": str(path),
                        "server_name": str(name),
                        "command": str(command) if command is not None else None,
                        "args": safe_args,
                        "matches_current_python": command is not None
                        and Path(str(command)).resolve(strict=False) == Path(sys.executable).resolve(),
                    }
                )
    return reports


def _mcp_tool_capabilities() -> list[dict[str, str]]:
    """Read MCP tool names, signatures, and descriptions from server source."""
    source_path = Path(__file__).with_name("mcp_server.py")
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, SyntaxError):
        return []
    tools: list[dict[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        ):
            continue
        signature = ast.unparse(node.args)
        tools.append(
            {
                "name": node.name,
                "signature": f"{node.name}({signature})",
                "description": ast.get_docstring(node) or "",
            }
        )
    return tools


def runtime_info(*, audit_external: bool = False) -> dict[str, Any]:
    """Return JSON-safe runtime, capability, skill provenance, and MCP details.

    ``audit_external`` enables read-only inspection of known JSON MCP client
    configs. It never changes those files and excludes environment variables.
    """
    from . import __version__

    mcp_available = importlib.util.find_spec("mcp") is not None
    module_version = __version__
    distribution_version = _distribution_version()
    if distribution_version is None:
        version_alignment = "source_checkout"
    elif distribution_version == module_version:
        version_alignment = "aligned"
    else:
        version_alignment = "distribution_module_mismatch"
    result: dict[str, Any] = {
        "runtime": {
            "package": "circuit-weaver",
            "package_version": module_version,
            "module_version": module_version,
            "distribution_version": distribution_version,
            "version_alignment": version_alignment,
            "import_path": str(Path(__file__).resolve()),
            "cli_command": [str(Path(sys.executable).resolve()), "-m", "circuit_weaver"],
            "path_cli_executable": shutil.which("circuit-weaver"),
            "python_executable": str(Path(sys.executable).resolve()),
            "python_version": sys.version.split()[0],
        },
        "capabilities": {
            "mcp": mcp_available,
            "mcp_transport": ["stdio", "sse"] if mcp_available else [],
            "mcp_tools": _mcp_tool_capabilities() if mcp_available else [],
            "native_tools": _native_tools(),
        },
        "skills": _skill_installations(),
        "mcp": {
            "transport": "stdio",
            "protocol": "Model Context Protocol",
            "command": [str(Path(sys.executable).resolve()), "-m", "circuit_weaver.mcp_server"],
        },
    }
    if audit_external:
        result["client_configs"] = _audit_mcp_configs()
    return result
