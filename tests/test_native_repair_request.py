"""Request-boundary tests for native same-net repair search bounds."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from circuit_weaver import _kicad_worker as worker
from circuit_weaver import _native_routing_repair as repair


class _RepairDispatched(Exception):
    """Stop worker execution immediately after observing the repair call."""


@pytest.mark.parametrize(
    ("request_fields", "expected"),
    [
        ({}, {"pair_index": 0, "max_expanded_states": 150_000, "grid_step_mm": 0.25}),
        (
            {"pair_index": 7, "max_expanded_states": 1, "grid_step_mm": 1.0},
            {"pair_index": 7, "max_expanded_states": 1, "grid_step_mm": 1.0},
        ),
    ],
    ids=("backward-compatible-defaults", "explicit-bounded-profile"),
)
def test_repair_open_worker_forwards_search_profile_to_native_repair(
    monkeypatch, request_fields, expected,
):
    board = SimpleNamespace(Zones=lambda: [])
    pcbnew = SimpleNamespace(LoadBoard=lambda _path: board)
    monkeypatch.setitem(sys.modules, "pcbnew", pcbnew)
    monkeypatch.setattr(worker, "_identity", lambda _board: {"J1": [("1", "NET_A")]})
    monkeypatch.setattr(worker, "_copper_state", lambda _board: frozenset())
    monkeypatch.setattr(worker, "_fixed_layout_state", lambda _board: "fixed-layout")
    monkeypatch.setattr(
        worker, "_ground_plane_source_geometry_state", lambda _board, _pcbnew: "source-geometry",
    )

    observed = {}

    def capture_repair(_board, _pcbnew, _report, **kwargs):
        observed.update(kwargs)
        raise _RepairDispatched

    monkeypatch.setitem(
        sys.modules, "_native_routing_repair", SimpleNamespace(repair_one=capture_repair),
    )
    request = {
        "operation": "repair_open",
        "board": "isolated-worker-copy.kicad_pcb",
        "drc_report": {"unconnected_items": []},
        **request_fields,
    }

    with pytest.raises(_RepairDispatched):
        worker.execute(request)

    assert observed == expected


@pytest.mark.parametrize(
    ("max_expanded_states", "grid_step_mm"),
    [(0, 1.0), (1, 0.0)],
    ids=("zero-state-budget", "zero-grid-step"),
)
def test_forwarded_search_bounds_are_rejected_before_native_mutation(
    monkeypatch, max_expanded_states, grid_step_mm,
):
    board = SimpleNamespace(Zones=lambda: [])
    monkeypatch.setitem(sys.modules, "_kicad_worker", SimpleNamespace())

    with pytest.raises(ValueError, match="repair search bounds are invalid"):
        repair.repair_one(
            board,
            SimpleNamespace(),
            {"unconnected_items": []},
            max_expanded_states=max_expanded_states,
            grid_step_mm=grid_step_mm,
        )
