from __future__ import annotations

import hashlib
from contextlib import nullcontext
from pathlib import Path

from circuit_weaver import routing_recovery as recovery
from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.validator import ValidationIssue


def _pair():
    return {
        "net": "N1",
        "items": [
            {"uuid": "pad-a", "net": "N1", "pos": {"x": 0.0, "y": 0.0}},
            {"uuid": "pad-b", "net": "N1", "pos": {"x": 1.0, "y": 0.0}},
        ],
    }


def _drc(path: Path, opens: int) -> DrcResult:
    payload = path.read_bytes()
    findings = tuple(ValidationIssue("drc", rule_id="CW-DRC-002", severity="blocker") for _ in range(opens))
    return DrcResult(
        status="ok",
        board=str(path),
        board_sha256=hashlib.sha256(payload).hexdigest(),
        findings=findings,
        raw_report={
            "violations": [],
            "unconnected_items": [_pair() for _ in range(opens)],
            "schematic_parity": [],
        },
    )


def _repair_report():
    return {
        "status": "candidate_only_requires_full_drc",
        "source_identity_preserved": True,
        "original_footprints_and_text_preserved": True,
        "original_board_geometry_preserved": True,
        "existing_copper_preserved": True,
        "net": "N1",
        "from_uuid": "pad-a",
        "to_uuid": "pad-b",
        "added_tracks": 1,
        "added_vias": 0,
        "expanded_states": 12,
    }


def test_standard_recovery_uses_deadline_but_does_not_request_connected_endpoints(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"source board")
    output = tmp_path / "recovered.kicad_pcb"
    remaining = iter((1, 0, 0))
    drc_calls = []
    requests = []

    def drc(path, **kwargs):
        drc_calls.append(kwargs)
        return _drc(Path(path), next(remaining))

    def worker(operation, **kwargs):
        assert operation == "repair_open"
        requests.append(kwargs)
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" routed")
        return {"status": "ok", "route_repair": _repair_report()}

    monkeypatch.setattr(recovery, "run_drc", drc)
    monkeypatch.setattr(recovery, "run_kicad", worker)
    monkeypatch.setattr(
        recovery,
        "_prepare_settings",
        lambda src, *_args, **_kwargs: {src: hashlib.sha256(Path(src).read_bytes()).hexdigest()},
    )
    monkeypatch.setattr(recovery, "_restore_settings", lambda *_args: None)
    monkeypatch.setattr(recovery, "_check_snapshots", lambda *_args: None)
    monkeypatch.setattr(recovery, "stage_project_footprint_context", lambda *_args: {})
    monkeypatch.setattr(recovery, "_pcb_output_lock", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(recovery, "_prepare_destination", lambda *_args, **_kwargs: None)

    result = recovery.recover_routing(source, output, timeout_seconds=20, max_links=1, max_pair_attempts=1)

    assert result["status"] == "ok", result
    assert len(requests) == 1
    assert "connected_endpoints" not in requests[0]
    assert drc_calls[0]["deadline"] > requests[0]["deadline"]
    assert drc_calls[1]["deadline"] == requests[0]["deadline"]
    assert requests[0]["timeout"] > 0
    assert requests[0]["timeout"] <= 20
    assert result["search_modes_tried"] == ["standard"]
    assert result["search_profiles_tried"][0]["mode"] == "standard"
    assert output.read_bytes().endswith(b" routed")
