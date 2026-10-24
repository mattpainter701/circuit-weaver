"""Tests for placement_optimizer and placement_viewer modules."""

from __future__ import annotations

import hashlib
import json
import math
import random
import subprocess
import sys

import pytest

from circuit_weaver.component_db import ComponentDef, PinDef


def _make_comp(ref: str, mpn: str = "", category: str = "digital", footprint: str = "QFN-32") -> ComponentDef:
    return ComponentDef(
        mpn=mpn or f"IC_{ref}",
        description=f"Test component {ref}",
        footprint=footprint,
        category=category,
        pins=[PinDef(number="1", name="VCC", electrical_type="power_in", side="L")],
        source_ref=ref,
    )


# ---------- placement_optimizer tests ----------


def test_optimize_empty_components():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    result = optimize_placement([], config=PlacementConfig())
    assert result["status"] == "ok"
    assert result["placements"] == {}


@pytest.mark.parametrize(
    "name,value",
    [
        ("restarts", 0),
        ("restarts", True),
        ("restarts", 33),
        ("iterations", -1),
        ("refinement_passes", 21),
        ("board_width_mm", float("nan")),
        ("board_width_mm", "30"),
        ("min_component_gap_mm", -0.5),
        ("cooling_rate", 0),
        ("seed", True),
    ],
)
def test_invalid_search_configuration_fails_before_placement(name, value):
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    with pytest.raises(ValueError):
        optimize_placement([_make_comp("U1")], config=PlacementConfig(**{name: value}))


def test_search_shares_iteration_budget_and_selects_a_legal_candidate():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    components = [_make_comp(f"U{i}") for i in range(1, 9)]
    config = PlacementConfig(iterations=301, restarts=3, seed=12)
    result = optimize_placement(components, config=config)
    search = result["search"]
    assert sum(row["iterations"] for row in search["candidates"]) == 301
    assert len(search["candidates"]) == 4
    assert search["refinement_evaluations"] > 0
    selected = search["candidates"][search["selected_candidate"]]
    assert selected["defects"] == 0
    assert selected["cost"] <= search["candidates"][0]["cost"]
    assert not result["quality"]["review_required"]
    assert result == optimize_placement(components, config=config)


def test_refinement_hard_defect_pruning_matches_exhaustive_candidate_ranking(monkeypatch):
    import circuit_weaver.placement_optimizer as optimizer

    config = optimizer.PlacementConfig(
        board_width_mm=30, board_height_mm=24, refinement_passes=2, min_component_gap_mm=0.5,
    )
    initial = [
        optimizer.ComponentPlacement(
            f"U{index + 1}",
            8 + (index % 4) * 0.35,
            8 + (index // 4) * 0.4,
            width=3.2,
            height=2.4,
            reference_size_mm=(1.2, 0.8),
        )
        for index in range(8)
    ]
    plan = optimizer._ConstraintPlan()
    pairs = {}
    total_cost_calls = 0
    original_total_cost = optimizer._total_cost

    def count_total_cost(*args, **kwargs):
        nonlocal total_cost_calls
        total_cost_calls += 1
        return original_total_cost(*args, **kwargs)

    monkeypatch.setattr(optimizer, "_total_cost", count_total_cost)
    pruned_state, pruned_evaluations = optimizer._refine_placement(
        [optimizer.replace(item) for item in initial], config, pairs, plan,
        skip_worse_hard_defects=True,
    )
    pruned_calls = total_cost_calls
    total_cost_calls = 0
    exhaustive_state, exhaustive_evaluations = optimizer._refine_placement(
        [optimizer.replace(item) for item in initial], config, pairs, plan,
        skip_worse_hard_defects=False,
    )

    assert pruned_state == exhaustive_state
    assert pruned_evaluations == exhaustive_evaluations
    assert pruned_calls < total_cost_calls


def test_refinement_evaluation_budget_counts_rejected_candidates_and_skips_family_search(monkeypatch):
    import circuit_weaver.placement_optimizer as optimizer

    config = optimizer.PlacementConfig(
        board_width_mm=30, board_height_mm=24, refinement_passes=2,
    )
    state = [
        optimizer.ComponentPlacement(
            f"U{index + 1}", 8 + index * 0.2, 8, width=3.2, height=2.4,
        )
        for index in range(4)
    ]
    defect_calls = 0

    def hard_defects(_candidate, _config, _plan):
        nonlocal defect_calls
        defect_calls += 1
        return 0 if defect_calls == 1 else 1

    def fail_if_family_search_runs(*_args, **_kwargs):
        raise AssertionError("family candidates must not be built after budget exhaustion")

    monkeypatch.setattr(optimizer, "_placement_hard_defects", hard_defects)
    monkeypatch.setattr(optimizer, "_family_alignment_candidates", fail_if_family_search_runs)
    refined, evaluations = optimizer._refine_placement(
        state, config, {}, optimizer._ConstraintPlan(), evaluation_budget=3,
    )

    assert evaluations == 3
    assert defect_calls == 4  # initial ranking plus all three hard-defect rejections
    assert refined == state


def test_optimizer_reports_refinement_budgets_and_preserves_locked_constraints(monkeypatch):
    import circuit_weaver.placement_optimizer as optimizer

    original_refine = optimizer._refine_placement
    requested_budgets = []

    def capture_budget(*args, **kwargs):
        requested_budgets.append(kwargs["evaluation_budget"])
        return original_refine(*args, **kwargs)

    monkeypatch.setattr(optimizer, "_refine_placement", capture_budget)
    components = [_make_comp(f"U{index}") for index in range(1, 9)]
    fixed = {"kind": "placement", "target": "U1", "x_mm": 7.25, "y_mm": 8.5}
    result = optimizer.optimize_placement(
        components,
        config=optimizer.PlacementConfig(
            board_width_mm=30, board_height_mm=24, iterations=30, restarts=3, seed=21,
        ),
        constraints=[fixed],
    )

    search = result["search"]
    assert requested_budgets == [512, 512, 512]
    assert search["refinement_evaluation_budgets"] == requested_budgets
    assert sum(row["refinement_evaluations"] for row in search["candidates"][1:]) == search[
        "refinement_evaluations"
    ]
    assert all(row["refinement_evaluations"] <= row["refinement_evaluation_budget"] for row in search["candidates"][1:])
    assert result["placements"]["U1"]["x"] == 7.25
    assert result["placements"]["U1"]["y"] == 8.5
    assert not result["quality"]["review_required"]


def test_reference_label_space_grid_matches_exhaustive_geometry():
    from circuit_weaver.placement_optimizer import (
        _LABEL_PROXY_BOARD_MARGIN_MM,
        _LABEL_PROXY_TARGET_SLOTS,
        ComponentPlacement,
        PlacementConfig,
        _overlap_boxes,
        _placement_courtyard_box,
        _point_box_distance_sq,
        _proxy_label_centers,
        _reference_class_prefix,
        _reference_label_space_metrics,
    )

    def exhaustive(placements, config):
        with_size = [placement for placement in placements if placement.reference_size_mm]
        boxes = {placement.ref: _placement_courtyard_box(placement) for placement in placements}
        result = []
        for placement in with_size:
            width, height = placement.reference_size_mm
            own_box = boxes[placement.ref]
            peers = [
                other
                for other in placements
                if other.ref != placement.ref
                and other.layer.casefold() == placement.layer.casefold()
                and _reference_class_prefix(other.ref) == _reference_class_prefix(placement.ref)
            ]
            obstacles = [
                other
                for other in placements
                if other.ref != placement.ref and other.layer.casefold() == placement.layer.casefold()
            ]
            feasible = set()
            for center in _proxy_label_centers(placement):
                label_box = (
                    center[0] - width / 2,
                    center[1] - height / 2,
                    center[0] + width / 2,
                    center[1] + height / 2,
                )
                if (
                    label_box[0] < _LABEL_PROXY_BOARD_MARGIN_MM
                    or label_box[1] < _LABEL_PROXY_BOARD_MARGIN_MM
                    or label_box[2] > config.board_width_mm - _LABEL_PROXY_BOARD_MARGIN_MM
                    or label_box[3] > config.board_height_mm - _LABEL_PROXY_BOARD_MARGIN_MM
                ):
                    continue
                if any(_overlap_boxes(label_box, boxes[other.ref]) for other in obstacles):
                    continue
                own_distance = _point_box_distance_sq(center, own_box)
                if any(_point_box_distance_sq(center, boxes[peer.ref]) < own_distance for peer in peers):
                    continue
                feasible.add(center)
                if len(feasible) >= _LABEL_PROXY_TARGET_SLOTS:
                    break
            result.append(
                {
                    "ref": placement.ref,
                    "nearby_associated_slots": len(feasible),
                    "target_slots": _LABEL_PROXY_TARGET_SLOTS,
                    "model": "sampled_face_corner_proxy",
                }
            )
        return result

    rng = random.Random(714205)
    config = PlacementConfig(board_width_mm=32, board_height_mm=24)
    # This fixed seed covers negative/out-of-board centers, rotated extents,
    # both layers, dense same-class peers, and candidate boxes crossing grid edges.
    for trial in range(6):
        placements = []
        for index in range(32):
            prefix = ("R", "C", "U", "J")[index % 4]
            placements.append(
                ComponentPlacement(
                    ref=f"{prefix}{index // 4 + 1}",
                    x=rng.uniform(-2, 34),
                    y=rng.uniform(-2, 26),
                    rotation=rng.choice((0, 30, 90, 270)),
                    layer=rng.choice(("front", "back")),
                    width=rng.uniform(0.5, 12),
                    height=rng.uniform(0.5, 9),
                    reference_size_mm=(rng.uniform(0.6, 3.6), rng.uniform(0.6, 2.1)),
                )
            )
        assert _reference_label_space_metrics(placements, config) == exhaustive(placements, config), trial


def test_reference_label_local_cache_ignores_far_boxes_and_invalidates_nearby_boxes():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _cached_reference_label_slots,
        _reference_label_space_metrics,
    )

    config = PlacementConfig(board_width_mm=40, board_height_mm=30)
    target = ComponentPlacement(
        "U1", 20, 15, width=4, height=3, reference_size_mm=(1.5, 1.2)
    )
    _cached_reference_label_slots.cache_clear()
    baseline = _reference_label_space_metrics([target], config)
    baseline_info = _cached_reference_label_slots.cache_info()

    far = ComponentPlacement("R1", 38, 28, width=0.4, height=0.4)
    assert _reference_label_space_metrics([target, far], config) == baseline
    far_info = _cached_reference_label_slots.cache_info()
    assert far_info.hits == baseline_info.hits + 1
    assert far_info.misses == baseline_info.misses

    # This component intersects the target's candidate-label region. Its
    # courtyard geometry must participate in the key and invalidate the result.
    near = ComponentPlacement("C1", 20, 12.7, width=0.2, height=0.2)
    _reference_label_space_metrics([target, far, near], config)
    near_info = _cached_reference_label_slots.cache_info()
    assert near_info.misses == far_info.misses + 1


def test_box_grid_large_ranges_use_conservative_overflow_fallback():
    from circuit_weaver.placement_optimizer import _BoxGridIndex

    index = _BoxGridIndex({
        1: (-1_000_000.0, -1_000_000.0, 1_000_000.0, 1_000_000.0),
        2: (0.0, 0.0, 1.0, 1.0),
        3: (100.0, 100.0, 101.0, 101.0),
    })
    # Large indexed boxes are always included, so exact downstream geometry
    # checks remain complete without allocating their millions of grid cells.
    assert index.query((50.0, 50.0, 51.0, 51.0)) == [1]
    # A large query falls back to every item instead of iterating an unbounded
    # range; callers still apply their original exact predicates.
    assert index.query((-1_000_000.0, -1_000_000.0, 1_000_000.0, 1_000_000.0)) == [1, 2, 3]


