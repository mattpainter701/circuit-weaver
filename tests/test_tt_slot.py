"""End-to-end safety checks for the Tiny Tapeout slot-map entry points."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from circuit_weaver import mcp_server, tt_slot

SHUTTLE_COMMIT = "a" * 40
PROJECT_COMMIT = "b" * 40


def _index(tmp_path: Path, *, macro: str = "tt_um_sample", repo: str = "https://github.com/example/sample") -> Path:
    path = tmp_path / "shuttle_index.json"
    path.write_text(
        json.dumps(
            {
                "name": "Tiny Tapeout SKY 26d",
                "repo": "https://github.com/TinyTapeout/tinytapeout-sky-26d",
                "commit": SHUTTLE_COMMIT,
                "commit_date": 1795554687,
                "layout": {"muxes": [["digital"]]},
                "version": 3,
                "projects": [
                    {
                        "macro": macro,
                        "address": 17,
                        "x": 2,
                        "y": 3,
                        "tiles": "1x1",
                        "repo": repo,
                        "commit": PROJECT_COMMIT,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _info(tmp_path: Path, *, macro: str = "tt_um_sample") -> Path:
    path = tmp_path / "info.yaml"
    path.write_text(
        "yaml_version: 6\n"
        "project:\n"
        f"  top_module: {macro}\n"
        "  clock_hz: 1000000\n"
        "pinout:\n"
        "  ui[0]: Button\n"
        "  uo[0]: Status\n",
        encoding="utf-8",
    )
    return path


def _layout_evidence(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "layout.oas"
    path.write_bytes(b"test layout evidence")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_local_unassigned_is_pending_and_writes_report(tmp_path):
    index = _index(tmp_path)
    report = tmp_path / "pending.html"
    completed = subprocess.run(
        [sys.executable, "-m", "circuit_weaver", "silicon", "slot-map", "tt_um_not_listed",
         "--index", str(index), "--report", str(report)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    result = json.loads(completed.stdout)
    assert result["status"] == "unassigned"
    assert result["assignment"] is None
    assert result["die"]["status"] == "awaiting_assignment"
    assert "Awaiting assignment" in report.read_text(encoding="utf-8")


def test_cli_expected_repo_mismatch_fails_without_report(tmp_path):
    index = _index(tmp_path)
    report = tmp_path / "should-not-exist.html"
    completed = subprocess.run(
        [sys.executable, "-m", "circuit_weaver", "silicon", "slot-map", "tt_um_sample",
         "--index", str(index), "--expected-repo", "https://github.com/example/other", "--report", str(report)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    result = json.loads(completed.stdout)
    assert result["status"] == "error"
    assert "different project repository" in result["error"]["message"]
    assert not report.exists()


def test_orchestration_reports_assignment_and_partial_layout_failure(monkeypatch, tmp_path):
    index = _index(tmp_path)
    info = _info(tmp_path)

    def no_layout(*args, **kwargs):
        raise ValueError("test layout unavailable")

    monkeypatch.setattr(tt_slot, "render_tt_slot", no_layout)
    report = tmp_path / "partial.html"
    layout, digest = _layout_evidence(tmp_path)
    result = tt_slot.map_tt_slot(
        "tt_um_sample", index=index, info=info, report=report, layout=layout,
        layout_commit=SHUTTLE_COMMIT, layout_sha256=digest,
    )
    assert result["status"] == "assigned"
    assert result["assignment"]["address"] == 17
    assert result["board"]["top_module"] == "tt_um_sample"
    assert any(pin["signal"] == "ui[0]" and pin["function"] == "Button" for pin in result["board"]["pins"])
    assert result["die"]["status"] == "unavailable"
    assert any("Die view unavailable" in issue for issue in result["issues"])
    assert "test layout unavailable" in report.read_text(encoding="utf-8")


def test_report_escapes_untrusted_values_and_never_embeds_stale_image(monkeypatch, tmp_path):
    index = _index(tmp_path, repo="https://github.com/example/<script>alert(1)</script>")
    report = tmp_path / "escaped.html"
    stale_image = b"old-image-must-not-be-used"

    def failed_render(_layout, output, *, macro):
        # A prior image at the same apparent path must not leak into this run.
        Path(output).write_bytes(stale_image)
        raise ValueError("native render failed after partial output")

    monkeypatch.setattr(tt_slot, "render_tt_slot", failed_render)
    layout, digest = _layout_evidence(tmp_path)
    result = tt_slot.map_tt_slot(
        "tt_um_sample", index=index, report=report, layout=layout,
        layout_commit=SHUTTLE_COMMIT, layout_sha256=digest,
    )
    html = report.read_text(encoding="utf-8")
    assert result["die"]["status"] == "unavailable"
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert base64.b64encode(stale_image).decode("ascii") not in html
    assert "data:image/png;base64," not in html


def test_mcp_local_index_assignment_and_argument_validation(tmp_path):
    index = _index(tmp_path)

    async def exercise():
        async with create_connected_server_and_client_session(mcp_server._create_app()) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert "silicon_slot_map" in names
            valid = await session.call_tool(
                "silicon_slot_map",
                {"macro": "tt_um_sample", "index": str(index)},
            )
            assert not valid.isError, valid.content
            result = json.loads(valid.content[0].text)
            assert result["status"] == "assigned"
            assert result["assignment"]["address"] == 17
            invalid = await session.call_tool(
                "silicon_slot_map",
                {"macro": "tt_um_sample", "index": "relative/index.json"},
            )
            payload = json.loads(invalid.content[0].text)
            assert payload["status"] == "error"

    asyncio.run(exercise())


@pytest.mark.parametrize("commit,digest", [("c" * 40, None), (SHUTTLE_COMMIT, "0" * 64)])
def test_offline_evidence_mismatch_preserves_existing_report(tmp_path, commit, digest):
    layout, expected = _layout_evidence(tmp_path)
    report = tmp_path / "report.html"
    report.write_text("previous verified revision", encoding="utf-8")
    with pytest.raises(ValueError, match="revision or SHA-256"):
        tt_slot.map_tt_slot("tt_um_sample", index=_index(tmp_path), layout=layout, layout_commit=commit,
                            layout_sha256=digest or expected, report=report)
    assert report.read_text(encoding="utf-8") == "previous verified revision"


def test_published_pin_functions_take_precedence_over_local_copy(monkeypatch, tmp_path):
    index = _index(tmp_path)
    published = _info(tmp_path)
    local = tmp_path / "local.yaml"
    local.write_text(published.read_text(encoding="utf-8").replace("Button", "Old pin label"), encoding="utf-8")
    source = {"commit": SHUTTLE_COMMIT, "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest()}
    monkeypatch.setattr(tt_slot, "fetch_tt_index", lambda *args: (index, source))
    monkeypatch.setattr(tt_slot, "fetch_tt_info", lambda *args: published)
    result = tt_slot.map_tt_slot("tt_um_sample", info=local)
    pin = next(row for row in result["board"]["pins"] if row["signal"] == "ui[0]")
    assert pin["function"] == "Button"
    assert result["issues"]
    assert result["published_info_sha256"] == hashlib.sha256(published.read_bytes()).hexdigest()


def test_assigned_then_pending_replaces_html_and_drops_old_address_and_image(monkeypatch, tmp_path):
    index = _index(tmp_path)
    layout, digest = _layout_evidence(tmp_path)
    report = tmp_path / "report.html"

    def render(_layout, output, *, macro):
        output.write_bytes(b"image fixture")
        return {"input_sha256": digest, "layout_cell": macro, "bbox_um": [1, 2, 3, 4]}

    monkeypatch.setattr(tt_slot, "render_tt_slot", render)
    result = tt_slot.map_tt_slot("tt_um_sample", index=index, layout=layout, layout_commit=SHUTTLE_COMMIT,
                                 layout_sha256=digest, info=_info(tmp_path), report=report)
    assert not result["issues"]
    assert result["die"]["status"] == "located"
    assert "data:image/png;base64," in report.read_text(encoding="utf-8")
    tt_slot.map_tt_slot("tt_um_sample", index=_index(tmp_path, macro="tt_um_other"), report=report)
    refreshed = report.read_text(encoding="utf-8")
    assert "Awaiting assignment" in refreshed
    assert "data:image/png;base64," not in refreshed
    assert "selector address <strong>17" not in refreshed
