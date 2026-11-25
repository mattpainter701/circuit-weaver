"""Exercise the optional installed Rust scorer against the Python placement objective."""

import random
from dataclasses import replace

import pytest

from circuit_weaver import placement_optimizer as opt
from circuit_weaver.component_db import ComponentDef, PinDef
from circuit_weaver.placement_scoring import select_scorer


def test_backend_selection(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "circuit_weaver_placement", None)
    monkeypatch.setenv("CIRCUIT_WEAVER_PLACEMENT_BACKEND", "python")
    assert select_scorer() is None
    assert select_scorer("auto") is None
    with pytest.raises(RuntimeError, match="wheel"):
        select_scorer("rust")
    with pytest.raises(ValueError, match="backend"):
        select_scorer("unknown")


@pytest.mark.skip_category("optional-tool")
@pytest.mark.parametrize("strategy", ["balanced", "cost", "si", "thermal"])
@pytest.mark.parametrize("pad_mode", [True, False])
def test_stateful_native_objective_tracks_arbitrary_candidates(strategy, pad_mode):
    pytest.importorskip("circuit_weaver_placement")
    Row = opt.ComponentPlacement
    pads = {"VCC": [(0.4, -0.3), (0, 1)], "GND": [(-0.5, 0)], "CLK": [(1.5, -0.5)]}
    rows = [
        Row(
            "U1",
            3,
            4,
            width=5,
            height=3,
            category="regulator",
            thermal_dissipation_w=0.8,
            pin_nets={"2": "VCC"},
            pin_offsets={"2": (0.4, -0.3)},
        ),
        Row("C1", 6, 4, parent_ref="U1", parent_pin="2", placement_role="decoupling"),
        Row("C2", 5, 5, parent_ref="U1", parent_pin="2", placement_role="decoupling"),
        Row("C3", 5, 3, parent_ref="U1", placement_role="output_cap"),
        Row("L1", 7, 4, parent_ref="U1", placement_role="inductor"),
        Row("U2", 8, 7, category="analog", thermal_dissipation_w=0.3),
        Row("J1", 20, 12, category="rf", layer="back"),
        Row("R1", 7, 8, parent_ref="absent"),
    ]
    rows = [
        replace(
            p,
            net_pad_offsets=pads if pad_mode else None,
            reference_size_mm=(2.3, 0.85),
            block_id="power" if i < 5 else "",
        )
        for i, p in enumerate(rows)
    ]
    config = opt.PlacementConfig(strategy=strategy, board_width_mm=22, board_height_mm=16)
    plan = opt._ConstraintPlan(
        keepouts=[{"x_mm": 4, "y_mm": 4, "width_mm": 1.3, "height_mm": 1.7}],
        edges={"J1": {"edge": "bottom"}},
        affinities=[{"target": "U1", "near": "U2", "max_distance_mm": 4}],
    )
    pairs = {("U1", "U2"): 2.5, ("J1", "U2"): 1.7}
    score = select_scorer("rust")

    def check(candidate):
        assert score(candidate, config, pairs, plan) == pytest.approx(
            opt._total_cost(candidate, config, pairs, plan), rel=3e-15, abs=1e-9
        )

    check(rows)
    rng = random.Random(192)
    for k in range(75):
        candidate = rows.copy()
        i = rng.randrange(len(rows))
        candidate[i] = replace(
            rows[i], x=rng.uniform(-1, 23), y=rng.uniform(-1, 17), rotation=rng.choice([0, 45, 90, 180, 270])
        )
        check(candidate)
        if k % 3:
            rows = candidate
        else:
            check(rows)  # Undo a rejected move, including the vacated label neighborhood.
    assert score.snapshots == 1
    assert score.pose_updates < score.evaluations * 2
    check(rows[::-1])
    assert score.snapshots == 2


@pytest.mark.skip_category("optional-tool")
def test_native_optimizer_preserves_selected_layout():
    pytest.importorskip("circuit_weaver_placement")
    parts = [
        ComponentDef(
            mpn=f"IC{i}",
            footprint="QFN-32",
            category="digital",
            source_ref=f"U{i}",
            pins=[PinDef(number="1", name="VCC", electrical_type="power_in", side="L")],
        )
        for i in range(1, 9)
    ]
    cfg = opt.PlacementConfig(iterations=120, seed=7, scoring_backend="python")
    baseline = opt.optimize_placement(parts, config=cfg)
    actual = opt.optimize_placement(parts, config=replace(cfg, scoring_backend="rust"))
    assert actual["search"].pop("scoring")["backend"] == "rust"
    assert baseline["search"].pop("scoring")["backend"] == "python"
    assert actual == baseline
