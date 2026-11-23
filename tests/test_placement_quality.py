from __future__ import annotations

import copy

import pytest

from circuit_weaver.placement_quality import review_placement_inventory


def _inventory():
    return {
        "status": "ok", "rectangular_outline": True,
        "bounds": {"x": 10, "y": 20, "width": 100, "height": 80},
        "footprints": [{"ref": "U1", "x": 100, "y": 90, "rotation": 0,
                        "courtyard_width": 10, "courtyard_height": 10}],
    }


def test_scattered_corner_exposes_large_unused_rectangle():
    result = review_placement_inventory(_inventory())
    assert result["status"] == "review_required"
    assert result["largest_empty_rectangle"]["fraction_of_usable_grid"] > 0.7
    assert result["electrical_or_manufacturing_approval"] is False


def test_native_rotated_offcenter_envelope_and_input_are_preserved():
    inventory = _inventory()
    inventory["footprints"] = [{"ref": "J1", "x": 60, "y": 60, "rotation": 90,
                                "courtyard_width": 40, "courtyard_height": 10,
                                "courtyard_center_x": 20, "courtyard_center_y": 0}]
    original = copy.deepcopy(inventory)
    result = review_placement_inventory(inventory, cell_size_mm=10)
    assert result["footprint_centroid_mm"] == {"x": 60, "y": 40}
    assert result["occupied_grid_fraction"] > 0
    assert inventory == original


def test_authored_reservation_excludes_explained_space_only():
    inventory = _inventory()
    inventory["footprints"][0].update(x=105, y=60, courtyard_width=10, courtyard_height=80)
    before = review_placement_inventory(inventory)
    after = review_placement_inventory(inventory, reserved_regions=[
        {"name": "antenna reserve", "x_mm": 10, "y_mm": 20, "width_mm": 90, "height_mm": 80},
    ])
    assert before["status"] == "review_required"
    assert after["status"] == "verified"
    assert after["largest_empty_rectangle"] is None
    assert after["occupied_grid_fraction"] == 1


def test_offboard_envelope_cannot_hide_behind_full_occupancy():
    inventory = _inventory()
    inventory["footprints"] = [
        {"ref": "U1", "x": 60, "y": 60, "rotation": 0, "courtyard_width": 100, "courtyard_height": 80},
        {"ref": "U2", "x": 200, "y": 200, "rotation": 0, "courtyard_width": 10, "courtyard_height": 10},
    ]
    result = review_placement_inventory(inventory, max_empty_fraction=0)
    assert result["largest_empty_rectangle"] is None
    assert result["status"] == "review_required"
    assert result["overhanging_footprints"] == ["U2"]


@pytest.mark.parametrize("mutate", [
    lambda data: data.update(rectangular_outline=False),
    lambda data: data.update(footprints=[]),
    lambda data: data["bounds"].update(width=float("nan")),
    lambda data: data["footprints"][0].pop("rotation"),
    lambda data: data["footprints"].append(dict(data["footprints"][0])),
])
def test_incomplete_native_evidence_stays_unverified(mutate):
    inventory = _inventory()
    mutate(inventory)
    assert review_placement_inventory(inventory)["status"] == "unverified"


@pytest.mark.parametrize("options", [
    {"cell_size_mm": True}, {"cell_size_mm": 0}, {"cell_size_mm": 0.001},
    {"max_empty_fraction": float("inf")}, {"max_empty_fraction": 1.1},
    {"reserved_regions": [{"name": "outside", "x_mm": 0, "y_mm": 20, "width_mm": 5, "height_mm": 5}]},
])
def test_invalid_or_unbounded_requests_are_rejected(options):
    with pytest.raises(ValueError):
        review_placement_inventory(_inventory(), **options)
