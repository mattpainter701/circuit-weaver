from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import circuit_weaver.placement_pipeline as placement_pipeline
from circuit_weaver.component_db import BypassCap, ComponentDef, PinDef, StrapConfig
from circuit_weaver.placement_optimizer import PlacementConfig
from circuit_weaver.placement_pipeline import (
    PLACEMENT_CONTEXT_FILENAME,
    PLACEMENT_HTML_FILENAME,
    PLACEMENT_RESULT_FILENAME,
    PLACEMENT_SVG_FILENAME,
    build_placement_inventory,
    generate_placement_review,
)


def _controller(*, source_ref: str = "") -> ComponentDef:
    return ComponentDef(
        mpn="CTRL-1",
        source_ref=source_ref,
        ref_prefix="U",
        value="Controller",
        footprint="Package_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.8x1.8mm",
        category="mcu",
        pins=[
            PinDef("1", "VDD", "power_in", "L"),
            PinDef("2", "GND", "power_in", "L"),
            PinDef("3", "DATA", "bidirectional", "R"),
        ],
        pin_nets={"3": "DATA_BUS"},
        power_pins={"1": "VDD_3P3", "2": "GND"},
        bypass_caps=[
            BypassCap(
                "1",
                "VDD_3P3",
                "GND",
                "100nF",
                "Capacitor_SMD:C_0402_1005Metric",
                role="decoupling",
            )
        ],
        straps=[
            StrapConfig(
                "3",
                "BOOT_MODE",
                "GND",
                "10k",
                "Resistor_SMD:R_0402_1005Metric",
                role="boot_strap",
            )
        ],
        functional_section="control",
        block_id="main_controller",
    )


def test_inventory_is_exhaustive_and_preserves_primary_connectivity() -> None:
    source = _controller()

    inventory = build_placement_inventory([source], include_auto_bypass=False)

    assert inventory.references == ["U1", "C1", "R1"]
    assert [component.source_ref for component in inventory.components] == inventory.references
    primary, bypass, strap = inventory.components
    assert source.source_ref == ""  # Manifest/reference allocation does not mutate the caller.
    assert primary.pin_nets == {"3": "DATA_BUS"}
    assert primary.power_pins == {"1": "VDD_3P3", "2": "GND"}
    assert primary.bypass_caps == []
    assert primary.straps == []

    assert bypass.value == "100nF"
    assert bypass.footprint == "Capacitor_SMD:C_0402_1005Metric"
    assert bypass.pin_nets == {"1": "VDD_3P3", "2": "GND"}
    assert bypass.functional_section == "control"
    assert bypass.block_id == "main_controller"
    assert bypass.placement_parent_ref == "U1"
    assert bypass.placement_parent_pin == "1"
    assert bypass.placement_role == "decoupling"
    assert "decoupling support" in bypass.description

    assert strap.value == "10k"
    assert strap.pin_nets == {"1": "BOOT_MODE", "2": "GND"}
    assert strap.placement_parent_ref == "U1"
    assert strap.placement_parent_pin == "3"
    assert strap.placement_role == "boot_strap"
    assert "boot strap support" in strap.description
    assert not strap.bypass_caps
    assert not strap.straps


def test_flattened_strap_does_not_invent_owner_connection_to_rail() -> None:
    from circuit_weaver.pcb_export import _build_net_component_map

    owner = ComponentDef(
        mpn="LOGIC",
        source_ref="U1",
        pins=[PinDef("1", "MODE", "input", "L")],
        pin_nets={"1": "MODE"},
        straps=[StrapConfig("1", "MODE", "GND", "10k", "Resistor_SMD:R_0402")],
    )
    inventory = build_placement_inventory([owner], include_auto_bypass=False)

    net_map = _build_net_component_map(inventory.components)

    assert net_map["MODE"] == ["R1", "U1"]
    assert net_map["GND"] == ["R1"]


