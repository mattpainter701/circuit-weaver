from pathlib import Path

import pytest

from circuit_weaver.pcb_layout import (
    _design_min_component_gap,
    _effective_min_component_gap,
    _native_reference_gap_floor,
    place_pcb,
)


def _candidate(unresolved=1, conflicts=1):
    return {
        "status": "ok",
        "placement": {
            "silkscreen": {
                "unresolved_count": unresolved,
                "remaining_conflict_count": conflicts,
            },
            "native_reference_gap_floor_mm": 2.5,
        },
        "drc": {"status": "ok"},
    }


def _run_rejected_search(tmp_path, monkeypatch, outcomes, *, design_spec=None):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"source")
    calls = []

    def place_once(board_path, output_path, **kwargs):
        calls.append(kwargs)
        outcome = dict(outcomes[len(calls) - 1])
        placement = dict(outcome.get("placement", {}))
        placement["effective_min_component_gap_mm"] = _effective_min_component_gap(
            design_spec, placement.get("native_reference_gap_floor_mm", 0.5),
            kwargs.get("feedback_min_component_gap_mm"))
        outcome["placement"] = placement
        return outcome

    monkeypatch.setattr("circuit_weaver.pcb_layout._place_pcb_once", place_once)
    result = place_pcb(source, tmp_path / "placed.kicad_pcb", design_spec=design_spec,
                       seed=17, iterations=6000, tidy_references=True)
    return result, calls


def test_spacing_feedback_starts_after_native_label_rejection(tmp_path, monkeypatch):
    result, calls = _run_rejected_search(
        tmp_path, monkeypatch, [_candidate(), _candidate(), _candidate()])

    assert result["status"] == "error"
    assert [call["feedback_min_component_gap_mm"] for call in calls] == [None, 3.0, 3.5]
    assert [attempt["min_component_gap_mm"] for attempt in result["placement_attempts"]] == [2.5, 3.0, 3.5]
    assert [call["seed"] for call in calls] == [17, 18, 19]
    assert all(call["iterations"] == 6000 for call in calls)


def test_spacing_feedback_preserves_explicit_board_minimum(tmp_path, monkeypatch):
    result, calls = _run_rejected_search(
        tmp_path, monkeypatch, [_candidate(), _candidate(), _candidate()],
        design_spec={"pcb_constraints": [{"kind": "placement", "target": "board",
                                         "min_component_gap_mm": "3.0"}]})

    assert result["status"] == "error"
    assert [call["feedback_min_component_gap_mm"] for call in calls] == [None, 3.5, 4.0]
    assert [attempt["min_component_gap_mm"] for attempt in result["placement_attempts"]] == [3.0, 3.5, 4.0]


def test_missing_or_malformed_native_report_fails_closed(tmp_path, monkeypatch):
    incomplete = _candidate()
    incomplete["placement"]["silkscreen"].pop("remaining_conflict_count")
    invalid = _candidate(unresolved=True)
    for case, candidate, reason in (
        ("incomplete", incomplete, "native_silkscreen_report_missing"),
        ("invalid", invalid, "native_silkscreen_count_invalid"),
    ):
        case_path = tmp_path / case
        case_path.mkdir()
        result, calls = _run_rejected_search(case_path, monkeypatch, [candidate, _candidate(), _candidate()])
        assert result["status"] == "error"
        assert len(calls) == len(result["placement_attempts"]) == 1
        assert calls[0]["feedback_min_component_gap_mm"] is None
        assert result["placement_attempts"][0]["rejection_reason"] == reason


def test_candidate_error_with_complete_label_report_fails_closed(tmp_path, monkeypatch):
    failed = _candidate()
    failed["status"] = "error"
    result, calls = _run_rejected_search(tmp_path, monkeypatch, [failed, _candidate(), _candidate()])

    assert result["status"] == "error"
    assert len(calls) == len(result["placement_attempts"]) == 1
    assert calls[0]["feedback_min_component_gap_mm"] is None


