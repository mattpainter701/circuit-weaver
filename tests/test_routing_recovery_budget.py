"""Keep native recovery fallback families reachable under one shared deadline."""

import hashlib
import shutil
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from circuit_weaver import routing_recovery as recovery
from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.validator import ValidationIssue


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds

    def set(self, value):
        self.value = value


def _drc(path, opens):
    board = Path(path)
    findings = tuple(
        ValidationIssue("drc", rule_id="CW-DRC-002", severity="blocker") for _ in range(opens)
    )
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
    return DrcResult(
        status="ok",
        board=str(board),
        board_sha256=hashlib.sha256(board.read_bytes()).hexdigest(),
        findings=findings,
        raw_report=raw,
    )


def _standard_report():
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


def _bidirectional_report(phase, grid_step_mm=0.5, pair_index=0):
    from_uuid, to_uuid = f"pad-{pair_index}-a", f"pad-{pair_index}-b"
    return {
        **_standard_report(),
        "from_uuid": from_uuid,
        "to_uuid": to_uuid,
        "search_mode": "bidirectional",
        "search_direction": "bidirectional",
        "requested_grid_step_mm": grid_step_mm,
        "search_grid_step_mm": grid_step_mm,
        "search_state_budget": 150000,
        "requested_grid_phase": list(phase),
        "search_grid_phase": list(phase),
        "search_grid_origin_mm": [10.0, 20.0],
        "connected_endpoint_anchors": {
            "verified": True,
            "from_uuid": from_uuid,
            "to_uuid": to_uuid,
            "from_cluster_item_count": 1,
            "to_cluster_item_count": 1,
            "anchor_vertex_limit_per_cluster": 32,
            "search_strategy": "single_shared_bidirectional_multiroot",
            "from_anchor_uuid": "anchor-a",
            "to_anchor_uuid": "anchor-b",
            "from_anchor_point_mm": [10.0, 20.0],
            "to_anchor_point_mm": [11.0, 20.0],
            "from_anchor_layer": 0,
            "to_anchor_layer": 0,
            "search_grid_origin_mm": [10.0, 20.0],
            "search_state_budget": 150000,
        },
    }