def test_generate_review_writes_exact_review_only_artifact_set(tmp_path) -> None:
    result = generate_placement_review(
        [_controller(source_ref="U3")],
        tmp_path,
        project_name="control-board",
        config=PlacementConfig(iterations=4, seed=7),
        include_auto_bypass=False,
    )

    expected_names = {
        PLACEMENT_RESULT_FILENAME,
        PLACEMENT_CONTEXT_FILENAME,
        PLACEMENT_SVG_FILENAME,
        PLACEMENT_HTML_FILENAME,
    }
    assert {path.name for path in tmp_path.iterdir()} == expected_names
    assert result["status"] == "review_required"
    assert result["review_required"] is True
    assert result["fabrication_ready"] is False
    assert result["generated_artifact_count"] == 4
    assert result["assembly_item_count"] == 3
    assert result["placement_component_count"] == 3
    assert result["reference_reconciliation"] == {
        "exact_match": True,
        "manifest_refs": ["C1", "R1", "U3"],
        "placement_refs": ["C1", "R1", "U3"],
        "missing_from_placement": [],
        "unexpected_in_placement": [],
    }
    assert result["review_gate"]["status"] == "blocked"
    blockers = result["review_gate"]["blockers"]
    assert {blocker["target"] for blocker in blockers} == {"C1", "R1", "board"}
    support_reasons = [
        blocker["reason"] for blocker in blockers if blocker["kind"] == "sourcing_metadata"
    ]
    assert all("assign a manufacturer" in reason for reason in support_reasons)
    assert set(result["artifact_paths"]) == {
        "placement_result",
        "placement_context",
        "placement_svg",
        "placement_html",
    }
    assert set(result["generated_artifact_paths"]) == set(result["artifact_paths"])

    persisted = json.loads((tmp_path / PLACEMENT_RESULT_FILENAME).read_text(encoding="utf-8"))
    assert persisted["status"] == "review_required"
    assert persisted["optimizer"]["iterations"] == 4
    assert persisted["optimizer"]["quality"]["review_required"] is True
    assert set(persisted["placements"]) == {"U3", "C1", "R1"}
    assert all(artifact["fabrication_ready"] is False for artifact in persisted["artifacts"])

    context = json.loads((tmp_path / PLACEMENT_CONTEXT_FILENAME).read_text(encoding="utf-8"))
    assert {component["ref"] for component in context["components"]} == {"U3", "C1", "R1"}
    assert context["artifact_kind"] == "placement_review_context"
    assert "Heuristic review aid only" in context["authority"]

    svg = (tmp_path / PLACEMENT_SVG_FILENAME).read_text(encoding="utf-8")
    html = (tmp_path / PLACEMENT_HTML_FILENAME).read_text(encoding="utf-8")
    for reference in ("U3", "C1", "R1"):
        assert f'data-ref="{reference}"' in svg
        assert f'data-ref="{reference}"' in html
    assert "Official examples" in html
    assert "Review blockers (3)" in html


def test_pipeline_uses_constraint_board_size_and_visualizes_keepout(tmp_path) -> None:
    result = generate_placement_review(
        [_controller(source_ref="U3")],
        tmp_path,
        project_name="constrained-board",
        config=PlacementConfig(iterations=600, seed=11),
        include_auto_bypass=False,
        constraints=[
            {
                "kind": "placement",
                "target": "board",
                "board_width_mm": 48,
                "board_height_mm": 32,
            },
            {"kind": "placement", "target": "U3", "x_mm": 30, "y_mm": 18},
            {
                "kind": "keepout",
                "target": "antenna",
                "x_mm": 0,
                "y_mm": 0,
                "width_mm": 8,
                "height_mm": 8,
            },
        ],
    )

    assert result["board"] == {"width_mm": 48.0, "height_mm": 32.0}
    assert result["placements"]["U3"]["x"] == 30.0
    assert result["placements"]["U3"]["constraint_locked"] is True
    assert result["optimizer"]["constraint_evaluation"]["violations"] == []
    context = json.loads((tmp_path / PLACEMENT_CONTEXT_FILENAME).read_text(encoding="utf-8"))
    assert context["board"] == {"width_mm": 48.0, "height_mm": 32.0}
    html = (tmp_path / PLACEMENT_HTML_FILENAME).read_text(encoding="utf-8")
    assert "Board: 48 x 32 mm" in html
    assert 'data-keepout="antenna"' in html


