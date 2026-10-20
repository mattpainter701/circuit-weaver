from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import circuit_weaver


def test_package_import_exposes_version():
    assert circuit_weaver.__version__ == "0.37.0"
    assert circuit_weaver.__version__ == version("circuit-weaver")


def test_cli_reports_version():
    result = subprocess.run(
        [sys.executable, "-m", "circuit_weaver", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == circuit_weaver.__version__


def test_validate_command_accepts_example_spec():
    example = Path(__file__).resolve().parent.parent / "src" / "circuit_weaver" / "examples" / "iot_sensor.yaml"

    result = subprocess.run(
        [sys.executable, "-m", "circuit_weaver", "validate", str(example)],
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["metadata"]["project"] == "IoT_Sensor"
    # No structural or electrical errors (warnings are OK; KiCad CLI may not be installed)
    structural_errors = [m for m in payload["categories"].get("structural", []) if m["level"] == "error"]
    electrical_errors = [m for m in payload["categories"].get("electrical", []) if m["level"] == "error"]
    assert not structural_errors, f"Structural errors: {structural_errors}"
    assert not electrical_errors, f"Electrical errors: {electrical_errors}"


def test_generate_command_writes_example_artifacts(tmp_path: Path):
    example = Path(__file__).resolve().parent.parent / "src" / "circuit_weaver" / "examples" / "iot_sensor.yaml"
    output_dir = tmp_path / "out"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "circuit_weaver",
            "generate",
            str(example),
            "--output",
            str(output_dir),
            "--no-svg",
            "--no-require-valid",
            "--no-readiness-gate",
        ],
        capture_output=True,
        text=True,
    )

    # Generate should produce artifacts even when KiCad CLI is unavailable
    assert (output_dir / "IoT_Sensor.kicad_sch").exists(), f"No schematic generated. stderr: {result.stderr[:500]}"
    assert not (output_dir / "main.kicad_sch").exists()
    assert (output_dir / "canonical_spec.yaml").exists()
    assert (output_dir / "design_ir.json").exists()


def test_export_jlcpcb_command_writes_csv_files(tmp_path: Path):
    sample = Path(__file__).resolve().parent.parent / "samples" / "iot_sensor_node" / "iot_sensor_node.yaml"
    output_dir = tmp_path / "jlcpcb_export"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "circuit_weaver",
            "export-jlcpcb",
            str(sample),
            "--output",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr[:500]

    # A CPL is never synthesized without a reconciled physical PCB.
    assert (output_dir / "bom_jlcpcb.csv").exists(), f"BOM file not created. stderr: {result.stderr[:500]}"
    assert not (output_dir / "cpl_jlcpcb.csv").exists()
    assert (output_dir / "assembly_manifest.json").exists()
    assert (output_dir / "delivery_manifest.json").exists()
    assert (output_dir / "README_jlcpcb.txt").exists(), f"README not created. stderr: {result.stderr[:500]}"

    # BOM should have correct column headers
    bom_content = (output_dir / "bom_jlcpcb.csv").read_text()
    assert "Comment,Designator,Footprint,LCSC Part#" in bom_content

    # At least one row should exist (header + 1 data row minimum)
    bom_lines = bom_content.strip().split("\n")
    assert len(bom_lines) >= 2, f"BOM should have header + data rows. Got: {bom_lines}"

    # Verify command status
    payload = json.loads(result.stdout)
    assert payload["status"] == "bom_only"
    assert payload["cpl"] == ""
    assert any("provide a real" in reason for reason in payload["blocked_reasons"])
    assert payload["component_count"] > 0
    assert payload["bom_rows"] > 0
