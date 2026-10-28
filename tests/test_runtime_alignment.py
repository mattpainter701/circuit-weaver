"""Focused contracts for runtime and skill installation alignment."""

import json
import sys
from pathlib import Path

from circuit_weaver import runtime_info as runtime_info_module
from circuit_weaver import skill_installer


def test_runtime_info_is_json_safe_and_uses_this_interpreter() -> None:
    payload = runtime_info_module.runtime_info()
    json.dumps(payload)

    assert payload["runtime"]["python_executable"] == str(Path(sys.executable).resolve())
    assert payload["runtime"]["import_path"].endswith("runtime_info.py")
    assert payload["runtime"]["package_version"] == payload["runtime"]["module_version"]
    assert payload["runtime"]["version_alignment"] in {"aligned", "distribution_module_mismatch", "source_checkout"}
    assert payload["mcp"]["command"] == [str(Path(sys.executable).resolve()), "-m", "circuit_weaver.mcp_server"]
    assert payload["runtime"]["cli_command"] == [str(Path(sys.executable).resolve()), "-m", "circuit_weaver"]
    capabilities = payload["capabilities"]["mcp_tools"]
    assert len(capabilities) >= 14
    assert {"start_project", "run_workflow", "workflow_status", "cancel_workflow", "library_access"}.issubset(
        {item["name"] for item in capabilities}
    )
    assert all(item["signature"].startswith(item["name"] + "(") for item in capabilities)
    assert all(item["description"] for item in capabilities)
    assert "client_configs" not in payload


def test_install_manifest_binds_interpreter_and_keeps_schema_one_readable(tmp_path: Path) -> None:
    destination_root = tmp_path / "destination"
    skill_dir = destination_root / "demo"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("prior skill\n", encoding="utf-8")
    hashes = {"SKILL.md": skill_installer._file_hash(skill_file)}

    skill_installer._write_manifest(skill_dir, "demo", hashes)
    manifest_path = skill_dir / skill_installer._MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert payload["schema"] == 2
    assert payload["python_executable"] == str(Path(sys.executable).resolve())
    assert payload["python_version"] == sys.version.split()[0]
    assert payload["package_version"] == runtime_info_module._package_version()
    assert payload["package_location"].endswith("circuit_weaver")
    assert payload["mcp_command"] == [str(Path(sys.executable).resolve()), "-m", "circuit_weaver.mcp_server"]
    assert payload["cli_command"] == [str(Path(sys.executable).resolve()), "-m", "circuit_weaver"]
    assert skill_installer._read_manifest(skill_dir, "demo") == hashes

    payload["schema"] = 1
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert skill_installer._read_manifest(skill_dir, "demo") == hashes

    source_root = tmp_path / "source"
    source_skill = source_root / "demo"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("updated skill\n", encoding="utf-8")
    outcome = skill_installer._copy_skill(source_root, destination_root, "demo")

    assert outcome["status"] == "installed"
    upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert upgraded["schema"] == 2


def test_external_config_audit_omits_environment_values(tmp_path: Path, monkeypatch) -> None:
    claude = tmp_path / "claude"
    claude.mkdir()
    config = claude / "config.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "circuit-weaver": {
                        "command": "python",
                        "args": ["-m", "circuit_weaver.mcp_server", "--api-key", "do-not-report-argument"],
                        "env": {"API_TOKEN": "do-not-report"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "missing-opencode"))

    reports = runtime_info_module._audit_mcp_configs()

    assert len(reports) == 1
    assert reports[0]["client"] == "claude"
    assert reports[0]["config_path"] == str(config)
    assert "env" not in json.dumps(reports)
    assert "do-not-report" not in json.dumps(reports)
    assert "do-not-report-argument" not in json.dumps(reports)


def test_runtime_reports_distribution_module_version_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(runtime_info_module, "_distribution_version", lambda: "0.35.0")

    payload = runtime_info_module.runtime_info()

    assert payload["runtime"]["version_alignment"] == "distribution_module_mismatch"
    assert payload["runtime"]["distribution_version"] == "0.35.0"
    assert payload["runtime"]["module_version"] != "0.35.0"