def test_missing_footprint_remains_nonphysical_review_blocked_placeholder(tmp_path) -> None:
    placeholder = ComponentDef(
        mpn="RESISTOR-ARRAY",
        source_ref="RP1",
        ref_prefix="RP",
        value="4x10k",
        footprint="",
        category="buses",
        pins=[PinDef("1", "SDA", "passive", "L")],
        pin_nets={"1": "SDA"},
    )

    result = generate_placement_review(
        [placeholder],
        tmp_path,
        config=PlacementConfig(strategy="simple"),
        include_auto_bypass=False,
    )

    blockers = result["review_gate"]["blockers"]
    geometry = next(blocker for blocker in blockers if blocker["kind"] == "footprint_geometry")
    assert geometry["target"] == "RP1"
    assert "nonphysical placeholder" in geometry["reason"]
    assert result["placements"]["RP1"]["geometry_status"] == "review_blocked_placeholder"
    svg = (tmp_path / PLACEMENT_SVG_FILENAME).read_text(encoding="utf-8")
    assert 'data-category="placeholder"' in svg
    html = (tmp_path / PLACEMENT_HTML_FILENAME).read_text(encoding="utf-8")
    assert "review_blocked_placeholder" in html


def test_empty_inventory_writes_only_truthful_blocked_result(tmp_path) -> None:
    # A blocked resume must not leave visuals from an earlier successful run.
    for filename in (PLACEMENT_CONTEXT_FILENAME, PLACEMENT_SVG_FILENAME, PLACEMENT_HTML_FILENAME):
        (tmp_path / filename).write_text("stale", encoding="utf-8")

    result = generate_placement_review([], tmp_path, project_name="empty")

    assert result["status"] == "blocked"
    assert result["fabrication_ready"] is False
    assert result["assembly_item_count"] == 0
    assert result["placement_component_count"] == 0
    assert result["artifact_count"] == 1
    assert result["expected_artifact_count"] == 4
    assert result["generated_artifact_count"] == 1
    assert set(result["generated_artifact_paths"]) == {"placement_result"}
    assert {path.name for path in tmp_path.iterdir()} == {PLACEMENT_RESULT_FILENAME}

    persisted = json.loads((tmp_path / PLACEMENT_RESULT_FILENAME).read_text(encoding="utf-8"))
    assert persisted["status"] == "blocked"
    assert "at least one physical assembly item" in persisted["blocked_reasons"][0]


def test_reference_mismatch_blocks_visual_artifacts(tmp_path) -> None:
    optimizer_result = {
        "status": "ok",
        "placements": {"U3": {"x": 10, "y": 10, "rotation": 0, "layer": "front"}},
        "board_width_mm": 100,
        "board_height_mm": 80,
        "iterations": 1,
        "quality": {},
    }
    with patch("circuit_weaver.placement_pipeline.optimize_placement", return_value=optimizer_result):
        result = generate_placement_review(
            [_controller(source_ref="U3")],
            tmp_path,
            include_auto_bypass=False,
        )

    assert result["status"] == "blocked"
    assert result["reference_reconciliation"]["missing_from_placement"] == ["C1", "R1"]
    assert {path.name for path in tmp_path.iterdir()} == {PLACEMENT_RESULT_FILENAME}
    assert set(result["generated_artifact_paths"]) == {"placement_result"}


