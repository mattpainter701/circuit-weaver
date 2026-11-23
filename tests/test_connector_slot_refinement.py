from __future__ import annotations

import pytest

from circuit_weaver.component_db import ComponentDef, PinDef
from circuit_weaver.placement_optimizer import (
    ComponentPlacement,
    PlacementConfig,
    _build_connectivity_pairs,
    _ConstraintPlan,
    _refine_interchangeable_connector_slots,
)


def native_pad(number: str, local_x: int, local_y: int) -> dict:
    return {
        "number": number,
        "net": "",
        "shape": 0,
        "attribute": 0,
        "size_iu": [1_000_000, 1_000_000],
        "drill_iu": [600_000, 600_000],
        "drill_shape": 1,
        "layers": [0, 31],
        "local_orientation_deg": 0.0,
        "offset_iu": [0, 0],
        "local_position_iu": [local_x, local_y],
        "known_shape": True,
    }


def connector(
    ref: str, private: str, *, footprint_id: str = "Conn:Header_1x2", pad2_size: int = 1_000_000
) -> tuple[ComponentDef, ComponentPlacement]:
    comp = ComponentDef(
        mpn=ref,
        source_ref=ref,
        footprint="Header_1x2",
        category="connector",
        pins=[PinDef("1", "1", "passive", "L"), PinDef("2", "2", "passive", "L")],
        pin_nets={"1": "COMMON", "2": private},
    )
    comp.placement_pin_offsets = {"1": (0.0, -1.27), "2": (0.0, 1.27)}
    comp.placement_geometry_status = "board_geometry"
    comp.placement_native_footprint_id = footprint_id
    comp.placement_native_pad_geometry = [
        {**native_pad("1", 0, -1_270_000), "net": "COMMON"},
        {**native_pad("2", 0, 1_270_000), "net": private, "size_iu": [pad2_size, pad2_size]},
    ]
    comp.placement_pad_offsets = {"COMMON": [(0.0, -1.27)], private: [(0.0, 1.27)]}
    comp.placement_layer = "front"
    comp.placement_width_mm = 2.54
    comp.placement_height_mm = 5.08
    p = ComponentPlacement(
        ref,
        10.0 if ref == "J-left" else 20.0,
        20.0,
        width=2.54,
        height=5.08,
        category="connector",
        geometry_status="board_geometry",
        pin_offsets=comp.placement_pin_offsets.copy(),
        pin_nets=comp.pin_nets.copy(),
        native_footprint_id=footprint_id,
        native_pad_geometry=comp.placement_native_pad_geometry,
        native_reference_locked=False,
        reference_size_mm=(3.0, 1.27) if ref == "J-left" else (5.0, 1.27),
    )
    if pad2_size != 1_000_000:
        p.native_pad_geometry = [{**row} for row in p.native_pad_geometry]
    return comp, p


def owner(ref: str, net: str, x: float) -> tuple[ComponentDef, ComponentPlacement]:
    comp = ComponentDef(
        mpn=ref, source_ref=ref, category="digital", pins=[PinDef("1", "1", "output", "L")], pin_nets={"1": net}
    )
    comp.placement_pin_offsets = {"1": (0.0, 0.0)}
    comp.placement_pad_offsets = {net: [(0.0, 0.0)]}
    comp.placement_native_footprint_id = "Test:Owner"
    comp.placement_native_pad_geometry = [{**native_pad("1", 0, 0), "net": net}]
    comp.placement_geometry_status = "board_geometry"
    comp.placement_width_mm = 3.0
    comp.placement_height_mm = 3.0
    comp.placement_layer = "front"
    p = ComponentPlacement(
        ref,
        x,
        20.0,
        category="digital",
        width=3.0,
        height=3.0,
        geometry_status="board_geometry",
        pin_offsets={"1": (0.0, 0.0)},
        pin_nets={"1": net},
        native_footprint_id="Test:Owner",
        native_pad_geometry=comp.placement_native_pad_geometry,
        native_reference_locked=False,
    )
    return comp, p


