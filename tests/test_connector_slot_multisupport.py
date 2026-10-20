from __future__ import annotations

import pytest
from test_connector_slot_refinement import native_pad

from circuit_weaver.component_db import ComponentDef, PinDef
from circuit_weaver.placement_optimizer import (
    ComponentPlacement,
    _ConstraintPlan,
    _refine_interchangeable_connector_slots,
)


def _part(ref, net, category, x, *, parent="", pads=None, pin_nets=None):
    pins = pads or ["1"]
    nets = pin_nets if pin_nets is not None else ({pins[0]: net} if net else {})
    comp = ComponentDef(
        mpn=ref,
        source_ref=ref,
        category=category,
        pins=[PinDef(number, number, "passive", "L") for number in pins],
        pin_nets=nets,
    )
    comp.placement_parent_ref = parent
    comp.placement_pin_offsets = {number: (0.0, 0.0) for number in pins}
    comp.placement_geometry_status = "board_geometry"
    comp.placement_pad_offsets = {n: [(0.0, 0.0)] for n in nets.values()}
    comp.placement_native_footprint_id = f"Test:{category}"
    comp.placement_native_pad_geometry = [{**native_pad(number, 0, 0), "net": nets.get(number, "")} for number in pins]
    comp.placement_layer = "front"
    comp.placement_width_mm = 3.0
    comp.placement_height_mm = 3.0
    placement = ComponentPlacement(
        ref,
        x,
        20.0,
        category=category,
        width=3.0,
        height=3.0,
        geometry_status="board_geometry",
        pin_offsets=comp.placement_pin_offsets.copy(),
        pin_nets=nets.copy(),
        parent_ref=parent,
        native_footprint_id=comp.placement_native_footprint_id,
        native_pad_geometry=[row.copy() for row in comp.placement_native_pad_geometry],
        native_reference_locked=False,
    )
    return comp, placement


def family_fixture(*, mixed_family=False, multiple_private=False, incomplete=False):
    from test_connector_slot_refinement import connector

    c1, j1 = connector("J-left", "LOAD-B")
    c2, j2 = connector("J-right", "LOAD-A")
    # The two connector headers share the same native shared-pad map, but each
    # private signal net reaches two support pads owned by one real root.
    roots = []
    supports = []
    states = []
    for tag, load, x in (("A", "LOAD-A", 10.0), ("B", "LOAD-B", 20.0)):
        root, rootp = _part(f"U-{tag}", f"CTRL-{tag}", "digital", x + 2, pads=["1"])
        roots.append(root)
        states.append(rootp)
        rparent = f"U-{tag}"
        cparent = f"U-other-{tag}" if mixed_family and tag == "A" else rparent
        r, rp = _part(f"R-{tag}", load, "passive", x + 1, parent=rparent)
        c, cp = _part(f"C-{tag}", load, "passive", x + 3, parent=cparent)
        supports.extend([r, c])
        states.extend([rp, cp])
        if cparent != rparent:
            # Add the second explicit owner root so the mixed case is a known,
            # but invalid, cross-family net rather than a missing-parent case.
            other, otherp = _part(cparent, f"CTRL-other-{tag}", "digital", x + 4)
            roots.append(other)
            states.append(otherp)
    comps = [c1, c2, *roots, *supports]
    state = [j1, j2, *states]
    if multiple_private:
        c1.pin_nets["3"] = "LOAD-EXTRA"
        c1.placement_pin_offsets["3"] = (1.27, 0.0)
        j1.pin_nets["3"] = "LOAD-EXTRA"
        j1.pin_offsets["3"] = (1.27, 0.0)
        j1.native_pad_geometry.append({**native_pad("3", 1_270_000, 0), "net": "LOAD-EXTRA"})
        extra, extra_p = _part("R-extra", "LOAD-EXTRA", "passive", 14.0, parent="U-A")
        comps.append(extra)
        state.append(extra_p)
    if incomplete:
        for tag, load in (("A", "LOAD-A"), ("B", "LOAD-B")):
            ghost, ghostp = _part(f"R-incomplete-{tag}", load, "passive", 18.0, parent=f"U-{tag}")
            ghostp.native_pad_geometry = None
            ghostp.pin_offsets = None
            comps.append(ghost)
            state.append(ghostp)
    return comps, state


def run_family(**kwargs):
    from circuit_weaver.placement_optimizer import (
        PlacementConfig,
        _build_connectivity_pairs,
    )

    comps, state = family_fixture(**kwargs)
    result, metadata = _refine_interchangeable_connector_slots(
        comps,
        state,
        PlacementConfig(board_width_mm=40, board_height_mm=40, min_component_gap_mm=0.0),
        _build_connectivity_pairs(comps),
        _ConstraintPlan(),
    )
    return comps, state, result, metadata


def test_same_known_support_family_accepts_one_private_net_with_two_external_pads():
    _comps, _state, result, meta = run_family()
    assert meta["accepted"] is True
    assert meta["algorithm"] == "native_private_net_support_family_euclidean_hungarian"
    assert meta["groups"][0]["size"] == 2
    assert meta["groups"][0]["matching_cost_evaluations"] == 8
    assert meta["groups"][0]["cost_matrix_entries"] == 4
    assert meta["groups"][0]["assignment_cubic_size_bound"] == 8
    assert [(p.ref, p.x) for p in result[:2]] == [("J-left", 20.0), ("J-right", 10.0)]


