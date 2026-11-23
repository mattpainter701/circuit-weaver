from __future__ import annotations

import importlib.util
from pathlib import Path

from circuit_weaver import placement_optimizer as optimizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
workflow_spec = importlib.util.spec_from_file_location(
    "candidate_validate_app_workflow", PROJECT_ROOT / "scripts" / "validate_app_workflow.py"
)
workflow = importlib.util.module_from_spec(workflow_spec)
workflow_spec.loader.exec_module(workflow)


def _window(index: int) -> dict:
    return {
        "kind": "keepout",
        "target": "board",
        "id": f"window-{index}",
        "x_mm": 4 + (index % 4) * 20,
        "y_mm": 6 + (index // 4) * 24,
        "width_mm": 12,
        "height_mm": 9,
    }


def _constraints() -> list[dict]:
    return [
        {"target": "outline", "width_mm": 180, "height_mm": 130},
        *[_window(i) for i in range(16)],
    ]


def test_runner_and_optimizer_ignore_all_keepout_dimensions_for_outline():
    constraints = _constraints()
    design = {"pcb_constraints": constraints}
    assert workflow._design_board_size(design) == (180.0, 130.0)

    config, plan = optimizer._build_constraint_plan(
        [], optimizer.PlacementConfig(board_width_mm=100, board_height_mm=80), constraints
    )
    assert (config.board_width_mm, config.board_height_mm) == (180.0, 130.0)
    assert len(plan.keepouts) == 16
    assert sum(row["kind"] == "keepout" for row in plan.applied) == 16
    assert not plan.deferred
    assert all(row["width_mm"] == 12.0 and row["height_mm"] == 9.0 for row in plan.keepouts)


def test_size_overrides_do_not_mutate_keepout_rectangles_and_keep_aliases():
    constraints = [
        {"kind": "placement", "target": "board", "width_mm": 180, "height_mm": 130},
        {"type": "placement", "ref": "pcb", "board_width_mm": 180, "board_height_mm": 130},
        {"kind": "placement", "board": {"width_mm": 180, "height_mm": 130}},
        *[_window(i) for i in range(16)],
        {"kind": "placement", "target": "J1", "width_mm": 7, "height_mm": 5},
    ]
    design = {"pcb_constraints": constraints}
    original_keepouts = [dict(item) for item in constraints if item.get("kind") == "keepout"]
    assert workflow._design_board_size(design) == (180.0, 130.0)

    workflow._apply_size_overrides(design, 200.0, 140.0)
    assert all(
        item == original for item, original in zip(
            [item for item in design["pcb_constraints"] if item.get("kind") == "keepout"],
            original_keepouts,
            strict=True,
        )
    )
    assert workflow._design_board_size(design) == (200.0, 140.0)
    assert design["pcb_constraints"][0]["width_mm"] == 200.0
    assert design["pcb_constraints"][1]["board_width_mm"] == 200.0
    assert design["pcb_constraints"][2]["board"]["width_mm"] == 200.0
    assert design["pcb_constraints"][-2]["width_mm"] == 7
    assert design["pcb_constraints"][-1] == {
        "kind": "placement", "target": "board", "width_mm": 200.0, "height_mm": 140.0,
    }


def test_original_size_resolution_ignores_keepouts_and_preserves_supported_aliases():
    design = {
        "pcb_constraints": [
            _window(0),
            {"kind": "placement", "target": "pcb", "board_width_mm": 182, "board_height_mm": 132},
            _window(1),
            {"kind": "placement", "board": {"width_mm": 184, "height_mm": 134}},
            _window(2),
        ]
    }
    assert workflow._design_board_size(design) == (184.0, 134.0)