def _setup(tmp_path, monkeypatch, clock, *, drc_counts, worker, prep=None):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"original partial pcb")
    output = tmp_path / "recovered.kicad_pcb"
    monkeypatch.setattr(recovery, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(recovery, "run_kicad", worker)

    def run_drc(path, **_):
        if prep is not None:
            prep.on_drc()
        return _drc(path, next(drc_counts))

    monkeypatch.setattr(recovery, "run_drc", run_drc)

    def prepare_settings(src, staged, **_):
        if prep is not None:
            prep.on_prepare(Path(staged))
        return {source: hashlib.sha256(source.read_bytes()).hexdigest()}

    monkeypatch.setattr(recovery, "_prepare_settings", prepare_settings)
    monkeypatch.setattr(recovery, "_restore_settings", lambda *_: None)
    monkeypatch.setattr(recovery, "_check_snapshots", lambda *_: None)
    monkeypatch.setattr(recovery, "stage_project_footprint_context", lambda *_: {"status": "ok"})
    monkeypatch.setattr(recovery, "_prepare_destination", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(recovery, "_publication_set", lambda staged, destination: [(staged, destination)])
    monkeypatch.setattr(recovery, "_pcb_output_lock", lambda *_: nullcontext())
    monkeypatch.setattr(
        recovery,
        "_publish_staged_transaction",
        lambda _root, publications: [shutil.copy2(src, dst) for src, dst in publications],
    )
    return source, output


def test_progress_in_standard_keeps_standard_active_then_carries_unused_time(tmp_path, monkeypatch):
    clock = FakeClock()
    counts = iter((3, 2, 1, 0, 0))
    calls = []

    def worker(_operation, **kwargs):
        mode = kwargs.get("search_mode", "standard")
        calls.append((mode, kwargs.get("grid_phase"), kwargs["timeout"]))
        if mode == "standard" and len(calls) == 3:
            clock.advance(2.0)
            return {"status": "error", "message": "no standard route"}
        clock.advance(5.0 if mode == "standard" else 1.0)
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" route")
        report = _standard_report() if mode == "standard" else _bidirectional_report(
            kwargs["grid_phase"], pair_index=kwargs["pair_index"]
        )
        return {"status": "ok", "route_repair": report}

    def drc_clock():
        clock.advance(1.0)

    class Prep:
        def on_prepare(self, _path):
            pass

        def on_drc(self):
            drc_clock()

    source, output = _setup(tmp_path, monkeypatch, clock, drc_counts=counts, worker=worker, prep=Prep())
    result = recovery.recover_routing(source, output, timeout_seconds=100, max_links=3, max_pair_attempts=4)

    assert result["status"] == "ok"
    assert [call[0] for call in calls] == ["standard", "standard", "standard", "bidirectional"]
    assert calls[-1][1] == [0.5, 0.5]
    event = next(event for event in result["profile_budget_events"] if event.get("from_family") == "standard")
    assert event["reason"] == "no_progress"
    assert event["carried_seconds"] > 0
    assert event["next_reserve_seconds"] > 0
    plan = result["profile_budget_events"][0]
    assert [row["reserve_seconds"] for row in plan["family_reserves"]] == pytest.approx([49.5, 19.8, 14.85, 14.85])
    assert output.is_file()


def test_preparation_expiry_is_not_an_attempt_and_next_family_uses_unique_prep_id(tmp_path, monkeypatch):
    clock = FakeClock()
    counts = iter((1, 0, 0))
    calls = []

    class Prep:
        def on_prepare(self, path):
            if path.parent.name == "proposal-0001":
                clock.set(51.0)

        def on_drc(self):
            pass

    def worker(_operation, **kwargs):
        calls.append((kwargs.get("search_mode", "standard"), kwargs.get("grid_phase"), kwargs["output"]))
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" route")
        return {"status": "ok", "route_repair": _bidirectional_report(
            kwargs["grid_phase"], pair_index=kwargs["pair_index"]
        )}

    source, output = _setup(tmp_path, monkeypatch, clock, drc_counts=counts, worker=worker, prep=Prep())
    result = recovery.recover_routing(source, output, timeout_seconds=100, max_links=2, max_pair_attempts=3)

    assert result["status"] == "ok"
    assert result["attempts"] == 1
    assert len(calls) == 1 and calls[0][0] == "bidirectional"
    assert calls[0][1] == [0.5, 0.5]
    unlaunched = [event for event in result["profile_budget_events"] if event.get("counted_as_attempt") is False]
    assert len(unlaunched) == 1
    assert unlaunched[0]["proposal_preparation_id"] == 1
    assert result["history"][0]["proposal_preparation_id"] == 2


def test_global_deadline_rejects_candidate_without_fresh_drc_or_publication(tmp_path, monkeypatch):
    clock = FakeClock()
    counts = iter((1,))
    drc_calls = []

    class Prep:
        def on_prepare(self, _path):
            pass

        def on_drc(self):
            drc_calls.append(clock.value)

    def worker(_operation, **kwargs):
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" late candidate")
        clock.set(10.0)
        return {"status": "ok", "route_repair": _standard_report()}

    source, output = _setup(tmp_path, monkeypatch, clock, drc_counts=counts, worker=worker, prep=Prep())
    result = recovery.recover_routing(source, output, timeout_seconds=10, max_links=2, max_pair_attempts=3)

    assert result["status"] == "partial"
    assert result["attempts"] == 1
    assert drc_calls == [0.0]
    assert result["history"][0]["rejected_reason"] == "global_deadline_before_fresh_drc"
    assert not output.exists()