def test_private_net_across_two_support_roots_is_rejected():
    _comps, _state, _result, meta = run_family(mixed_family=True)
    assert meta["accepted"] is False
    assert meta["ineligible_reasons"].get("requires_exactly_one_private_pad_net", 0) >= 1


def test_multiple_private_connector_nets_are_rejected():
    _comps, _state, _result, meta = run_family(multiple_private=True)
    assert meta["accepted"] is False
    assert meta["ineligible_reasons"].get("requires_exactly_one_private_pad_net", 0) >= 1


def test_incomplete_external_endpoint_inventory_makes_net_ambiguous():
    _comps, _state, _result, meta = run_family(incomplete=True)
    assert meta["accepted"] is False
    assert meta["eligible_component_count"] == 0
    assert meta["ineligible_reasons"].get("requires_exactly_one_private_pad_net", 0) >= 1


def test_shared_pad_net_map_must_match_across_slots():
    comps, state = family_fixture()
    comps[1].pin_nets["1"] = "DIFFERENT-COMMON"
    state[1].pin_nets["1"] = "DIFFERENT-COMMON"
    state[1].native_pad_geometry[0]["net"] = "DIFFERENT-COMMON"
    # Each distinct shared net has endpoints in two different support roots.
    # It is therefore not mistaken for a single-family private signal net.
    for net in ("COMMON", "DIFFERENT-COMMON"):
        for root in ("U-A", "U-B"):
            endpoint, endpointp = _part(f"R-{net}-{root}", net, "passive", 5.0, parent=root)
            comps.append(endpoint)
            state.append(endpointp)
    from circuit_weaver.placement_optimizer import (
        PlacementConfig,
        _build_connectivity_pairs,
    )

    _, meta = _refine_interchangeable_connector_slots(
        comps,
        state,
        PlacementConfig(board_width_mm=40, board_height_mm=40, min_component_gap_mm=0.0),
        _build_connectivity_pairs(comps),
        _ConstraintPlan(),
    )
    assert meta["eligible_component_count"] == 2
    assert meta["groups"] == []
    assert meta["moved_refs"] == []
    assert meta["accepted"] is False


def test_strict_single_support_pad_behavior_remains_eligible():
    from test_connector_slot_refinement import fixture, run

    comps, state, plan = fixture()
    _result, meta = run(comps, state, plan)
    assert meta["accepted"] is True
    assert meta["algorithm"] == "native_private_net_support_family_euclidean_hungarian"


def test_state_only_support_child_excludes_connector_owner_from_moving_alone():
    from circuit_weaver.placement_optimizer import ComponentPlacement

    comps, state = family_fixture()
    state.append(
        ComponentPlacement(
            "STATE-ONLY-C1",
            30.0,
            30.0,
            category="passive",
            width=1.0,
            height=1.0,
            geometry_status="board_geometry",
            parent_ref="J-left",
        )
    )
    from circuit_weaver.placement_optimizer import (
        PlacementConfig,
        _build_connectivity_pairs,
    )

    _result, meta = _refine_interchangeable_connector_slots(
        comps,
        state,
        PlacementConfig(board_width_mm=40, board_height_mm=40, min_component_gap_mm=0.0),
        _build_connectivity_pairs(comps),
        _ConstraintPlan(),
    )
    assert meta["eligible_component_count"] == 1
    assert meta["groups"] == []
    assert meta["moved_refs"] == []
    assert meta["accepted"] is False
    assert meta["ineligible_reasons"].get("owned_support_family", 0) >= 1


@pytest.mark.parametrize("ownership_case", ["missing_root", "cyclic_root"])
def test_private_support_endpoints_with_missing_or_cyclic_root_are_rejected(ownership_case):
    comps, state = family_fixture()
    comp_by_ref = {comp.source_ref: comp for comp in comps}
    state_by_ref = {placement.ref: placement for placement in state}
    if ownership_case == "missing_root":
        comp_by_ref["R-A"].placement_parent_ref = "NO-SUCH-OWNER"
        state_by_ref["R-A"].parent_ref = "NO-SUCH-OWNER"
    else:
        comp_by_ref["U-A"].placement_parent_ref = "U-B"
        state_by_ref["U-A"].parent_ref = "U-B"
        comp_by_ref["U-B"].placement_parent_ref = "U-A"
        state_by_ref["U-B"].parent_ref = "U-A"

    from circuit_weaver.placement_optimizer import (
        PlacementConfig,
        _build_connectivity_pairs,
    )

    _result, meta = _refine_interchangeable_connector_slots(
        comps,
        state,
        PlacementConfig(board_width_mm=40, board_height_mm=40, min_component_gap_mm=0.0),
        _build_connectivity_pairs(comps),
        _ConstraintPlan(),
    )
    assert meta["groups"] == []
    assert meta["moved_refs"] == []
    assert meta["accepted"] is False
    assert meta["ineligible_reasons"].get("requires_exactly_one_private_pad_net", 0) >= 1