def test_source_change_aborts_before_feedback_retry(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    source.write_bytes(b"source")
    calls = []

    def place_once(board_path, output_path, **kwargs):
        calls.append(kwargs)
        Path(board_path).write_bytes(b"concurrent source change")
        return _candidate()

    monkeypatch.setattr("circuit_weaver.pcb_layout._place_pcb_once", place_once)
    result = place_pcb(source, tmp_path / "placed.kicad_pcb", seed=3, tidy_references=True)

    assert result["status"] == "error"
    assert "changed during processing" in result["message"]
    assert len(calls) == 1
    assert calls[0]["feedback_min_component_gap_mm"] is None


def test_native_spacing_floor_uses_clearance_padded_reference_height():
    assert round(_native_reference_gap_floor([{"reference_size_mm": [3.55, 2.0965]}]), 4) == 2.4965
    assert _native_reference_gap_floor([{}]) == 0.5


def test_authored_gap_floor_matches_optimizer_board_aliases_and_order():
    assert _design_min_component_gap({"pcb_constraints": [
        {"type": "placement", "ref": "board", "min_component_gap_mm": 3.0},
        {"kind": "placement", "target": "other", "board_width_mm": 160,
         "min_component_gap_mm": 4.0},
        {"type": "placement", "ref": "board", "min_component_gap_mm": "5.0"},
        {"kind": "routing", "target": "board", "min_component_gap_mm": 9.0},
    ]}) == 5.0
    assert _design_min_component_gap({"pcb_constraints": [
        {"kind": "placement", "board": {"width_mm": 160}, "min_component_gap_mm": 0.1},
    ]}) == 0.1


def test_effective_gap_uses_largest_authored_native_and_feedback_floor():
    spec = {"pcb_constraints": [{"kind": "placement", "target": "board",
                                 "min_component_gap_mm": 3.25}]}
    assert _effective_min_component_gap(spec, 2.5) == 3.25
    assert _effective_min_component_gap(None, 2.5) == 2.5
    assert _effective_min_component_gap(None, 2.5, 3.0) == 3.0
    assert _effective_min_component_gap(spec, 4.0, 3.5) == 4.0


def test_tidy_false_does_not_apply_native_reference_floor(tmp_path, monkeypatch):
    from circuit_weaver.drc_runner import DrcResult
    from circuit_weaver.pcb_layout import _place_pcb_once

    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    footprint = {"ref": "R1", "footprint": "Resistor_SMD:R_0603_1608Metric",
                 "width": 2.0, "height": 1.0, "courtyard_width": 2.0,
                 "courtyard_height": 1.0, "courtyard_center_x": 0.0,
                 "courtyard_center_y": 0.0, "reference_size_mm": [3.5, 2.0965],
                 "pads": [], "layer": "front", "locked": False}
    monkeypatch.setattr("circuit_weaver.pcb_layout.preflight_pcb", lambda *_: {"routable": True})
    def worker(operation, **kwargs):
        if operation == "inspect":
            return {"status": "ok", "rectangular_outline": True, "track_count": 0, "zone_count": 0,
                    "bounds": {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0},
                    "footprints": [footprint]}
        Path(kwargs["output"]).write_bytes(b"staged")
        return {"status": "ok", "silkscreen": {}}
    observed = {}
    def optimizer(components, *, config, constraints):
        observed["constraints"] = constraints
        return {"placements": {"R1": {"x": 10.0, "y": 10.0, "rotation": 0.0, "layer": "front"}},
                "quality": {"review_required": False},
                "board_width_mm": config.board_width_mm, "board_height_mm": config.board_height_mm}
    monkeypatch.setattr("circuit_weaver.pcb_layout.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_layout.optimize_placement", optimizer)
    monkeypatch.setattr(
        "circuit_weaver.pcb_layout.run_drc", lambda board, **_: DrcResult(status="ok", board=str(board)))
    monkeypatch.setattr("circuit_weaver.pcb_layout._publish", lambda staged, dest, *_: Path(staged).replace(dest))

    result = _place_pcb_once(source, destination, tidy_references=False)

    assert result["status"] == "ok"
    assert round(result["placement"]["native_reference_gap_floor_mm"], 4) == 2.4965
    assert not any(rule.get("target") == "board" and rule.get("min_component_gap_mm") == 2.4965
                   for rule in observed["constraints"])


@pytest.mark.parametrize("failure_stage", [None, "worker", "drc"])
def test_tidy_first_attempt_applies_native_floor_before_optimizer(tmp_path, monkeypatch, failure_stage):
    from circuit_weaver.drc_runner import DrcResult
    from circuit_weaver.pcb_layout import _place_pcb_once

    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    footprint = {"ref": "R1", "footprint": "Resistor_SMD:R_0603_1608Metric",
                 "width": 2.0, "height": 1.0, "courtyard_width": 2.0,
                 "courtyard_height": 1.0, "courtyard_center_x": 0.0,
                 "courtyard_center_y": 0.0, "reference_size_mm": [3.5, 2.0965],
                 "pads": [], "layer": "front", "locked": False}
    monkeypatch.setattr("circuit_weaver.pcb_layout.preflight_pcb", lambda *_: {"routable": True})

    def worker(operation, **kwargs):
        if operation == "inspect":
            return {"status": "ok", "rectangular_outline": True, "track_count": 0, "zone_count": 0,
                    "bounds": {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0},
                    "footprints": [footprint]}
        if failure_stage == "worker":
            return {"status": "error", "message": "Native diagnostic failure"}
        Path(kwargs["output"]).write_bytes(b"staged")
        return {"status": "ok", "silkscreen": {"unresolved_count": 0,
                                                    "remaining_conflict_count": 0}}

    observed = {}
    def optimizer(components, *, config, constraints):
        observed["calls"] = observed.get("calls", 0) + 1
        observed["constraints"] = constraints
        return {"placements": {"R1": {"x": 10.0, "y": 10.0, "rotation": 0.0, "layer": "front"}},
                "quality": {"review_required": False},
                "board_width_mm": config.board_width_mm, "board_height_mm": config.board_height_mm}

    monkeypatch.setattr("circuit_weaver.pcb_layout.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_layout.optimize_placement", optimizer)
    monkeypatch.setattr(
        "circuit_weaver.pcb_layout.run_drc",
        lambda board, **_: DrcResult(status="error" if failure_stage == "drc" else "ok", board=str(board)))
    monkeypatch.setattr("circuit_weaver.pcb_layout._publish", lambda staged, dest, *_: Path(staged).replace(dest))

    result = _place_pcb_once(source, destination, tidy_references=True)

    assert result["status"] == ("error" if failure_stage else "ok")
    assert destination.exists() is (failure_stage is None)
    assert source.read_bytes() == b"source"
    assert observed["calls"] == 1  # Native failures cannot trigger the optimizer-only fallback.
    board_gaps = [rule["min_component_gap_mm"] for rule in observed["constraints"]
                  if rule.get("target") == "board" and "min_component_gap_mm" in rule]
    assert len(board_gaps) == 1 and round(board_gaps[0], 4) == 2.4965
    assert round(result["placement"]["native_reference_gap_floor_mm"], 4) == 2.4965
    assert round(result["placement"]["effective_min_component_gap_mm"], 4) == 2.4965


@pytest.mark.parametrize("fallback_review_required", [False, True])
@pytest.mark.parametrize("feedback_floor", [None, 1.75, 3.0])
def test_native_floor_review_retries_once_at_authored_gap_and_keeps_gates(
        tmp_path, monkeypatch, fallback_review_required, feedback_floor):
    from circuit_weaver.drc_runner import DrcResult
    from circuit_weaver.pcb_layout import _place_pcb_once

    source, destination = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_bytes(b"source")
    footprint = {"ref": "R1", "footprint": "Resistor_SMD:R_0603_1608Metric",
                 "width": 2.0, "height": 1.0, "courtyard_width": 2.0,
                 "courtyard_height": 1.0, "courtyard_center_x": 0.0,
                 "courtyard_center_y": 0.0, "reference_size_mm": [3.5, 2.0965],
                 "pads": [], "layer": "front", "locked": False}
    monkeypatch.setattr("circuit_weaver.pcb_layout.preflight_pcb", lambda *_: {"routable": True})
    operations = []

    def worker(operation, **kwargs):
        operations.append(operation)
        if operation == "inspect":
            return {"status": "ok", "rectangular_outline": True, "track_count": 0, "zone_count": 0,
                    "bounds": {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0},
                    "footprints": [footprint]}
        Path(kwargs["output"]).write_bytes(b"staged")
        return {"status": "ok", "silkscreen": {"unresolved_count": 0,
                                                    "remaining_conflict_count": 0}}

    calls = []

    def optimizer(components, *, config, constraints):
        gaps = [rule["min_component_gap_mm"] for rule in constraints
                if rule.get("target") == "board" and "min_component_gap_mm" in rule]
        calls.append((config, gaps[-1]))
        review_required = len(calls) == 1 or fallback_review_required
        return {"placements": {"R1": {"x": 10.0, "y": 10.0, "rotation": 0.0, "layer": "front"}},
                "quality": {"review_required": review_required},
                "board_width_mm": config.board_width_mm, "board_height_mm": config.board_height_mm}

    monkeypatch.setattr("circuit_weaver.pcb_layout.run_kicad", worker)
    monkeypatch.setattr("circuit_weaver.pcb_layout.optimize_placement", optimizer)
    monkeypatch.setattr(
        "circuit_weaver.pcb_layout.run_drc", lambda board, **_: DrcResult(status="ok", board=str(board)))
    monkeypatch.setattr("circuit_weaver.pcb_layout._publish", lambda staged, dest, *_: Path(staged).replace(dest))

    result = _place_pcb_once(source, destination, iterations=73, seed=11, tidy_references=True,
                             feedback_min_component_gap_mm=feedback_floor)
    placement = result["placement"]

    if feedback_floor is not None and feedback_floor >= 2.4965:
        assert [round(gap, 4) for _, gap in calls] == [3.0]
        assert result["status"] == "error"
        assert placement["effective_min_component_gap_mm"] == 3.0
        assert len(placement["gap_search_attempts"]) == 1
        assert "place" not in operations
        return

    fallback_gap = max(0.5, feedback_floor or 0)
    assert [round(gap, 4) for _, gap in calls] == [2.4965, fallback_gap]
    assert calls[0][0] == calls[1][0]
    assert round(placement["preferred_min_component_gap_mm"], 4) == 2.4965
    assert placement["effective_min_component_gap_mm"] == fallback_gap
    assert [search["iterations"] for search in placement["gap_search_attempts"]] == [73, 73]
    assert [search["seed"] for search in placement["gap_search_attempts"]] == [11, 11]

    if fallback_review_required:
        assert result["status"] == "error"
        assert "place" not in operations
        assert [search["review_required"] for search in placement["gap_search_attempts"]] == [True, True]
    else:
        assert result["status"] == "ok"
        assert [search["review_required"] for search in placement["gap_search_attempts"]] == [True, False]
