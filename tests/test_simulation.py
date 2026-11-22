"""Tests for the simulation orchestrator."""

from pathlib import Path
from unittest.mock import patch

from circuit_weaver.component_db import ComponentDef
from circuit_weaver.simulation import (
    DesignSimulationReport,
    SimulationPlan,
    _evaluate_simulation_targets,
    plan_simulations,
    run_design_simulations,
    score_simulation_confidence,
)
from circuit_weaver.spice_runner import SimulationResult


def _make_component(ref, mpn="", category="", description=""):
    comp = ComponentDef.__new__(ComponentDef)
    comp.source_ref = ref
    comp.mpn = mpn
    comp.category = category
    comp.description = description
    comp.value = ""
    comp.ref_prefix = ref[0] if ref else ""
    comp.pin_nets = {"1": "VIN", "2": "VOUT"}
    comp.source_mpn = ""
    comp.source_value = ""
    comp.source_description = ""
    comp.source_manufacturer = ""
    comp.footprint = ""
    comp.lcsc_pn = ""
    comp.digikey_pn = ""
    comp.pins = []
    comp.power_pins = {}
    comp.power_reqs = []
    comp.bypass_caps = []
    comp.straps = []
    return comp


class TestPlanSimulations:
    def test_detects_power_regulators(self):
        components = [
            _make_component("U1", mpn="TPS62300", category="buck", description="3.3V buck converter"),
            _make_component("R1", category="resistor", description="10k resistor"),
        ]
        plan = plan_simulations(components)
        assert len(plan.power_sims) >= 1
        assert plan.power_sims[0]["ref"] == "U1"

    def test_detects_ldo(self):
        components = [
            _make_component("U2", mpn="TLV75533", category="ldo", description="LDO regulator"),
        ]
        plan = plan_simulations(components)
        assert len(plan.power_sims) == 1
        assert len(plan.thermal_sims) == 1

    def test_detects_filters(self):
        components = [
            _make_component("U3", category="filter", description="LC filter"),
        ]
        plan = plan_simulations(components)
        assert len(plan.signal_sims) == 1
        assert plan.signal_sims[0]["category"] == "filter"

    def test_detects_opamps(self):
        components = [
            _make_component("U4", description="Precision op-amp"),
        ]
        plan = plan_simulations(components)
        assert len(plan.signal_sims) == 1
        assert plan.signal_sims[0]["category"] == "opamp"

    def test_empty_components(self):
        plan = plan_simulations([])
        assert plan.total == 0

    def test_passive_only_no_sims(self):
        components = [
            _make_component("R1", category="resistor", description="resistor"),
            _make_component("C1", category="capacitor", description="capacitor"),
        ]
        plan = plan_simulations(components)
        assert plan.total == 0


