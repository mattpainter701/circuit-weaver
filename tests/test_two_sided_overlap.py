from __future__ import annotations

import json

from circuit_weaver._kicad_worker import (
    _native_copper_layer_inventory,
    _native_roundrect_radius_ratio,
    _native_surface_shape_known,
    _opposite_courtyard_state,
)
from circuit_weaver.placement_optimizer import (
    ComponentPlacement,
    PlacementConfig,
    _build_result,
    _cost_support_body_clearance,
    _overlap_area,
    _overlapping_pairs,
    _proven_native_surface,
    _proven_opposite_native_smd_sides,
    _rectangle_clearance,
)


def _pad(side: str = "front") -> dict:
    return {
        "number": "1",
        "shape": 0,
        "attribute": 1,  # pcbnew.PAD_ATTRIB_SMD
        "size_iu": [500_000, 500_000],
        "drill_iu": [0, 0],
        "layers": [0, 1, 13] if side == "front" else [2, 3, 15],
        "copper_layers": [0] if side == "front" else [2],
        "known_shape": True,
        "surface_shape_known": True,
        "native_roundrect": False,
        "roundrect_radius_ratio": None,
    }


def _placement(ref: str, side: str, **changes) -> ComponentPlacement:
    values = {
        "ref": ref,
        "x": 0.0,
        "y": 0.0,
        "layer": side,
        "width": 2.0,
        "height": 2.0,
        "geometry_status": "board_geometry",
        "native_footprint_id": "Test:Footprint",
        "native_pad_geometry": [_pad(side)],
        "native_surface_copper_layer_id": 0 if side == "front" else 2,
        "native_surface_copper_side": side,
        "native_board_copper_layer_ids": [0, 2, 4, 6, 8, 10],
        "native_board_copper_layer_inventory_complete": True,
        "native_known_copper_layer_ids": [0, 2, 4, 6, 8, 10],
        "native_known_copper_layer_inventory_complete": True,
        "native_footprint_is_smd": True,
        "native_opposite_courtyard_known": True,
        "native_opposite_courtyard_present": False,
    }
    values.update(changes)
    return ComponentPlacement(**values)


def test_only_proven_opposite_surface_smd_bodies_are_disjoint():
    front = _placement("R1", "front")
    back = _placement("R2", "back")

    assert _proven_native_surface(front) == "front"
    assert _proven_native_surface(back) == "back"
    assert _overlap_area(front, back, gap=0.5) == 0
    # The general geometric helper remains finite and layer-agnostic.
    assert _rectangle_clearance(front, back) == 0


def test_in_place_native_pad_mutation_invalidates_cached_surface_proof():
    front = _placement("R1", "front")
    back = _placement("R2", "back")

    assert _overlap_area(front, back) == 0
    front.native_pad_geometry[0]["drill_iu"][0] = 100_000

    assert _proven_native_surface(front) is None
    assert _overlap_area(front, back) > 0


def test_disabled_inner_copper_layer_in_pad_mask_remains_blocking():
    mixed = _placement("R1", "front", native_pad_geometry=[{
        **_pad("front"), "layers": [0, 1, 4, 13], "copper_layers": [0, 4],
    }])
    back = _placement("R2", "back")

    assert _proven_native_surface(mixed) is None
    assert _overlap_area(mixed, back) > 0


def test_opposite_surface_support_body_exemption_keeps_result_json_finite():
    parent = _placement("U1", "front")
    support = _placement("C1", "back", parent_ref="U1")
    config = PlacementConfig()

    assert _cost_support_body_clearance([parent, support], config) == 0
    result = _build_result([parent, support], config, 10, 1.0, 1.0)
    json.dumps(result, allow_nan=False)
    assert result["quality"]["support_body_violations"] == []
    assert result["quality"]["support_distances"][0]["distance_mm"] == 0


def test_tht_npth_unknown_mixed_and_opposite_courtyard_stay_blocking():
    back = _placement("R2", "back")

    cases = [
        {"native_pad_geometry": [{**_pad("front"), "attribute": 0,
                                  "drill_iu": [600_000, 600_000],
                                  "layers": list(range(63))}]},
        {"native_pad_geometry": [{**_pad("front"), "attribute": 3,
                                  "drill_iu": [700_000, 700_000],
                                  "layers": list(range(63))}]},
        {"native_pad_geometry": [{**_pad("front"), "known_shape": False}]},
        {"native_pad_geometry": [{**_pad("front"), "layers": [0, 2, 1, 13]}]},
        {"native_opposite_courtyard_known": None},
        {"native_opposite_courtyard_present": True},
        {"native_footprint_is_smd": False},
        {"native_footprint_is_smd": None},
        {"native_surface_copper_side": "unknown"},
        {"native_surface_copper_layer_id": 9},
        {"native_board_copper_layer_inventory_complete": False},
        {"native_footprint_id": ""},
        {"geometry_status": "estimated"},
    ]
    for changes in cases:
        unknown = _placement("U1", "front", **changes)
        assert _proven_native_surface(unknown) is None
        assert _overlap_area(unknown, back, gap=0.0) > 0


