"""Regression tests for the public programmatic workflow contracts."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import circuit_weaver.dispatcher as dispatcher
from circuit_weaver.component_db import ComponentDef
from circuit_weaver.dispatcher import ValidationMessage, ValidationReport, apply_design_patch
from circuit_weaver.erc_runner import ErcResult

pytestmark = pytest.mark.skip_category("platform")


def _valid_report() -> ValidationReport:
    return ValidationReport(
        profile="standard",
        valid=True,
        categories={},
        summary={"errors": 0, "warnings": 0},
        metadata={},
    )


def test_legacy_profile_alias_is_explicitly_deprecated() -> None:
    with pytest.warns(DeprecationWarning, match="mvp_strict"):
        assert dispatcher._ensure_profile("mvp_strict") == "standard"


def test_documented_add_patch_alias_adds_a_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "validate_design", lambda *_args, **_kwargs: _valid_report())
    spec = {"project": "patch-contract", "blocks": []}

    with pytest.warns(DeprecationWarning, match="upsert_blocks"):
        result = apply_design_patch(
            spec,
            {
                "add": [
                    {
                        "id": "power:U3",
                        "section": "power",
                        "kind": "component",
                        "ref": "U3",
                        "ic": "ADP1706",
                    }
                ]
            },
        )

    assert result["accepted"] is True
    assert [block["ref"] for block in result["updated_spec"]["blocks"]] == ["U3"]
    assert result["diff"]["added_blocks"]


def test_unknown_patch_operation_fails_instead_of_succeeding_empty() -> None:
    with pytest.raises(ValueError, match="Unsupported design patch operation.*ad"):
        apply_design_patch({"project": "patch-contract", "blocks": []}, {"ad": []})


def test_patch_alias_and_canonical_operation_cannot_be_mixed() -> None:
    with pytest.raises(ValueError, match="both 'add' and 'upsert_blocks'"):
        apply_design_patch(
            {"project": "patch-contract", "blocks": []},
            {"add": [], "upsert_blocks": []},
        )


def test_artifact_manifest_inventory_is_machine_readable(tmp_path: Path) -> None:
    root = tmp_path / "contract.kicad_sch"
    root.write_text("(kicad_sch)\n", encoding="utf-8")
    nested = tmp_path / "reports" / "validation.json"
    nested.parent.mkdir()
    nested.write_text("{}\n", encoding="utf-8")

    manifest_path = dispatcher._write_artifact_manifest(
        tmp_path,
        "contract",
        root,
        valid=True,
        kicad_verified=True,
        verification_status="verified",
        erc={
            "status": "ok",
            "schematic": str(root),
            "errors": 0,
            "warnings": 1,
            "skip_reason": "",
            "violations": [],
        },
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 2
    assert payload["root_schematic"] == "contract.kicad_sch"
    assert payload["valid"] is True
    assert payload["kicad_verified"] is True
    assert payload["verification_status"] == "verified"
    assert payload["erc"] == {
        "status": "ok",
        "schematic": "contract.kicad_sch",
        "errors": 0,
        "warnings": 1,
        "skip_reason": "",
        "violations": [],
    }
    assert {entry["relative_path"] for entry in payload["artifacts"]} == {
        "contract.kicad_sch",
        "reports/validation.json",
    }
    assert all(entry["path"] == entry["relative_path"] for entry in payload["artifacts"])
    assert str(tmp_path) not in manifest_path.read_text(encoding="utf-8")


class _StubIR:
    blocks: list = []

    def to_dict(self) -> dict:
        return {"project": "contract", "blocks": []}


class _StubCompiled:
    ir = _StubIR()
    metadata = {"project": "contract"}
    components: list = []
    repair_actions: list = []


def _stub_generation(output_path: Path) -> tuple[list[Path], Path]:
    root = output_path / "contract.kicad_sch"
    root.write_text("(kicad_sch)\n", encoding="utf-8")
    return [root], root


def _patch_generation_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    erc_result: ErcResult,
) -> list[Path | None]:
    from circuit_weaver import erc_runner, firmware_export, test_point_gen

    annotation_targets: list[Path | None] = []
    monkeypatch.setattr(dispatcher, "validate_design", lambda *_args, **_kwargs: _valid_report())
    monkeypatch.setattr(dispatcher, "compile_design_ir", lambda *_args, **_kwargs: _StubCompiled())

    def generate(_compiled, output_path: Path, **_kwargs):
        return _stub_generation(output_path)

    monkeypatch.setattr(dispatcher, "_generate_compiled_artifacts", generate)
    def generate_test_points(
        _ir,
        output_dir: Path,
        *,
        project_name: str,
        schematic_path=None,
        **_kwargs,
    ):
        annotation_targets.append(schematic_path)
        csv_path = output_dir / f"{project_name}_test_points.csv"
        csv_path.write_text("reference,net,x_mm,y_mm,side,notes\n", encoding="utf-8")
        return {
            "csv_path": str(csv_path),
            "test_point_count": 0,
            "test_points": [],
            "annotated_schematic": False,
        }

    monkeypatch.setattr(test_point_gen, "generate_test_point_artifacts", generate_test_points)
    monkeypatch.setattr(firmware_export, "is_mcu", lambda _component: False)
    monkeypatch.setattr(erc_runner, "run_erc", lambda _schematic: erc_result)
    return annotation_targets


def test_placement_svg_contains_compiled_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = ComponentDef(
        mpn="TEST-IC",
        source_ref="U1",
        value="TEST-IC",
        footprint="Package_QFN:QFN-16-1EP_3x3mm_P0.5mm",
        category="digital",
    )
    compiled = _StubCompiled()
    compiled.components = [component]
    _patch_generation_dependencies(
        monkeypatch,
        erc_result=ErcResult(status="skipped", skip_reason="not installed"),
    )
    monkeypatch.setattr(dispatcher, "compile_design_ir", lambda *_args, **_kwargs: compiled)

    result = dispatcher.generate_artifacts(
        {"project": "contract"},
        output_dir=tmp_path,
        svg_placement=True,
    )

    placement_path = Path(result["placement_svg"])
    assert str(placement_path) in result["files"]
    content = placement_path.read_text(encoding="utf-8")
    assert 'data-ref="U1"' in content
    assert "QFN-16" in content


def test_generate_inventory_uses_final_immutable_schematic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = _patch_generation_dependencies(
        monkeypatch,
        erc_result=ErcResult(status="skipped", skip_reason="not installed"),
    )

    result = dispatcher.generate_artifacts({"project": "contract"}, output_dir=tmp_path)

    assert targets == [None]
    assert result["verification_status"] == "unverified"
    assert result["kicad_verified"] is False
    assert Path(result["artifact_manifest"]).is_file()
    lock_path = tmp_path / dispatcher._GENERATION_LOCK_FILENAME
    assert lock_path.is_file()
    expected_files = {
        str(path)
        for path in tmp_path.rglob("*")
        if path.is_file()
        and path != lock_path
        and ".circuit-weaver" not in path.relative_to(tmp_path).parts
    }
    assert set(result["files"]) == expected_files
    assert str(lock_path) not in result["files"]
    manifest = json.loads(Path(result["artifact_manifest"]).read_text(encoding="utf-8"))
    assert dispatcher._GENERATION_LOCK_FILENAME not in {
        entry["path"] for entry in manifest["artifacts"]
    }


def test_distinct_output_generations_are_serialized_around_global_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_generation_dependencies(
        monkeypatch,
        erc_result=ErcResult(status="skipped", skip_reason="not installed"),
    )
    entered_a = threading.Event()
    attempted_b = threading.Event()
    entered_b = threading.Event()
    release_a = threading.Event()

    def validate(spec: dict, *_args, **_kwargs) -> ValidationReport:
        if spec["project"] == "run-a":
            entered_a.set()
            assert release_a.wait(timeout=5), "test did not release the first generation"
        else:
            entered_b.set()
        return _valid_report()

    def run_b() -> dict:
        attempted_b.set()
        return dispatcher.generate_artifacts(
            {"project": "run-b"},
            output_dir=tmp_path / "run-b",
        )

    monkeypatch.setattr(dispatcher, "validate_design", validate)
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(
            dispatcher.generate_artifacts,
            {"project": "run-a"},
            output_dir=tmp_path / "run-a",
        )
        assert entered_a.wait(timeout=5)
        future_b = executor.submit(run_b)
        assert attempted_b.wait(timeout=5)
        try:
            assert not entered_b.wait(timeout=0.25), (
                "a second output entered generation while the process-global logging context was active"
            )
        finally:
            release_a.set()
        result_a = future_a.result(timeout=5)
        result_b = future_b.result(timeout=5)

    assert entered_b.is_set()
    for result in (result_a, result_b):
        assert Path(result["artifact_manifest"]).is_file()
        assert all(not path.endswith(dispatcher._GENERATION_LOCK_FILENAME) for path in result["files"])


def test_reused_output_ignores_stale_artifacts_and_preserves_user_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_schematic = tmp_path / "stale_from_previous_run.kicad_sch"
    stale_schematic.write_text("(kicad_sch\n", encoding="utf-8")
    user_notes = tmp_path / "keep-my-notes.md"
    user_notes.write_text("user-owned\n", encoding="utf-8")
    old_manifest = tmp_path / "artifact_manifest.json"
    old_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "old",
                "root_schematic": str(stale_schematic),
                "artifacts": [
                    {
                        "path": str(stale_schematic),
                        "relative_path": stale_schematic.name,
                        "kind": "schematic",
                        "size_bytes": stale_schematic.stat().st_size,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _patch_generation_dependencies(
        monkeypatch,
        erc_result=ErcResult(
            status="skipped",
            schematic=str(tmp_path / "contract.kicad_sch"),
            skip_reason="not installed",
        ),
    )

    result = dispatcher.generate_artifacts({"project": "contract"}, output_dir=tmp_path)

    assert stale_schematic.read_text(encoding="utf-8") == "(kicad_sch\n"
    assert user_notes.read_text(encoding="utf-8") == "user-owned\n"
    assert str(stale_schematic) not in result["files"]
    assert str(user_notes) not in result["files"]

    manifest = json.loads(Path(result["artifact_manifest"]).read_text(encoding="utf-8"))
    artifact_names = {entry["path"] for entry in manifest["artifacts"]}
    assert stale_schematic.name not in artifact_names
    assert user_notes.name not in artifact_names
    assert manifest["root_schematic"] == "contract.kicad_sch"
    assert manifest["valid"] is True
    assert manifest["kicad_verified"] is False
    assert manifest["verification_status"] == "unverified"
    assert manifest["erc"]["status"] == "skipped"
    assert manifest["erc"]["schematic"] == "contract.kicad_sch"
    assert str(tmp_path) not in json.dumps(manifest)


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")


@pytest.mark.parametrize(
    "reserved_name",
    [
        "artifact_manifest.json",
        "circuit-weaver.log",
        "design.log",
        dispatcher._GENERATION_LOCK_FILENAME,
    ],
)
def test_preflight_target_symlink_escape_fails_before_target_is_opened(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    outside = tmp_path / f"outside-{reserved_name}"
    outside.write_text("sentinel\n", encoding="utf-8")
    _symlink_or_skip(output_dir / reserved_name, outside)

    with pytest.raises(ValueError, match="Refusing to write output outside"):
        dispatcher.generate_artifacts({"project": "contract"}, output_dir=output_dir)

    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_non_string_project_name_fails_before_output_creation(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"

    with pytest.raises(ValueError, match="Project name must be a string"):
        dispatcher.generate_artifacts({"project": ["not", "a", "name"]}, output_dir=output_dir)

    assert not output_dir.exists()


@pytest.mark.parametrize(
    "reserved_name",
    [
        "canonical_spec.yaml",
        "design_ir.json",
        "validation_report.json",
        "placement_readiness.json",
        "contract_test_points.csv",
    ],
)
def test_post_generation_target_symlink_escape_is_never_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reserved_name: str,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-{reserved_name}"
    outside.write_text("sentinel\n", encoding="utf-8")
    _symlink_or_skip(tmp_path / reserved_name, outside)
    _patch_generation_dependencies(
        monkeypatch,
        erc_result=ErcResult(status="skipped", skip_reason="not installed"),
    )

    with pytest.raises(ValueError, match="Refusing to write output outside"):
        dispatcher.generate_artifacts({"project": "contract"}, output_dir=tmp_path)

    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_reserved_manifest_directory_fails_before_generation_mutates_output(tmp_path: Path) -> None:
    manifest_path = tmp_path / "artifact_manifest.json"
    manifest_path.mkdir()

    with pytest.raises(ValueError, match="Reserved artifact manifest path is a directory"):
        dispatcher.generate_artifacts({"project": "contract"}, output_dir=tmp_path)

    assert manifest_path.is_dir()
    assert list(tmp_path.iterdir()) == [manifest_path]


def test_rejected_explicit_project_root_does_not_persist_failure_state(tmp_path: Path) -> None:
    from circuit_weaver.project_state import ProjectState, project_state_path, save_project_state

    declared_root = tmp_path / "declared-root"
    state_path = save_project_state(declared_root, ProjectState(name="declared-root"))
    original_state = state_path.read_bytes()
    output_dir = tmp_path / "outside-output"

    with pytest.raises(ValueError, match="inside its explicit project root"):
        dispatcher.generate_artifacts(
            {"project": "contract"}, output_dir=output_dir, project_root=declared_root
        )

    assert state_path.read_bytes() == original_state
    assert not project_state_path(output_dir).exists()


def test_generation_lock_path_must_be_a_regular_file(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    lock_path = output_dir / dispatcher._GENERATION_LOCK_FILENAME
    lock_path.mkdir()

    with pytest.raises(ValueError, match="lock path must be a regular file"):
        dispatcher.generate_artifacts({"project": "contract"}, output_dir=output_dir)

    assert lock_path.is_dir()
    assert list(output_dir.iterdir()) == [lock_path]


def test_unmovable_prior_manifest_rolls_back_without_mutating_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "artifact_manifest.json"
    manifest_text = '{"verification_status": "verified"}\n'
    manifest_path.write_text(manifest_text, encoding="utf-8")
    _patch_generation_dependencies(
        monkeypatch,
        erc_result=ErcResult(status="skipped", skip_reason="not installed"),
    )
    original_replace = dispatcher.os.replace

    def refuse_manifest_move(source: str | Path, destination: str | Path) -> None:
        if Path(source) == manifest_path:
            raise PermissionError("locked")
        original_replace(source, destination)

    monkeypatch.setattr(dispatcher.os, "replace", refuse_manifest_move)

    with pytest.raises(PermissionError, match="locked"):
        dispatcher.generate_artifacts({"project": "contract"}, output_dir=tmp_path)

    assert manifest_path.read_text(encoding="utf-8") == manifest_text
    assert not (tmp_path / "contract.kicad_sch").exists()
    assert not (tmp_path / "validation_report.json").exists()


def test_validation_report_serializes_message_and_report_evidence_ids() -> None:
    report = ValidationReport(
        profile="standard",
        valid=False,
        categories={
            "electrical": [
                ValidationMessage("electrical", "rule", "error", "U1", "finding", evidence_ids=["EV-DATASHEET-a"])
            ]
        },
        evidence_ids=["EV-CALC-b", "EV-DATASHEET-a"],
        evidence_manifest="evidence_manifest.json",
    )

    payload = report.to_dict()

    assert payload["evidence_ids"] == ["EV-CALC-b", "EV-DATASHEET-a"]
    assert payload["evidence_manifest"] == "evidence_manifest.json"
    assert payload["categories"]["electrical"][0]["evidence_ids"] == ["EV-DATASHEET-a"]


def test_concurrent_process_cannot_invalidate_manifest_while_lock_is_held(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    manifest_path = output_dir / "artifact_manifest.json"
    manifest_text = '{"verification_status": "verified", "owner": "first"}\n'
    manifest_path.write_text(manifest_text, encoding="utf-8")

    holder_script = """