def test_runtime_reports_native_tool_paths_without_running_probes(monkeypatch) -> None:
    from circuit_weaver import autoroute

    monkeypatch.setattr(
        runtime_info_module,
        "_kicad_python_candidate",
        lambda: ("C:/KiCad/python.exe", "known_install_path"),
    )
    monkeypatch.setattr(autoroute, "_find_kicad_cli", lambda: "C:/KiCad/kicad-cli.exe")
    monkeypatch.setattr(autoroute, "_find_java", lambda: "C:/Java/bin/java.exe")
    monkeypatch.setattr(autoroute, "_find_freerouting_jar", lambda: Path("C:/Freerouting/freerouting.jar"))
    monkeypatch.setattr(
        autoroute,
        "_find_freerouting_command",
        lambda: ["C:/Java/bin/java.exe", "-jar", "C:/Freerouting/freerouting.jar"],
    )

    native = runtime_info_module.runtime_info()["capabilities"]["native_tools"]

    assert native["discovery"] == "path_only_no_version_probe"
    assert native["kicad_python"] == {
        "path": "C:/KiCad/python.exe",
        "candidate_source": "known_install_path",
        "found": True,
        "pcbnew_verified": False,
    }
    assert native["kicad_cli"]["path"] == "C:/KiCad/kicad-cli.exe"
    assert native["java"]["path"] == "C:/Java/bin/java.exe"
    assert native["freerouting"]["available"] is True
    assert native["freerouting"]["jar_path"] == str(Path("C:/Freerouting/freerouting.jar"))


def test_kicad_python_discovery_does_not_call_plain_python_a_kicad_runtime(monkeypatch) -> None:
    monkeypatch.delenv("CIRCUIT_WEAVER_KICAD_PYTHON", raising=False)
    monkeypatch.delenv("KICAD_PYTHON", raising=False)
    monkeypatch.setattr(runtime_info_module.shutil, "which", lambda _candidate: None)
    monkeypatch.setattr(Path, "is_file", lambda _path: False)
    monkeypatch.setattr(runtime_info_module.importlib.util, "find_spec", lambda _name: None)

    assert runtime_info_module._kicad_python_candidate() == (None, None)


def test_kicad_python_discovery_prefers_known_install_path_without_probe(monkeypatch) -> None:
    monkeypatch.delenv("CIRCUIT_WEAVER_KICAD_PYTHON", raising=False)
    monkeypatch.delenv("KICAD_PYTHON", raising=False)
    known_path = "C:/Program Files/KiCad/10.0/bin/python.exe"
    monkeypatch.setattr(
        runtime_info_module.shutil,
        "which",
        lambda candidate: known_path if candidate == known_path else None,
    )
    monkeypatch.setattr(Path, "is_file", lambda _path: False)
    monkeypatch.setattr(
        runtime_info_module.importlib.util,
        "find_spec",
        lambda _name: (_ for _ in ()).throw(AssertionError("must not probe current Python when KiCad path exists")),
    )

    path, source = runtime_info_module._kicad_python_candidate()

    assert path == str(Path(known_path).resolve())
    assert source == "known_install_path"


def test_known_legacy_codex_skill_moves_to_canonical_agents_path_safely(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source_skill = source / "demo"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("new canonical skill\n", encoding="utf-8")
    (source_skill / "guide.md").write_text("unchanged support file\n", encoding="utf-8")
    legacy = tmp_path / ".codex" / "skills" / "demo"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("known old skill\n", encoding="utf-8")
    (legacy / "guide.md").write_text("unchanged support file\n", encoding="utf-8")
    old_hash = skill_installer._file_hash(legacy / "SKILL.md")
    monkeypatch.setitem(skill_installer._LEGACY_PRISTINE_SKILL_MD_HASHES, "demo", frozenset({old_hash}))
    destination_root = tmp_path / ".agents" / "skills"

    outcome = skill_installer._copy_skill(
        source,
        destination_root,
        "demo",
        legacy_source_dir=legacy,
    )

    assert outcome["status"] == "installed"
    assert not legacy.exists()
    assert (destination_root / "demo" / "SKILL.md").read_text(encoding="utf-8") == "new canonical skill\n"
    manifest = json.loads((destination_root / "demo" / skill_installer._MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["schema"] == 2


def test_legacy_codex_skill_with_custom_file_is_not_moved_or_overwritten(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source_skill = source / "demo"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("new canonical skill\n", encoding="utf-8")
    (source_skill / "guide.md").write_text("canonical support file\n", encoding="utf-8")
    legacy = tmp_path / ".codex" / "skills" / "demo"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("known old skill\n", encoding="utf-8")
    (legacy / "guide.md").write_text("user customization\n", encoding="utf-8")
    old_hash = skill_installer._file_hash(legacy / "SKILL.md")
    monkeypatch.setitem(skill_installer._LEGACY_PRISTINE_SKILL_MD_HASHES, "demo", frozenset({old_hash}))
    destination_root = tmp_path / ".agents" / "skills"

    outcome = skill_installer._copy_skill(
        source,
        destination_root,
        "demo",
        legacy_source_dir=legacy,
    )

    assert outcome["status"] == "conflict"
    assert legacy.is_dir()
    assert (legacy / "guide.md").read_text(encoding="utf-8") == "user customization\n"
    assert not (destination_root / "demo").exists()
