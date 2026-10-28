import hashlib
import json
from pathlib import Path

import pytest

from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.routing_recovery import recover_routing
from circuit_weaver.validator import ValidationIssue


def _drc(path: Path, opens: int, *, status="ok", other_blocker=False, unknown=False, raw_override=None):
    data = path.read_bytes()
    findings = [ValidationIssue("drc", rule_id="CW-DRC-002", severity="blocker") for _ in range(opens)]
    if unknown:
        findings[0] = ValidationIssue("drc", rule_id="CW-DRC-999", severity="blocker")
    if other_blocker:
        findings.append(ValidationIssue("drc", rule_id="CW-DRC-001", severity="blocker"))
    raw = {
        "violations": [],
        "unconnected_items": [
            {
                "net": "N1",
                "items": [
                    {"uuid": f"pad-{index}-a", "net": "N1", "pos": {"x": float(index), "y": 0.0}},
                    {"uuid": f"pad-{index}-b", "net": "N1", "pos": {"x": float(index) + 0.5, "y": 0.0}},
                ],
            }
            for index in range(opens)
        ],
        "schematic_parity": [],
    }
    if raw_override is not None:
        raw = raw_override
    return DrcResult(
        status=status,
        board=str(path),
        board_sha256=hashlib.sha256(data).hexdigest(),
        findings=tuple(findings),
        raw_report=raw,
    )


def _wire_files(tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"partial board")
    destination = tmp_path / "recovered.kicad_pcb"
    monkeypatch.setattr(
        "circuit_weaver.routing_recovery._prepare_settings",
        lambda src, staged, **_: {src: hashlib.sha256(src.read_bytes()).hexdigest()},
    )
    monkeypatch.setattr("circuit_weaver.routing_recovery._restore_settings", lambda *_: None)
    return source, destination


def _worker_report():
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


def _bidirectional_worker_report(grid_step_mm=0.5):
    return {
        **_worker_report(),
        "search_mode": "bidirectional",
        "search_direction": "bidirectional",
        "requested_grid_step_mm": grid_step_mm,
        "search_grid_step_mm": grid_step_mm,
        "search_state_budget": 150000,
    }


def test_recovery_tries_next_pair_after_nonprogress_then_resets_pair_index(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    counts = iter((2, 2, 1, 0, 0))

    def drc(path, **_):
        return _drc(Path(path), next(counts))

    pair_indices = []

    def worker(operation, **kwargs):
        assert operation == "repair_open"
        assert kwargs["drc_report"] is not None
        pair_indices.append(kwargs["pair_index"])
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" routed")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    evidence = tmp_path / "evidence"
    result = recover_routing(source, destination, evidence_dir=evidence, max_pair_attempts=2)

    assert result["status"] == "ok"
    assert result["initial_open_count"] == 2
    assert result["remaining_open_count"] == 0
    assert result["attempts"] == 3
    assert result["links_repaired"] == 2
    assert pair_indices == [0, 1, 0]
    assert destination.is_file()
    assert len(list(evidence.glob("routing-recovery-*/partial-progress.kicad_pcb"))) == 1
    run_evidence = next(evidence.glob("routing-recovery-*"))
    assert (run_evidence / "history.json").is_file()
    assert (run_evidence / "attempt-0003" / "attempt.json").is_file()
    assert result["fabrication_ready"] is False


