"""Adversarial tests for transactional generated-artifact ownership."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from circuit_weaver import dispatcher, project_state
from circuit_weaver.project_state import (
    get_project_state_summary,
    load_project_state,
    record_generation_state,
)

pytestmark = pytest.mark.skip_category("platform")


def _seed_owned_output(project: Path) -> tuple[Path, Path, Path, Path]:
    project.mkdir()
    spec = project / "design.yaml"
    spec.write_text("project: Transactional\nblocks: []\n", encoding="utf-8")
    output = project / "output"
    output.mkdir()
    root = output / "Transactional.kicad_sch"
    stale_sheet = output / "obsolete_power.kicad_sch"
    stale_mcu = output / "sdkconfig.defaults"
    root.write_text("(kicad_sch old-root)", encoding="utf-8")
    stale_sheet.write_text("(kicad_sch stale-sheet)", encoding="utf-8")
    stale_mcu.write_text("CONFIG_OLD_BOARD=y\n", encoding="utf-8")
    record_generation_state(
        output,
        project_name="Transactional",
        spec_path=spec,
        output_dir=output,
        phase="generated",
        artifacts=[root, stale_sheet, stale_mcu],
    )
    return spec, output, root, stale_sheet


def _successful_staged_generation(_spec: dict, *, output_dir: str | Path, **_kwargs) -> dict:
    output = Path(output_dir)
    root = output / "Transactional.kicad_sch"
    validation = output / "validation_report.json"
    root.write_text("(kicad_sch new-root)", encoding="utf-8")
    validation.write_text(json.dumps({"valid": True}), encoding="utf-8")
    # Deliberately return an incomplete legacy list: the public transaction
    # must inventory the complete staging tree instead of trusting it.
    return {
        "output_dir": str(output),
        "project": "Transactional",
        "root_schematic": str(root),
        "files": [str(root)],
        "validation_report": str(validation),
        "valid": True,
    }


def test_success_removes_only_obsolete_owned_files_and_records_complete_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, output, root, stale_sheet = _seed_owned_output(tmp_path / "project")
    stale_mcu = output / "sdkconfig.defaults"
    user_file = output / "customer-notes.txt"
    user_file.write_text("do not touch\n", encoding="utf-8")
    monkeypatch.setattr(dispatcher, "_generate_artifacts_in_place", _successful_staged_generation)

    result = dispatcher.generate_artifacts(
        {"project": "Transactional", "blocks": []},
        output_dir=output,
        spec_path=spec,
    )

    validation = output / "validation_report.json"
    assert root.read_text(encoding="utf-8") == "(kicad_sch new-root)"
    assert not stale_sheet.exists()
    assert not stale_mcu.exists()
    assert user_file.read_text(encoding="utf-8") == "do not touch\n"
    assert set(result["files"]) == {str(root), str(validation)}
    assert Path(result["root_schematic"]) == root
    assert Path(result["validation_report"]) == validation

    state = load_project_state(output)
    assert state is not None
    assert {Path(record["path"]).name for record in state.artifacts} == {
        "Transactional.kicad_sch",
        "validation_report.json",
    }
    summary = get_project_state_summary(output)
    assert summary["status"] == "generated"
    assert summary["reconciliation"]["dirty"] is False


def test_failed_staged_generation_preserves_prior_owned_and_unowned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, output, root, stale_sheet = _seed_owned_output(tmp_path / "project")
    user_file = output / "customer-notes.txt"
    user_file.write_text("keep me\n", encoding="utf-8")

    def fail_after_partial_write(_spec: dict, *, output_dir: str | Path, **_kwargs):
        staging = Path(output_dir)
        (staging / "partial.kicad_sch").write_text("partial", encoding="utf-8")
        (staging / "circuit-weaver.log").write_text(
            "unique failed-run diagnostic marker\n", encoding="utf-8"
        )
        raise RuntimeError("simulated generator failure")

    monkeypatch.setattr(dispatcher, "_generate_artifacts_in_place", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="simulated generator failure"):
        dispatcher.generate_artifacts(
            {"project": "Transactional", "blocks": []},
            output_dir=output,
            spec_path=spec,
        )

    assert root.read_text(encoding="utf-8") == "(kicad_sch old-root)"
    assert stale_sheet.read_text(encoding="utf-8") == "(kicad_sch stale-sheet)"
    assert (output / "sdkconfig.defaults").is_file()
    assert user_file.read_text(encoding="utf-8") == "keep me\n"
    assert not (output / "partial.kicad_sch").exists()
    assert (output / "circuit-weaver.log").read_text(encoding="utf-8").count(
        "unique failed-run diagnostic marker"
    ) == 1
    state = load_project_state(output)
    assert state is not None
    assert state.status == "generation_failed"
    assert {Path(record["path"]).name for record in state.artifacts} == {
        "Transactional.kicad_sch",
        "obsolete_power.kicad_sch",
        "sdkconfig.defaults",
    }


def test_successful_staging_refuses_to_overwrite_unowned_path_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, output, root, stale_sheet = _seed_owned_output(tmp_path / "project")
    collision = output / "validation_report.json"
    collision.write_text('{"customer_owned": true}\n', encoding="utf-8")
    monkeypatch.setattr(dispatcher, "_generate_artifacts_in_place", _successful_staged_generation)

    with pytest.raises(FileExistsError, match="not recorded as Circuit Weaver-owned"):
        dispatcher.generate_artifacts(
            {"project": "Transactional", "blocks": []},
            output_dir=output,
            spec_path=spec,
        )

    assert collision.read_text(encoding="utf-8") == '{"customer_owned": true}\n'
    assert root.read_text(encoding="utf-8") == "(kicad_sch old-root)"
    assert stale_sheet.is_file()


def test_publication_rejects_nested_live_parent_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, output, root, stale_sheet = _seed_owned_output(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    try:
        (output / "reports").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    def generate_nested_report(_spec: dict, *, output_dir: str | Path, **_kwargs) -> dict:
        staging = Path(output_dir)
        report = staging / "reports" / "validation.json"
        report.parent.mkdir()
        report.write_text('{"valid": true}\n', encoding="utf-8")
        return {"output_dir": str(staging), "project": "Transactional", "files": [str(report)]}

    monkeypatch.setattr(dispatcher, "_generate_artifacts_in_place", generate_nested_report)

    with pytest.raises(ValueError, match="Refusing to write output outside"):
        dispatcher.generate_artifacts(
            {"project": "Transactional", "blocks": []},
            output_dir=output,
            spec_path=spec,
        )

    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    assert not (outside / "validation.json").exists()
    assert root.read_text(encoding="utf-8") == "(kicad_sch old-root)"
    assert stale_sheet.is_file()


def test_failed_state_commit_rolls_back_published_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, output, root, stale_sheet = _seed_owned_output(tmp_path / "project")
    monkeypatch.setattr(dispatcher, "_generate_artifacts_in_place", _successful_staged_generation)
    real_record = project_state.record_generation_state

    def fail_generated_commit(*args, **kwargs):
        if kwargs.get("phase") == "generated":
            raise OSError("simulated state commit failure")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(project_state, "record_generation_state", fail_generated_commit)

    with pytest.raises(OSError, match="state commit failure"):
        dispatcher.generate_artifacts(
            {"project": "Transactional", "blocks": []},
            output_dir=output,
            spec_path=spec,
        )

    assert root.read_text(encoding="utf-8") == "(kicad_sch old-root)"
    assert stale_sheet.read_text(encoding="utf-8") == "(kicad_sch stale-sheet)"
    assert not (output / "validation_report.json").exists()
    state = load_project_state(output)
    assert state is not None
    assert state.status == "generation_failed"


def test_success_merges_staging_diagnostics_into_retained_live_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, output, _root, _stale_sheet = _seed_owned_output(tmp_path / "project")
    live_log = output / "circuit-weaver.log"
    live_log.write_text("prior diagnostic\n", encoding="utf-8")

    def generate_with_unique_log(spec_payload: dict, *, output_dir: str | Path, **kwargs):
        result = _successful_staged_generation(spec_payload, output_dir=output_dir, **kwargs)
        (Path(output_dir) / "circuit-weaver.log").write_text(
            "unique current-run diagnostic marker\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(dispatcher, "_generate_artifacts_in_place", generate_with_unique_log)

    result = dispatcher.generate_artifacts(
        {"project": "Transactional", "blocks": []},
        output_dir=output,
        spec_path=spec,
    )

    content = live_log.read_text(encoding="utf-8")
    assert "prior diagnostic" in content
    assert content.count("unique current-run diagnostic marker") == 1
    assert str(live_log) in result["files"]
    state = load_project_state(output)
    assert state is not None
    log_record = next(record for record in state.artifacts if Path(record["path"]).name == live_log.name)
    assert log_record["kind"] == "generation_log"
    assert log_record["reconciliation_policy"] == "append_only"


def test_transaction_preserves_relative_public_result_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _output, _root, _stale_sheet = _seed_owned_output(tmp_path / "project")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dispatcher, "_generate_artifacts_in_place", _successful_staged_generation)

    result = dispatcher.generate_artifacts(
        {"project": "Transactional", "blocks": []},
        output_dir=Path("project") / "output",
        spec_path=spec,
    )

    assert result["output_dir"] == str(Path("project") / "output")
    assert result["root_schematic"] == str(
        Path("project") / "output" / "Transactional.kicad_sch"
    )
    assert all(not Path(path).is_absolute() for path in result["files"])


def test_deep_output_keeps_native_staging_and_replacement_paths_below_max_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final path that fits Windows must not fail from verbose internal names."""
    filename = "UnseenScale_Multichannel_Analog_Setpoint_Card.kicad_pro"
    # Reproduce the failing workflow's 235-character published project path.
    padding = 235 - len(str(tmp_path / "generated" / filename)) - 1
    assert padding > 0
    parent = tmp_path / ("x" * padding)
    output = parent / "generated"
    observed: list[Path] = []

    def generate(_spec: dict, *, output_dir: str | Path, **_kwargs) -> dict:
        path = Path(output_dir) / filename
        observed.append(path)
        assert len(str(path)) < 260
        path.write_text("native project", encoding="utf-8")
        return {"output_dir": str(output_dir), "project": "Deep", "files": [str(path)]}

    original_replace = dispatcher.os.replace

    def replace(source, destination, *args, **kwargs):
        for candidate in (Path(source), Path(destination)):
            if candidate.name == filename:
                observed.append(candidate)
                assert len(str(candidate)) < 260
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(dispatcher, "_generate_artifacts_in_place", generate)
    monkeypatch.setattr(dispatcher.os, "replace", replace)
    # The second generation exercises backup and replacement of owned output.
    for _ in range(2):
        dispatcher.generate_artifacts({"project": "Deep", "blocks": []}, output_dir=output)
    assert (output / filename).read_text(encoding="utf-8") == "native project"
    assert any(path.parent.name.startswith(".cw-b-") for path in observed)
    assert any(path.parent.name.startswith(".cw-s-") for path in observed)