def test_reference_label_spatial_pruning_preserves_seeded_search(monkeypatch):
    from circuit_weaver.placement_optimizer import (
        PlacementConfig,
        _BoxGridIndex,
        optimize_placement,
    )

    components = []
    for index in range(16):
        comp = _make_comp(f"R{index + 1}", category=("analog", "digital")[index % 2])
        comp.placement_width_mm = 3.0 + index % 3
        comp.placement_height_mm = 2.0 + index % 4
        comp.placement_reference_size_mm = (2.3, 1.4)
        components.append(comp)
    config = PlacementConfig(
        board_width_mm=60,
        board_height_mm=48,
        iterations=40,
        restarts=2,
        refinement_passes=0,
        seed=20261018,
    )
    indexed = optimize_placement(components, config=config)

    # Returning every box restores the exhaustive candidate scan while keeping
    # the same exact geometric predicates, allowing end-to-end result equality.
    monkeypatch.setattr(_BoxGridIndex, "query", lambda self, _box: sorted(self._boxes))
    exhaustive = optimize_placement(components, config=config)
    assert indexed == exhaustive


def test_more_restarts_than_iterations_do_not_exceed_budget():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    result = optimize_placement([_make_comp("U1")], config=PlacementConfig(iterations=2, restarts=8, seed=0))
    assert result["search"]["restarts"] == 2
    assert sum(row["iterations"] for row in result["search"]["candidates"]) == 2


def test_gap_is_edge_to_edge_and_oblique_footprint_extent_is_conservative():
    from circuit_weaver.placement_optimizer import ComponentPlacement, _effective_dimensions, _overlap_area

    a = ComponentPlacement("U1", 10, 10, width=2, height=2)
    b = ComponentPlacement("U2", 12.5, 10, width=2, height=2)
    assert _overlap_area(a, b, gap=0.5) == 0
    b.x -= 0.01
    assert _overlap_area(a, b, gap=0.5) > 0
    a.rotation = 45
    assert _effective_dimensions(a) == pytest.approx((math.sqrt(8), math.sqrt(8)))


def test_overlap_sweep_matches_exhaustive_geometry_for_rotated_courtyards():
    from itertools import combinations

    from circuit_weaver.placement_optimizer import ComponentPlacement, _overlap_area, _overlapping_pairs

    placements = [
        ComponentPlacement(
            f"R{i}",
            x=((i * 37) % 23) * 1.13 - 10.0,
            y=((i * 19) % 17) * 1.07 - 8.0,
            rotation=(0, 45, 90, 135, 180)[i % 5],
            width=0.8 + (i % 4),
            height=0.6 + (i % 3),
        )
        for i in range(60)
    ]
    expected = [
        (i, j, area)
        for (i, first), (j, second) in combinations(enumerate(placements), 2)
        if (area := _overlap_area(first, second, 0.5)) > 0
    ]
    assert _overlapping_pairs(placements, 0.5) == expected


def test_overlap_sweep_preserves_negative_gap_helper_geometry():
    from itertools import combinations

    from circuit_weaver.placement_optimizer import ComponentPlacement, _overlap_area, _overlapping_pairs

    placements = [
        ComponentPlacement("A", -2, 3, width=2, height=1, rotation=45),
        ComponentPlacement("B", -1.2, 3.1, width=3, height=1.5, rotation=90),
        ComponentPlacement("C", 5, -4, width=0.5, height=0.5),
    ]
    expected = [
        (i, j, area)
        for (i, first), (j, second) in combinations(enumerate(placements), 2)
        if (area := _overlap_area(first, second, -0.25)) > 0
    ]
    assert _overlapping_pairs(placements, -0.25) == expected


def test_spatial_legalizer_matches_recorded_baseline_layouts():
    from circuit_weaver.placement_optimizer import ComponentPlacement, PlacementConfig, _legalize_overlaps

    expected_hashes = {
        0: "9609ad1936e300bad7d50d6eb1faad6bc235ddee6d85107b56fae468828534bb",
        7: "33f5785f677411d03d691a7e74e98287e39c9368bf84856b9e35d92bc1bf2784",
        13: "137647a38d9ada1f05083cf0c4dabeea01d2fce8c7237d37e1f50ac9f9bc557b",
        24: "52ef2bc20d73757421e1adb328791eb8ace1ec8a9a9e72a2df3a7eeb6659a63a",
        59: "0d92073048ea171dd9d4a4ae629684179848f7837cf33639ef125cdfa86578af",
    }
    rng = random.Random(937421)
    for case in range(60):
        count = 8 + case % 29
        placements = []
        for index in range(count):
            parent = f"U{max(0, index // 4)}" if index % 4 and index // 4 < count else ""
            placements.append(
                ComponentPlacement(
                    ref=f"P{case}_{index}",
                    x=rng.uniform(-8, 48),
                    y=rng.uniform(-7, 45),
                    rotation=rng.choice([0, 45, 90, 135, 180]),
                    width=rng.uniform(0.5, 7),
                    height=rng.uniform(0.4, 5),
                    parent_ref=parent,
                    constraint_locked=index % 11 == 0,
                    locked=index % 13 == 0,
                )
            )
        if case not in expected_hashes:
            continue
        config = PlacementConfig(
            board_width_mm=30 + case % 17,
            board_height_mm=28 + case % 13,
            min_component_gap_mm=(case % 4) * 0.25,
            support_body_clearance_mm=(case % 5) * 0.4,
            edge_clearance_mm=0.5,
        )
        result, moves = _legalize_overlaps(placements, config)
        payload = {"moves": moves, "placements": [(p.ref, p.x, p.y, p.rotation) for p in result]}
        signature = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
        assert signature == expected_hashes[case]


def test_locked_support_reserves_space_before_movable_owner():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _legalize_overlaps,
        _overlap_area,
    )

    owner = ComponentPlacement("U1", 20, 20, width=6, height=6)
    fixed = ComponentPlacement("C1", 20, 20, parent_ref="U1", constraint_locked=True)
    result, _ = _legalize_overlaps([owner, fixed], PlacementConfig())
    by_ref = {p.ref: p for p in result}
    assert (by_ref["C1"].x, by_ref["C1"].y) == (20, 20)
    assert _overlap_area(by_ref["U1"], by_ref["C1"], 0.5) == 0


def test_subcentimeter_fixed_coordinates_remain_exact_and_conflict_is_reported():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    fixed = {"kind": "placement", "target": "U1", "x_mm": 25.1234, "y_mm": 25.5678}
    result = optimize_placement(
        [_make_comp("U1"), _make_comp("U2")],
        config=PlacementConfig(iterations=30, seed=0),
        constraints=[fixed, {**fixed, "target": "U2"}],
    )
    for p in result["placements"].values():
        assert (p["x"], p["y"]) == (25.1234, 25.5678)
    assert result["quality"]["overlaps"]
    assert result["quality"]["review_required"]


def test_fanout_does_not_overweight_each_connected_component():
    from circuit_weaver.placement_optimizer import _build_connectivity_pairs

    components = [_make_comp(f"U{i}") for i in range(1, 7)]
    for comp in components:
        comp.pin_nets = {"1": "DATA_BUS"}
    pairs = _build_connectivity_pairs(components)
    assert sum(weight for pair, weight in pairs.items() if "U1" in pair) == pytest.approx(1.0)


def test_routing_estimate_measures_real_support_refs_but_excludes_ground():
    from circuit_weaver.placement_optimizer import ComponentPlacement, _routing_estimate

    components = [_make_comp("U1"), _make_comp("C1"), _make_comp("J1")]
    for comp in components:
        comp.pin_nets = {"1": "SIGNAL", "2": "GND"}
    components[1].placement_parent_ref = "U1"
    state = [ComponentPlacement("U1", 10, 10), ComponentPlacement("C1", 12, 20), ComponentPlacement("J1", 30, 15)]
    result = _routing_estimate(state, components)
    assert result["estimated"] is True
    assert result["total_hpwl_mm"] == 30
    assert result["nets"] == [{"net": "SIGNAL", "component_count": 3, "hpwl_mm": 30}]


def test_topology_relax_seed_moves_primary_family_toward_weighted_anchored_loads():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _ConstraintPlan,
        _topology_relaxed_seed,
    )

    config = PlacementConfig(board_width_mm=100, board_height_mm=80)
    state = [
        ComponentPlacement("Q7", 15, 15, width=4, height=4, category="power"),
        ComponentPlacement("C42", 17, 18, width=1, height=1, parent_ref="Q7", category="passive"),
        ComponentPlacement("P4", 40, 35, width=4, height=4, category="connector", edge_constrained=True),
        ComponentPlacement("CN11", 80, 55, width=4, height=4, category="connector"),
    ]
    pairs = {("Q7", "P4"): 1.0, ("Q7", "CN11"): 3.0}

    relaxed, counts = _topology_relaxed_seed(state, pairs, config, _ConstraintPlan())
    reversed_state, _ = _topology_relaxed_seed(list(reversed(state)), pairs, config, _ConstraintPlan())
    original = {item.ref: item for item in state}
    result = {item.ref: item for item in relaxed}
    reversed_result = {item.ref: item for item in reversed_state}
    weighted_target = ((40 * 1 + 80 * 3) / 4, (35 * 1 + 55 * 3) / 4)

    assert counts == {"eligible_families": 1, "moved_families": 1}
    assert math.dist((result["Q7"].x, result["Q7"].y), weighted_target) < math.dist(
        (original["Q7"].x, original["Q7"].y), weighted_target
    )
    assert (result["P4"].x, result["P4"].y) == (40, 35)
    assert (result["CN11"].x, result["CN11"].y) == (80, 55)
    assert result["C42"].parent_ref == "Q7"
    assert (result["C42"].x - result["Q7"].x, result["C42"].y - result["Q7"].y) == (2, 3)
    assert {ref: (item.x, item.y) for ref, item in result.items()} == {
        ref: (item.x, item.y) for ref, item in reversed_result.items()
    }


@pytest.mark.parametrize("protected", ["fixed", "edge"])
def test_topology_relax_seed_preserves_authored_fixed_and_edge_locked_families(protected):
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _ConstraintPlan,
        _topology_relaxed_seed,
    )

    owner = ComponentPlacement("Q9", 12, 12, width=3, height=3)
    if protected == "fixed":
        owner.locked = True
        owner.constraint_locked = True
        plan = _ConstraintPlan()
    else:
        owner.edge_constrained = True
        owner.rotation_locked = True
        plan = _ConstraintPlan(edges={"Q9": {"edge": "left", "max_distance_mm": 0}})
    support = ComponentPlacement("R42", 14, 12, width=1, height=1, parent_ref="Q9")
    load = ComponentPlacement("PORT5", 80, 60, width=3, height=3, category="connector")

    relaxed, counts = _topology_relaxed_seed(
        [owner, support, load], {("Q9", "PORT5"): 1.0},
        PlacementConfig(board_width_mm=100, board_height_mm=80), plan,
    )

    assert counts["moved_families"] == 0
    assert [(item.x, item.y) for item in relaxed] == [(12, 12), (14, 12), (80, 60)]


