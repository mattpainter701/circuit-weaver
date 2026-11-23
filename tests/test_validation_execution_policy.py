"""Execution-scope controls for design validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from circuit_weaver import dispatcher
from circuit_weaver.project_spec import _parse_yaml

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"


def _basic_spec() -> dict:
    return _parse_yaml(SAMPLES_DIR / "led_power_indicator" / "led_power_indicator.yaml")


def test_native_export_disabled_keeps_pure_artifact_and_determinism_checks(monkeypatch) -> None:
    original = dispatcher._generate_compiled_artifacts
    generated = []

    def capture_generation(*args, **kwargs):
        result = original(*args, **kwargs)
        generated.append((kwargs.get("export_svg"), result))
        return result

    def forbidden_native_call(*_args, **_kwargs):
        raise AssertionError("native export discovery or subprocess must not run")

    monkeypatch.setattr(dispatcher, "_generate_compiled_artifacts", capture_generation)
    monkeypatch.setattr(dispatcher, "_kicad_cli_path", forbidden_native_call)
    monkeypatch.setattr(dispatcher.subprocess, "run", forbidden_native_call)

    report = dispatcher.validate_design(
        _basic_spec(),
        use_kicad=False,
        enrich_parts=False,
        check_native_export=False,
        check_determinism=True,
    )

    assert len(generated) == 2
    assert [export_svg for export_svg, _ in generated] == [False, False]
    for _export_svg, (files, root_schematic) in generated:
        assert root_schematic is not None
        assert any(Path(path).suffix == ".kicad_sch" for path in files)
    assert report.metadata["native_export"]["status"] == "not_run"
    assert report.metadata["native_export"]["scope"] == "KiCad CLI schematic SVG export"
    assert not any(
        message.code in {"kicad-export-failed", "artifact-generation-failed"}
        for messages in report.categories.values()
        for message in messages
    )


def test_default_native_export_is_still_requested_and_failures_still_fail_validation(monkeypatch) -> None:
    commands = []

    def fail_export(command, **_kwargs):
        commands.append(command)
        raise dispatcher.subprocess.CalledProcessError(1, command, stderr="sentinel native export failure")

    monkeypatch.setattr(dispatcher, "_kicad_cli_path", lambda: Path("fake-kicad-cli"))
    monkeypatch.setattr(dispatcher.subprocess, "run", fail_export)

    report = dispatcher.validate_design(
        _basic_spec(),
        use_kicad=False,
        enrich_parts=False,
        check_determinism=False,
    )

    assert len(commands) == 1
    assert commands[0][1:4] == ["sch", "export", "svg"]
    assert report.valid is False
    assert report.metadata["native_export"]["status"] == "failed"
    assert "reason" not in report.metadata["native_export"]
    assert any(message.code == "kicad-export-failed" for message in report.categories["implementation"])


def test_successful_default_export_requires_nonempty_svg_and_clears_not_reached_reason(monkeypatch) -> None:
    commands = []

    def successful_export(command, **_kwargs):
        commands.append(command)
        svg_dir = Path(command[command.index("-o") + 1])
        (svg_dir / "root.svg").write_text("<svg></svg>", encoding="utf-8")
        return dispatcher.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dispatcher, "_kicad_cli_path", lambda: Path("fake-kicad-cli"))
    monkeypatch.setattr(dispatcher.subprocess, "run", successful_export)

    report = dispatcher.validate_design(
        _basic_spec(),
        use_kicad=False,
        enrich_parts=False,
        check_determinism=False,
    )

    assert len(commands) == 1
    assert report.metadata["native_export"]["status"] == "passed"
    assert report.metadata["native_export"]["svg_count"] == 1
    assert "reason" not in report.metadata["native_export"]
    assert "error" not in report.metadata["native_export"]


def test_successful_process_without_svg_does_not_claim_native_export_passed(monkeypatch) -> None:
    def empty_success(command, **_kwargs):
        return dispatcher.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dispatcher, "_kicad_cli_path", lambda: Path("fake-kicad-cli"))
    monkeypatch.setattr(dispatcher.subprocess, "run", empty_success)

    report = dispatcher.validate_design(
        _basic_spec(),
        use_kicad=False,
        enrich_parts=False,
        check_determinism=False,
    )

    assert report.valid is False
    assert report.metadata["native_export"]["status"] == "failed"
    assert "no nonempty SVG" in report.metadata["native_export"]["error"]
    assert any(message.code == "kicad-export-failed" for message in report.categories["implementation"])


def test_missing_cli_reports_unverified_with_specific_reason(monkeypatch) -> None:
    def forbidden_subprocess(*_args, **_kwargs):
        raise AssertionError("no KiCad CLI should be invoked when discovery returns none")

    monkeypatch.setattr(dispatcher, "_kicad_cli_path", lambda: None)
    monkeypatch.setattr(dispatcher.subprocess, "run", forbidden_subprocess)

    report = dispatcher.validate_design(
        _basic_spec(),
        use_kicad=False,
        enrich_parts=False,
        check_determinism=False,
    )

    assert report.metadata["native_export"]["status"] == "unverified"
    assert report.metadata["native_export"]["reason"] == "KiCad CLI was not found"
    assert report.metadata["native_export"]["scope"] == "KiCad CLI schematic SVG export"


@pytest.mark.parametrize("value", [1, None, "false"])
def test_native_export_policy_rejects_non_boolean_values(value) -> None:
    with pytest.raises(TypeError, match="check_native_export must be a bool"):
        dispatcher.validate_design(
            {},
            check_native_export=value,
        )
