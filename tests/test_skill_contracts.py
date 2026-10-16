"""Release contracts for cross-agent skill discovery and workflow recipes."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOTS = (
    REPO_ROOT / "skills",
    REPO_ROOT / ".agents" / "skills",
    REPO_ROOT / "project-skills",
)
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: missing opening frontmatter delimiter"
    try:
        raw = text.split("---\n", 2)[1]
    except IndexError as exc:  # pragma: no cover - assertion message is the useful result
        raise AssertionError(f"{path}: missing closing frontmatter delimiter") from exc
    payload = yaml.safe_load(raw)
    assert isinstance(payload, dict), f"{path}: frontmatter must be a mapping"
    return payload


def _skill_files() -> list[Path]:
    return sorted(root / child.name / "SKILL.md" for root in SKILL_ROOTS for child in root.iterdir() if child.is_dir())


@pytest.mark.parametrize("skill_file", _skill_files(), ids=lambda path: str(path.parent.relative_to(REPO_ROOT)))
def test_skill_frontmatter_is_portable(skill_file: Path) -> None:
    assert skill_file.is_file(), f"{skill_file.parent}: skill directory has no SKILL.md"
    metadata = _frontmatter(skill_file)
    assert set(metadata) == {"name", "description"}, f"{skill_file}: only name and description are portable"
    name = metadata["name"]
    description = metadata["description"]
    assert isinstance(name, str) and NAME_PATTERN.fullmatch(name), f"{skill_file}: invalid skill name {name!r}"
    assert len(name) <= 64
    assert name == skill_file.parent.name, f"{skill_file}: name must match directory"
    assert isinstance(description, str) and description.strip()
    assert len(description) <= 1024, f"{skill_file}: description exceeds OpenCode's 1024-character limit"


def test_repository_shims_reference_existing_canonical_skills() -> None:
    for shim in sorted((REPO_ROOT / ".agents" / "skills").glob("*/SKILL.md")):
        text = shim.read_text(encoding="utf-8")
        references = re.findall(r"`((?:skills|project-skills)/[^`]+/SKILL\.md)`", text)
        assert references, f"{shim}: compatibility shim does not name its canonical skill"
        for relative in references:
            assert (REPO_ROOT / relative).is_file(), f"{shim}: missing canonical target {relative}"


def test_circuit_weaver_recipe_matches_cli_contract() -> None:
    text = (REPO_ROOT / "skills" / "circuit-weaver" / "SKILL.md").read_text(encoding="utf-8")
    assert "scaffold --template" in text
    assert "--name \"${PROJECT_NAME}\"" not in text
    assert "--mcu" not in text
    assert "--power-converter" not in text
    assert "placement.json" in text
    assert "placement_result.json" in text
    assert "placement_review_context.json" in text
    assert "placement_editor.html" in text
    assert "artifact_manifest.json" in text
    assert "_placement.kicad_pcb" not in text
    assert "--svg-placement" not in text
    assert "--allow-partial" in text
    assert "import-design" in text
    assert "analyze-design" in text
    assert "status <project>" in text
    assert "resume <project>" in text
    assert "export-dual-cpl design.yaml --pcb board.kicad_pcb" in text
    assert "--require-kicad" in text
    assert "--routed-board" in text
    assert not re.search(r"autoroute[^\n]*_placement\.kicad_pcb", text)
    assert not re.search(r"(?:check-dfm|export-gerbers)[^\n]*_placement\.kicad_pcb", text)


def test_circuit_weaver_sample_route_works_for_pypi_installs() -> None:
    text = (REPO_ROOT / "skills" / "circuit-weaver" / "SKILL.md").read_text(encoding="utf-8")

    assert "importlib.resources" in text
    assert "joinpath('examples','iot_sensor.yaml')" in text
    assert "full source-checkout gallery is bundled in the wheel" in text
    assert "Which sample would you like to start from? [1-13]" not in text
    assert "cp -r samples/<sample>/ ~/<user-chosen-name>/" not in text
    assert "do not assume a POSIX shell command" in text


def test_confidence_score_does_not_claim_manufacturing_readiness() -> None:
    text = (REPO_ROOT / "skills" / "circuit-weaver" / "SKILL.md").read_text(encoding="utf-8")

    assert "manufacturing-readiness \"${PROJECT_NAME}\"" in text
    assert "manufacturing readiness authority" in text
    assert re.search(r"Do not infer\s+`fabrication_ready` from a confidence score", text)
    assert "ready_for_fab** (80+, no blockers): Design is ready to order" not in text


def test_agent_platform_docs_describe_optional_mcp_boundary() -> None:
    text = (REPO_ROOT / "docs" / "agent-platforms.md").read_text(encoding="utf-8")

    for tool in (
        "validate_design",
        "generate_artifacts",
        "discover_projects",
        "research_component",
        "manufacturing_readiness",
    ):
        assert tool in text
    assert 'pip install "circuit-weaver[mcp]"' in text
    assert "Installing skills does not configure an MCP client." in text


def test_design_wizard_does_not_route_preview_board() -> None:
    text = (REPO_ROOT / "skills" / "design-wizard" / "SKILL.md").read_text(encoding="utf-8")
    assert "artifact_manifest" in text
    assert "placement.json" in text
    assert "placement_result.json" in text
    assert "placement_review_context.json" in text
    assert "_placement.kicad_pcb" not in text
    assert "--svg-placement" not in text
    assert "--allow-partial" in text
    assert "--routed-board" in text
    assert not re.search(r"autoroute[^\n]*_placement\.kicad_pcb", text)


def test_packaged_skill_mirrors_are_byte_identical() -> None:
    source_root = REPO_ROOT / "skills"
    bundled_root = REPO_ROOT / "src" / "circuit_weaver" / "_bundled_skills"

    def mirrored_files(root: Path) -> dict[Path, Path]:
        return {
            path.relative_to(root): path
            for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        }

    source_files = mirrored_files(source_root)
    bundled_files = mirrored_files(bundled_root)
    assert source_files.keys() == bundled_files.keys()
    for relative, source in sorted(source_files.items()):
        assert source.read_bytes() == bundled_files[relative].read_bytes(), f"stale packaged mirror: {relative}"


def test_bundled_orchestrators_do_not_invoke_unbundled_project_skills() -> None:
    orchestrators = "\n".join(
        (REPO_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        for name in ("circuit-weaver", "design-wizard")
    )
    unbundled = ("sim", "autoroute", "kicad-gen", "kicad-hierarchy", "kicad-pcb-place", "kicad-pinmap")
    for name in unbundled:
        assert f"${name}" not in orchestrators
        assert not re.search(rf"(?<![\w.-])/{re.escape(name)}(?=[`\s.,)])", orchestrators)
    assert re.search(r"not\s+(?:included by PyPI `install-skills`|installed from PyPI)", orchestrators)
    assert re.search(r"verifying\s+its `SKILL\.md`\s+exists", orchestrators)


def test_project_skill_recipes_do_not_assume_unshipped_scripts() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "project-skills").glob("*/SKILL.md")
    )
    assert "python3 scripts/generate_placement.py" not in combined
    assert "python3 scripts/generate_schematics.py" not in combined
    assert "scripts/audit_pinmap.py <ref>" not in combined
    assert "python3 scripts/validate_pinmaps.py" not in combined


def test_autoroute_project_skill_uses_validated_specctra_contract() -> None:
    text = (REPO_ROOT / "project-skills" / "autoroute" / "SKILL.md").read_text(encoding="utf-8")
    assert "circuit-weaver autoroute board.kicad_pcb" in text
    assert "--output board.ses" in text
    assert "status: partial" in text
    assert "java -jar" not in text
    assert "--routed-board" in text


def test_kicad_skill_uses_durable_multi_artifact_analysis_flow() -> None:
    text = (REPO_ROOT / "skills" / "kicad" / "SKILL.md").read_text(encoding="utf-8")
    assert 'import-design "${SOURCE_PATH}" --analyze' in text
    assert 'analyze-design "${PROJECT_ROOT}"' in text
    assert 'status "${PROJECT_ROOT}"' in text
    assert 'resume "${PROJECT_ROOT}"' in text
    assert ".circuit-weaver/analysis/index.json" in text
    assert "Gerber" in text and "X2" in text and "unknown" in text


def test_readme_uses_current_platform_and_cli_contracts() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "$circuit-weaver" in text
    assert "~/.agents/skills" in text
    assert "~/.codex/skills" not in text
    assert "import-placement placement.svg board.kicad_pcb -o out.kicad_pcb" in text
    assert "import-design ./legacy_board --analyze" in text
    assert "placement_review_context.json" in text
    assert "--allow-partial" in text
    assert "export-dual-cpl design.yaml --pcb electrical_board.kicad_pcb" in text
    assert "export-gerbers design.yaml" not in text
    assert "check-dfm design.yaml" not in text
    assert "panelize design.yaml" not in text
    assert "annotates the schematic with TP labels" not in text
    assert "importlib.resources" in text
    assert "iot_sensor_example/design.yaml" in text


def test_cli_reference_does_not_advertise_removed_or_unsafe_contracts() -> None:
    text = (REPO_ROOT / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    assert "import-design <source>" in text
    assert "analyze-design <project>" in text
    assert "resume <project>" in text
    assert "placement_review_context.json" in text
    assert "--allow-partial" in text
    assert "export-dual-cpl <spec.yaml> --pcb <board.kicad_pcb>" in text
    assert "--require-kicad" in text
    assert "--layers" not in text
    assert "_routed.kicad_pcb" not in text
    assert "falls back to routing the `.kicad_pcb` directly" not in text


@pytest.mark.skip_category("platform")
@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="native bash is not installed")
def test_bash_installer_parses() -> None:
    subprocess.run(["bash", "-n", str(REPO_ROOT / "install.sh")], check=True)