def test_optimize_exposes_topology_seed_and_falls_back_deterministically_without_links():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    config = PlacementConfig(board_width_mm=100, board_height_mm=80, iterations=20, seed=73)
    first = optimize_placement([_make_comp("U7"), _make_comp("U8")], config=config)
    second = optimize_placement([_make_comp("U7"), _make_comp("U8")], config=config)

    seed = first["search"]["topology_seed"]
    assert seed["accepted"] is False
    assert seed["moved_families"] == 0
    assert seed["candidate_rank"] == seed["fallback_baseline_rank"]
    assert first["search"]["candidates"][0]["kind"] == "legalized_baseline"
    assert first["placements"] == second["placements"]


def test_optimize_accepts_topology_seed_only_when_existing_rank_improves():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    driver = _make_comp("Q7", category="power", footprint="SOIC-16")
    driver.pin_nets = {"1": "LOAD_SIG"}
    load = _make_comp("PORT4", category="connector", footprint="SOT-23")
    load.pin_nets = {"1": "LOAD_SIG"}
    config = PlacementConfig(board_width_mm=100, board_height_mm=80, iterations=1, seed=3, restarts=1)

    result = optimize_placement([driver, load], config=config)

    seed = result["search"]["topology_seed"]
    assert seed["candidate_rank"]["hard_defects"] <= seed["fallback_baseline_rank"]["hard_defects"]
    if seed["accepted"]:
        assert tuple(seed["candidate_rank"].values()) < tuple(seed["fallback_baseline_rank"].values())
        assert result["search"]["candidates"][0]["kind"] == "topology_relaxed_seed"
    else:
        assert result["search"]["candidates"][0]["kind"] == "legalized_baseline"


def test_refinement_never_trades_a_hard_constraint_for_connectivity():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _ConstraintPlan,
        _placement_rank,
        _refine_placement,
    )

    config = PlacementConfig(refinement_passes=3)
    # U2 is on its declared left edge; U1 attracts it strongly across a keepout.
    state = [ComponentPlacement("U1", 70, 40, constraint_locked=True), ComponentPlacement("U2", 2, 40)]
    plan = _ConstraintPlan(edges={"U2": {"edge": "left", "max_distance_mm": 0}})
    pairs = {("U1", "U2"): 1000.0}
    before = _placement_rank(state, config, pairs, plan)
    refined, _ = _refine_placement(state, config, pairs, plan)
    assert _placement_rank(refined, config, pairs, plan) <= before
    assert refined[1].x == 2
    assert (refined[0].x, refined[0].y) == (70, 40)


def test_placement_cli_exposes_bounded_search_controls():
    from pathlib import Path

    spec = Path(__file__).resolve().parent.parent / "samples/usb_regulated_supply/usb_regulated_supply.yaml"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "circuit_weaver",
            "optimize-placement",
            str(spec),
            "--iterations",
            "20",
            "--restarts",
            "2",
            "--refinement-passes",
            "0",
            "--seed",
            "7",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(process.stdout)
    assert result["search"]["restarts"] == 2
    assert result["search"]["refinement_evaluations"] == 0
    assert result["reference_reconciliation"]["exact_match"] is True


def test_optimize_single_component():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    comps = [_make_comp("U1", category="power")]
    result = optimize_placement(comps, config=PlacementConfig(iterations=100, seed=42))
    assert result["status"] == "ok"
    assert "U1" in result["placements"]
    p = result["placements"]["U1"]
    assert "x" in p and "y" in p and "rotation" in p and "layer" in p


def test_optimize_multiple_components():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    comps = [
        _make_comp("U1", category="power"),
        _make_comp("U2", category="digital"),
        _make_comp("C1", category="passive", footprint="0402"),
        _make_comp("C2", category="passive", footprint="0402"),
        _make_comp("J1", category="connector", footprint="USB-C"),
    ]
    result = optimize_placement(comps, config=PlacementConfig(iterations=500, seed=42))
    assert result["status"] == "ok"
    assert len(result["placements"]) == 5
    # All within board bounds
    for ref, p in result["placements"].items():
        assert 0 < p["x"] < 100
        assert 0 < p["y"] < 80


def test_optimize_no_overlaps():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    comps = [_make_comp(f"U{i}", category="digital", footprint="SOIC-8") for i in range(1, 8)]
    result = optimize_placement(comps, config=PlacementConfig(iterations=2000, seed=42))
    placements = result["placements"]

    # Check no two components have the exact same position
    positions = [(p["x"], p["y"]) for p in placements.values()]
    assert len(set(positions)) == len(positions), "Duplicate positions found"


def test_legalization_removes_physical_overlaps_even_without_sa():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    comps = [_make_comp(f"U{i}", category="digital", footprint="SOIC-8") for i in range(1, 10)]
    result = optimize_placement(comps, config=PlacementConfig(strategy="simple"))

    assert result["quality"]["overlaps"] == []
    assert result["quality"]["legalization_moves"] > 0


def test_overlap_geometry_uses_rotated_dimensions():
    from circuit_weaver.placement_optimizer import ComponentPlacement, _overlap_area

    narrow_after_rotation = ComponentPlacement(ref="U1", x=10, y=10, width=10, height=2, rotation=90)
    neighbor = ComponentPlacement(ref="U2", x=13, y=10, width=2, height=2)

    assert _overlap_area(narrow_after_rotation, neighbor) == 0


def test_optimize_simple_strategy_skips_sa():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    comps = [_make_comp("U1")]
    result = optimize_placement(comps, config=PlacementConfig(strategy="simple"))
    assert result["iterations"] == 0  # Simple skips SA
    assert result["strategy"] == "simple"


def test_optimize_thermal_strategy():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    comps = [_make_comp("U1", category="power"), _make_comp("U2", category="power")]
    result = optimize_placement(comps, config=PlacementConfig(strategy="thermal", iterations=500, seed=42))
    assert result["strategy"] == "thermal"


def test_optimize_with_specs_dir(tmp_path):
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "ic_thermal.json").write_text(
        json.dumps({"IC_U1": {"pdiss_max_w": 2.5, "type": "buck_converter"}}), encoding="utf-8"
    )
    (specs_dir / "si_params.json").write_text(
        json.dumps({"IC_U2": {"requires_impedance_control": True}}), encoding="utf-8"
    )

    comps = [_make_comp("U1", category="power"), _make_comp("U2", category="digital")]
    result = optimize_placement(comps, config=PlacementConfig(iterations=200, seed=42), specs_dir=str(specs_dir))
    assert result["status"] == "ok"


def test_optimize_thermal_warnings():
    from circuit_weaver.placement_optimizer import ComponentPlacement, PlacementConfig, _build_result

    state = [ComponentPlacement(ref="U1", x=50, y=40, thermal_dissipation_w=3.5)]
    result = _build_result(state, PlacementConfig(), 0, 0, 0)
    assert len(result["thermal_warnings"]) == 1
    assert "3.5W" in result["thermal_warnings"][0]


def test_optimize_deterministic_with_seed():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    comps = [_make_comp(f"U{i}") for i in range(1, 6)]
    r1 = optimize_placement(comps, config=PlacementConfig(iterations=500, seed=123))
    r2 = optimize_placement(comps, config=PlacementConfig(iterations=500, seed=123))
    assert r1["placements"] == r2["placements"]


def test_optimize_connected_pair_settles_closer_than_unconnected_pair():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    comps = [
        ComponentDef(
            mpn="IC_U1",
            description="Connected power block",
            footprint="QFN-32",
            category="power",
            pins=[PinDef(number="1", name="LINK", electrical_type="bidirectional", side="L")],
            pin_nets={"1": "CTRL_LINK"},
            source_ref="U1",
        ),
        ComponentDef(
            mpn="IC_U2",
            description="Connected digital block",
            footprint="QFN-32",
            category="digital",
            pins=[PinDef(number="1", name="LINK", electrical_type="bidirectional", side="L")],
            pin_nets={"1": "CTRL_LINK"},
            source_ref="U2",
        ),
        _make_comp("U3", category="power"),
        _make_comp("U4", category="digital"),
    ]
    result = optimize_placement(comps, config=PlacementConfig(strategy="balanced", iterations=1500, seed=42))
    placements = result["placements"]

    def _dist(a: str, b: str) -> float:
        dx = placements[a]["x"] - placements[b]["x"]
        dy = placements[a]["y"] - placements[b]["y"]
        return (dx * dx + dy * dy) ** 0.5

    assert _dist("U1", "U2") < _dist("U3", "U4")


def test_support_passive_starts_and_stays_near_owner():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    owner = _make_comp("U1", category="digital", footprint="QFN-32")
    cap = _make_comp("C1", category="passive", footprint="0402")
    cap.placement_parent_ref = "U1"
    cap.placement_role = "decoupling"

    result = optimize_placement(
        [owner, cap],
        config=PlacementConfig(strategy="balanced", iterations=800, seed=42),
    )
    u1 = result["placements"]["U1"]
    c1 = result["placements"]["C1"]
    distance = ((u1["x"] - c1["x"]) ** 2 + (u1["y"] - c1["y"]) ** 2) ** 0.5

    assert distance < 6.0
    assert result["quality"]["missing_parents"] == []
    assert result["quality"]["support_body_violations"] == []
    assert result["quality"]["max_support_distance_mm"] < 6.0


def test_parent_affinity_uses_nearest_rotated_shared_signal_pads():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        _cost_parent_affinity,
        _parent_affinity_distance,
    )

    parent = ComponentPlacement("J1", 10, 10, rotation=90, net_pad_offsets={"VBUS": [(2, 0), (10, 0)]})
    support = ComponentPlacement(
        "C5",
        12,
        8,
        rotation=180,
        parent_ref="J1",
        placement_role="decoupling",
        net_pad_offsets={"VBUS": [(2, 0)]},
    )

    distance, model = _parent_affinity_distance(parent, support)

    assert distance == pytest.approx(0.0)
    assert model == "pad_endpoints"
    assert _cost_parent_affinity([parent, support]) == pytest.approx(0.0)
    # Center-only affinity would see a nonzero separation despite coincident pads.
    assert math.hypot(parent.x - support.x, parent.y - support.y) > 2.0


def test_decoupling_affinity_scores_both_rotated_supply_and_return_legs():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        _cost_parent_affinity,
        _local_power_loop_geometry,
    )

    parent = ComponentPlacement(
        "U1",
        0,
        0,
        rotation=90,
        net_pad_offsets={"VIN": [(0, 2)], "GND": [(0, -2)]},
    )
    long_return = ComponentPlacement(
        "C1",
        3,
        0,
        rotation=90,
        parent_ref="U1",
        placement_role="decoupling",
        net_pad_offsets={"VIN": [(0, -1)], "GND": [(0, 3)]},
    )
    short_return = ComponentPlacement(
        "C2",
        3,
        0,
        rotation=90,
        parent_ref="U1",
        placement_role="decoupling",
        net_pad_offsets={"VIN": [(0, -1)], "GND": [(0, -5)]},
    )

    long_geometry = _local_power_loop_geometry(parent, long_return)
    short_geometry = _local_power_loop_geometry(parent, short_return)

    assert long_geometry == {
        "supply_net": "VIN",
        "ground_net": "GND",
        "owner_pin": "",
        "supply_leg_mm": pytest.approx(0),
        "return_leg_mm": pytest.approx(8),
        "geometric_loop_mm": pytest.approx(8),
    }
    assert short_geometry["supply_leg_mm"] == pytest.approx(0)
    assert short_geometry["return_leg_mm"] == pytest.approx(0)
    assert _cost_parent_affinity([parent, short_return]) < _cost_parent_affinity([parent, long_return])