def fixture(
    *,
    fixed: bool = False,
    edge: bool = False,
    supported: bool = False,
    left_fp: str = "Conn:Header_1x2",
    right_fp: str = "Conn:Header_1x2",
    left_size: int = 1_000_000,
    right_size: int = 1_000_000,
    left_rotation: float = 0.0,
    right_rotation: float = 0.0,
    left_layer: str = "front",
    reference_locked: bool = False,
    mutate_pad: tuple[str, object] | None = None,
    multiple_private: bool = False,
):
    left_net, right_net = "LOAD-B", "LOAD-A"
    c1, j1 = connector("J-left", left_net, footprint_id=left_fp, pad2_size=left_size)
    c2, j2 = connector("J-right", right_net, footprint_id=right_fp, pad2_size=right_size)
    j1.rotation, j2.rotation = left_rotation, right_rotation
    j1.layer = left_layer
    j1.native_reference_locked = reference_locked
    if mutate_pad:
        j1.native_pad_geometry[0][mutate_pad[0]] = mutate_pad[1]
    o1, u1 = owner("U-alpha", "LOAD-A", 10.0)
    o2, u2 = owner("U-beta", "LOAD-B", 20.0)
    if multiple_private:
        c1.pin_nets["3"] = "LOAD-C"
        c1.placement_pin_offsets["3"] = (1.27, 0.0)
        j1.pin_nets["3"] = "LOAD-C"
        j1.pin_offsets["3"] = (1.27, 0.0)
        j1.native_pad_geometry.append({**native_pad("3", 1_270_000, 0), "net": "LOAD-C"})
        o3, u3 = owner("U-gamma", "LOAD-C", 15.0)
        return [c1, c2, o1, o2, o3], [j1, j2, u1, u2, u3]
    components, state = [c1, c2, o1, o2], [j1, j2, u1, u2]
    plan = _ConstraintPlan()
    if fixed:
        plan.fixed["J-left"] = {"x_mm": j1.x, "y_mm": j1.y}
        j1.constraint_locked = j1.locked = True
    if edge:
        plan.edges["J-right"] = {"edge": "right", "max_distance_mm": 1.0}
        j2.edge_constrained = j2.rotation_locked = True
    if supported:
        support = ComponentDef(mpn="C-support", source_ref="C-support", category="passive")
        support.placement_parent_ref = "J-left"
        components.append(support)
    return components, state, plan


def run(components, state, plan=None, *, max_group_size=256):
    config = PlacementConfig(board_width_mm=40, board_height_mm=40, min_component_gap_mm=0.0)
    return _refine_interchangeable_connector_slots(
        components,
        state,
        config,
        _build_connectivity_pairs(components),
        plan or _ConstraintPlan(),
        max_group_size=max_group_size,
    )


def test_euclidean_assignment_is_deterministic_and_preserves_component_identity():
    components, state, plan = fixture()
    before_identity = [
        (
            p.ref,
            p.pin_nets.copy(),
            p.native_footprint_id,
            p.reference_size_mm,
            [row.copy() for row in p.native_pad_geometry],
        )
        for p in state
    ]
    first, detail = run(components, state, plan)
    second, detail2 = run(components, state, plan)
    assert detail["accepted"] is True
    assert detail["moved_refs"] == ["J-left", "J-right"]
    assert [(p.ref, p.x, p.y, p.rotation, p.layer) for p in first[:2]] == [
        ("J-left", 20.0, 20.0, 0.0, "front"),
        ("J-right", 10.0, 20.0, 0.0, "front"),
    ]
    assert [
        (p.ref, p.pin_nets.copy(), p.native_footprint_id, p.reference_size_mm, p.native_pad_geometry) for p in first[:2]
    ] == before_identity[:2]
    assert [
        (
            p.ref,
            p.pin_nets.copy(),
            p.native_footprint_id,
            p.reference_size_mm,
            [row.copy() for row in p.native_pad_geometry],
        )
        for p in state
    ] == before_identity
    assert [(p.ref, p.x, p.y) for p in first] == [(p.ref, p.x, p.y) for p in second]
    assert detail["assignment"] == detail2["assignment"]
    assert detail["geometry_multiset_preserved"] is True
    assert detail["groups"][0]["matching_cost_evaluations"] == 4
    assert detail["groups"][0]["cost_matrix_entries"] == 4
    assert detail["groups"][0]["assignment_cubic_size_bound"] == 8


def test_equal_rank_candidate_falls_back_without_changing_input():
    components, state, plan = fixture()
    # Force a symmetric arrangement where both slots have equal distance to
    # both owners and swap cannot improve the ordinary rank.
    state[2].x, state[2].y = 15.0, 15.0
    state[3].x, state[3].y = 15.0, 25.0
    state[0].x, state[0].y = 10.0, 20.0
    state[1].x, state[1].y = 20.0, 20.0
    before = [(p.ref, p.x, p.y) for p in state]
    result, detail = run(components, state, plan)
    assert detail["accepted"] is False
    assert [(p.ref, p.x, p.y) for p in result] == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fixed": True},
        {"edge": True},
        {"supported": True},
        {"left_fp": "Other:Header"},
        {"left_size": 1_200_000},
        {"left_rotation": 90.0},
        {"left_layer": "back"},
        {"reference_locked": True},
        {"mutate_pad": ("drill_iu", [500_000, 500_000])},
        {"mutate_pad": ("attribute", 1)},
        {"mutate_pad": ("layers", [0, 31, 32])},
        {"mutate_pad": ("local_orientation_deg", 45.0)},
        {"mutate_pad": ("local_position_iu", [1000, 1000])},
        {"mutate_pad": ("offset_iu", [1000, 1000])},
        {"mutate_pad": ("shape", 4)},
    ],
)
def test_constraints_and_incompatible_slots_are_excluded(kwargs):
    components, state, plan = fixture(**kwargs)
    result, detail = run(components, state, plan)
    assert detail["accepted"] is False
    assert [(p.ref, p.x, p.y, p.rotation) for p in result] == [(p.ref, p.x, p.y, p.rotation) for p in state]


