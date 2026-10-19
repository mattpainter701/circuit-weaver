"""Distribution-level contracts that must hold for every PyPI release."""

from __future__ import annotations

from importlib import resources
from importlib.metadata import entry_points, metadata, version
from pathlib import Path

import circuit_weaver
from circuit_weaver.project_spec import _parse_yaml


def test_distribution_version_and_core_dependencies() -> None:
    package_metadata = metadata("circuit-weaver")
    requirements = package_metadata.get_all("Requires-Dist") or []

    assert version("circuit-weaver") == circuit_weaver.__version__ == "0.36.0"
    assert any(
        requirement.lower().startswith("pyyaml") and "extra ==" not in requirement
        for requirement in requirements
    )


def test_distribution_exposes_supported_extras_and_commands() -> None:
    package_metadata = metadata("circuit-weaver")
    requirements = [requirement.lower() for requirement in package_metadata.get_all("Requires-Dist") or []]
    extras = set(package_metadata.get_all("Provides-Extra") or [])
    commands = {entry_point.name for entry_point in entry_points(group="console_scripts")}

    assert {"api", "mcp", "yaml", "lookup", "pdf", "test", "dev", "all"} <= extras
    assert any(requirement.startswith("mcp") and 'extra == "mcp"' in requirement for requirement in requirements)
    assert any(requirement.startswith("fastapi") and 'extra == "api"' in requirement for requirement in requirements)
    assert {"circuit-weaver", "circuit-weaver-mcp"} <= commands


def test_default_install_parses_nested_yaml(tmp_path: Path) -> None:
    spec_path = tmp_path / "design.yaml"
    spec_path.write_text(
        """\
metadata:
  project: nested-yaml-contract
blocks:
  - id: U1
    type: mcu
    params:
      interfaces:
        i2c:
          enabled: true
""",
        encoding="utf-8",
    )

    parsed = _parse_yaml(spec_path)

    assert parsed["blocks"][0]["params"]["interfaces"]["i2c"]["enabled"] is True


def test_distribution_includes_packaged_iot_example() -> None:
    example = resources.files("circuit_weaver").joinpath("examples", "iot_sensor.yaml")

    assert example.is_file()
    with resources.as_file(example) as example_path:
        parsed = _parse_yaml(example_path)

    assert parsed["project"] == "IoT_Sensor"
    assert parsed["power"]


def test_release_workflow_rejects_tag_version_mismatch() -> None:
    release_workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    workflow_text = release_workflow.read_text(encoding="utf-8")

    assert "GITHUB_REF_NAME" in workflow_text
    assert "Tag/version mismatch" in workflow_text
    assert "twine check" in workflow_text
    assert 'kicad-major: "8"' in workflow_text
    assert 'kicad-major: "9"' in workflow_text
    assert 'kicad-major: "10"' in workflow_text
    assert "circuit_weaver/examples/iot_sensor.yaml" in workflow_text
    assert "__pycache__" in workflow_text


def test_kicad_workflows_use_pinned_official_containers_and_native_runtime_checks() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    runtime_script = repo_root / "scripts" / "prepare_kicad_ci_container.sh"
    runtime_text = runtime_script.read_text(encoding="utf-8")
    expected_images = {
        "8": "ghcr.io/kicad/kicad:8.0@sha256:5716d82aa247abdbe512650eb4e8fce5a4031cbbef0a2c8df83208a69fef87b2",
        "9": "ghcr.io/kicad/kicad:9.0@sha256:e638b79b0321f29395a5b783e94bb9f3c73303e8da15da27b8f5cb4b67a37729",
        "10": "ghcr.io/kicad/kicad:10.0-full@sha256:347d5b7d5d88207ba4217cdf594eeccd142279600d023e7a7e426f8599c77708",
    }

    assert "kicad-cli version" in runtime_text
    assert "import pcbnew" in runtime_text
    assert "pcbnew.BOARD()" in runtime_text
    assert "/usr/share/kicad/template/fp-lib-table" in runtime_text
    assert '"$HOME/.config/kicad/${expected_major}.0/fp-lib-table"' in runtime_text
    for required_model in (
        "Capacitor_SMD.3dshapes/C_0402_1005Metric.step",
        "Package_TO_SOT_SMD.3dshapes/SOT-223.step",
        "Connector_USB.3dshapes/USB_C_Receptacle_GCT_USB4085.step",
    ):
        assert required_model in runtime_text
    assert "model_dir=/usr/share/kicad/3dmodels" in runtime_text
    assert "KICAD10_3DMODEL_DIR=%s\\n" in runtime_text
    assert '>> "$GITHUB_ENV"' in runtime_text

    for workflow_name in ("ci.yml", "release.yml"):
        workflow = repo_root / ".github" / "workflows" / workflow_name
        workflow_text = workflow.read_text(encoding="utf-8")

        assert "options: --user 0" in workflow_text
        assert "apt-get update -o Acquire::Retries=3 -o APT::Update::Error-Mode=any" in workflow_text
        assert "python3-venv curl git ca-certificates" in workflow_text
        assert "bash scripts/prepare_kicad_ci_container.sh" in workflow_text
        for image in expected_images.values():
            assert image in workflow_text
        install_at = workflow_text.index("bash scripts/prepare_kicad_ci_container.sh")
        generate_at = workflow_text.index("python -m circuit_weaver generate")

        assert install_at < generate_at