def test_decoupling_power_loop_requires_a_known_rail_and_ground_pair():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        _local_power_loop_geometry,
        _net_weight,
    )

    parent = ComponentPlacement(
        "U1",
        10,
        10,
        net_pad_offsets={"RESET_N": [(0, 0)], "VIN": [(1, 0)], "GND": [(0, 1)]},
    )
    reset_cap = ComponentPlacement(
        "C1",
        12,
        10,
        parent_ref="U1",
        placement_role="decoupling",
        net_pad_offsets={"RESET_N": [(0, 0)], "GND": [(1, 0)]},
    )
    one_leg_cap = ComponentPlacement(
        "C2",
        12,
        10,
        parent_ref="U1",
        placement_role="decoupling",
        net_pad_offsets={"VIN": [(0, 0)]},
    )
    signal_support = ComponentPlacement(
        "R1",
        12,
        10,
        parent_ref="U1",
        placement_role="pullup",
        net_pad_offsets={"VIN": [(0, 0)], "GND": [(1, 0)]},
    )

    assert _local_power_loop_geometry(parent, reset_cap) is None
    assert _local_power_loop_geometry(parent, one_leg_cap) is None
    assert _local_power_loop_geometry(parent, signal_support) is None
    assert _net_weight("GND") == 0.0


def test_decoupling_supply_leg_uses_declared_owner_pin_when_rail_is_shared():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        _cost_parent_affinity,
        _local_power_loop_geometry,
    )

    parent = ComponentPlacement(
        "U1",
        0,
        0,
        net_pad_offsets={"VDD_3P3": [(0, 0), (10, 0)], "GND": [(10, 2)]},
        pin_offsets={"9": (10, 0)},
        pin_nets={"9": "VDD_3P3"},
    )
    cap_near_wrong_pin = ComponentPlacement(
        "C1",
        0,
        0,
        parent_ref="U1",
        parent_pin="9",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(0, 0)], "GND": [(0, 2)]},
    )
    cap_near_intended_pin = ComponentPlacement(
        "C2",
        10,
        0,
        parent_ref="U1",
        parent_pin="9",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(0, 0)], "GND": [(0, 2)]},
    )

    wrong_loop = _local_power_loop_geometry(parent, cap_near_wrong_pin)
    intended_loop = _local_power_loop_geometry(parent, cap_near_intended_pin)

    assert wrong_loop["owner_pin"] == intended_loop["owner_pin"] == "9"
    assert wrong_loop["supply_leg_mm"] == pytest.approx(10)
    assert intended_loop["supply_leg_mm"] == pytest.approx(0)
    assert _cost_parent_affinity([parent, cap_near_intended_pin]) < _cost_parent_affinity([parent, cap_near_wrong_pin])


def test_output_cap_is_attracted_to_same_owner_inductor_on_shared_rail():
    from circuit_weaver.placement_optimizer import ComponentPlacement, _cost_parent_affinity

    parent = ComponentPlacement("U1", 10, 10)
    output_cap = ComponentPlacement(
        "C1",
        30,
        10,
        parent_ref="U1",
        placement_role="output_cap",
        net_pad_offsets={"VDD_3P3": [(-1, 0)]},
    )
    near_inductor = ComponentPlacement(
        "L1",
        32,
        10,
        parent_ref="U1",
        placement_role="inductor",
        net_pad_offsets={"VDD_3P3": [(1, 0)]},
    )
    far_inductor = ComponentPlacement(
        "L1",
        42,
        10,
        parent_ref="U1",
        placement_role="inductor",
        net_pad_offsets={"VDD_3P3": [(1, 0)]},
    )

    assert _cost_parent_affinity([parent, output_cap, near_inductor]) < _cost_parent_affinity(
        [parent, output_cap, far_inductor]
    )


def test_local_power_loop_metric_uses_pad_geometry_and_ignores_unrelated_ground():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _build_result,
        _cost_parent_affinity,
    )

    parent = ComponentPlacement("U1", 10, 10, net_pad_offsets={"VDD_3P3": [(1, 0)], "GND": [(0, 1)]})
    cap = ComponentPlacement(
        "C1",
        12,
        10,
        parent_ref="U1",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(-1, 0)], "GND": [(0, -1)]},
    )
    unrelated = ComponentPlacement("U2", 80, 60, net_pad_offsets={"GND": [(0, 0)]})

    result = _build_result([parent, cap], PlacementConfig(), 0, 0, 0)

    assert result["quality"]["local_power_loop_distances"] == [
        {
            "support_ref": "C1",
            "parent_ref": "U1",
            "supply_net": "VDD_3P3",
            "ground_net": "GND",
            "owner_pin": "",
            "supply_leg_mm": 0.0,
            "return_leg_mm": 2.83,
            "geometric_loop_mm": 2.83,
            "model": "rotated_pad_endpoint_sum",
        }
    ]
    assert result["quality"]["max_local_power_loop_mm"] == 2.83
    assert _cost_parent_affinity([parent, cap, unrelated]) == pytest.approx(_cost_parent_affinity([parent, cap]))


def test_support_seed_uses_shared_signal_pad_side_and_preserves_body_clearance():
    from circuit_weaver.placement_optimizer import (
        PlacementConfig,
        _init_placements,
    )

    owner = _make_comp("U1", category="digital", footprint="QFN-32")
    owner.placement_pad_offsets = {"USB_CC1": [(2.5, 0.0)], "GND": [(0.0, 2.0)]}
    support = _make_comp("R1", category="passive", footprint="0402")
    support.placement_parent_ref = "U1"
    support.placement_pad_offsets = {"USB_CC1": [(-0.4, 0.0)], "GND": [(0.4, 0.0)]}

    state = _init_placements(
        [owner, support],
        PlacementConfig(board_width_mm=50, board_height_mm=40),
        {},
        {},
    )
    by_ref = {placement.ref: placement for placement in state}
    parent, resistor = by_ref["U1"], by_ref["R1"]

    # The USB_CC1 pad points right, so its support resistor starts on that side.
    assert resistor.x > parent.x
    assert resistor.y == pytest.approx(parent.y)
    assert resistor.x - parent.x == pytest.approx(parent.width / 2 + resistor.width / 2 + 1.5)


def test_support_seed_prefers_declared_owner_pin_and_optimizer_carries_pin_metadata():
    from circuit_weaver.placement_optimizer import PlacementConfig, _init_placements

    owner = _make_comp("U1", category="power", footprint="QFN-32")
    owner.placement_pad_offsets = {
        "VIN": [(-5.0, 0.0), (5.0, 0.0)],
        "GND": [(0.0, 2.0)],
    }
    owner.placement_pin_offsets = {"5": (5.0, 0.0)}
    owner.power_pins = {"5": "VIN"}
    cap = _make_comp("C1", category="passive", footprint="0402")
    cap.placement_parent_ref = "U1"
    cap.placement_parent_pin = "5"
    cap.placement_role = "input_cap"
    cap.placement_pad_offsets = {"VIN": [(0.4, 0.0)], "GND": [(-0.4, 0.0)]}
    cap.placement_reference_size_mm = (1.2, 0.7)

    state = _init_placements(
        [owner, cap],
        PlacementConfig(board_width_mm=60, board_height_mm=40),
        {},
        {},
    )
    by_ref = {placement.ref: placement for placement in state}
    parent, support = by_ref["U1"], by_ref["C1"]

    assert support.parent_pin == "5"
    assert parent.pin_nets["5"] == "VIN"
    assert support.x > parent.x
    assert support.reference_size_mm == (1.2, 0.7)


def test_reference_label_space_proxy_prefers_associated_slots_and_is_bounded():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _cost_reference_label_space,
        _reference_label_space_metrics,
    )

    config = PlacementConfig(board_width_mm=30, board_height_mm=30)
    target = ComponentPlacement(
        "C1",
        15,
        15,
        width=1.6,
        height=0.8,
        reference_size_mm=(2.2, 1.0),
    )
    remote_peer = ComponentPlacement("C2", 27, 15, width=1.6, height=0.8)
    open_metrics = _reference_label_space_metrics([target, remote_peer], config)
    assert open_metrics[0]["nearby_associated_slots"] >= 2
    assert _cost_reference_label_space([target, remote_peer], config) == 0

    cage = [
        ComponentPlacement("C2", 17.5, 15, width=1.6, height=0.8),
        ComponentPlacement("C3", 12.5, 15, width=1.6, height=0.8),
        ComponentPlacement("C4", 15, 16.5, width=1.6, height=0.8),
        ComponentPlacement("C5", 15, 13.5, width=1.6, height=0.8),
    ]
    crowded_metrics = _reference_label_space_metrics([target, *cage], config)
    crowded_target = next(metric for metric in crowded_metrics if metric["ref"] == "C1")
    assert crowded_target["nearby_associated_slots"] < open_metrics[0]["nearby_associated_slots"]
    assert 0 < _cost_reference_label_space([target, *cage], config) <= 500


def test_reference_label_space_proxy_skips_missing_metadata_and_respects_board_edges():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _cost_reference_label_space,
        _reference_label_space_metrics,
    )

    config = PlacementConfig(board_width_mm=10, board_height_mm=10)
    no_metadata = ComponentPlacement("R1", 1.5, 5, width=1.0, height=0.6)
    edge_label = ComponentPlacement(
        "C1",
        1.5,
        5,
        width=1.6,
        height=0.8,
        reference_size_mm=(8.0, 8.0),
    )
    metrics = _reference_label_space_metrics([no_metadata, edge_label], config)
    assert [metric["ref"] for metric in metrics] == ["C1"]
    assert metrics[0]["nearby_associated_slots"] == 0
    assert _cost_reference_label_space([no_metadata], config) == 0


def test_reference_label_proxy_ignores_opposite_side_courtyards():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _reference_label_space_metrics,
    )

    config = PlacementConfig(board_width_mm=30, board_height_mm=30)
    target = ComponentPlacement(
        "C1",
        15,
        15,
        width=1.6,
        height=0.8,
        reference_size_mm=(2.2, 1.0),
        layer="front",
    )
    back_peer = ComponentPlacement("C2", 15, 15, width=6.0, height=6.0, layer="back")

    metrics = _reference_label_space_metrics([target, back_peer], config)

    assert metrics[0]["nearby_associated_slots"] == 2


def test_equivalent_decouplers_round_robin_real_shared_rail_pads():
    from circuit_weaver.placement_optimizer import (
        PlacementConfig,
        _init_placements,
        _rectangle_clearance,
    )

    owner = _make_comp("U1", category="digital", footprint="LQFP-48")
    owner.placement_width_mm = 8.0
    owner.placement_height_mm = 6.0
    owner.placement_geometry_status = "board_geometry"
    owner.placement_pad_offsets = {
        "VDD_3P3": [(-4, -3), (4, -3), (4, 3), (-4, 3)],
        "GND": [(-3, -3), (3, -3), (3, 3), (-3, 3)],
    }
    caps = []
    for ref in ("C1", "C2", "C3", "C4"):
        cap = _make_comp(ref, category="passive", footprint="0603")
        cap.placement_width_mm = 1.6
        cap.placement_height_mm = 0.8
        cap.placement_parent_ref = "U1"
        cap.placement_role = "decoupling"
        cap.placement_pad_offsets = {"VDD_3P3": [(0, 0)], "GND": [(0, 0)]}
        caps.append(cap)

    placements = _init_placements(
        [owner, *caps],
        PlacementConfig(board_width_mm=60, board_height_mm=50),
        {},
        {},
    )
    by_ref = {placement.ref: placement for placement in placements}
    parent = by_ref["U1"]
    seeds = [(by_ref[ref].x - parent.x, by_ref[ref].y - parent.y) for ref in ("C1", "C2", "C3", "C4")]

    assert len({(round(x, 2), round(y, 2)) for x, y in seeds}) == 4
    assert len({(math.copysign(1, x), math.copysign(1, y)) for x, y in seeds}) == 4
    assert all(_rectangle_clearance(by_ref[ref], parent) >= 1.5 for ref in ("C1", "C2", "C3", "C4"))


