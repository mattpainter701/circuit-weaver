from __future__ import annotations

import logging
from pathlib import Path

import yaml

from circuit_weaver.generator import (
    _candidate_contacts_foreign_net,
    _orthogonal_grid_route,
    _polyline_is_clear,
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
