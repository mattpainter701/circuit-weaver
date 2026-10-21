from __future__ import annotations

import unittest
from dataclasses import replace

from circuit_weaver import placement_optimizer as o


def native_component(ref, x, y, pads, *, parent_ref="", role="", locked=False,
                     constraint_locked=False, edge_constrained=False, rotation_locked=False):
    pin_offsets = {str(number): tuple(offset) for number, net, offset in pads}
    pin_nets = {str(number): net for number, net, _offset in pads}
    net_offsets = {}
    rows = []
    for number, net, offset in pads:
        net_offsets.setdefault(net, []).append(tuple(offset))
        rows.append({"number": str(number), "net": net,
                     "local_position_iu": [round(offset[0] * 1_000_000), round(offset[1] * 1_000_000)],
                     "copper_layers": [0]})
    return o.ComponentPlacement(
        ref=ref, x=x, y=y, rotation=0.0, layer="front", width=8.0 if ref.startswith("U") else 2.0,
        height=6.0 if ref.startswith("U") else 1.0, parent_ref=parent_ref,
        placement_role=role, locked=locked, constraint_locked=constraint_locked,
        edge_constrained=edge_constrained, rotation_locked=rotation_locked,
        geometry_status="board_geometry", net_pad_offsets=net_offsets,
        pin_offsets=pin_offsets, pin_nets=pin_nets, native_footprint_id=f"Test:{ref}",
        native_pad_geometry=rows, native_reference_locked=False,
        native_surface_copper_layer_id=0, native_surface_copper_side="front",
        native_board_copper_layer_ids=[0, 31], native_board_copper_layer_inventory_complete=True,
        native_known_copper_layer_ids=[0, 31], native_known_copper_layer_inventory_complete=True,
        native_footprint_is_smd=True,
    )


def fixture(*, support_x=65.0, rotation_locked=False):
    parent = native_component("U1", 40.0, 40.0,
                               [("1", "VDD_3P3", (-2.0, -1.0)), ("2", "VDD_3P3", (2.0, -1.0)),
                                ("3", "GND", (-2.0, 1.0)), ("4", "GND", (2.0, 1.0))])
    support = native_component("C1", support_x, 40.0,
                               [("1", "VDD_3P3", (-0.5, 0.0)), ("2", "GND", (0.5, 0.0))],
                               parent_ref="U1", role="decoupling", rotation_locked=rotation_locked)
    cfg = o.PlacementConfig(board_width_mm=100.0, board_height_mm=80.0,
                            edge_clearance_mm=1.0, min_component_gap_mm=0.5,
                            support_body_clearance_mm=1.5, refinement_passes=1)
    return [parent, support], cfg, o._ConstraintPlan()