def test_dedicated_reset_net_outweighs_shared_power_rail_for_pullup_affinity_and_seed():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _parent_affinity_distance,
        _support_seed_position,
    )

    parent = ComponentPlacement(
        "U2",
        20,
        20,
        width=8,
        height=6,
        net_pad_offsets={"RESET_N": [(-2, 0)], "VDD_3P3": [(4, 0)]},
    )
    pullup = ComponentPlacement(
        "R4",
        25,
        20,
        width=1.6,
        height=0.8,
        parent_ref="U2",
        placement_role="pullup",
        net_pad_offsets={"RESET_N": [(0, 0)], "VDD_3P3": [(0, 0)]},
    )

    distance, model = _parent_affinity_distance(parent, pullup)
    seed_x, seed_y = _support_seed_position(parent, 1.6, 0.8, pullup.net_pad_offsets, PlacementConfig())

    assert distance == pytest.approx(7.0)
    assert model == "pad_endpoints"
    assert seed_x < parent.x
    assert seed_y == pytest.approx(parent.y)


def test_parent_affinity_preserves_each_dedicated_signal_connection():
    from circuit_weaver.placement_optimizer import ComponentPlacement, _parent_affinity_distance

    parent = ComponentPlacement(
        "U1",
        10,
        10,
        net_pad_offsets={"BST": [(0, 0)], "SW": [(0, 0)]},
    )
    bootstrap_cap = ComponentPlacement(
        "C3",
        10,
        10,
        parent_ref="U1",
        placement_role="bootstrap",
        net_pad_offsets={"BST": [(0, 0)], "SW": [(8, 0)]},
    )

    distance, model = _parent_affinity_distance(parent, bootstrap_cap)

    assert distance == pytest.approx(8.0)
    assert model == "pad_endpoints"


def test_support_seed_falls_back_to_ring_without_shared_signal_pads():
    from circuit_weaver.placement_optimizer import PlacementConfig, _init_placements

    owner = _make_comp("U1", category="digital", footprint="QFN-32")
    owner.placement_pad_offsets = {"GND": [(2.0, 0.0)]}
    support = _make_comp("C1", category="passive", footprint="0402")
    support.placement_parent_ref = "U1"
    support.placement_pad_offsets = {"GND": [(0.0, 0.0)]}

    state = _init_placements(
        [owner, support],
        PlacementConfig(board_width_mm=50, board_height_mm=40),
        {},
        {},
    )
    parent, cap = state
    assert cap.x < parent.x
    assert cap.y == pytest.approx(parent.y)


def test_rotated_diagonal_support_seed_meets_euclidean_body_clearance():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _overlap_area,
        _rectangle_clearance,
        _support_seed_position,
    )

    parent = ComponentPlacement(
        "U1",
        20,
        20,
        rotation=45,
        width=6,
        height=2,
        net_pad_offsets={"SIGNAL": [(3, 0)]},
    )
    config = PlacementConfig(min_component_gap_mm=3.0, support_body_clearance_mm=1.5)
    x, y = _support_seed_position(parent, 1, 0.5, {"SIGNAL": [(0, 0)]}, config)
    support = ComponentPlacement("R1", x, y, width=1, height=0.5, parent_ref="U1")

    assert x > parent.x
    assert y < parent.y
    assert _rectangle_clearance(support, parent) >= config.min_component_gap_mm
    assert _overlap_area(support, parent, gap=config.min_component_gap_mm) == 0


def test_topology_matched_cc_supports_compact_under_legal_refinement():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _ConstraintPlan,
        _cost_support_peer_alignment,
        _refine_placement,
        _support_peer_groups,
    )

    parent = ComponentPlacement("J1", 20, 20, width=6, height=6, constraint_locked=True)
    cc1 = ComponentPlacement(
        "R1",
        14,
        20,
        width=1,
        height=0.5,
        parent_ref="J1",
        placement_role="cc_pulldown",
        net_pad_offsets={"CC1_J1": [(0.4, 0)], "GND": [(0, 0)]},
    )
    cc2 = ComponentPlacement(
        "R2",
        35,
        20,
        width=1,
        height=0.5,
        parent_ref="J1",
        placement_role="cc_pulldown",
        net_pad_offsets={"CC2_J1": [(-0.4, 0)], "GND": [(0, 0)]},
    )
    unrelated = ComponentPlacement(
        "C1",
        40,
        30,
        width=1,
        height=0.5,
        parent_ref="J1",
        placement_role="decoupling",
        net_pad_offsets={"VBUS": [(0.4, 0)], "GND": [(0, 0)]},
    )
    state = [parent, cc1, cc2, unrelated]
    config = PlacementConfig(board_width_mm=50, board_height_mm=40, refinement_passes=3)
    initial_alignment_cost = _cost_support_peer_alignment(state)

    refined, _ = _refine_placement(state, config, {}, _ConstraintPlan())
    by_ref = {placement.ref: placement for placement in refined}
    final_cc_span = math.hypot(by_ref["R1"].x - by_ref["R2"].x, by_ref["R1"].y - by_ref["R2"].y)

    assert final_cc_span < 5
    assert _cost_support_peer_alignment(refined) < initial_alignment_cost
    assert [[member.ref for member in group] for group in _support_peer_groups(state)] == [["R1", "R2"]]


def test_support_families_use_signal_topology_and_ignore_shared_power_rails():
    from circuit_weaver.placement_optimizer import ComponentPlacement, _support_peer_groups

    parent = ComponentPlacement(
        "U1",
        20,
        20,
        width=8,
        height=6,
        net_pad_offsets={"VBUS_5V": [(-2, 0), (2, 0)], "USB_CC1": [(2, 1)], "USB_CC2": [(2, -1)]},
    )
    local_a = ComponentPlacement(
        "C1",
        18,
        20,
        parent_ref="U1",
        placement_role="decoupling",
        net_pad_offsets={"VBUS_5V": [(0, 0)]},
    )
    local_b = ComponentPlacement(
        "C2",
        22,
        20,
        parent_ref="U1",
        placement_role="decoupling",
        net_pad_offsets={"VBUS_5V": [(0, 0)]},
    )
    remote = ComponentPlacement(
        "C3",
        40,
        20,
        parent_ref="U1",
        placement_role="decoupling",
        net_pad_offsets={"VBUS_5V": [(0, 0)]},
    )
    cc1 = ComponentPlacement(
        "R1",
        20,
        10,
        parent_ref="U1",
        placement_role="termination",
        net_pad_offsets={"USB_CC1": [(0, 0)], "GND": [(0, 0)]},
    )
    cc2 = ComponentPlacement(
        "R2",
        20,
        30,
        parent_ref="U1",
        placement_role="termination",
        net_pad_offsets={"USB_CC2": [(0, 0)], "GND": [(0, 0)]},
    )
    reset_pullup = ComponentPlacement(
        "R3",
        20,
        10,
        parent_ref="U1",
        placement_role="pullup",
        net_pad_offsets={"RESET": [(0, 0)], "VDD_3P3": [(0, 0)]},
    )
    reset_filter = ComponentPlacement(
        "C5",
        22,
        12,
        parent_ref="U1",
        placement_role="decoupling",
        net_pad_offsets={"RESET": [(0, 0)], "GND": [(0, 0)]},
    )
    boot_pullup = ComponentPlacement(
        "R4",
        20,
        30,
        parent_ref="U1",
        placement_role="pullup",
        net_pad_offsets={"BOOT": [(0, 0)], "VDD_3P3": [(0, 0)]},
    )

    groups = {
        frozenset(member.ref for member in group)
        for group in _support_peer_groups(
            [
                parent,
                local_a,
                local_b,
                remote,
                cc1,
                cc2,
                reset_pullup,
                reset_filter,
                boot_pullup,
            ]
        )
    }
    assert groups == {
        frozenset({"R1", "R2"}),
        frozenset({"R3", "C5"}),
    }


def test_support_families_group_power_caps_by_explicit_owner_pin_only():
    from circuit_weaver.placement_optimizer import ComponentPlacement, _support_peer_groups

    parent = ComponentPlacement("U1", 20, 20, pin_nets={"9": "VDD_3P3", "24": "VDD_3P3"})
    first = ComponentPlacement(
        "C1",
        14,
        19,
        parent_ref="U1",
        parent_pin="9",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(0, 0)], "GND": [(0.4, 0)]},
    )
    same_pin = ComponentPlacement(
        "C2",
        14,
        23,
        parent_ref="U1",
        parent_pin="9",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(0, 0)], "GND": [(-0.4, 0)]},
    )
    other_pin = ComponentPlacement(
        "C3",
        25,
        20,
        parent_ref="U1",
        parent_pin="24",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(0, 0)], "GND": [(0, 0)]},
    )

    groups = _support_peer_groups([parent, first, same_pin, other_pin])

    assert [[member.ref for member in group] for group in groups] == [["C1", "C2"]]


def test_support_families_do_not_group_symbolic_or_mismatched_owner_pins():
    from circuit_weaver.placement_optimizer import ComponentPlacement, _support_peer_groups

    parent = ComponentPlacement("U1", 20, 20, pin_nets={"9": "VDD_3P3"})
    symbolic_a = ComponentPlacement(
        "C1",
        14,
        19,
        parent_ref="U1",
        parent_pin="auto",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(0, 0)], "GND": [(0.4, 0)]},
    )
    symbolic_b = ComponentPlacement(
        "C2",
        14,
        23,
        parent_ref="U1",
        parent_pin="auto",
        placement_role="decoupling",
        net_pad_offsets={"VDD_5V": [(0, 0)], "GND": [(-0.4, 0)]},
    )
    mismatched_a = ComponentPlacement(
        "C3",
        14,
        19,
        parent_ref="U1",
        parent_pin="9",
        placement_role="decoupling",
        net_pad_offsets={"VDD_5V": [(0, 0)], "GND": [(0.4, 0)]},
    )
    mismatched_b = ComponentPlacement(
        "C4",
        14,
        23,
        parent_ref="U1",
        parent_pin="9",
        placement_role="decoupling",
        net_pad_offsets={"VDD_5V": [(0, 0)], "GND": [(-0.4, 0)]},
    )

    assert _support_peer_groups([parent, symbolic_a, symbolic_b, mismatched_a, mismatched_b]) == []


def test_same_owner_pin_power_caps_prefer_a_row_to_open_space_near_the_owner():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        _cost_support_peer_alignment,
    )

    parent = ComponentPlacement("U1", 20, 20, pin_nets={"9": "VDD_3P3"})
    first = ComponentPlacement(
        "C1",
        14,
        19,
        parent_ref="U1",
        parent_pin="9",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(0, 0)], "GND": [(0.4, 0)]},
    )
    second_row = ComponentPlacement(
        "C2",
        18,
        19,
        parent_ref="U1",
        parent_pin="9",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(0, 0)], "GND": [(-0.4, 0)]},
    )
    second_column = ComponentPlacement(
        "C2",
        14,
        23,
        parent_ref="U1",
        parent_pin="9",
        placement_role="decoupling",
        net_pad_offsets={"VDD_3P3": [(0, 0)], "GND": [(-0.4, 0)]},
    )

    assert _cost_support_peer_alignment([parent, first, second_row]) < _cost_support_peer_alignment(
        [parent, first, second_column]
    )


