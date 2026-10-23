"""Tests for the simulation orchestrator."""

from unittest.mock import patch

from circuit_weaver.component_db import ComponentDef
from circuit_weaver.simulation import (
    DesignSimulationReport,
    SimulationPlan,
    _verify_targets,
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
    def test_explicit_voltage_and_current_targets_select_exact_vectors_with_units(self):
        result = SimulationResult(
            status="ok",
            sim_type="op",
            evidence_verified=True,
            traces={"v(out)": [3.3], "v(out_aux)": [9.0], "i(vsupply)": [-0.02]},
        )
        _verify_targets(
            result,
            {
                "rail": {
                    "metric": "voltage",
                    "trace": "v(out)",
                    "unit": "V",
                    "comparator": "between",
                    "min": 3.2,
                    "max": 3.4,
                },
                "load": {
                    "metric": "current",
                    "trace": "i(vsupply)",
                    "unit": "A",
                    "comparator": "abs_le",
                    "value": 0.03,
                },
                "ambiguous": {"metric": "voltage", "trace": "v(out", "unit": "V", "comparator": "ge", "value": 0},
            },
            "U1",
            [],
        )
        assert result.target_checks == {"rail": "passed", "load": "passed", "ambiguous": "unverified"}

    def test_explicit_target_unknown_unit_trace_and_nonfinite_are_unverified(self):
        result = SimulationResult(
            status="ok",
            sim_type="op",
            evidence_verified=True,
            traces={"v(out)": [float("nan")]},
        )
        _verify_targets(
            result,
            {
                "unit": {"metric": "voltage", "trace": "v(out)", "unit": "mV", "comparator": "ge", "value": 0},
                "trace": {"metric": "voltage", "trace": "v(out2)", "unit": "V", "comparator": "ge", "value": 0},
                "nonfinite": {"metric": "voltage", "trace": "v(out)", "unit": "V", "comparator": "ge", "value": 0},
                "impossible": {
                    "metric": "voltage",
                    "trace": "v(out)",
                    "unit": "V",
                    "comparator": "between",
                    "min": 4,
                    "max": 3,
                },
            },
            "U1",
            [],
        )
        assert result.target_checks == {
            "unit": "unverified",
            "trace": "unverified",
            "nonfinite": "unverified",
            "impossible": "unverified",
        }

    def test_boolean_explicit_limits_are_not_numeric_engineering_bounds(self):
        result = SimulationResult(
            status="ok", sim_type="op", evidence_verified=True, traces={"v(out)": [1.0]}
        )
        _verify_targets(
            result,
            {
                "boolean_value": {
                    "metric": "voltage",
                    "trace": "v(out)",
                    "unit": "V",
                    "comparator": "ge",
                    "value": True,
                },
                "boolean_window": {
                    "metric": "voltage",
                    "trace": "v(out)",
                    "unit": "V",
                    "comparator": "between",
                    "min": False,
                    "max": 2.0,
                },
            },
            "U1",
            [],
        )
        assert result.target_checks == {"boolean_value": "unverified", "boolean_window": "unverified"}

    def test_dc_sweep_requires_explicit_endpoint_sample(self):
        result = SimulationResult(
            status="ok", sim_type="dc", evidence_verified=True, traces={"v(out)": [0.0, 1.0, 2.0]}
        )
        target = {"metric": "voltage", "trace": "v(out)", "unit": "V", "comparator": "between", "min": 1.9, "max": 2.1}
        _verify_targets(result, {"without_endpoint": target}, "U1", [])
        assert result.target_checks["without_endpoint"] == "unverified"
        _verify_targets(result, {"endpoint": {**target, "sample": "endpoint"}}, "U1", [])
        assert result.target_checks["endpoint"] == "passed"

    def test_explicit_targets_are_limited_to_supported_analysis_kinds(self):
        voltage = {"metric": "voltage", "trace": "v(out)", "unit": "V", "comparator": "ge", "value": 0}
        ripple = {"metric": "ripple", "trace": "v(out)", "unit": "mV", "comparator": "le", "value": 100}
        ac = SimulationResult(
            status="ok",
            sim_type="ac",
            evidence_verified=True,
            traces={"v(out)": [complex(1, 3)]},
        )
        _verify_targets(ac, {"ac_voltage": voltage, "ac_ripple": ripple}, "U1", [])
        assert ac.target_checks == {"ac_voltage": "unverified", "ac_ripple": "unverified"}

    def test_operating_point_target_requires_one_real_sample(self):
        target = {"metric": "voltage", "trace": "v(out)", "unit": "V", "comparator": "ge", "value": 0}
        result = SimulationResult(status="ok", sim_type="op", evidence_verified=True, traces={"v(out)": [1.0, 2.0]})
        _verify_targets(result, {"op_voltage": target}, "U1", [])
        assert result.target_checks["op_voltage"] == "unverified"

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
                status="ok", sim_type="tran", evidence_verified=True, target_checks={"ripple_mv": "passed"}
            ),
            SimulationResult(
                status="ok", sim_type="ac", evidence_verified=True, target_checks={"bandwidth_hz_min": "passed"}
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
                status="ok", sim_type="tran", evidence_verified=True, target_checks={"ripple_mv": "passed"}
            ),
            SimulationResult(status="failed", sim_type="ac"),
        ]
        score, grade = score_simulation_confidence(results)
        assert 0 < score < 100

    def test_empty_results(self):
        score, grade = score_simulation_confidence([])
        assert score == 0.0
        assert grade == "F"

    def test_process_success_without_verified_targets_is_not_confidence(self):
        results = [SimulationResult(status="ok", sim_type="tran", evidence_verified=True)]
        score, grade = score_simulation_confidence(results)
        assert score == 0.0
        assert grade == "F"

    def test_unverified_target_is_not_confidence(self):
        results = [
            SimulationResult(
                status="ok", sim_type="tran", evidence_verified=True, target_checks={"ripple_mv": "unverified"}
            )
        ]
        score, grade = score_simulation_confidence(results)
        assert score == 0.0
        assert grade == "F"

    def test_nonfinite_target_or_metric_cannot_pass(self):
        for target, measured in ((float("inf"), 1.0), (1.0, float("inf")), (float("nan"), 1.0)):
            result = SimulationResult(
                status="ok",
                sim_type="tran",
                evidence_verified=True,
                metrics={"v(out)_ripple_mv": measured},
            )
            recommendations = []
            _verify_targets(result, {"ripple_mv": target}, "U1", recommendations)
            assert result.target_checks["ripple_mv"] == "unverified"
            assert score_simulation_confidence([result])[0] == 0.0

    def test_legacy_boolean_limit_is_not_numeric(self):
        result = SimulationResult(
            status="ok",
            sim_type="tran",
            evidence_verified=True,
            metrics={"v(out)_ripple_mv": 0.5},
        )
        _verify_targets(result, {"ripple_mv": True}, "U1", [])

        assert result.target_checks["ripple_mv"] == "unverified"
        assert score_simulation_confidence([result])[0] == 0.0

    def test_ambiguous_or_untyped_ripple_targets_are_unverified(self):
        for targets, metrics in (
            ({"ripple": 3000}, {"v(out)_ripple_mv": 1000.0}),
            ({"ripple_mv": 3000}, {"v(out)_ripple_mv": 1000.0, "v(aux)_ripple_mv": 1200.0}),
            ({"voltage": 3000}, {"v(out)_ripple_mv": 1000.0}),
        ):
            result = SimulationResult(status="ok", sim_type="tran", evidence_verified=True, metrics=metrics)
            _verify_targets(result, targets, "U1", [])
            assert all(state == "unverified" for state in result.target_checks.values())

    def test_ripple_and_bandwidth_targets_require_exact_metric_schema(self):
        result = SimulationResult(
            status="ok",
            sim_type="ac",
            evidence_verified=True,
            metrics={"input_bandwidth_hz": 1_000_000.0},
        )
        _verify_targets(result, {"bandwidth_hz_min": 100_000}, "U1", [])
        assert result.target_checks["bandwidth_hz_min"] == "unverified"

        result = SimulationResult(
            status="ok",
            sim_type="tran",
            evidence_verified=True,
            metrics={"v(out)_ripple_mv": 1200.0},
        )
        _verify_targets(result, {"ripple_max_mv": 1500}, "U1", [])
        assert result.target_checks["ripple_max_mv"] == "passed"

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
        assert d["status"] == "unverified"
        assert d["confidence_score"] == 75.0
        assert d["grade"] == "C"
        assert "fix something" in d["recommendations"]
        assert d["coverage"] == {}


