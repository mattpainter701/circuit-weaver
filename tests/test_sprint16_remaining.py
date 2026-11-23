"""Tests for Sprint 16 P1/P2: SI constraints, thermal analysis, dual-sided CPL, panelization."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from circuit_weaver.component_db import ComponentDef, PinDef


def _make_comp(ref, mpn="", category="digital", footprint="QFN-32", description="", pin_nets=None):
    return ComponentDef(
        mpn=mpn or f"IC_{ref}",
        description=description or f"Test {ref}",
        footprint=footprint,
        category=category,
        pins=[PinDef(number="1", name="VCC", electrical_type="power_in", side="L")],
        source_ref=ref,
        pin_nets=pin_nets or {},
    )


# --- SI constraints ---


def test_si_no_high_speed_buses():
    from circuit_weaver.si_constraints import analyze_si_constraints

    result = analyze_si_constraints([_make_comp("U1"), _make_comp("R1", category="passive", footprint="0402")])
    assert result["status"] == "ok"
    assert len(result["buses_detected"]) == 0
    assert "No high-speed" in result["summary"]


def test_si_detects_usb_from_nets():
    from circuit_weaver.si_constraints import analyze_si_constraints

    result = analyze_si_constraints([_make_comp("U1", pin_nets={"1": "USB_DP", "2": "USB_DM", "3": "VBUS"})])
    assert any(b["bus_type"] == "usb2" for b in result["buses_detected"])
    assert result["impedance_constraints"][0]["target_ohms"] == 90


def test_si_generic_ddr_nets_leave_generation_unresolved():
    from circuit_weaver.si_constraints import analyze_si_constraints

    result = analyze_si_constraints([_make_comp("U1", pin_nets={"1": "DDR_DQ0", "2": "DDR_DQ1", "3": "DDR_DQS0"})])
    assert [(bus["bus_type"], bus["generation_status"]) for bus in result["buses_detected"]] == [
        ("ddr_unknown", "unknown")
    ]
    assert result["impedance_constraints"] == []
    assert result["unverified_constraints"][0]["nets"] == ["DDR_DQ0", "DDR_DQ1", "DDR_DQS0"]


def test_si_detects_ethernet_from_description():
    from circuit_weaver.si_constraints import analyze_si_constraints

    result = analyze_si_constraints([_make_comp("U1", description="Gigabit Ethernet PHY")])
    assert any(b["bus_type"] == "ethernet_1g" for b in result["buses_detected"])


def test_si_diff_pair_detection():
    from circuit_weaver.si_constraints import analyze_si_constraints

    result = analyze_si_constraints([_make_comp("U1", pin_nets={"1": "USB_D+", "2": "USB_D-", "3": "GND"})])
    assert len(result["diff_pairs"]) >= 1


def test_si_ddr_matching_requires_device_and_lane_evidence():
    from circuit_weaver.si_constraints import analyze_si_constraints

    result = analyze_si_constraints(
        [_make_comp("U1", pin_nets={"1": "DDR_DQ0", "2": "DDR_DQ1", "3": "DDR_DQ2", "4": "DDR_DQ3"})]
    )
    assert result["length_groups"] == []
    unresolved = result["unverified_constraints"][0]
    assert unresolved["status"] == "unknown"
    assert unresolved["nets"] == ["DDR_DQ0", "DDR_DQ1", "DDR_DQ2", "DDR_DQ3"]
    assert unresolved["generated_hard_constraints"] == []
    assert "signal-role and byte-lane grouping evidence" in unresolved["required_inputs"]


def test_si_routing_rules_usb():
    from circuit_weaver.si_constraints import analyze_si_constraints

    result = analyze_si_constraints([_make_comp("U1", pin_nets={"1": "USB_DP", "2": "USB_DM"})])
    assert any(
        "diff pair" in r["description"].lower() or "spacing" in r["description"].lower()
        for r in result["routing_rules"]
    )


def test_si_can_bus():
    from circuit_weaver.si_constraints import analyze_si_constraints

    result = analyze_si_constraints([_make_comp("U1", pin_nets={"1": "CANH", "2": "CANL"})])
    assert any(b["bus_type"] == "can" for b in result["buses_detected"])
    assert any(ic["target_ohms"] == 120 for ic in result["impedance_constraints"])


# --- Thermal analysis ---


def test_thermal_no_power_components():
    from circuit_weaver.thermal_analysis import analyze_thermal

    result = analyze_thermal([_make_comp("R1", category="passive", footprint="0402")])
    assert result["status"] == "ok"
    assert result["total_power_w"] == 0


def test_thermal_with_spec_data(tmp_path):
    from circuit_weaver.thermal_analysis import analyze_thermal

    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "metadata.json").write_text(
        json.dumps({"IC_U1": {"theta_ja": 45.0, "pdiss_max_w": 2.0, "tj_max": 150.0}}), encoding="utf-8"
    )
    (specs / "ic_thermal.json").write_text("{}", encoding="utf-8")
    result = analyze_thermal([_make_comp("U1", category="power")], specs_dir=str(specs))
    assert result["total_power_w"] == pytest.approx(2.0)
    assert result["components"][0]["tj_calculated"] == pytest.approx(115.0)
    assert result["components"][0]["status"] == "ok"


def test_thermal_critical_detection(tmp_path):
    from circuit_weaver.thermal_analysis import analyze_thermal

    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "metadata.json").write_text(
        json.dumps({"IC_U1": {"theta_ja": 100.0, "pdiss_max_w": 1.5, "tj_max": 125.0}}), encoding="utf-8"
    )
    (specs / "ic_thermal.json").write_text("{}", encoding="utf-8")
    result = analyze_thermal([_make_comp("U1", category="power")], specs_dir=str(specs))
    assert result["components"][0]["status"] == "critical"
    assert len(result["hotspots"]) == 1


def test_thermal_proximity_warning(tmp_path):
    from circuit_weaver.thermal_analysis import analyze_thermal

    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "metadata.json").write_text(
        json.dumps(
            {
                "IC_U1": {"theta_ja": 40, "pdiss_max_w": 1.0, "tj_max": 150},
                "IC_U2": {"theta_ja": 40, "pdiss_max_w": 1.0, "tj_max": 150},
            }
        ),
        encoding="utf-8",
    )
    (specs / "ic_thermal.json").write_text("{}", encoding="utf-8")
    result = analyze_thermal(
        [_make_comp("U1", category="power"), _make_comp("U2", category="power")],
        {"U1": {"x": 50, "y": 40}, "U2": {"x": 55, "y": 42}},
        specs_dir=str(specs),
    )
    assert len(result["proximity_warnings"]) >= 1


def test_thermal_heatmap_svg(tmp_path):
    from circuit_weaver.thermal_analysis import generate_heatmap_svg

    svg = generate_heatmap_svg([_make_comp("U1")], {"U1": {"x": 50, "y": 40}}, output_path=tmp_path / "hm.svg")
    assert "<svg" in svg
    assert (tmp_path / "hm.svg").exists()


def test_thermal_heatmap_with_data(tmp_path):
    from circuit_weaver.thermal_analysis import generate_heatmap_svg

    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "metadata.json").write_text(
        json.dumps({"IC_U1": {"theta_ja": 50, "pdiss_max_w": 1.5, "tj_max": 150}}), encoding="utf-8"
    )
    (specs / "ic_thermal.json").write_text("{}", encoding="utf-8")
    svg = generate_heatmap_svg([_make_comp("U1", category="power")], {"U1": {"x": 50, "y": 40}}, specs_dir=str(specs))
    assert "U1" in svg
    assert "radialGradient" in svg


# --- Dual-sided CPL ---


def test_dual_cpl_all_top(tmp_path):
    from circuit_weaver.jlcpcb_export import write_dual_sided_cpl

    result = write_dual_sided_cpl(
        [_make_comp("U1"), _make_comp("C1", category="passive", footprint="0402")],
        {"U1": (50, 40, 0, "top"), "C1": (55, 42, 90, "top")},
        tmp_path,
    )
    assert result["top_count"] == 2
    assert result["bottom_count"] == 0


def test_dual_cpl_split_layers(tmp_path):
    from circuit_weaver.jlcpcb_export import write_dual_sided_cpl

    result = write_dual_sided_cpl(
        [_make_comp("U1"), _make_comp("C1", category="passive")],
        {"U1": (50, 40, 0, "top"), "C1": (55, 42, 90, "bottom")},
        tmp_path,
    )
    assert result["top_count"] == 1
    assert result["bottom_count"] == 1
    assert "U1" in (tmp_path / "cpl_top.csv").read_text(encoding="utf-8")
    assert "C1" in (tmp_path / "cpl_bottom.csv").read_text(encoding="utf-8")


def test_dual_cpl_bottom_warnings(tmp_path):
    from circuit_weaver.jlcpcb_export import write_dual_sided_cpl

    result = write_dual_sided_cpl(
        [_make_comp("U1", footprint="QFN-48"), _make_comp("J1", category="connector", footprint="USB-C-THT")],
        {"U1": (50, 40, 0, "bottom"), "J1": (10, 40, 0, "bottom")},
        tmp_path,
    )
    assert len(result["warnings"]) >= 1


def test_dual_cpl_simultaneous_warning(tmp_path):
    from circuit_weaver.jlcpcb_export import write_dual_sided_cpl

    result = write_dual_sided_cpl(
        [_make_comp("U1")], {"U1": (50, 40, 0, "top")}, tmp_path, assembly_mode="dual-sided-simultaneous"
    )
    assert any("simultaneous" in w.lower() or "Simultaneous" in w for w in result["warnings"])


# --- Panelization ---


def test_panelize_basic():
    from circuit_weaver.panelizer import suggest_panel

    result = suggest_panel(50, 40, qty=100)
    assert result["status"] == "ok"
    assert len(result["panel_options"]) >= 1


def test_panelize_small_board():
    from circuit_weaver.panelizer import suggest_panel

    best = suggest_panel(20, 15, qty=50)["panel_options"][0]
    assert best["boards_per_panel"] >= 4


def test_panelize_large_board():
    from circuit_weaver.panelizer import suggest_panel

    best = suggest_panel(95, 95, qty=10)["panel_options"][0]
    assert best["boards_per_panel"] == 1


def test_panelize_breakaway_positions():
    from circuit_weaver.panelizer import suggest_panel

    best = suggest_panel(30, 25, qty=20)["panel_options"][0]
    if best["cols"] > 1:
        assert len(best["breakaway_positions"]["x_lines_mm"]) == best["cols"] - 1


def test_panelize_cost_estimate():
    from circuit_weaver.panelizer import suggest_panel

    ce = suggest_panel(30, 25, qty=100)["cost_estimate"]
    assert ce["panelized"]["per_board"] > 0
    assert ce["savings_pct"] >= 0


def test_panelize_design_rules():
    from circuit_weaver.panelizer import suggest_panel

    assert len(suggest_panel(30, 25)["design_rules"]) >= 3


def test_panelize_mouse_bite():
    from circuit_weaver.panelizer import PanelConfig, suggest_panel

    result = suggest_panel(30, 25, config=PanelConfig(breakaway_type="mouse-bite"))
    assert any("mouse-bite" in r.lower() or "Mouse-bite" in r for r in result["design_rules"])


def test_panelize_tiny_board_warning():
    from circuit_weaver.panelizer import suggest_panel

    assert any("minimum" in w.lower() for w in suggest_panel(4, 3, qty=50)["warnings"])


# --- CLI help ---


def test_cli_si_constraints_help():
    r = subprocess.run(
        [sys.executable, "-m", "circuit_weaver.dispatcher", "si-constraints", "--help"], capture_output=True, text=True
    )
    assert r.returncode == 0


def test_cli_thermal_analysis_help():
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "circuit_weaver.dispatcher",
            "thermal-analysis",
            "--help",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0


def test_cli_dual_cpl_help():
    r = subprocess.run(
        [sys.executable, "-m", "circuit_weaver.dispatcher", "export-dual-cpl", "--help"], capture_output=True, text=True
    )
    assert r.returncode == 0


def test_cli_panelize_help():
    r = subprocess.run(
        [sys.executable, "-m", "circuit_weaver.dispatcher", "panelize", "--help"], capture_output=True, text=True
    )
    assert r.returncode == 0
