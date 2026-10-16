from types import SimpleNamespace

import pytest

from circuit_weaver.component_db import ComponentDef
from circuit_weaver.pcb_layout import (
    _center_constraints,
    _design_intent,
    _layout_review,
    _native_origin,
    _rotated_offset,
)


@pytest.fixture
def intent(monkeypatch):
    owner = ComponentDef(mpn="chip", source_ref="U1", footprint="Package:IC", category="power",
                         pin_nets={"1": "VIN", "2": "GND"}, block_id="supply")
    support = ComponentDef(mpn="cap", source_ref="C1", footprint="Capacitor:C_0402", category="passive",
                           pin_nets={"1": "VIN", "2": "GND"})
    support.placement_parent_ref = "U1"
    support.placement_role = "decoupling"
    constraints = [{"kind": "placement", "target": "board", "width_mm": 36, "height_mm": 30}]
    monkeypatch.setattr("circuit_weaver.design_loader.compile_design_ir", lambda *a, **k: SimpleNamespace(
        components=[owner, support], ir=SimpleNamespace(pcb_constraints=constraints)))
    monkeypatch.setattr("circuit_weaver.placement_pipeline.build_placement_inventory",
                        lambda components: SimpleNamespace(components=components))
    footprints = [
        {"ref": "U1", "footprint": "IC", "pads": [{"number": "1", "net": "VIN"}, {"number": "2", "net": "GND"}]},
        {"ref": "C1", "footprint": "C_0402", "pads": [{"number": "1", "net": "GND"}, {"number": "2", "net": "VIN"}]},
    ]
    return footprints, {"width": 36.05, "height": 30.05}, owner, support


def test_design_intent_binds_native_inventory_and_support_endpoints(intent):
    footprints, bounds, owner, support = intent
    by_ref, constraints = _design_intent({}, footprints, bounds)
    assert by_ref == {"U1": owner, "C1": support}
    assert by_ref["C1"].placement_parent_ref == "U1"
    assert by_ref["U1"].block_id == "supply"
    assert constraints[0]["width_mm"] == 36


@pytest.mark.parametrize("mismatch", ["reference", "footprint", "primary_net", "support_net", "board_size"])
def test_stale_design_intent_is_rejected_before_optimization(intent, mismatch):
    footprints, bounds, _owner, _support = intent
    if mismatch == "reference":
        footprints[0]["ref"] = "U2"
    elif mismatch == "footprint":
        footprints[0]["footprint"] = "Other_IC"
    elif mismatch == "primary_net":
        footprints[0]["pads"][0]["net"] = "WRONG"
    elif mismatch == "support_net":
        footprints[1]["pads"][0]["net"] = "WRONG"
    else:
        bounds["width"] = 80
    with pytest.raises(ValueError, match="match"):
        _design_intent({}, footprints, bounds)


def test_layout_review_exposes_missing_connector_access_and_excess_whitespace():
    component = ComponentDef(mpn="usb", source_ref="J1", category="connector")
    component.placement_width_mm = 8
    component.placement_height_mm = 8
    placements = {"J1": {"x": 40, "y": 30, "rotation": 0}}
    bounds = {"width": 80, "height": 60}
    review = _layout_review(placements, [component], [], bounds)
    assert review["status"] == "review_required"
    assert review["connectors"][0]["edge_and_orientation_specified"] is False
    assert len(review["warnings"]) == 2
    assert review["fabrication_ready"] is False
    explicit = _layout_review(placements, [component], [{"target": "J1", "edge": "bottom", "rotation": 0}],
                              {"width": 20, "height": 20})
    assert explicit["status"] == "intent_specified"
    assert explicit["connectors"][0]["edge_and_orientation_specified"] is True


def test_off_center_courtyard_constraints_preserve_native_origin_and_rotation():
    item = {"ref": "J1", "x": 100, "y": 200, "rotation": 90, "locked": True,
            "courtyard_center_x": 3, "courtyard_center_y": 4}
    rule = {"kind": "placement", "target": "J1", "position": {"x": 10, "y": 20}, "rotation": 90}
    centered = _center_constraints([rule], [item], {"x": 90, "y": 180})
    assert centered[0]["x_mm"] == pytest.approx(14)
    assert centered[0]["y_mm"] == pytest.approx(17)
    assert rule["position"] == {"x": 10, "y": 20}
    assert "x_mm" not in rule
    dx, dy = _rotated_offset(3, 4, 90)
    assert centered[0]["x_mm"] + 90 - dx == pytest.approx(100)
    assert centered[0]["y_mm"] + 180 - dy == pytest.approx(200)
    with pytest.raises(ValueError, match="locked"):
        _center_constraints([{**rule, "rotation": 180}], [item], {"x": 90, "y": 180})


def test_movable_native_origins_survive_dsn_coordinate_resolution_and_fixed_origins_are_preserved():
    placement = {"x": 14.853368, "y": 23.845, "rotation": 0, "constraint_locked": False}
    assert _native_origin(placement, (2.975, 4.025), {"x": 0, "y": 0}) == pytest.approx((11.8784, 19.82))
    x, y = _native_origin({**placement, "constraint_locked": True}, (2.975, 4.025), {"x": 0, "y": 0})
    assert x == pytest.approx(11.878368, abs=1e-9)
    assert y == pytest.approx(19.82)


def test_native_placement_is_invariant_to_uuid_dependent_footprint_iteration(tmp_path, monkeypatch):
    from pathlib import Path

    from circuit_weaver.drc_runner import DrcResult
    from circuit_weaver.pcb_layout import place_pcb

    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"source board")
    footprints = [{"ref": ref, "footprint": "IC", "x": x, "y": 10, "rotation": 0,
                   "layer": "front", "locked": False, "width": 3, "height": 2,
                   "pads": [{"number": "1", "net": "SIGNAL", "local_x": 0, "local_y": 0}]}
                  for ref, x in (("U1", 8), ("U2", 22), ("U3", 28))]
    monkeypatch.setattr("circuit_weaver.pcb_layout.preflight_pcb", lambda _path: {"routable": True})

    def native(operation, **kwargs):
        if operation == "inspect":
            return {"status": "ok", "rectangular_outline": True, "track_count": 0, "zone_count": 0,
                    "bounds": {"x": 0, "y": 0, "width": 36, "height": 30}, "footprints": footprints}
        Path(kwargs["output"]).write_bytes(b"placed board")
        return {"status": "ok"}

    monkeypatch.setattr("circuit_weaver.pcb_layout.run_kicad", native)
    monkeypatch.setattr("circuit_weaver.pcb_layout.run_drc", lambda *a, **k: DrcResult(status="ok", board=str(a[0])))
    first = place_pcb(source, tmp_path / "first.kicad_pcb", iterations=40, seed=7)
    footprints.reverse()
    second = place_pcb(source, tmp_path / "second.kicad_pcb", iterations=40, seed=7)
    assert first["status"] == second["status"] == "ok"
    assert first["placement"]["placements"] == second["placement"]["placements"]
    assert source.read_bytes() == b"source board"