@pytest.mark.parametrize(
    "bad",
    [
        {"opens": 2, "status": "skipped"},
        {"opens": 2, "other_blocker": True},
        {"opens": 2, "unknown": True},
        {
            "opens": 2,
            "raw_override": {
                "violations": [],
                "unconnected_items": [{}, {}],
                "schematic_parity": [],
                "truncated": True,
            },
        },
    ],
)
def test_recovery_rejects_invalid_or_ambiguous_initial_drc(tmp_path, monkeypatch, bad):
    source, destination = _wire_files(tmp_path, monkeypatch)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", lambda path, **_: _drc(Path(path), **bad))
    monkeypatch.setattr(
        "circuit_weaver.routing_recovery.run_kicad", lambda *_a, **_k: pytest.fail("invalid DRC reached the worker")
    )
    result = recover_routing(source, destination)
    assert result["status"] == "error"
    assert not destination.exists()
    retained = Path(result["evidence_dir"])
    inventory = json.loads((retained / "initial-drc.json").read_text())
    expected = _drc(source, **bad)
    assert inventory["raw_report"] == expected.raw_report
    assert inventory["drc"]["status"] == expected.status
    assert inventory["board_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (retained / "input-partial.kicad_pcb").read_bytes() == source.read_bytes()


def test_recovery_keeps_existing_output_when_candidate_drc_regresses(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    destination.write_bytes(b"prior published board")
    calls = 0

    def drc(path, **_):
        nonlocal calls
        calls += 1
        return _drc(Path(path), 2 if calls == 1 else 1, other_blocker=calls == 2)

    def worker(_operation, **kwargs):
        Path(kwargs["output"]).write_bytes(b"bad candidate")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, overwrite=True)
    assert result["status"] == "partial"
    assert destination.read_bytes() == b"prior published board"


def test_recovery_rejects_unknown_worker_status_and_preserves_source(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    source_bytes = source.read_bytes()
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", lambda path, **_: _drc(Path(path), 1))
    monkeypatch.setattr(
        "circuit_weaver.routing_recovery.run_kicad",
        lambda *_a, **_k: {"status": "mystery", "route_repair": _worker_report()},
    )
    result = recover_routing(source, destination)
    assert result["status"] == "partial"
    assert source.read_bytes() == source_bytes
    assert not destination.exists()


def test_recovery_stages_and_restores_project_settings_and_local_library_context(tmp_path, monkeypatch):
    project = tmp_path / "project"
    library = project / "libraries" / "Local.pretty"
    library.mkdir(parents=True)
    (library / "Part.kicad_mod").write_text("footprint", encoding="utf-8")
    (project / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/libraries/Local.pretty")))\n',
        encoding="utf-8",
    )
    source = project / "input.kicad_pcb"
    source.write_bytes(b"partial board")
    source.with_suffix(".kicad_pro").write_bytes(b"authored project")
    source.with_suffix(".kicad_dru").write_bytes(b"authored rules")
    destination = project / "recovered.kicad_pcb"
    counts = iter((1, 0, 0))

    def drc(path, **_):
        board = Path(path)
        assert board.with_suffix(".kicad_pro").read_bytes() == b"authored project"
        assert board.with_suffix(".kicad_dru").read_bytes() == b"authored rules"
        assert (board.parent / "fp-lib-table").is_file()
        assert (board.parent / "libraries" / "Local.pretty" / "Part.kicad_mod").is_file()
        return _drc(board, next(counts))

    def worker(_operation, **kwargs):
        candidate = Path(kwargs["output"])
        assert candidate.with_suffix(".kicad_pro").read_bytes() == b"authored project"
        assert candidate.with_suffix(".kicad_dru").read_bytes() == b"authored rules"
        assert (candidate.parent / "fp-lib-table").is_file()
        Path(kwargs["output"]).write_bytes(b"candidate")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination)
    assert result["status"] == "ok"
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"authored project"
    assert destination.with_suffix(".kicad_dru").read_bytes() == b"authored rules"


def test_recovery_accepts_more_than_twelve_links_with_configured_link_budget(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    counts = iter([15, *range(14, 1, -1)])

    def drc(path, **_):
        return _drc(Path(path), next(counts))

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    calls = 0

    def worker(_operation, **kwargs):
        nonlocal calls
        calls += 1
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b"x")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, max_links=13, max_pair_attempts=1, evidence_dir=tmp_path / "evidence")
    assert result["status"] == "partial"
    assert calls == 13
    assert result["links_repaired"] == 13
    run_evidence = next((tmp_path / "evidence").glob("routing-recovery-*"))
    assert (run_evidence / "partial-progress.kicad_pcb").is_file()
    assert len(list(run_evidence.glob("attempt-*/attempt.json"))) == 13
    assert not destination.exists()


def test_recovery_sweeps_pairs_beyond_first_twelve(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    drc_calls = 0

    def drc(path, **_):
        nonlocal drc_calls
        drc_calls += 1
        return _drc(Path(path), 13 if drc_calls == 1 else 0)

    pair_indices = []

    def worker(_operation, **kwargs):
        pair_indices.append(kwargs["pair_index"])
        if kwargs["pair_index"] < 12:
            return {"status": "error", "message": "no route found"}
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" routed")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, max_links=1, max_pair_attempts=13)

    assert result["status"] == "ok"
    assert pair_indices == list(range(13))
    assert result["attempts"] == 13
    assert result["links_repaired"] == 1