def _fixed_part(reference: str) -> ComponentDef:
    return ComponentDef(
        mpn=f"PART-{reference}",
        source_ref=reference,
        ref_prefix="U",
        value=f"Part {reference}",
        footprint="Package_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.8x1.8mm",
        category="mcu",
        pins=[PinDef("1", "IO", "bidirectional", "R")],
        pin_nets={"1": f"NET_{reference}"},
    )


def test_two_fixed_overlapping_components_block_review_artifact_readiness(tmp_path) -> None:
    result = generate_placement_review(
        [_fixed_part("U1"), _fixed_part("U2")],
        tmp_path,
        config=PlacementConfig(strategy="simple"),
        include_auto_bypass=False,
        constraints=[
            {
                "kind": "placement",
                "target": "board",
                "board_width_mm": 30,
                "board_height_mm": 20,
            },
            {"kind": "placement", "target": "U1", "x_mm": 10, "y_mm": 10},
            {"kind": "placement", "target": "U2", "x_mm": 10, "y_mm": 10},
        ],
    )

    assert result["status"] == "blocked"
    overlap = next(
        blocker
        for blocker in result["review_gate"]["blockers"]
        if blocker["kind"] == "placement_overlap"
    )
    assert overlap["refs"] == ["U1", "U2"]
    statuses = {row["kind"]: row["status"] for row in result["artifacts"]}
    assert statuses == {
        "placement_result": "ready",
        "placement_context": "review_blocked",
        "placement_svg": "review_blocked",
        "placement_html": "review_blocked",
    }
    result_row = next(row for row in result["artifacts"] if row["kind"] == "placement_result")
    assert "blocked-state commit manifest" in result_row["reason"]
    assert all((tmp_path / filename).is_file() for filename in (
        PLACEMENT_RESULT_FILENAME,
        PLACEMENT_CONTEXT_FILENAME,
        PLACEMENT_SVG_FILENAME,
        PLACEMENT_HTML_FILENAME,
    ))


def test_all_optimizer_quality_defects_become_explicit_blockers(tmp_path) -> None:
    owner = _fixed_part("U1")
    support = _fixed_part("U2")
    support.placement_parent_ref = "U404"
    optimizer_result = {
        "status": "ok",
        "placements": {
            "U1": {
                "x": 5,
                "y": 5,
                "rotation": 0,
                "layer": "front",
                "width_mm": 3,
                "height_mm": 3,
            },
            "U2": {
                "x": 5,
                "y": 5,
                "rotation": 0,
                "layer": "front",
                "width_mm": 3,
                "height_mm": 3,
            },
        },
        "board_width_mm": 30,
        "board_height_mm": 20,
        "iterations": 0,
        "quality": {
            "overlaps": [["U1", "U2"]],
            "outside_board": ["U1"],
            "missing_parents": ["U2"],
            "support_body_violations": [
                {
                    "support_ref": "U2",
                    "parent_ref": "U1",
                    "clearance_mm": 0.0,
                    "required_mm": 1.5,
                }
            ],
        },
        "constraint_evaluation": {
            "board_dimension_source": "constraints",
            "unsupported": [],
            "violations": [
                {
                    "kind": "keepout",
                    "target": "U1",
                    "reason": "Footprint intersects keepout antenna.",
                }
            ],
        },
    }
    with patch("circuit_weaver.placement_pipeline.optimize_placement", return_value=optimizer_result):
        result = generate_placement_review(
            [owner, support],
            tmp_path,
            include_auto_bypass=False,
        )

    kinds = {blocker["kind"] for blocker in result["review_gate"]["blockers"]}
    assert {
        "placement_overlap",
        "placement_outside_board",
        "placement_support_parent_missing",
        "placement_support_clearance",
        "placement_constraint_violation",
    } <= kinds
    assert result["status"] == "blocked"
    assert not all(row["status"] == "ready" for row in result["artifacts"])