def test_same_surface_smd_overlap_remains_blocking():
    front_a = _placement("R1", "front")
    front_b = _placement("R2", "front")

    assert _overlap_area(front_a, front_b, gap=0.0) > 0
    assert _rectangle_clearance(front_a, front_b) == 0


def test_same_side_pair_skips_native_pad_proof(monkeypatch):
    import circuit_weaver.placement_optimizer as optimizer

    front_a = _placement("R1", "front")
    front_b = _placement("R2", "front")

    def unexpected_proof(_placement):
        raise AssertionError("same-side pairs do not need surface metadata")

    monkeypatch.setattr(optimizer, "_proven_native_surface", unexpected_proof)
    assert not _proven_opposite_native_smd_sides(front_a, front_b)
    assert _overlap_area(front_a, front_b, gap=0.0) > 0


def test_sweep_pairs_match_canonical_geometry_with_mixed_surface_proofs():
    placements = [
        _placement("F1", "front", x=0.0),
        _placement("B1", "back", x=0.2),
        _placement("T1", "front", x=0.4, native_pad_geometry=[{
            **_pad("front"), "attribute": 0, "drill_iu": [600_000, 600_000],
            "layers": list(range(63)),
        }]),
        _placement("B2", "back", x=8.0),
    ]
    expected = [
        (i, j, area)
        for i in range(len(placements))
        for j in range(i + 1, len(placements))
        if (area := _overlap_area(placements[i], placements[j], 0.25)) > 0
    ]

    assert _overlapping_pairs(placements, 0.25) == expected
    assert all((i, j) != (0, 1) for i, j, _area in _overlapping_pairs(placements, 0.25))
    assert any((i, j) == (1, 2) for i, j, _area in _overlapping_pairs(placements, 0.25))


def test_native_inspector_records_opposite_courtyard_presence_and_unknown():
    class Empty:
        def IsEmpty(self):
            return True

    class Present:
        def IsEmpty(self):
            return False

    class FailingFootprint:
        def GetLayer(self):
            return 1

        def GetCourtyard(self, _layer):
            raise RuntimeError("native layer query unavailable")

        def GraphicalItems(self):
            return []

    class Footprint:
        def __init__(self, layer, courtyard):
            self.layer = layer
            self.courtyard = courtyard

        def GetLayer(self):
            return self.layer

        def GetCourtyard(self, layer):
            self.requested_layer = layer
            return self.courtyard

        def GraphicalItems(self):
            return self.graphics

    class Layers:
        F_CrtYd = 51
        B_CrtYd = 52
        B_Cu = 2

    front = Footprint(0, Empty())
    front.graphics = []
    back = Footprint(2, Present())
    back.graphics = []
    assert _opposite_courtyard_state(front, Layers) == (True, False)
    assert front.requested_layer == Layers.B_CrtYd
    assert _opposite_courtyard_state(back, Layers) == (True, True)
    assert back.requested_layer == Layers.F_CrtYd
    assert _opposite_courtyard_state(FailingFootprint(), Layers) == (False, None)

    class CourtyardGraphic:
        def GetLayer(self):
            return Layers.B_CrtYd

    malformed = Footprint(0, Empty())
    malformed.graphics = [CourtyardGraphic()]
    assert _opposite_courtyard_state(malformed, Layers) == (True, True)

    class BadGraphic:
        def GetLayer(self):
            raise RuntimeError("unreadable graphic layer")

    unknown = Footprint(0, Empty())
    unknown.graphics = [BadGraphic()]
    assert _opposite_courtyard_state(unknown, Layers) == (False, None)