def test_recovery_timeout_finalizes_partial_evidence_without_publishing(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    source_bytes = source.read_bytes()
    drc_calls = 0

    def drc(path, **_):
        nonlocal drc_calls
        drc_calls += 1
        if drc_calls == 3:
            raise TimeoutError("fresh DRC timed out")
        return _drc(Path(path), 2 if drc_calls == 1 else 1)

    def worker(_operation, **kwargs):
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" routed")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    evidence = tmp_path / "evidence"
    result = recover_routing(source, destination, evidence_dir=evidence)

    assert result["status"] == "error"
    assert result["message"] == "fresh DRC timed out"
    assert result["initial_open_count"] == 2
    assert result["remaining_open_count"] == 1
    assert result["links_repaired"] == 1
    assert result["attempts"] == 2
    assert result["partial_board"] is not None
    assert Path(result["partial_board"]).is_file()
    assert result["partial_board_sha256"] == hashlib.sha256(Path(result["partial_board"]).read_bytes()).hexdigest()
    assert source.read_bytes() == source_bytes
    assert not destination.exists()
    run_evidence = Path(result["evidence_dir"])
    history = json.loads((run_evidence / "history.json").read_text(encoding="utf-8"))
    assert history["status"] == "error"
    assert history["message"] == result["message"]
    assert history["source_sha256"] == result["source_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert history["links_repaired"] == 1
    assert len(history["history"]) == 1


def test_standard_success_never_invokes_bidirectional_fallback(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    counts = iter((1, 0, 0))
    requests = []

    def worker(_operation, **kwargs):
        requests.append(kwargs)
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" routed")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", lambda path, **_: _drc(Path(path), next(counts)))
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination)

    assert result["status"] == "ok"
    assert result["search_modes_tried"] == ["standard"]
    assert result["search_profiles_tried"] == [{"mode": "standard", "grid_step_mm": None}]
    assert len(requests) == 1
    assert not {"search_mode", "grid_step_mm", "max_expanded_states"} & requests[0].keys()
    assert result["history"][0]["search_mode"] == "standard"


def test_stalled_standard_sweep_switches_once_and_stays_bidirectional_after_progress(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    drc_counts = iter((2, 1, 0, 0))
    calls = []

    def drc(path, **_):
        return _drc(Path(path), next(drc_counts))

    def worker(_operation, **kwargs):
        mode = kwargs.get("search_mode", "standard")
        calls.append((mode, kwargs["pair_index"], {k: kwargs[k] for k in ("grid_step_mm", "max_expanded_states")
                                                     if k in kwargs}))
        if mode == "standard":
            return {"status": "error", "message": "default profile found no path"}
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" routed")
        return {"status": "ok", "route_repair": _bidirectional_worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, max_links=2, max_pair_attempts=3)

    assert result["status"] == "ok"
    assert [row[0] for row in calls] == ["standard", "standard", "bidirectional", "bidirectional"]
    assert [row[1] for row in calls] == [0, 1, 1, 0]
    assert calls[2][2] == {"grid_step_mm": 0.5, "max_expanded_states": 150000}
    assert calls[3][2] == {"grid_step_mm": 0.5, "max_expanded_states": 150000}
    assert result["search_modes_tried"] == ["standard", "bidirectional"]
    assert result["search_profiles_tried"] == [
        {"mode": "standard", "grid_step_mm": None},
        {"mode": "bidirectional", "grid_step_mm": 0.5},
    ]
    assert [row["search_mode"] for row in result["history"]] == [
        "standard", "standard", "bidirectional", "bidirectional"
    ]


def test_no_progress_in_fallback_stops_without_oscillation(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", lambda path, **_: _drc(Path(path), 2))

    def worker(_operation, **kwargs):
        calls.append(kwargs.get("search_mode", "standard"))
        return {"status": "error", "message": "no route"}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, max_links=2, max_pair_attempts=4)

    assert result["status"] == "partial"
    assert calls == [
        "standard", "standard",
        "bidirectional", "bidirectional",
        "bidirectional", "bidirectional",
        "bidirectional", "bidirectional",
    ]
    assert result["search_modes_tried"] == ["standard", "bidirectional"]
    assert result["search_profiles_tried"] == [
        {"mode": "standard", "grid_step_mm": None},
        {"mode": "bidirectional", "grid_step_mm": 0.5},
        {"mode": "bidirectional", "grid_step_mm": 0.25},
        {"mode": "bidirectional", "grid_step_mm": 0.125},
    ]
    assert result["attempts"] == 8


def test_fallback_candidate_requires_native_bidirectional_acknowledgment(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", lambda path, **_: _drc(Path(path), 1))

    def worker(_operation, **kwargs):
        mode = kwargs.get("search_mode", "standard")
        calls.append(mode)
        if mode == "standard":
            return {"status": "error", "message": "no route"}
        Path(kwargs["output"]).write_bytes(b"unacknowledged candidate")
        report = _bidirectional_worker_report()
        report.pop("search_mode")
        return {"status": "ok", "route_repair": report}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, max_links=1, max_pair_attempts=2)

    assert result["status"] == "partial"
    assert calls == ["standard", "bidirectional"]
    assert result["attempts"] == 2
    assert result["links_repaired"] == 0
    assert not destination.exists()