def test_mid_publish_failure_removes_stale_result_and_partial_new_set(tmp_path) -> None:
    owned = {
        PLACEMENT_RESULT_FILENAME,
        PLACEMENT_CONTEXT_FILENAME,
        PLACEMENT_SVG_FILENAME,
        PLACEMENT_HTML_FILENAME,
    }
    for filename in owned:
        (tmp_path / filename).write_text(f"stale:{filename}", encoding="utf-8")

    real_replace = placement_pipeline._replace_staged_file
    published: list[str] = []

    def fail_during_publish(source: Path, destination: Path) -> None:
        published.append(destination.name)
        if len(published) == 2:
            raise OSError("injected mid-publish failure")
        real_replace(source, destination)

    with patch(
        "circuit_weaver.placement_pipeline._replace_staged_file",
        side_effect=fail_during_publish,
    ):
        with pytest.raises(OSError, match="injected mid-publish"):
            generate_placement_review(
                [_fixed_part("U1")],
                tmp_path,
                include_auto_bypass=False,
                config=PlacementConfig(strategy="simple"),
            )

    assert published == [PLACEMENT_SVG_FILENAME, PLACEMENT_HTML_FILENAME]
    assert not any((tmp_path / filename).exists() for filename in owned)
    assert not list(tmp_path.parent.glob(f".{tmp_path.name}.placement-stage-*"))


def test_staging_write_failure_cannot_leave_prior_success_marker(tmp_path) -> None:
    owned = {
        PLACEMENT_RESULT_FILENAME,
        PLACEMENT_CONTEXT_FILENAME,
        PLACEMENT_SVG_FILENAME,
        PLACEMENT_HTML_FILENAME,
    }
    for filename in owned:
        (tmp_path / filename).write_text(f"stale:{filename}", encoding="utf-8")

    def fail_svg_write(*_args, output_path, **_kwargs):
        assert Path(output_path).parent != tmp_path
        assert not any((tmp_path / filename).exists() for filename in owned)
        raise OSError("injected staged write failure")

    with patch("circuit_weaver.placement_pipeline.export_placement_svg", side_effect=fail_svg_write):
        with pytest.raises(OSError, match="injected staged write"):
            generate_placement_review(
                [_fixed_part("U1")],
                tmp_path,
                include_auto_bypass=False,
                config=PlacementConfig(strategy="simple"),
            )

    assert not any((tmp_path / filename).exists() for filename in owned)


def test_output_lock_serializes_concurrent_generations_into_one_coherent_set(tmp_path) -> None:
    original_optimize = placement_pipeline.optimize_placement
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_guard = threading.Lock()
    call_count = 0

    def controlled_optimize(*args, **kwargs):
        nonlocal call_count
        with call_guard:
            call_count += 1
            index = call_count
        if index == 1:
            first_entered.set()
            assert release_first.wait(5)
        else:
            second_entered.set()
        return original_optimize(*args, **kwargs)

    errors: list[BaseException] = []

    def run(project: str) -> None:
        try:
            generate_placement_review(
                [_fixed_part("U1")],
                tmp_path,
                project_name=project,
                include_auto_bypass=False,
                config=PlacementConfig(strategy="simple"),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with patch("circuit_weaver.placement_pipeline.optimize_placement", side_effect=controlled_optimize):
        first = threading.Thread(target=run, args=("generation-a",))
        second = threading.Thread(target=run, args=("generation-b",))
        first.start()
        assert first_entered.wait(5)
        second.start()
        assert not second_entered.wait(0.2)
        release_first.set()
        first.join(5)
        second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert second_entered.is_set()
    result = json.loads((tmp_path / PLACEMENT_RESULT_FILENAME).read_text(encoding="utf-8"))
    assert result["project"] == "generation-b"
    assert "generation-b PCB Placement Review" in (
        tmp_path / PLACEMENT_SVG_FILENAME
    ).read_text(encoding="utf-8")
    assert "generation-b PCB Placement Review" in (
        tmp_path / PLACEMENT_HTML_FILENAME
    ).read_text(encoding="utf-8")