def test_native_copper_inventory_is_explicit_and_fails_closed():
    class Mask:
        def Seq(self):
            return [0, 2, 4, 6]

    class Stack:
        def Seq(self):
            return [0, 2, 4, 6]

    class EnabledLayers:
        def CuStack(self):
            return Stack()

    class Board:
        def GetEnabledLayers(self):
            return EnabledLayers()

        def GetCopperLayerCount(self):
            return 4

    class PCBNew:
        F_Cu = 0
        B_Cu = 2
        In1_Cu = 4
        In2_Cu = 6
        class LSET:
            @staticmethod
            def AllCuMask():
                return Mask()

    assert _native_copper_layer_inventory(Board(), PCBNew) == ([0, 2, 4, 6], True)

    class IncompletePCBNew(PCBNew):
        class LSET:
            @staticmethod
            def AllCuMask():
                class IncompleteMask:
                    def Seq(self):
                        return [0, 2, 4, 6, 8]
                return IncompleteMask()

    assert _native_copper_layer_inventory(Board(), IncompletePCBNew) == ([0, 2, 4, 6], False)

    class MissingBoard:
        pass

    assert _native_copper_layer_inventory(MissingBoard(), PCBNew) == ([], False)


def test_native_copper_inventory_accepts_native_lseq_iteration_without_seq():
    class LSEQ:
        def __iter__(self):
            return iter([0, 2, 4, 6])

    class EnabledLayers:
        def CuStack(self):
            return LSEQ()

    class Board:
        def GetEnabledLayers(self):
            return EnabledLayers()

        def GetCopperLayerCount(self):
            return 4

    class PCBNew:
        F_Cu = 0
        B_Cu = 2
        In1_Cu = 4
        In2_Cu = 6
        class LSET:
            @staticmethod
            def AllCuMask():
                class Mask:
                    def Seq(self):
                        return [0, 2, 4, 6]
                return Mask()

    assert _native_copper_layer_inventory(Board(), PCBNew) == ([0, 2, 4, 6], True)



def test_roundrect_has_separate_validated_occupancy_shape_contract():
    class PCBNew:
        PAD_SHAPE_ROUNDRECT = 4
        PAD_SHAPE_CIRCLE = 0
        PAD_SHAPE_RECT = 1
        PAD_SHAPE_OVAL = 2

    class Pad:
        def __init__(self, shape, ratio):
            self.shape = shape
            self.ratio = ratio

        def GetShape(self):
            return self.shape

        def GetRoundRectRadiusRatio(self):
            return self.ratio

    native = Pad(4, 0.25)
    ratio = _native_roundrect_radius_ratio(native, PCBNew)
    assert ratio == 0.25
    assert _native_surface_shape_known(native, PCBNew) is True
    roundrect = _placement("R1", "front", native_pad_geometry=[{
        **_pad("front"), "shape": 4, "known_shape": False,
        "surface_shape_known": True, "native_roundrect": True,
        "roundrect_radius_ratio": ratio,
    }])
    back = _placement("R2", "back")
    assert _proven_native_surface(roundrect) == "front"
    assert _overlap_area(roundrect, back) == 0

    # The legacy geometry contract stays false, and malformed ratio metadata
    # cannot preserve a positive surface proof after content mutation.
    roundrect.native_pad_geometry[0]["roundrect_radius_ratio"] = 0.75
    assert _proven_native_surface(roundrect) is None
    assert _overlap_area(roundrect, back) > 0
    assert _native_surface_shape_known(Pad(99, 0.2), PCBNew) is False
    assert _native_roundrect_radius_ratio(Pad(4, float("nan")), PCBNew) is None


def test_overlap_sweep_memoizes_surface_proofs_only_within_one_pass(monkeypatch):
    import circuit_weaver.placement_optimizer as optimizer

    placements = [
        *[_placement(f"F{i}", "front", x=0.01 * i) for i in range(4)],
        *[_placement(f"B{i}", "back", x=0.01 * i) for i in range(3)],
    ]
    original = optimizer._proven_native_surface
    calls = []

    def counted(placement):
        calls.append(placement.ref)
        return original(placement)

    monkeypatch.setattr(optimizer, "_proven_native_surface", counted)
    first = _overlapping_pairs(placements)
    assert all((i < 4) == (j < 4) for i, j, _ in first)
    assert len(calls) == 7
    assert set(calls) == {placement.ref for placement in placements}

    calls.clear()
    placements[0].native_pad_geometry[0]["drill_iu"][0] = 100_000
    second = _overlapping_pairs(placements)
    assert len(calls) == 7
    assert any(i == 0 and j >= 4 for i, j, _ in second)
    assert _overlap_area(placements[0], placements[4]) > 0