def test_support_families_ignore_numeric_voltage_rail_aliases():
    from circuit_weaver.placement_optimizer import ComponentPlacement, _support_peer_groups

    parent = ComponentPlacement("U1", 20, 20, width=8, height=6)
    plus_3v3 = ComponentPlacement(
        "R1",
        18,
        20,
        parent_ref="U1",
        placement_role="pullup",
        net_pad_offsets={"+3V3": [(0, 0)]},
    )
    plus_5v = ComponentPlacement(
        "R2",
        22,
        20,
        parent_ref="U1",
        placement_role="pullup",
        net_pad_offsets={"+5V": [(0, 0)]},
    )

    assert _support_peer_groups([parent, plus_3v3, plus_5v]) == []


def test_compactness_includes_an_owner_and_its_support_parts():
    from circuit_weaver.placement_optimizer import ComponentPlacement, PlacementConfig, _cost_compactness

    config = PlacementConfig(board_width_mm=50, board_height_mm=40)
    close = [
        ComponentPlacement("U1", 20, 20, width=6, height=6),
        ComponentPlacement("C1", 15, 20, parent_ref="U1", category="passive"),
    ]
    spread = [ComponentPlacement(**placement.__dict__) for placement in close]
    spread[1].x = 35

    assert _cost_compactness(close, config) < _cost_compactness(spread, config)


def test_multi_owner_compactness_centers_the_full_support_courtyard_envelope():
    from circuit_weaver.placement_optimizer import ComponentPlacement, PlacementConfig, _cost_compactness

    config = PlacementConfig(board_width_mm=50, board_height_mm=40)
    state = [
        ComponentPlacement("U1", 12, 20, width=6, height=6),
        ComponentPlacement("U2", 38, 20, width=6, height=6),
        ComponentPlacement("C1", 12, 28, width=2, height=1, parent_ref="U1"),
        ComponentPlacement("C2", 38, 28, width=2, height=1, parent_ref="U2"),
    ]
    recentered_supports = [ComponentPlacement(**placement.__dict__) for placement in state]
    recentered_supports[2].y -= 3
    recentered_supports[3].y -= 3

    assert _cost_compactness(recentered_supports, config) < _cost_compactness(state, config)


def test_refinement_moves_owner_support_families_toward_other_owners():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _ConstraintPlan,
        _refine_placement,
    )

    state = [
        ComponentPlacement("U1", 12, 20, width=6, height=6),
        ComponentPlacement("U2", 38, 20, width=6, height=6),
        ComponentPlacement("C1", 12, 27, width=1.6, height=0.8, parent_ref="U1"),
        ComponentPlacement("C2", 38, 27, width=1.6, height=0.8, parent_ref="U2"),
    ]
    config = PlacementConfig(board_width_mm=50, board_height_mm=40, refinement_passes=3)

    refined, _ = _refine_placement(state, config, {}, _ConstraintPlan())
    by_ref = {placement.ref: placement for placement in refined}

    assert math.hypot(by_ref["U1"].x - by_ref["U2"].x, by_ref["U1"].y - by_ref["U2"].y) < 26
    assert all(
        _placement.parent_ref == "U1" and math.hypot(_placement.x - by_ref["U1"].x, _placement.y - by_ref["U1"].y) < 8
        for _placement in [by_ref["C1"]]
    )
    assert all(
        _placement.parent_ref == "U2" and math.hypot(_placement.x - by_ref["U2"].x, _placement.y - by_ref["U2"].y) < 8
        for _placement in [by_ref["C2"]]
    )


def test_diagonal_family_alignment_recenters_asymmetric_courtyards_together():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _ConstraintPlan,
        _placement_rank,
        _refine_placement,
    )

    state = [
        ComponentPlacement("U1", 10, 8, width=6, height=6),
        ComponentPlacement("U2", 35, 26, width=6, height=6),
        ComponentPlacement("C1", 10, 15, width=2, height=1, parent_ref="U1"),
        ComponentPlacement("C2", 35, 33, width=2, height=1, parent_ref="U2"),
    ]
    config = PlacementConfig(board_width_mm=50, board_height_mm=40, strategy="cost")
    plan = _ConstraintPlan()
    initial_rank = _placement_rank(state, config, {}, plan)
    single_family_move = [ComponentPlacement(**placement.__dict__) for placement in state]
    single_family_move[0].y += 9
    single_family_move[2].y += 9

    assert _placement_rank(single_family_move, config, {}, plan) > initial_rank

    refined, _ = _refine_placement(state, config, {}, plan)
    by_ref = {placement.ref: placement for placement in refined}

    assert _placement_rank(refined, config, {}, plan) < initial_rank
    assert abs(by_ref["U1"].y - by_ref["U2"].y) < 0.1
    assert abs(by_ref["C1"].y - by_ref["C2"].y) < 0.1


@pytest.mark.parametrize("protected_field", ["constraint_locked", "edge_constrained"])
def test_family_alignment_does_not_recenter_a_protected_layout(protected_field):
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _family_alignment_candidates,
    )

    state = [
        ComponentPlacement("U1", 5, 5, width=3, height=3),
        ComponentPlacement("U2", 15, 10, width=3, height=3),
        ComponentPlacement("U3", 35, 20, width=3, height=3),
        ComponentPlacement("C2", 15, 13, width=1, height=0.5, parent_ref="U2"),
        ComponentPlacement("C3", 35, 23, width=1, height=0.5, parent_ref="U3"),
    ]
    setattr(state[0], protected_field, True)

    candidates = _family_alignment_candidates(state, PlacementConfig(board_width_mm=50, board_height_mm=40))

    assert candidates
    assert all((candidate[0].x, candidate[0].y) == (5, 5) for candidate in candidates)


def test_family_alignment_ignores_unowned_components_without_support_families():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _family_alignment_candidates,
    )

    unowned = [ComponentPlacement(f"U{index}", 5 * index, 5 * index, category="other") for index in range(1, 10)]

    assert _family_alignment_candidates(unowned, PlacementConfig()) == []


def test_family_alignment_keeps_a_courtyard_channel_between_asymmetric_families():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _family_alignment_candidates,
    )

    state = [
        ComponentPlacement("U1", 10, 8, width=6, height=6),
        ComponentPlacement("U2", 27, 19, width=6, height=6),
        ComponentPlacement("C1", 18, 8, width=2, height=1, parent_ref="U1"),
        ComponentPlacement("C2", 20, 19, width=2, height=1, parent_ref="U2"),
    ]
    config = PlacementConfig(board_width_mm=50, board_height_mm=40)
    channel = max(2 * config.support_body_clearance_mm, 4 * config.min_component_gap_mm)

    candidates = _family_alignment_candidates(state, config)
    aligned = [candidate for candidate in candidates if abs(candidate[0].y - candidate[1].y) < 0.01]

    def family_bounds(candidate, refs):
        members = [placement for placement in candidate if placement.ref in refs]
        return (
            min(placement.x - placement.width / 2 for placement in members),
            max(placement.x + placement.width / 2 for placement in members),
        )

    assert any(
        family_bounds(candidate, {"U2", "C2"})[0] - family_bounds(candidate, {"U1", "C1"})[1] >= channel - 0.01
        for candidate in aligned
    )

    reversed_state = [state[index] for index in (1, 0, 3, 2)]
    reversed_candidates = _family_alignment_candidates(reversed_state, config)
    assert any(
        abs(candidate[0].y - candidate[1].y) < 0.01
        and max(
            family_bounds(candidate, {"U2", "C2"})[0] - family_bounds(candidate, {"U1", "C1"})[1],
            family_bounds(candidate, {"U1", "C1"})[0] - family_bounds(candidate, {"U2", "C2"})[1],
        )
        >= channel - 0.01
        for candidate in reversed_candidates
    )


def test_parent_affinity_ignores_ground_pad_coincidence_and_falls_back_to_centers():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        _parent_affinity_distance,
    )

    parent = ComponentPlacement("U1", 10, 10, net_pad_offsets={"GND": [(20, 0)]})
    support_with_ground = ComponentPlacement("C1", 30, 10, parent_ref="U1", net_pad_offsets={"GND": [(0, 0)]})
    support_without_metadata = ComponentPlacement("C1", 30, 10, parent_ref="U1")

    ground_distance, ground_model = _parent_affinity_distance(parent, support_with_ground)
    center_distance, center_model = _parent_affinity_distance(parent, support_without_metadata)

    assert ground_distance == pytest.approx(20.0)
    assert center_distance == pytest.approx(20.0)
    assert ground_model == center_model == "component_centers"


def test_support_distance_quality_reports_pad_endpoint_model_when_available():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _build_result,
    )

    parent = ComponentPlacement("U1", 10, 10, net_pad_offsets={"VBUS": [(2, 0)]})
    support = ComponentPlacement("C1", 15, 10, parent_ref="U1", net_pad_offsets={"VBUS": [(0, 0)]})
    result = _build_result([parent, support], PlacementConfig(), 0, 0, 0)

    assert result["quality"]["support_distance_model"] == "pad_endpoints"
    assert result["quality"]["support_distances"] == [
        {"support_ref": "C1", "parent_ref": "U1", "distance_mm": 3.0, "model": "pad_endpoints"}
    ]
    assert result["quality"]["max_support_distance_mm"] == 3.0


def test_rf_and_usb_categories_have_explicit_edge_zones():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    components = [
        _make_comp("U1", category="rf", footprint="QFN-32"),
        _make_comp("J1", category="usb", footprint="USB-C"),
    ]
    result = optimize_placement(components, config=PlacementConfig(strategy="simple"))

    rf = result["placements"]["U1"]
    usb = result["placements"]["J1"]
    assert rf["x"] > result["board_width_mm"] * 0.75
    assert rf["y"] < result["board_height_mm"] * 0.3
    assert usb["y"] > result["board_height_mm"] * 0.75


def test_category_zones_are_absolute_edge_relative_and_aspect_aware():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    components = [
        _make_comp("U1", category="rf"),
        _make_comp("U2", category="motor"),
        _make_comp("U3", category="audio"),
        _make_comp("U4", category="power_management"),
        _make_comp("U5", category="unrecognized_category"),
    ]
    wide = optimize_placement(
        components,
        config=PlacementConfig(board_width_mm=160, board_height_mm=80, strategy="simple"),
    )
    tall = optimize_placement(
        components,
        config=PlacementConfig(board_width_mm=80, board_height_mm=160, strategy="simple"),
    )

    assert wide["board_width_mm"] - wide["placements"]["U1"]["x"] == pytest.approx(18.0)
    assert tall["board_width_mm"] - tall["placements"]["U1"]["x"] == pytest.approx(18.0)
    assert wide["board_height_mm"] - wide["placements"]["U2"]["y"] == pytest.approx(18.0)
    assert tall["placements"]["U2"]["y"] == pytest.approx(140.0)
    assert wide["placements"]["U3"]["x"] != wide["placements"]["U5"]["x"]
    assert wide["placements"]["U4"]["x"] == pytest.approx(20.0)


