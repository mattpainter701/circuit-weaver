from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import klayout.db as kdb
import pytest

from circuit_weaver.silicon_handoff import review_silicon_handoff


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _gds(path, top="chip"):
    layout = kdb.Layout()
    cell = layout.create_cell(top)
    layer = layout.layer(1, 0)
    cell.shapes(layer).insert(kdb.Box(0, 0, 1000, 1000))
    layout.write(str(path))


def _manifest(tmp_path, scope="top_level"):
    _gds(tmp_path / "chip.gds")
    roles = []
    for role in (
        "layout", "pin_power_map", "integration_spec", "drc", "lvs", "timing",
        "provider_precheck", "bringup_plan", "operator_extra",
    ):
        path = tmp_path / f"{role}.txt"
        path.write_text(f"evidence for {role}\n", encoding="utf-8")
        item = {"role": role, "path": path.name, "sha256": _sha(path)}
        if role == "layout":
            item["path"] = "chip.gds"
            item["sha256"] = _sha(tmp_path / "chip.gds")
            item["scope"] = scope
        roles.append(item)
    layout_hash = next(item["sha256"] for item in roles if item["role"] == "layout")
    role_hash = {item["role"]: item["sha256"] for item in roles}
    verification = {
        role: {"status": "pass", "layout_sha256": layout_hash, "report_sha256": role_hash[role],
               "role": role, "provider": "example-provider", "run": "run-26a"}
        for role in ("drc", "lvs", "timing", "provider_precheck")
    }
    manifest = {
        "schema": 1, "provider": "example-provider", "run": "run-26a", "declared_top": "chip",
        "roles": roles, "required_roles": ["operator_extra"], "verification": verification,
    }
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def test_complete_manifest_is_reviewable_and_bundle_is_deterministic(tmp_path, monkeypatch):
    path, _ = _manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    one = review_silicon_handoff(path, bundle="review.zip")
    first = (tmp_path / "review.zip").read_bytes()
    (tmp_path / "review.zip").unlink()
    two = review_silicon_handoff(path, bundle="review.zip")
    assert one["status"] == two["status"] == "ready_for_provider_review"
    assert set(one["report_modes"]) == {"drc", "lvs", "timing", "provider_precheck"}
    assert set(one["report_modes"].values()) == {"attested"}
    assert one["bundle_sha256"] == two["bundle_sha256"]
    with zipfile.ZipFile(tmp_path / "review.zip") as archive:
        assert all(entry.create_system == 3 for entry in archive.infolist())
        assert "handoff_manifest.json" in archive.namelist()
        assert archive.read("chip.gds") == (tmp_path / "chip.gds").read_bytes()
    assert first == (tmp_path / "review.zip").read_bytes()


def test_missing_stale_or_retargeted_proofs_are_incomplete_and_do_not_write_bundle(tmp_path, monkeypatch):
    path, manifest = _manifest(tmp_path)
    manifest["verification"]["lvs"]["layout_sha256"] = "0" * 64
    manifest["verification"]["timing"]["report_sha256"] = "1" * 64
    manifest["verification"]["provider_precheck"]["run"] = "other-run"
    manifest["roles"] = [item for item in manifest["roles"] if item["role"] != "bringup_plan"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = review_silicon_handoff(path, bundle="must-not-exist.zip")
    assert result["status"] == "incomplete"
    assert any("missing role: bringup_plan" in item for item in result["blockers"])
    assert any("current layout SHA-256" in item for item in result["blockers"])
    assert any("current 'timing' artifact" in item for item in result["blockers"])
    assert any("provider and run" in item for item in result["blockers"])
    assert not (tmp_path / "must-not-exist.zip").exists()


def test_core_macro_is_not_ready_and_layout_top_is_checked(tmp_path, monkeypatch):
    path, manifest = _manifest(tmp_path, scope="core_macro")
    monkeypatch.chdir(tmp_path)
    result = review_silicon_handoff(path)
    assert result["status"] == "incomplete"
    assert any("core_macro" in item for item in result["blockers"])
    manifest["roles"][0]["scope"] = "top_level"
    manifest["declared_top"] = "absent"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = review_silicon_handoff(path)
    assert any("absent from GDS" in item for item in result["blockers"])


def test_shuttle_wrapper_scope_is_supported(tmp_path, monkeypatch):
    path, manifest = _manifest(tmp_path, scope="shuttle_wrapper")
    monkeypatch.chdir(tmp_path)
    assert review_silicon_handoff(path)["status"] == "ready_for_provider_review"


def test_missing_artifact_and_comment_only_report_are_actionable(tmp_path, monkeypatch):
    path, manifest = _manifest(tmp_path)
    (tmp_path / "drc.txt").write_text("# generated report\n// no results\n", encoding="utf-8")
    manifest["roles"] = [item for item in manifest["roles"] if item["role"] != "timing"]
    (tmp_path / "pin_power_map.txt").unlink()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = review_silicon_handoff(path)
    assert result["status"] == "incomplete"
    assert any("artifact file is missing" in item for item in result["blockers"])
    assert any("comments only" in item for item in result["blockers"])
    assert any("missing role: timing" in item for item in result["blockers"])


def test_unselected_target_is_incomplete(tmp_path, monkeypatch):
    path, manifest = _manifest(tmp_path)
    del manifest["provider"]
    del manifest["run"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = review_silicon_handoff(path)
    assert result["status"] == "incomplete"
    assert any("select the shuttle provider" in item for item in result["blockers"])
    assert any("select the shuttle run" in item for item in result["blockers"])


@pytest.mark.parametrize("bad_path", ["../escape", "C:/absolute", "/absolute", "a/../b"])
def test_unsafe_artifact_paths_rejected(tmp_path, monkeypatch, bad_path):
    path, manifest = _manifest(tmp_path)
    manifest["roles"][1]["path"] = bad_path
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="unsafe artifact path"):
        review_silicon_handoff(path)


def test_case_colliding_archive_paths_and_bundle_input_clobber_are_rejected(tmp_path, monkeypatch):
    path, manifest = _manifest(tmp_path)
    manifest["roles"][1]["path"] = "CHIP.GDS"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="duplicate or case-colliding"):
        review_silicon_handoff(path)
    path, _ = _manifest(tmp_path)
    with pytest.raises(ValueError, match="overwrite an input"):
        review_silicon_handoff(path, bundle="chip.gds")


def test_symlinked_artifact_is_rejected(tmp_path, monkeypatch):
    path, manifest = _manifest(tmp_path)
    linked = tmp_path / "linked.txt"
    linked.write_text("linked target", encoding="utf-8")
    manifest["roles"][1]["path"] = linked.name
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "linked.txt" or original(self))
    with pytest.raises(ValueError, match="symlink artifact path"):
        review_silicon_handoff(path)