def test_expired_resolution_family_skips_unstarted_phases_and_keeps_later_reserve(tmp_path, monkeypatch):
    clock = FakeClock()
    counts = iter((1, 0, 0))
    calls = []

    class Prep:
        def on_prepare(self, path):
            if path.parent.name == "proposal-0002":
                # The .5 mm family has consumed its carried time and reserve.
                clock.set(71.0)

        def on_drc(self):
            pass

    def worker(_operation, **kwargs):
        mode = kwargs.get("search_mode", "standard")
        step = kwargs.get("grid_step_mm")
        calls.append((mode, step, kwargs.get("grid_phase")))
        if mode == "standard":
            return {"status": "error", "message": "no standard route"}
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" route")
        return {
            "status": "ok",
            "route_repair": _bidirectional_report(
                kwargs["grid_phase"], step, pair_index=kwargs["pair_index"]
            ),
        }

    source, output = _setup(tmp_path, monkeypatch, clock, drc_counts=counts, worker=worker, prep=Prep())
    result = recovery.recover_routing(source, output, timeout_seconds=100, max_links=2, max_pair_attempts=5)

    assert result["status"] == "ok"
    assert result["attempts"] == 2
    assert calls == [("standard", None, None), ("bidirectional", 0.25, [0.5, 0.5])]
    event = next(
        event for event in result["profile_budget_events"]
        if event.get("reason") == "family_budget_exhausted" and event.get("from_family") == "bidirectional_0_5mm"
    )
    assert [profile["grid_phase"] for profile in event["skipped_unstarted_profiles"]] == [
        [0.5, 0.5], [0.5, 0.0], [0.0, 0.5], [0.0, 0.0],
    ]
    assert [profile["grid_step_mm"] for profile in result["search_profiles_tried"]] == [None, 0.25]
    assert result["history"][-1]["proposal_preparation_id"] == 3


def test_positive_subsecond_timeout_is_passed_without_rounding_to_zero_or_one(tmp_path, monkeypatch):
    clock = FakeClock()
    counts = iter((1,))
    timeouts = []

    def worker(_operation, **kwargs):
        timeouts.append(kwargs["timeout"])
        return {"status": "error", "message": "no route"}

    source, output = _setup(tmp_path, monkeypatch, clock, drc_counts=counts, worker=worker)
    result = recovery.recover_routing(source, output, timeout_seconds=0.01, max_links=1, max_pair_attempts=1)

    assert result["status"] == "partial"
    assert result["attempts"] == 1
    assert len(timeouts) == 1
    assert 0 < timeouts[0] < 1


def test_total_attempt_cap_is_still_max_links_times_pair_attempts(tmp_path, monkeypatch):
    clock = FakeClock()
    counts = iter((1,))
    calls = []

    def worker(_operation, **_kwargs):
        calls.append(1)
        return {"status": "error", "message": "no route"}

    source, output = _setup(tmp_path, monkeypatch, clock, drc_counts=counts, worker=worker)
    result = recovery.recover_routing(source, output, timeout_seconds=100, max_links=2, max_pair_attempts=2)

    assert result["status"] == "partial"
    assert result["attempts"] == 4
    assert len(calls) == 4


def test_absolute_deadline_is_shared_across_native_profiles_and_initial_drc(tmp_path, monkeypatch):
    clock = FakeClock()
    counts = iter((1,))
    drc_calls = []
    worker_calls = []

    def run_drc(path, **kwargs):
        drc_calls.append({"timeout": kwargs["timeout"], "deadline": kwargs["deadline"]})
        return _drc(Path(path), next(counts))

    def worker(_operation, **kwargs):
        worker_calls.append({
            "mode": kwargs.get("search_mode", "standard"),
            "phase": kwargs.get("grid_phase"),
            "connected_endpoints": kwargs.get("connected_endpoints"),
            "timeout": kwargs["timeout"],
            "deadline": kwargs["deadline"],
        })
        if len(worker_calls) == 1:
            clock.advance(4.0)
        return {"status": "error", "message": "no route"}

    source, output = _setup(tmp_path, monkeypatch, clock, drc_counts=counts, worker=worker)
    monkeypatch.setattr(recovery, "run_drc", run_drc)
    monkeypatch.setattr(recovery, "run_kicad", worker)

    result = recovery.recover_routing(
        source,
        output,
        timeout_seconds=10.0,
        max_links=1,
        max_pair_attempts=2,
    )

    assert result["status"] == "partial"
    assert result["attempts"] == 2
    assert drc_calls == [{"timeout": 10.0, "deadline": 10.0}]
    assert worker_calls[0] == {
        "mode": "standard",
        "phase": None,
        "connected_endpoints": None,
        "timeout": 5.0,
        "deadline": 5.0,
    }
    assert worker_calls[1]["mode"] == "bidirectional"
    assert worker_calls[1]["phase"] == [0.5, 0.5]
    assert worker_calls[1]["connected_endpoints"] is True
    assert worker_calls[1]["timeout"] == 3.0
    assert worker_calls[1]["deadline"] == 7.0
