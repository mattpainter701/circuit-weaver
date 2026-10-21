from __future__ import annotations

from types import SimpleNamespace

import pytest

from circuit_weaver import pcb_layout
from circuit_weaver.pcb_layout import _validate_design_outline_constraints


def _keepout(index: int, *, kind: str = "keepout") -> dict:
    return {
        "kind": kind,
        "target": "board",
        "id": f"virtual_slot_{index}",
        "x_mm": float(index),
        "y_mm": float(index),
        "width_mm": 1.5,
        "height_mm": 1.5,
    }


def test_native_outline_validation_ignores_keepout_window_dimensions():
    constraints = [
        {"kind": "placement", "target": "board", "width_mm": 180, "height_mm": 130},
        *[_keepout(index) for index in range(16)],
    ]
    _validate_design_outline_constraints(constraints, {"width": 180.0, "height": 130.0})


def test_keepout_kind_is_not_reinterpreted_as_an_outline_even_with_board_target():
    constraints = [
        _keepout(0),
        {"kind": "keepout", "target": "board", "board_width_mm": 1.5, "board_height_mm": 1.5},
    ]
    _validate_design_outline_constraints(constraints, {"width": 180.0, "height": 130.0})


def test_mismatched_actual_outline_still_fails_closed():
    with pytest.raises(ValueError, match="native Edge.Cuts outline"):
        _validate_design_outline_constraints(
            [{"kind": "placement", "target": "outline", "width_mm": 1.5, "height_mm": 130}],
            {"width": 180.0, "height": 130.0},
        )


def test_supported_legacy_and_nested_outline_aliases_are_checked():
    _validate_design_outline_constraints(
        [
            {"target": "pcb", "width_mm": 180, "height_mm": 130},
            {"type": "placement", "ref": "pcb", "board_width_mm": 180, "board_height_mm": 130},
            {"kind": "placement", "board": {"width_mm": 180, "height_mm": 130}},
        ],
        {"width": 180.0, "height": 130.0},
    )
    with pytest.raises(ValueError, match="native Edge.Cuts outline"):
        _validate_design_outline_constraints(
            [{"kind": "placement", "board": {"width_mm": 1.5, "height_mm": 130}}],
            {"width": 180.0, "height": 130.0},
        )


def test_component_placement_dimensions_are_not_outline_declarations():
    _validate_design_outline_constraints(
        [{"kind": "placement", "target": "J1", "width_mm": 7, "height_mm": 5}],
        {"width": 180.0, "height": 130.0},
    )


def _patch_design_intent_dependencies(monkeypatch, components):
    import circuit_weaver.design_loader as design_loader
    import circuit_weaver.placement_pipeline as placement_pipeline

    monkeypatch.setattr(
        design_loader,
        "compile_design_ir",
        lambda spec, enrich_parts=False: SimpleNamespace(
            components=components,
            ir=SimpleNamespace(pcb_constraints=spec["pcb_constraints"]),
        ),
    )
    monkeypatch.setattr(
        placement_pipeline,
        "build_placement_inventory",
        lambda values: SimpleNamespace(components=values),
    )


def test_design_intent_handoff_accepts_outline_with_board_targeted_keepouts(monkeypatch):
    component = SimpleNamespace(
        source_ref="R1", footprint="Device:R", power_pins={}, pin_nets={}
    )
    _patch_design_intent_dependencies(monkeypatch, [component])
    constraints = [
        {"kind": "placement", "target": "board", "width_mm": 180, "height_mm": 130},
        *[_keepout(index) for index in range(16)],
    ]

    _, returned_constraints = pcb_layout._design_intent(
        {"pcb_constraints": constraints},
        [{"ref": "R1", "footprint": "Device:R", "pads": []}],
        {"x": 0.0, "y": 0.0, "width": 180.0, "height": 130.0},
    )

    assert returned_constraints == constraints


def test_design_intent_handoff_still_rejects_real_outline_mismatch(monkeypatch):
    component = SimpleNamespace(
        source_ref="R1", footprint="Device:R", power_pins={}, pin_nets={}
    )
    _patch_design_intent_dependencies(monkeypatch, [component])
    with pytest.raises(ValueError, match="native Edge.Cuts outline"):
        pcb_layout._design_intent(
            {"pcb_constraints": [{"kind": "placement", "target": "outline",
                                  "width_mm": 1.5, "height_mm": 130}]},
            [{"ref": "R1", "footprint": "Device:R", "pads": []}],
            {"x": 0.0, "y": 0.0, "width": 180.0, "height": 130.0},
        )