import sys
from pathlib import Path
from circuit_weaver.dispatcher import _generation_output_lock

with _generation_output_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.readline()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(output_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"

        with pytest.raises(ValueError, match="already in progress"):
            dispatcher.generate_artifacts({"project": "contract"}, output_dir=output_dir)

        assert manifest_path.read_text(encoding="utf-8") == manifest_text
    finally:
        if holder.stdin is not None:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.stdin.close()
        try:
            holder.wait(timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=10)

    assert holder.returncode == 0, holder.stderr.read() if holder.stderr else ""
    with dispatcher._generation_output_lock(output_dir):
        pass


def test_final_erc_errors_are_hard_generation_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_manifest = tmp_path / "artifact_manifest.json"
    old_manifest.write_text('{"verification_status": "verified"}\n', encoding="utf-8")
    _patch_generation_dependencies(
        monkeypatch,
        erc_result=ErcResult(status="ok", errors=1),
    )

    with pytest.raises(ValueError, match="Final KiCad ERC found 1 error"):
        dispatcher.generate_artifacts({"project": "contract"}, output_dir=tmp_path)

    # Transactional regeneration preserves the prior committed artifact set;
    # durable project state records that the attempted replacement failed.
    assert old_manifest.read_text(encoding="utf-8") == '{"verification_status": "verified"}\n'
    assert not list(tmp_path.glob(".artifact_manifest.*.tmp"))


def test_require_kicad_rejects_skipped_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_generation_dependencies(
        monkeypatch,
        erc_result=ErcResult(status="skipped", skip_reason="not installed"),
    )

    with pytest.raises(ValueError, match="required but skipped"):
        dispatcher.generate_artifacts(
            {"project": "contract"},
            output_dir=tmp_path,
            require_kicad=True,
        )
