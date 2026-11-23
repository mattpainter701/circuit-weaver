from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from circuit_weaver.generator import (
    _WIRE_PTS_RE,
    _candidate_contacts_foreign_net,
    _clear_stub_length,
    _marker_route_avoiding_foreign_nets,
    _orthogonal_grid_route,
    _polyline_is_clear,
    _route_local_connection,
    _segment_hits_box,
    generate_from_components,
    snap,
)
from circuit_weaver.project_spec import resolve_project_spec


def test_orthogonal_grid_route_respects_obstacles_and_foreign_nets():
    start = (snap(0.0), snap(0.0))
    end = (snap(10.0), snap(0.0))
    blockers = [(4.0, -1.0, 6.0, 1.0)]
    route_state = {"reserved_segments": [("OTHER", (2.0, -2.0, 2.0, 2.0))]}

    route = _orthogonal_grid_route(start, end, blockers, route_state, "TARGET")

    assert route is not None
    assert route[0] == start and route[-1] == end
    assert all(_polyline_is_clear(route, box) for box in blockers)
    assert not _candidate_contacts_foreign_net(route, route_state, "TARGET")


def test_local_router_uses_clear_grid_corridor_before_any_fallback():
    start = (snap(0.0), snap(0.0))
    end = (snap(10.0), snap(0.0))
    blockers = [(4.0, -1.0, 6.0, 1.0)]
    route_state = {"reserved_segments": [("OTHER", (2.0, -2.0, 2.0, 2.0))]}
    wires = []

    _route_local_connection(*start, *end, wires, "TARGET", route_state=route_state, obstacles=blockers)

    points = []
    for wire in wires:
        match = _WIRE_PTS_RE.search(wire)
        assert match is not None
        points.append(tuple(float(value) for value in match.groups()))
    assert points
    assert points[0][:2] == start
    assert points[-1][2:] == end
    assert all(not _segment_hits_box(*segment, box) for segment in points for box in blockers)
    route = [points[0][:2], *(segment[2:] for segment in points)]
    assert not _candidate_contacts_foreign_net(route, route_state, "TARGET")


def test_local_router_refuses_foreign_net_cage_without_emitting_a_crossing():
    wires = []
    sealed_net = {
        "reserved_segments": [
            ("OTHER", (-1.0, -1.0, 1.0, -1.0)),
            ("OTHER", (1.0, -1.0, 1.0, 1.0)),
            ("OTHER", (1.0, 1.0, -1.0, 1.0)),
            ("OTHER", (-1.0, 1.0, -1.0, -1.0)),
        ]
    }

    with pytest.raises(ValueError, match="without crossing a symbol body or contacting a reserved foreign net"):
        _route_local_connection(
            0.0,
            0.0,
            10.0,
            0.0,
            wires,
            "TARGET",
            route_state=sealed_net,
        )

    assert wires == []


def test_power_marker_stub_detours_around_adjacent_passive_body():
    start = (0.0, 2.54)
    neighboring_body = [(-1.905, -1.905, 1.905, 1.905)]
    desired = 6.35
    chosen_length = _clear_stub_length(*start, 270, desired, neighboring_body)

    route = _marker_route_avoiding_foreign_nets(
        "GND",
        *start,
        270,
        chosen_length,
        net_anchors=None,
        route_state={"reserved_segments": []},
        body_boxes=neighboring_body,
    )

    assert route[0] == start
    assert len(route) > 2
    assert all(_polyline_is_clear(route, box) for box in neighboring_body)


def test_rf_frontend_dense_decoupling_generates_net_label_fallback(tmp_path, caplog):
    repo = Path(__file__).resolve().parents[1]
    spec = yaml.safe_load((repo / "samples/rf_frontend/rf_frontend.yaml").read_text(encoding="utf-8"))
    components, metadata = resolve_project_spec(spec)

    with caplog.at_level(logging.WARNING, logger="circuit_weaver.generator"):
        generate_from_components(
            components,
            output_dir=str(tmp_path),
            project_name=metadata["project"],
            company=metadata.get("company", ""),
            validate=True,
            hierarchical=True,
            stable_uuids=True,
        )

    schematic = tmp_path / "RF_Frontend.kicad_sch"
    assert schematic.is_file()
    text = schematic.read_text(encoding="utf-8")
    assert '(global_label "DEC4_1P3"' in text
    assert "local route for DEC4_1P3 has no electrically clear corridor; using a net label" in caplog.text


def test_dac_dual_output_labels_remain_electrically_separate(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    spec = yaml.safe_load(
        (repo / "benchmarks/general_scale/cases/precision_control_daq.yaml").read_text(encoding="utf-8")
    )
    components, metadata = resolve_project_spec(spec)

    generate_from_components(
        components,
        output_dir=str(tmp_path),
        project_name=metadata["project"],
        company=metadata.get("company", ""),
        validate=False,
        readiness_gate=False,
        stable_uuids=True,
    )

    schematics = list(tmp_path.rglob("*.kicad_sch"))
    assert schematics
    text = "\n".join(path.read_text(encoding="utf-8") for path in schematics)
    assert "VOUTA_RAW_U15" in text
    assert "VOUTB_RAW_U15" in text