class TestJointSupportRefinement(unittest.TestCase):
    def test_remote_rail_endpoints_prevent_span_regression(self):
        state, cfg, plan = fixture()
        remote = native_component("R9", 95.0, 40.0,
                                  [("1", "VDD_3P3", (0.0, 0.0)),
                                   ("2", "GND", (0.0, 0.0))])
        state.append(remote)
        parent, support = state[:2]
        rail, ret = "VDD_3P3", "GND"
        old_legs = o._power_support_legs(parent, support, rail, ret)
        old_hpwl = (o._power_net_hpwl(state, rail), o._power_net_hpwl(state, ret))
        safe = [replace(p) for p in state]
        safe[1].x, safe[1].y = 47.0, 40.0
        self.assertIsNotNone(o._power_support_prefilter(
            safe, state, support.ref, parent.ref, rail, ret, cfg, plan, old_legs, old_hpwl))
        wider = [replace(p) for p in state]
        wider[1].x, wider[1].y = 40.0, 34.0
        new_legs = o._power_support_legs(parent, wider[1], rail, ret)
        self.assertTrue(all(new < old for new, old in zip(new_legs, old_legs)))
        self.assertGreater(o._power_net_hpwl(wider, rail), old_hpwl[0])
        self.assertIsNone(o._power_support_prefilter(
            wider, state, support.ref, parent.ref, rail, ret, cfg, plan, old_legs, old_hpwl))

    def test_native_pad_map_proof_rejects_disagreement(self):
        state, _cfg, plan = fixture()
        support = state[1]
        self.assertTrue(o._power_support_native_maps_complete(support))
        bad = replace(support, pin_nets={**support.pin_nets, "1": "OTHER"})
        self.assertFalse(o._power_support_native_maps_complete(bad))
        self.assertIsNone(o._power_support_eligibility(state, bad, plan))

    def test_global_hpwl_gate_requires_complete_maps_for_entire_state(self):
        state, _cfg, _plan = fixture()
        self.assertTrue(o._power_support_state_maps_complete(state))
        unknown = replace(native_component("R9", 72.0, 60.0,
                                            [("1", "VDD_3P3", (0.0, 0.0)),
                                             ("2", "GND", (1.0, 0.0))]),
                         native_pad_geometry=None)
        partial = [*state, unknown]
        self.assertFalse(o._power_support_state_maps_complete(partial))
        calls = 0
        real = o._power_support_refinement_targets

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        o._power_support_refinement_targets = counted
        try:
            o._refine_placement(partial, o.PlacementConfig(refinement_passes=1), {},
                                o._ConstraintPlan(), evaluation_budget=3)
        finally:
            o._power_support_refinement_targets = real
        self.assertEqual(calls, 0)

    def test_global_map_proof_allows_tht_multilayer_duplicate_numbers_and_npth(self):
        state, _cfg, _plan = fixture()
        support = state[1]
        rows = [dict(row) for row in support.native_pad_geometry]
        for row in rows:
            row["copper_layers"] = [0, 31]
        rows.append({"number": "1", "net": "VDD_3P3", "local_position_iu": [1_000_000, 2_000_000],
                     "copper_layers": [0, 31]})
        rows.append({"number": "", "net": "", "local_position_iu": [3_000_000, 4_000_000],
                     "copper_layers": []})
        offsets = dict(support.pin_offsets)
        offsets.pop("1")
        net_offsets = {net: list(points) for net, points in support.net_pad_offsets.items()}
        net_offsets["VDD_3P3"].append((1.0, 2.0))
        tht = replace(support, native_footprint_is_smd=False, native_pad_geometry=rows,
                      pin_offsets=offsets, net_pad_offsets=net_offsets)
        self.assertTrue(o._power_support_state_maps_complete([state[0], tht]))
        self.assertFalse(o._power_support_native_maps_complete(tht))

    def test_support_map_proof_malformed_offsets_fail_closed(self):
        state, _cfg, _plan = fixture()
        support = state[1]
        short_offset = replace(support, pin_offsets={**support.pin_offsets, "1": (0.0,)})
        self.assertFalse(o._power_support_native_maps_complete(short_offset))
        malformed_endpoints = replace(support, net_pad_offsets={"VDD_3P3": 1, "GND": []})
        self.assertFalse(o._power_support_native_maps_complete(malformed_endpoints))
        nonnumeric_endpoint = replace(support, net_pad_offsets={"VDD_3P3": [("bad", 0.0)], "GND": [(0.5, 0.0)]})
        self.assertFalse(o._power_support_native_maps_complete(nonnumeric_endpoint))
        long_offset = replace(support, pin_offsets={**support.pin_offsets, "1": (0.0, 0.0, 1.0)})
        self.assertFalse(o._power_support_native_maps_complete(long_offset))
        self.assertFalse(o._power_support_state_maps_complete([state[0], long_offset]))

    def test_global_map_rejects_unnumbered_netted_pad(self):
        state, _cfg, _plan = fixture()
        component = state[1]
        rows = [dict(row) for row in component.native_pad_geometry]
        rows.append({"number": "", "net": "VDD_3P3", "local_position_iu": [3_000_000, 4_000_000],
                     "copper_layers": [0]})
        malformed = replace(component, native_pad_geometry=rows)
        self.assertFalse(o._power_support_state_maps_complete([state[0], malformed]))

    def test_global_map_proof_malformed_native_point_fails_closed(self):
        state, _cfg, _plan = fixture()
        malformed = replace(state[1], native_pad_geometry=[dict(row) for row in state[1].native_pad_geometry])
        malformed.native_pad_geometry[0]["local_position_iu"] = ["not-a-coordinate", 0]
        self.assertFalse(o._power_support_state_maps_complete([state[0], malformed]))

    def test_joint_pose_generation_uses_native_pairs_and_quarter_turns(self):
        state, cfg, plan = fixture()
        support = state[1]
        rows = o._power_support_joint_candidates(state, support, cfg, plan)
        self.assertGreater(len(rows), 0)
        self.assertGreater(len(rows), 64)
        self.assertLessEqual(len(rows), 128)
        self.assertEqual({key[0] for _candidate, key in rows}, {0, 1})
        self.assertEqual({key[3] for _candidate, key in rows}, {0, 1})
        eligible = o._power_support_eligibility(state, support, plan)
        self.assertIsNotNone(eligible)
        parent, rail, return_net = eligible
        old_legs = o._power_support_legs(parent, support, rail, return_net)
        old_hpwl = (o._power_net_hpwl(state, rail), o._power_net_hpwl(state, return_net))
        self.assertTrue(all(o._power_support_prefilter(candidate, state, support.ref, parent.ref,
                                                       rail, return_net, cfg, plan, old_legs, old_hpwl)
                            is not None for candidate, _key in rows))
        poses = {(candidate[1].x, candidate[1].y, candidate[1].rotation, candidate[1].layer)
                 for candidate, _key in rows}
        self.assertEqual(len(poses), len(rows))
        self.assertEqual({pose[2] for pose in poses}, {0.0, 90.0, 180.0, 270.0})
        self.assertEqual({pose[3] for pose in poses}, {"front"})
        self.assertTrue(all(candidate[0].x == state[0].x and candidate[0].y == state[0].y
                            and candidate[0].rotation == state[0].rotation for candidate, _key in rows))

    def test_rotation_lock_restricts_candidates_and_preserves_side(self):
        state, cfg, plan = fixture(rotation_locked=True)
        rows = o._power_support_joint_candidates(state, state[1], cfg, plan)
        self.assertTrue(rows)
        self.assertEqual({candidate[1].rotation for candidate, _key in rows}, {state[1].rotation})
        self.assertEqual({candidate[1].layer for candidate, _key in rows}, {state[1].layer})

    def test_support_must_share_owner_side(self):
        state, _cfg, plan = fixture()
        state[1] = replace(state[1], layer="back", native_surface_copper_side="back",
                           native_surface_copper_layer_id=31,
                           native_pad_geometry=[{**row, "copper_layers": [31]}
                                                for row in state[1].native_pad_geometry])
        self.assertTrue(o._power_support_native_maps_complete(state[1]))
        self.assertIsNone(o._power_support_eligibility(state, state[1], plan))

    def test_support_locks_fail_closed(self):
        for field in ("locked", "constraint_locked", "edge_constrained"):
            with self.subTest(field=field):
                state, cfg, plan = fixture()
                state[1] = replace(state[1], **{field: True})
                self.assertEqual(o._power_support_joint_candidates(state, state[1], cfg, plan), [])

    def test_target_cohort_is_deterministic_and_capped_at_worst_sixteen(self):
        state = []
        for index in range(18):
            parent = native_component(f"U{index:02d}", 10.0, 20.0,
                                      [("1", "VDD_3P3", (-2.0, -1.0)), ("2", "GND", (2.0, 1.0))])
            support = native_component(f"C{index:02d}", 15.0 + index, 20.0,
                                       [("1", "VDD_3P3", (-0.5, 0.0)), ("2", "GND", (0.5, 0.0))],
                                       parent_ref=parent.ref, role="decoupling")
            state.extend((parent, support))
        plan = o._ConstraintPlan()
        first = o._power_support_refinement_targets(state, o.PlacementConfig(), plan)
        second = o._power_support_refinement_targets(state, o.PlacementConfig(), plan)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertEqual(first[0], "C17")
        self.assertNotIn("C00", first)
    def test_refinement_charges_full_rank_work_to_existing_budget(self):
        state, cfg, plan = fixture()
        hard_calls = 0
        cost_calls = 0
        real_hard = o._placement_hard_defects
        real_cost = o._total_cost

        def counted_hard(*args, **kwargs):
            nonlocal hard_calls
            hard_calls += 1
            return real_hard(*args, **kwargs)

        def counted_cost(*args, **kwargs):
            nonlocal cost_calls
            cost_calls += 1
            return real_cost(*args, **kwargs)

        o._placement_hard_defects = counted_hard
        o._total_cost = counted_cost
        try:
            _result, evaluations = o._refine_placement(state, cfg, {}, plan, evaluation_budget=3)
        finally:
            o._placement_hard_defects = real_hard
            o._total_cost = real_cost
        self.assertEqual(evaluations, 3)
        self.assertLessEqual(hard_calls, 4)  # one baseline rank plus at most three candidate ranks
        self.assertLessEqual(cost_calls, hard_calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