def test_multiple_private_nets_are_excluded():
    components, state = fixture(multiple_private=True)
    result, detail = run(components, state)
    assert detail["accepted"] is False
    assert detail["eligible_component_count"] == 1
    assert [(p.ref, p.x, p.y) for p in result] == [(p.ref, p.x, p.y) for p in state]


def test_oversized_group_is_explicitly_skipped():
    components, state, plan = fixture()
    result, detail = run(components, state, plan, max_group_size=1)
    assert detail["accepted"] is False
    assert detail["groups"] == [{"size": 2, "skipped": "group_size_limit"}]
    assert [(p.ref, p.x, p.y) for p in result] == [(p.ref, p.x, p.y) for p in state]


def test_unknown_native_pad_shape_fails_closed():
    components, state, plan = fixture()
    state[0].native_pad_geometry[0]["known_shape"] = False
    result, detail = run(components, state, plan)
    assert detail["eligible_component_count"] == 1
    assert detail["accepted"] is False
    assert [(p.ref, p.x, p.y) for p in result] == [(p.ref, p.x, p.y) for p in state]


def test_optimizer_runs_slot_refinement_after_all_restart_refinement(monkeypatch):
    import circuit_weaver.placement_optimizer as optimizer

    components, _state, _plan = fixture()
    order = []
    original_refine = optimizer._refine_placement
    original_slots = optimizer._refine_interchangeable_connector_slots

    def observed_refine(*args, **kwargs):
        order.append("restart_refinement")
        return original_refine(*args, **kwargs)

    def observed_slots(*args, **kwargs):
        order.append("final_slot_candidate")
        return original_slots(*args, **kwargs)

    monkeypatch.setattr(optimizer, "_refine_placement", observed_refine)
    monkeypatch.setattr(optimizer, "_refine_interchangeable_connector_slots", observed_slots)
    result = optimizer.optimize_placement(
        components,
        config=optimizer.PlacementConfig(
            board_width_mm=40,
            board_height_mm=40,
            iterations=2,
            restarts=1,
            refinement_passes=0,
            min_component_gap_mm=0.0,
            seed=7,
        ),
    )
    assert order == ["restart_refinement", "final_slot_candidate"]
    assert result["search"]["native_connector_slot_refinement"]["execution_order"] == (
        "final candidate after all annealing and refinement restarts"
    )


def test_partial_native_endpoint_inventory_makes_private_net_ambiguous():
    components, state, plan = fixture()
    hidden = ComponentDef(
        mpn="U-extra",
        source_ref="U-extra",
        category="digital",
        pins=[PinDef("1", "1", "input", "L")],
        pin_nets={"1": "LOAD-B"},
    )
    hidden.placement_geometry_status = "board_geometry"
    hidden.placement_native_pad_geometry = [{**native_pad("1", 0, 0), "net": "LOAD-B"}]
    # The third native endpoint is present in the pad inventory but omitted
    # from the optimizer offset map, so it must not disappear from net counts.
    hidden.placement_pin_offsets = {}
    hidden_state = ComponentPlacement(
        "U-extra",
        30.0,
        20.0,
        category="digital",
        geometry_status="board_geometry",
        pin_offsets={},
        pin_nets={"1": "LOAD-B"},
        native_footprint_id="Test:Extra",
        native_pad_geometry=hidden.placement_native_pad_geometry,
        native_reference_locked=False,
    )
    components.append(hidden)
    state.append(hidden_state)
    result, detail = run(components, state, plan)
    assert detail["accepted"] is False
    assert detail["eligible_component_count"] == 1
    assert [(p.ref, p.x, p.y) for p in result] == [(p.ref, p.x, p.y) for p in state]


@pytest.mark.parametrize("matrix", [[[float("nan")]], [[float("inf")]], [[0.0, 1.0], [2.0]]])
def test_assignment_rejects_malformed_or_nonfinite_cost_matrix(matrix):
    from circuit_weaver.placement_optimizer import _slot_hungarian

    with pytest.raises(ValueError):
        _slot_hungarian(matrix)