def test_machine_constraints_override_board_and_drive_fixed_edge_and_keepout_placement():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    components = [_make_comp("U1"), _make_comp("U2")]
    result = optimize_placement(
        components,
        config=PlacementConfig(strategy="balanced", iterations=1200, seed=4),
        constraints=[
            {
                "kind": "placement",
                "target": "board",
                "board_width_mm": 50,
                "board_height_mm": 30,
            },
            {"kind": "placement", "target": "U1", "x_mm": 8, "y_mm": 8, "rotation": 90},
            {"kind": "placement", "target": "U2", "edge": "right"},
            {
                "kind": "keepout",
                "target": "middle",
                "x_mm": 20,
                "y_mm": 0,
                "width_mm": 10,
                "height_mm": 30,
            },
        ],
    )

    assert (result["board_width_mm"], result["board_height_mm"]) == (50, 30)
    assert {
        key: result["placements"]["U1"][key]
        for key in (
            "x",
            "y",
            "rotation",
            "layer",
            "locked",
            "constraint_locked",
            "width_mm",
            "height_mm",
        )
    } == {
        "x": 8.0,
        "y": 8.0,
        "rotation": 90.0,
        "layer": "front",
        "locked": True,
        "constraint_locked": True,
        "width_mm": 6.0,
        "height_mm": 6.0,
    }
    assert result["placements"]["U2"]["x"] == 46.0
    evaluation = result["constraint_evaluation"]
    assert evaluation["board_dimension_source"] == "constraints"
    assert evaluation["applied_count"] == 4
    assert evaluation["unsupported"] == []
    assert evaluation["violations"] == []


def test_edge_constraint_rotation_is_explicit_and_remains_independent_of_position():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    component = _make_comp("J1", category="connector", footprint="USB-C")
    result = optimize_placement(
        [component],
        config=PlacementConfig(strategy="balanced", iterations=80, seed=9),
        constraints=[{"kind": "placement", "target": "J1", "edge": "bottom", "max_distance_mm": 0.25, "rotation": 270}],
    )

    placement = result["placements"]["J1"]
    assert placement["rotation"] == 270
    assert placement["rotation_locked"] is True
    assert placement["constraint_locked"] is False
    assert result["constraint_evaluation"]["violations"] == []
    assert result["quality"]["review_required"] is False


def test_nonorthogonal_edge_orientation_uses_rotated_footprint_extent():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    result = optimize_placement(
        [_make_comp("J1", category="connector", footprint="USB-C")],
        config=PlacementConfig(strategy="simple", board_width_mm=50, board_height_mm=40),
        constraints=[{"kind": "placement", "target": "J1", "edge": "bottom", "max_distance_mm": 0, "rotation": 45}],
    )

    placement = result["placements"]["J1"]
    assert placement["rotation"] == 45
    assert result["constraint_evaluation"]["violations"] == []
    assert result["quality"]["outside_board"] == []


@pytest.mark.parametrize("rotation", [None, "sideways", float("nan"), float("inf"), True])
def test_malformed_edge_rotation_is_reported_unsupported(rotation):
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    result = optimize_placement(
        [_make_comp("J1", category="connector")],
        config=PlacementConfig(strategy="simple"),
        constraints=[{"kind": "placement", "target": "J1", "edge": "bottom", "rotation": rotation}],
    )

    unsupported = result["constraint_evaluation"]["unsupported"]
    assert len(unsupported) == 1
    assert "rotation must be a finite number" in unsupported[0]["reason"]
    assert result["quality"]["review_required"] is True


def test_edge_orientation_does_not_overwrite_a_fixed_rotation():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    result = optimize_placement(
        [_make_comp("J1", category="connector")],
        config=PlacementConfig(strategy="simple", board_width_mm=50, board_height_mm=40),
        constraints=[
            {"kind": "placement", "target": "J1", "x_mm": 20.123, "y_mm": 20.456, "rotation": 45},
            {"kind": "placement", "target": "J1", "edge": "bottom", "rotation": 90},
        ],
    )

    placement = result["placements"]["J1"]
    assert (placement["x"], placement["y"], placement["rotation"]) == (20.123, 20.456, 45.0)
    assert placement["constraint_locked"] is True
    assert result["constraint_evaluation"]["violations"]
    assert any(v["kind"] == "edge_orientation" for v in result["constraint_evaluation"]["violations"])


def test_functional_grouping_uses_block_then_section_and_excludes_support_parts():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _cost_functional_grouping,
        _functional_group_metrics,
        _total_cost,
    )

    close = [
        ComponentPlacement("U1", 10, 10, block_id="usb"),
        ComponentPlacement("U2", 12, 10, block_id="USB"),
        ComponentPlacement("U3", 80, 60, functional_section="power"),
        ComponentPlacement("U4", 82, 60, functional_section="POWER"),
        ComponentPlacement("C1", 11, 11, block_id="usb", parent_ref="U1"),
    ]
    spread = [ComponentPlacement(**{**placement.__dict__}) for placement in close]
    spread[1].x = 50
    spread[3].x = 30

    assert _cost_functional_grouping(close) < _cost_functional_grouping(spread)
    assert _total_cost(close, PlacementConfig(strategy="simple")) > _total_cost(
        [ComponentPlacement(**{**placement.__dict__, "block_id": "", "functional_section": ""}) for placement in close],
        PlacementConfig(strategy="simple"),
    )
    metrics = _functional_group_metrics(close)
    assert {(item["grouping"], item["id"], tuple(item["refs"])) for item in metrics} == {
        ("block_id", "usb", ("U1", "U2")),
        ("functional_section", "power", ("U3", "U4")),
    }
    assert _cost_functional_grouping(spread) == _cost_functional_grouping(
        [ComponentPlacement(**{**placement.__dict__, "x": placement.x + 1000}) for placement in spread]
    )


def test_functional_affinity_softens_conflicting_category_anchors():
    from circuit_weaver.placement_optimizer import (
        ComponentPlacement,
        PlacementConfig,
        _cost_zone,
        _total_cost,
    )

    config = PlacementConfig(strategy="balanced", board_width_mm=100, board_height_mm=80)
    grouped = [
        ComponentPlacement("U1", 45, 40, width=2, height=2, category="analog", block_id="receiver"),
        ComponentPlacement("U2", 49, 40, width=2, height=2, category="comms", block_id="receiver"),
    ]
    scattered = [ComponentPlacement(**placement.__dict__) for placement in grouped]
    scattered[0].x, scattered[0].y = 50, 55
    scattered[1].x, scattered[1].y = 72, 40
    pairs = {("U1", "U2"): 4.0}

    assert _cost_zone(grouped, config, pairs) < _cost_zone(grouped, config)
    assert _total_cost(grouped, config, pairs) < _total_cost(scattered, config, pairs)


def test_zone_attraction_for_support_parts_inherits_owner_connectivity():
    from circuit_weaver.placement_optimizer import ComponentPlacement, PlacementConfig, _cost_zone

    config = PlacementConfig(board_width_mm=100, board_height_mm=80)
    owner = ComponentPlacement("U1", 20, 20, category="analog")
    support = ComponentPlacement("C1", 21, 20, category="passive", parent_ref="U1", parent_pin="5")
    linked = ComponentPlacement("U2", 80, 60, category="digital")
    pairs = {("U1", "U2"): 2.0}

    family_cost = _cost_zone([owner, support, linked], config, pairs)
    independent_cost = _cost_zone([owner, support, linked], config)

    assert family_cost < independent_cost


def test_connected_category_families_keep_fixed_and_edge_constraints_exact():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    analog = _make_comp("U1", category="analog", footprint="SOIC-8")
    comms = _make_comp("U2", category="comms", footprint="SOIC-8")
    analog.pin_nets = {"1": "RX_DATA"}
    comms.pin_nets = {"1": "RX_DATA"}

    result = optimize_placement(
        [analog, comms],
        config=PlacementConfig(board_width_mm=100, board_height_mm=80, iterations=10, seed=11),
        constraints=[
            {"kind": "placement", "target": "board", "board_width_mm": 100, "board_height_mm": 80},
            {"kind": "placement", "target": "U1", "x_mm": 12.125, "y_mm": 14.25},
            {"kind": "placement", "target": "U2", "edge": "right", "max_distance_mm": 0},
        ],
    )

    fixed = result["placements"]["U1"]
    edge = result["placements"]["U2"]
    assert (fixed["x"], fixed["y"], fixed["constraint_locked"]) == (12.125, 14.25, True)
    assert edge["x"] == pytest.approx(100 - result["constraint_evaluation"]["effective_board"]["edge_clearance_mm"] - 3)
    assert edge["edge_constrained"] is True
    assert result["constraint_evaluation"]["violations"] == []


def test_large_seed_uses_centered_functional_family_grid_without_anchor_clamping():
    from circuit_weaver.placement_optimizer import (
        PlacementConfig,
        _ConstraintPlan,
        _init_placements,
    )

    components = [
        _make_comp(f"U{index:02d}", category="analog" if index < 24 else "connector", footprint="0402")
        for index in range(48)
    ]
    for component in components[:4]:
        component.block_id = "rf_front_end"
    for component in components:
        component.pin_nets = {}
    for index in range(10):
        components[index].pin_nets[f"out{index}"] = f"TRACE_{index}"
        components[index + 1].pin_nets[f"in{index}"] = f"TRACE_{index}"
    components[0].pin_nets["rf"] = "RF_IN"
    support = _make_comp("C100", category="passive", footprint="0402")
    support.placement_parent_ref = "U00"
    support.placement_parent_pin = "1"
    support.pin_nets = {"1": "RF_IN"}
    components.append(support)

    config = PlacementConfig(board_width_mm=260, board_height_mm=190, seed=31)
    plan = _ConstraintPlan(
        fixed={"U46": {"x_mm": 30.125, "y_mm": 24.25, "rotation": 0}},
        edges={"U47": {"edge": "right", "max_distance_mm": 0}},
    )
    placements = _init_placements(components, config, {}, {}, plan)
    by_ref = {placement.ref: placement for placement in placements}
    movable_roots = [by_ref[f"U{index:02d}"] for index in range(46)]

    assert all(1.5 <= item.x <= 258.5 and 1.5 <= item.y <= 188.5 for item in movable_roots)
    assert len({(item.x, item.y) for item in movable_roots}) == 46
    assert max(item.x for item in movable_roots) < 258
    assert max(item.y for item in movable_roots) < 188
    block = [by_ref[f"U{index:02d}"] for index in range(4)]
    assert max(math.dist((a.x, a.y), (b.x, b.y)) for a in block for b in block) < 15
    connected_neighbor = math.dist((by_ref["U03"].x, by_ref["U03"].y), (by_ref["U04"].x, by_ref["U04"].y))
    unrelated_pair = math.dist((by_ref["U04"].x, by_ref["U04"].y), (by_ref["U20"].x, by_ref["U20"].y))
    assert connected_neighbor < unrelated_pair
    assert (by_ref["U46"].x, by_ref["U46"].y) == (30.125, 24.25)
    assert by_ref["U46"].constraint_locked is True
    assert by_ref["U47"].x == pytest.approx(260 - config.edge_clearance_mm - 0.5)
    assert by_ref["U47"].edge_constrained is True
    assert by_ref["C100"].parent_ref == "U00"
    assert by_ref["C100"].pin_nets == {"1": "RF_IN"}
    assert by_ref["U00"].pin_nets == components[0].pin_nets