class TestScoreSimulationConfidence:
    def test_all_ok(self):
        results = [
            SimulationResult(
                status="ok", sim_type="tran", evidence_verified=True,
                metrics={"ripple_mv": 5}, target_checks={"ripple_mv": "passed"},
            ),
            SimulationResult(
                status="ok", sim_type="ac", evidence_verified=True,
                metrics={"bandwidth_hz": 1000}, target_checks={"bandwidth_hz": "passed"},
            ),
        ]
        score, grade = score_simulation_confidence(results)
        assert score == 100.0
        assert grade == "A"

    def test_all_skipped(self):
        results = [
            SimulationResult(status="skipped", sim_type="tran"),
        ]
        score, grade = score_simulation_confidence(results)
        assert score == 0.0
        assert grade == "N/A"

    def test_mixed_results(self):
        results = [
            SimulationResult(
                status="ok", sim_type="tran", evidence_verified=True,
                metrics={"ripple_mv": 5}, target_checks={"ripple_mv": "passed"},
            ),
            SimulationResult(status="failed", sim_type="ac"),
        ]
        score, grade = score_simulation_confidence(results)
        assert 0 < score < 100

    def test_process_success_without_verified_raw_metrics_is_not_confidence(self):
        score, grade = score_simulation_confidence([SimulationResult(status="ok", sim_type="tran")])
        assert score == 0.0
        assert grade == "F"

    def test_verified_flag_without_metrics_is_not_confidence(self):
        result = SimulationResult(status="ok", sim_type="tran", evidence_verified=True)
        assert score_simulation_confidence([result]) == (0.0, "F")

    def test_duration_or_ac_point_count_cannot_pass_ripple_or_phase_margin(self):
        tran = SimulationResult(
            status="ok", sim_type="tran", evidence_verified=True,
            metrics={"sim_duration_s": 0.01},
        )
        ac = SimulationResult(
            status="ok", sim_type="ac", evidence_verified=True,
            metrics={"frequency_points": 100},
        )
        _evaluate_simulation_targets(tran, {"ripple_mv": 50})
        _evaluate_simulation_targets(ac, {"phase_margin_deg": 45})
        assert tran.target_checks == {"ripple_mv": "unverified"}
        assert ac.target_checks == {"phase_margin_deg": "unverified"}
        assert score_simulation_confidence([tran, ac]) == (0.0, "F")

    def test_empty_results(self):
        score, grade = score_simulation_confidence([])
        assert score == 0.0
        assert grade == "F"

    def test_to_dict(self):
        plan = SimulationPlan()
        report = DesignSimulationReport(
            plan=plan,
            confidence_score=75.0,
            grade="C",
            summary="test",
            recommendations=["fix something"],
        )
        d = report.to_dict()
        assert d["confidence_score"] == 75.0
        assert d["grade"] == "C"
        assert "fix something" in d["recommendations"]


class TestRunDesignSimulations:
    def test_power_targets_apply_to_analysis_that_can_measure_them(self, tmp_path):
        plan = SimulationPlan(
            power_sims=[
                {
                    "ref": "U1",
                    "analyses": ["tran", "ac"],
                    "target_metrics": {"ripple_mv": 50, "phase_margin_deg": 45},
                }
            ]
        )

        def export(_components, output, **_kwargs):
            output = Path(output)
            output.write_text(".end")
            return output

        def run(_netlist, *, sim_type, **_kwargs):
            if sim_type == "tran":
                return SimulationResult(
                    status="ok", sim_type="tran", evidence_verified=True,
                    metrics={"V(out)_ripple_mv": 10},
                )
            return SimulationResult(
                status="ok", sim_type="ac", evidence_verified=True,
                metrics={"frequency_points": 100},
            )

        with patch("circuit_weaver.spice_netlist.export_spice_netlist", side_effect=export), patch(
            "circuit_weaver.spice_runner.run_simulation", side_effect=run
        ):
            report = run_design_simulations([], plan, output_dir=tmp_path / "sims")

        transient, ac = report.results
        assert transient.target_checks == {"ripple_mv": "passed"}
        assert ac.target_checks == {"phase_margin_deg": "unverified"}
        assert report.confidence_score == 50.0
        assert "1 unverified" in report.summary

    def test_all_skip_gracefully_without_ngspice(self, tmp_path):
        components = [
            _make_component("U1", mpn="TPS62300", category="buck", description="buck converter"),
        ]
        with patch("circuit_weaver.spice_runner._find_ngspice", return_value=None):
            report = run_design_simulations(
                components,
                output_dir=tmp_path / "sims",
            )
        assert isinstance(report, DesignSimulationReport)
        assert all(r.status == "skipped" for r in report.results)
        assert report.confidence_score == 0.0
        assert any("ngspice" in r.lower() for r in report.recommendations)

    def test_generates_netlist_files(self, tmp_path):
        components = [
            _make_component("U1", mpn="TPS62300", category="buck", description="buck converter"),
        ]
        output = tmp_path / "sims"
        with patch("circuit_weaver.spice_runner._find_ngspice", return_value=None):
            run_design_simulations(components, output_dir=output)
        # Netlists should have been generated even if sim was skipped
        cir_files = list(output.glob("*.cir"))
        assert len(cir_files) > 0

    def test_plan_to_dict(self):
        plan = SimulationPlan(
            power_sims=[{"ref": "U1", "analyses": ["tran"]}],
            signal_sims=[],
            thermal_sims=[],
        )
        d = plan.to_dict()
        assert d["total"] == 1
        assert len(d["power_sims"]) == 1
