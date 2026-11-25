import hashlib
import io
import json
import zipfile

import pytest

from circuit_weaver import tt_slot_source as source

SHA = "d" * 40


def _zip(name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


def _index() -> bytes:
    data = {
        "name": "Tiny Tapeout SKY 26d",
        "repo": "https://github.com/TinyTapeout/tinytapeout-sky-26d",
        "commit": SHA,
        "commit_date": 1795554687,
        "layout": {"muxes": [["digital"]]},
        "version": 3,
        "projects": [],
    }
    return json.dumps(data).encode()


def test_only_allowlisted_sku_is_accepted():
    assert source._normalize_shuttle("TTSKY26d") == (
        "Tiny Tapeout SKY 26d",
        "TinyTapeout/tinytapeout-sky-26d",
    )
    assert source._normalize_shuttle("ttsky26D")[0] == "Tiny Tapeout SKY 26d"
    with pytest.raises(ValueError, match="supported SKU"):
        source._normalize_shuttle("TTSKY99a")


def test_safe_extract_checks_digest_and_traversal(tmp_path):
    payload = _zip("shuttle_index.json", _index())
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert source._safe_extract(payload, {"shuttle_index.json"}, digest, tmp_path).read_bytes() == _index()
    with pytest.raises(ValueError, match="digest"):
        source._safe_extract(payload, {"shuttle_index.json"}, "sha256:" + "0" * 64, tmp_path)
    unsafe = _zip("../shuttle_index.json", _index())
    with pytest.raises(ValueError, match="unsafe"):
        source._safe_extract(unsafe, {"shuttle_index.json"}, "sha256:" + hashlib.sha256(unsafe).hexdigest(), tmp_path)


def test_fetch_index_uses_latest_successful_push_and_pins_metadata(monkeypatch, tmp_path):
    payload = _index()
    archive = _zip("shuttle_index.json", payload)
    run = {"id": 10, "head_sha": SHA, "head_branch": "main", "event": "push", "conclusion": "success", "run_number": 4}
    monkeypatch.setattr(source, "_runs", lambda *args, **kwargs: [run])
    artifact = {"digest": "sha256:" + hashlib.sha256(archive).hexdigest(), "workflow_run": {"id": 10, "head_sha": SHA}}
    monkeypatch.setattr(source, "_artifact", lambda *args: (archive, artifact))
    path, metadata = source.fetch_tt_index("TTSKY26d", tmp_path)
    assert json.loads(path.read_text())["commit"] == SHA
    assert metadata["gds_run_id"] == 10
    assert metadata["repo"] == "TinyTapeout/tinytapeout-sky-26d"


def test_fetch_index_fails_closed_when_index_sha_differs(monkeypatch, tmp_path):
    data = json.loads(_index())
    data["commit"] = "e" * 40
    archive = _zip("shuttle_index.json", json.dumps(data).encode())
    run = {"id": 10, "head_sha": SHA, "head_branch": "main", "event": "push", "conclusion": "success", "run_number": 4}
    monkeypatch.setattr(source, "_runs", lambda *args, **kwargs: [run])
    artifact = {"digest": "sha256:" + hashlib.sha256(archive).hexdigest(), "workflow_run": {"id": 10, "head_sha": SHA}}
    monkeypatch.setattr(source, "_artifact", lambda *args: (archive, artifact))
    with pytest.raises(ValueError, match="does not match"):
        source.fetch_tt_index("TTSKY26d", tmp_path)


def test_fetch_layout_requires_one_same_commit_artifact(monkeypatch, tmp_path):
    artifact_zip = _zip("final.oas", b"OASIS")
    run = {"id": 22, "head_sha": SHA, "event": "workflow_run", "conclusion": "success"}
    monkeypatch.setattr(source, "_runs", lambda *args, **kwargs: [run])
    artifact = {
        "digest": "sha256:" + hashlib.sha256(artifact_zip).hexdigest(),
        "workflow_run": {"id": 22, "head_sha": SHA},
    }
    monkeypatch.setattr(source, "_artifact", lambda *args: (artifact_zip, artifact))
    path, metadata = source.fetch_tt_layout({"repo": "TinyTapeout/tinytapeout-sky-26d", "commit": SHA}, tmp_path)
    assert path.name == "layout.oas"
    assert metadata["precheck_run_id"] == 22
    monkeypatch.setattr(source, "_runs", lambda *args, **kwargs: [run, run])
    with pytest.raises(ValueError, match="exactly one"):
        source.fetch_tt_layout({"repo": "TinyTapeout/tinytapeout-sky-26d", "commit": SHA}, tmp_path)


def test_runs_query_filters_to_requested_head_sha(monkeypatch):
    endpoints = []

    def json_api(endpoint):
        endpoints.append(endpoint)
        return {"workflow_runs": []}

    monkeypatch.setattr(source, "_json_api", json_api)
    assert source._runs("TinyTapeout/tinytapeout-sky-26d", "precheck.yaml", event="workflow_run",
                        status="success", head_sha=SHA) == []
    assert f"head_sha={SHA}" in endpoints[0]


def test_layout_skips_only_runs_missing_final_oas(monkeypatch, tmp_path):
    runs = [
        {"id": 21, "head_sha": SHA, "event": "workflow_run", "conclusion": "success"},
        {"id": 22, "head_sha": SHA, "event": "workflow_run", "conclusion": "success"},
    ]
    monkeypatch.setattr(source, "_runs", lambda *args, **kwargs: runs)
    archive = _zip("final.oas", b"OASIS")
    artifact = {
        "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
        "workflow_run": {"id": 22, "head_sha": SHA},
    }

    def missing_then_success(_repo, run_id, _name):
        if run_id == 21:
            raise source._MissingArtifactError("no final_oas artifact")
        return archive, artifact

    monkeypatch.setattr(source, "_artifact", missing_then_success)
    _path, metadata = source.fetch_tt_layout({"repo": "TinyTapeout/tinytapeout-sky-26d", "commit": SHA}, tmp_path)
    assert metadata["precheck_run_id"] == 22

    def integrity_failure(_repo, _run_id, _name):
        raise ValueError("artifact digest mismatch")

    monkeypatch.setattr(source, "_artifact", integrity_failure)
    with pytest.raises(ValueError, match="digest mismatch"):
        source.fetch_tt_layout({"repo": "TinyTapeout/tinytapeout-sky-26d", "commit": SHA}, tmp_path)


def test_fetch_info_uses_pinned_commit_and_validates_macro(monkeypatch, tmp_path):
    calls = []

    def api(endpoint, *, raw=False):
        calls.append((endpoint, raw))
        return b"project:\n  top_module: tt_um_sample\n"

    monkeypatch.setattr(source, "_gh_api", api)
    path = source.fetch_tt_info({"repo": "TinyTapeout/tinytapeout-sky-26d", "commit": SHA}, "tt_um_sample", tmp_path)
    assert "ref=" + SHA in calls[0][0]
    assert calls[0][1] is True
    assert path.read_text().startswith("project:")
    with pytest.raises(ValueError, match="macro"):
        source.fetch_tt_info({"repo": "TinyTapeout/tinytapeout-sky-26d", "commit": SHA}, "../bad", tmp_path)
