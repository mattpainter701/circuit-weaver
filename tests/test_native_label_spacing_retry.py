from pathlib import Path

from circuit_weaver.pcb_layout import _design_min_component_gap, _native_reference_gap_floor, place_pcb


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
        return outcomes[len(calls) - 1]

    monkeypatch.setattr("circuit_weaver.pcb_layout._place_pcb_once", place_once)
    result = place_pcb(source, tmp_path / "placed.kicad_pcb", design_spec=design_spec,
                       seed=17, iterations=6000, tidy_references=True)
    return result, calls


def test_spacing_feedback_starts_after_native_label_rejection(tmp_path, monkeypatch):
    result, calls = _run_rejected_search(
        tmp_path, monkeypatch, [_candidate(), _candidate(), _candidate()])

    assert result["status"] == "error"
    assert [call["feedback_min_component_gap_mm"] for call in calls] == [None, 2.5, 3.0]
    assert [attempt["min_component_gap_mm"] for attempt in result["placement_attempts"]] == [0.5, 2.5, 3.0]
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