def test_result_reports_functional_group_span_and_optimizer_preserves_group_metadata():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    left = _make_comp("U1", category="digital")
    right = _make_comp("U2", category="digital")
    left.block_id = right.block_id = "controller"
    left.functional_section = right.functional_section = "communications"
    result = optimize_placement([left, right], config=PlacementConfig(strategy="simple"))

    groups = result["functional_grouping"]["groups"]
    assert groups == [
        {
            "grouping": "block_id",
            "id": "controller",
            "refs": ["U1", "U2"],
            "member_count": 2,
            "mean_pairwise_distance_mm": pytest.approx(7.5),
            "max_pairwise_distance_mm": pytest.approx(7.5),
        }
    ]
    assert result["functional_grouping"]["objective_cost"] >= 0


def test_unmachine_readable_placement_constraint_is_never_claimed_as_applied():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    result = optimize_placement(
        [_make_comp("U1")],
        config=PlacementConfig(strategy="simple"),
        constraints=[
            {
                "kind": "placement",
                "target": "U1",
                "description": "Put this somewhere sensible near the enclosure opening",
            }
        ],
    )

    assert result["constraint_evaluation"]["applied_count"] == 0
    assert result["constraint_evaluation"]["unsupported"][0]["target"] == "U1"
    assert result["quality"]["review_required"] is True


def test_pin_relative_prose_is_blocked_without_pad_coordinates():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    result = optimize_placement(
        [_make_comp("U1"), _make_comp("C1", category="passive", footprint="0402")],
        config=PlacementConfig(strategy="simple"),
        constraints=[
            {
                "kind": "placement",
                "target": "C1",
                "constraint": "within 3mm of U1 pin 5",
            }
        ],
    )

    unsupported = result["constraint_evaluation"]["unsupported"]
    assert unsupported and "pad coordinates" in unsupported[0]["reason"]


def test_balanced_strategy_compacts_disconnected_functional_islands():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    components = [
        _make_comp("U1", category="power"),
        _make_comp("U2", category="digital"),
        _make_comp("U3", category="analog"),
        _make_comp("U4", category="comms"),
        _make_comp("U5", category="sensor"),
    ]
    simple = optimize_placement(components, config=PlacementConfig(strategy="simple", seed=7))
    balanced = optimize_placement(
        components,
        config=PlacementConfig(strategy="balanced", iterations=3000, seed=7),
    )

    def _area(result):
        xs = [placement["x"] for placement in result["placements"].values()]
        ys = [placement["y"] for placement in result["placements"].values()]
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    assert _area(balanced) < _area(simple) * 0.8
    assert balanced["quality"]["overlaps"] == []


def test_source_mpn_drives_thermal_metadata_and_shared_geometry(tmp_path):
    from circuit_weaver.placement_optimizer import (
        PlacementConfig,
        estimate_component_size,
        optimize_placement,
    )

    component = _make_comp("U1", mpn="REGISTRY_ALIAS", footprint="QFN-32")
    component.source_mpn = "REAL-MPN"
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "ic_thermal.json").write_text(json.dumps({"REAL-MPN": {"pdiss_max_w": 2.5}}), encoding="utf-8")

    result = optimize_placement(
        [component],
        config=PlacementConfig(strategy="simple"),
        specs_dir=specs,
    )

    assert estimate_component_size(component) == (6.0, 6.0)
    assert result["placements"]["U1"]["width_mm"] == 6.0
    assert result["thermal_warnings"] == ["U1: 2.5W dissipation — ensure adequate copper area"]


def test_shared_geometry_parses_body_dimensions_and_known_module_connector_patterns():
    from circuit_weaver.placement_optimizer import estimate_footprint_size

    assert estimate_footprint_size("Package_LGA:Bosch_LGA-8_2.5x2.5mm", "U1") == (2.5, 2.5)
    assert estimate_footprint_size("RF_Module:ESP32-WROOM-32E", "U2") == (18.0, 25.5)
    assert estimate_footprint_size("Connector_USB:USB_C_Receptacle", "J1") == (9.0, 7.5)
    assert estimate_footprint_size("", "RP1") == (4.0, 3.0)


def test_unspecified_board_uses_compact_unverified_review_canvas():
    from circuit_weaver.placement_optimizer import PlacementConfig, optimize_placement

    components = [
        _make_comp("U1", footprint="RF_Module:ESP32-WROOM-32E"),
        _make_comp("U2", footprint="Package_LGA:Bosch_LGA-8_2.5x2.5mm"),
        _make_comp("J1", category="connector", footprint="Connector_USB:USB_C_Receptacle"),
    ] + [_make_comp(f"C{index}", category="passive", footprint="0402") for index in range(1, 13)]

    result = optimize_placement(
        components,
        config=PlacementConfig(strategy="simple"),
    )

    assert result["board_width_mm"] < 100
    assert result["board_height_mm"] < 80
    evaluation = result["constraint_evaluation"]
    assert evaluation["board_dimension_source"] == "derived_review"
    assert evaluation["effective_board"]["dimensions_verified"] is False


# ---------- placement_viewer tests ----------


def test_viewer_generates_html():
    from circuit_weaver.placement_viewer import generate_viewer

    comps = [_make_comp("U1", category="power"), _make_comp("C1", category="passive", footprint="0402")]
    placements = {
        "U1": {"x": 30, "y": 20, "rotation": 0, "layer": "front"},
        "C1": {"x": 35, "y": 25, "rotation": 90, "layer": "front"},
    }
    html = generate_viewer(comps, placements, board_width_mm=60, board_height_mm=40)
    assert "<!DOCTYPE html>" in html
    assert "U1" in html
    assert "C1" in html
    assert "PCB Placement Viewer" in html


def test_viewer_writes_file(tmp_path):
    from circuit_weaver.placement_viewer import generate_viewer

    comps = [_make_comp("U1")]
    placements = {"U1": {"x": 50, "y": 40, "rotation": 0, "layer": "front"}}
    out = tmp_path / "viewer.html"
    generate_viewer(comps, placements, output_path=out)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content


def test_viewer_thermal_overlay():
    from circuit_weaver.placement_viewer import generate_viewer

    comps = [_make_comp("U1", category="power")]
    placements = {"U1": {"x": 50, "y": 40, "rotation": 0, "layer": "front"}}
    thermal = {"IC_U1": {"pdiss_max_w": 2.0}}
    html = generate_viewer(comps, placements, thermal_data=thermal)
    assert "thermal-overlay" in html or "Thermal" in html


def test_viewer_export_csv_button():
    from circuit_weaver.placement_viewer import generate_viewer

    comps = [_make_comp("U1")]
    placements = {"U1": {"x": 50, "y": 40, "rotation": 0, "layer": "front"}}
    html = generate_viewer(comps, placements)
    assert "Export CSV" in html
    assert "exportCSV" in html


def test_viewer_is_a_closed_loop_editor_not_a_static_preview():
    from circuit_weaver.placement_viewer import generate_viewer

    comps = [_make_comp("U1"), _make_comp("U2")]
    comps[0].pin_nets = {"1": "SHARED"}
    comps[1].pin_nets = {"1": "SHARED"}
    placements = {
        "U1": {"x": 20, "y": 20, "rotation": 0, "layer": "front"},
        "U2": {"x": 35, "y": 20, "rotation": 0, "layer": "front"},
    }

    html = generate_viewer(comps, placements)

    assert "pointerdown" in html
    assert "Drag to move" in html
    assert "Export JSON" in html
    assert "Export Editable SVG" in html
    assert "function exportSVG()" in html
    assert 'data-scale-px-per-mm="8.0"' in html
    assert 'data-layer="front"' in html
    assert "group.dataset.layer = c.layer" in html
    assert "const DESIGN_FINGERPRINT" in html
    assert "saved.design_fingerprint !== DESIGN_FINGERPRINT" in html
    assert "savedRefs.length !== currentRefs.length" in html
    assert "artifact_kind: 'placement_edits'" in html
    assert 'id="connections-layer"' in html
    assert 'id="net-select"' in html
    assert "updateQuality" in html


def test_viewer_embeds_placement_brief_and_official_references():
    from circuit_weaver.placement_viewer import generate_viewer

    comp = _make_comp("U1", category="power")
    context = {
        "authority": "Datasheet is authoritative.",
        "rules": [{"priority": "critical", "guidance": "Keep the hot loop short."}],
        "research_queries": [{"query": "U1 official layout"}],
        "references": [
            {
                "title": "Vendor layout guide",
                "url": "https://example.com/layout",
                "why": "Reference placement geometry.",
            }
        ],
    }
    html = generate_viewer(
        [comp],
        {"U1": {"x": 10, "y": 10, "rotation": 0, "layer": "front"}},
        placement_context=context,
    )

    assert "Placement brief" in html
    assert "Datasheet is authoritative" in html
    assert "Keep the hot loop short" in html
    assert "Vendor layout guide" in html


def test_viewer_renders_keepouts_hard_locks_and_readable_rotated_labels():
    from circuit_weaver.placement_viewer import generate_viewer

    component = _make_comp("U1")
    component.source_mpn = "REAL-MPN"
    html = generate_viewer(
        [component],
        {
            "U1": {
                "x": 8,
                "y": 8,
                "rotation": 180,
                "layer": "front",
                "locked": True,
                "constraint_locked": True,
                "width_mm": 6,
                "height_mm": 6,
            }
        },
        board_width_mm=50,
        board_height_mm=30,
        thermal_data={"REAL-MPN": {"pdiss_max_w": 2.0}},
        placement_context={
            "constraint_evaluation": {
                "applied": [{"kind": "fixed_position", "target": "U1"}],
                "keepouts": [
                    {
                        "id": "antenna",
                        "x_mm": 20,
                        "y_mm": 0,
                        "width_mm": 10,
                        "height_mm": 12,
                    }
                ],
            },
            "review_gate": {"status": "review_required", "blockers": []},
        },
    )

    assert 'id="constraints-layer"' in html
    assert 'data-keepout="antenna"' in html
    assert 'transform="rotate(-180)"' in html
    assert '"mpn": "REAL-MPN"' in html
    assert '"pdiss_w": 2.0' in html
    assert "if (COMPS[ref].constraint_locked) continue" in html
    assert "if (COMPS[selectedRef].constraint_locked) return" in html
    assert "components violate keepouts" in html


def test_viewer_storage_fingerprint_changes_with_board_or_constraints():
    import re

    from circuit_weaver.placement_viewer import generate_viewer

    component = _make_comp("U1")
    placements = {"U1": {"x": 8, "y": 8, "rotation": 0, "layer": "front"}}
    first = generate_viewer([component], placements, 40, 30)
    second = generate_viewer([component], placements, 50, 30)
    constrained = generate_viewer(
        [component],
        placements,
        40,
        30,
        placement_context={"constraint_evaluation": {"applied": [{"kind": "edge", "target": "U1", "edge": "left"}]}},
    )

    def _fingerprint(html: str) -> str:
        match = re.search(r"const DESIGN_FINGERPRINT = '([0-9a-f]+)'", html)
        assert match
        return match.group(1)

    assert len({_fingerprint(first), _fingerprint(second), _fingerprint(constrained)}) == 3


def test_viewer_empty_components():
    from circuit_weaver.placement_viewer import generate_viewer

    html = generate_viewer([], {})
    assert "<!DOCTYPE html>" in html


# ---------- CLI integration tests ----------


def test_cli_optimize_placement_help():
    result = subprocess.run(
        [sys.executable, "-m", "circuit_weaver.dispatcher", "optimize-placement", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "optimize-placement" in result.stdout or "simulated annealing" in result.stdout


def test_cli_placement_viewer_help():
    result = subprocess.run(
        [sys.executable, "-m", "circuit_weaver.dispatcher", "placement-viewer", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "placement-viewer" in result.stdout or "interactive" in result.stdout