def test_fine_bidirectional_profile_starts_only_after_coarse_full_sweep(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    drc_counts = iter((2, 1, 0, 0))
    calls = []

    def drc(path, **_):
        return _drc(Path(path), next(drc_counts))

    def worker(_operation, **kwargs):
        mode = kwargs.get("search_mode", "standard")
        grid = kwargs.get("grid_step_mm")
        pair_index = kwargs["pair_index"]
        calls.append((mode, grid, pair_index))
        if mode == "standard" or grid == 0.5:
            return {"status": "error", "message": "profile did not find a route"}
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" fine routed")
        return {"status": "ok", "route_repair": _bidirectional_worker_report(0.25)}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, max_links=2, max_pair_attempts=4)

    assert result["status"] == "ok"
    assert calls == [
        ("standard", None, 0), ("standard", None, 1),
        ("bidirectional", 0.5, 1), ("bidirectional", 0.5, 0),
        ("bidirectional", 0.25, 1), ("bidirectional", 0.25, 0),
    ]
    assert result["search_modes_tried"] == ["standard", "bidirectional"]
    assert result["search_profiles_tried"] == [
        {"mode": "standard", "grid_step_mm": None},
        {"mode": "bidirectional", "grid_step_mm": 0.5},
        {"mode": "bidirectional", "grid_step_mm": 0.25},
    ]
    assert [entry["search_profile_grid_mm"] for entry in result["history"]] == [
        None, None, 0.5, 0.5, 0.25, 0.25,
    ]


def test_finest_bidirectional_profile_starts_after_coarse_and_fine_sweeps_stall(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    drc_counts = iter((1, 0, 0))
    calls = []

    def drc(path, **_):
        return _drc(Path(path), next(drc_counts))

    def worker(_operation, **kwargs):
        mode = kwargs.get("search_mode", "standard")
        grid = kwargs.get("grid_step_mm")
        calls.append((mode, grid, kwargs.get("max_expanded_states")))
        if mode == "standard" or grid in {0.5, 0.25}:
            return {"status": "error", "message": "profile did not find a route"}
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" finest routed")
        return {"status": "ok", "route_repair": _bidirectional_worker_report(0.125)}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, max_links=2, max_pair_attempts=4)

    assert result["status"] == "ok"
    assert calls == [
        ("standard", None, None),
        ("bidirectional", 0.5, 150000),
        ("bidirectional", 0.25, 150000),
        ("bidirectional", 0.125, 150000),
    ]
    assert result["search_profiles_tried"] == [
        {"mode": "standard", "grid_step_mm": None},
        {"mode": "bidirectional", "grid_step_mm": 0.5},
        {"mode": "bidirectional", "grid_step_mm": 0.25},
        {"mode": "bidirectional", "grid_step_mm": 0.125},
    ]
    assert result["history"][-1]["accepted"] is True
    assert result["history"][-1]["native_report"]["requested_grid_step_mm"] == 0.125
    assert result["history"][-1]["native_report"]["search_grid_step_mm"] == 0.125
    assert result["history"][-1]["native_report"]["search_state_budget"] == 150000


def test_fallback_shares_attempt_cap_and_does_not_reset_deadline(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", lambda path, **_: _drc(Path(path), 1))

    def worker(_operation, **kwargs):
        calls.append(kwargs.get("search_mode", "standard"))
        return {"status": "error", "message": "no route"}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    capped = recover_routing(source, destination, max_links=1, max_pair_attempts=1)
    assert capped["status"] == "partial"
    assert capped["attempts"] == 1
    assert calls == ["standard"]

    source2, destination2 = _wire_files(tmp_path / "deadline", monkeypatch)
    clock = [10.0]
    monkeypatch.setattr("circuit_weaver.routing_recovery.time.monotonic", lambda: clock[0])

    def exhaust_deadline(_operation, **kwargs):
        calls.append(kwargs.get("search_mode", "standard"))
        clock[0] = 12.0
        return {"status": "error", "message": "no route"}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", exhaust_deadline)
    timed = recover_routing(source2, destination2, timeout_seconds=1, max_links=2, max_pair_attempts=2)
    assert timed["status"] == "partial"
    assert timed["attempts"] == 1
    assert timed["search_modes_tried"] == ["standard"]
    assert calls[-1] == "standard"