class TestRunDesignSimulations:
    def test_explicit_testbench_plan_passes_sources_and_targets_through(self, tmp_path):
        components = [
            _make_component("R1"),
            _make_component("C1"),
        ]
        components[0].value = "1k"
        components[0].pin_nets = {"1": "VIN", "2": "VOUT"}
        components[1].value = "100n"
        components[1].pin_nets = {"1": "VOUT", "2": "GND"}
        spec = {
            "simulation": {
                "analyses": [
                    {
                        "ref": "rc_filter",
                        "kind": "ac",
                        "analysis_params": {
                            "variation": "dec",
                            "points": 30,
                            "fstart": "10",
                            "fstop": "100k",
                            "input_trace": "v(vin)",
                            "output_trace": "v(vout)",
                        },
                        "sources": [
                            {"ref": "VTEST", "type": "V", "positive": "VIN", "negative": "GND", "dc": 0, "ac": 1}
                        ],
                        "target_metrics": {"bandwidth_hz_max": 2000},
                    }
                ]
            }
        }
        plan = plan_simulations(components, spec=spec)
        assert plan.total == 1
        analysis = plan.signal_sims[0]
        assert analysis["analyses"] == ["ac"]
        assert analysis["target_metrics"] == {"bandwidth_hz_max": 2000}

        def run_fake(_path, *, sim_type, analysis_params):
            assert sim_type == "ac"
            assert analysis_params == spec["simulation"]["analyses"][0]["analysis_params"]
            return SimulationResult(status="ok", sim_type="ac", evidence_verified=True, metrics={"bandwidth_hz": 1500})

        with patch("circuit_weaver.spice_runner.run_simulation", side_effect=run_fake):
            report = run_design_simulations(components, plan, output_dir=tmp_path / "sims")
        netlist = (tmp_path / "sims" / "rc_filter_ac.cir").read_text()
        assert "VTEST VIN 0 DC 0 AC 1" in netlist
        assert ".ac dec 30 10 100k" in netlist
        assert report.results[0].target_checks["bandwidth_hz_max"] == "passed"

    def test_explicit_ac_trace_selectors_must_name_connected_nodes(self):
        import pytest

        with pytest.raises(ValueError, match="connected non-ground net"):
            plan_simulations(
                [_make_component("R1")],
                spec={
                    "simulation": {
                        "analyses": [
                            {
                                "ref": "filter",
                                "kind": "ac",
                                "analysis_params": {"input_trace": "v(vin)", "output_trace": "v(missing)"},
                            }
                        ]
                    }
                },
            )

    def test_malformed_explicit_analysis_fails_closed(self):
        import pytest

        with pytest.raises(ValueError, match="kind must be ac, tran, op, or dc"):
            plan_simulations([], spec={"simulation": {"analyses": [{"ref": "x", "kind": "tran\n.end"}]}})

    def test_unsupported_regulator_is_failed_even_without_ngspice(self, tmp_path):
        components = [
            _make_component("U1", mpn="TPS62300", category="buck", description="buck converter"),
        ]
        with patch("circuit_weaver.spice_runner._find_ngspice", return_value=None):
            report = run_design_simulations(
                components,
                output_dir=tmp_path / "sims",
            )
        assert isinstance(report, DesignSimulationReport)
        assert all(r.status == "failed" for r in report.results)
        assert report.status == "failed"
        assert report.confidence_score == 0.0
        assert any("model" in r.lower() for r in report.recommendations)

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
