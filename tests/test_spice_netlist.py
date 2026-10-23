"""Tests for SPICE netlist generation."""

import pytest

from circuit_weaver.component_db import ComponentDef
from circuit_weaver.spice_netlist import (
    _generate_analysis_cards,
    _normalize_node,
    _parse_value,
    _passive_to_spice,
    export_spice_netlist,
)


def _make_passive(ref, value, pin1_net="VDD", pin2_net="GND"):
    comp = ComponentDef.__new__(ComponentDef)
    comp.source_ref = ref
    comp.value = value
    comp.mpn = ""
    comp.pin_nets = {"1": pin1_net, "2": pin2_net}
    comp.category = ""
    comp.description = ""
    comp.ref_prefix = ref[0]
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


class TestNormalizeNode:
    def test_gnd_variants(self):
        assert _normalize_node("GND") == "0"
        assert _normalize_node("ground") == "0"
        assert _normalize_node("VSS") == "0"
        assert _normalize_node("0") == "0"

    def test_empty_is_rejected_instead_of_silently_grounded(self):
        with pytest.raises(ValueError, match="cannot be treated as ground"):
            _normalize_node("")

    def test_regular_net(self):
        assert _normalize_node("VDD_3V3") == "VDD_3V3"

    def test_special_chars_replaced(self):
        result = _normalize_node("NET/A+B")
        assert "/" not in result
        assert "+" not in result


class TestParseValue:
    def test_simple_numeric(self):
        assert _parse_value("10k") == "10k"
        assert _parse_value("100") == "100"

    def test_uppercase_m_is_mega_and_lowercase_m_stays_milli(self):
        assert _parse_value("1M") == "1Meg"
        assert _parse_value("1m") == "1m"
        assert _parse_value("1MΩ") == "1Meg"

    def test_units_removed(self):
        assert _parse_value("100nF") == "100n"
        assert _parse_value("4.7uH") == "4.7u"
        assert _parse_value("10ohm") == "10"

    def test_r_notation(self):
        assert _parse_value("4R7") == "4.7"
        assert _parse_value("10R") == "10"

    def test_picohenry(self):
        assert _parse_value("100pH") == "100p"

    def test_value_with_spaces(self):
        assert _parse_value("100 ohms") == "100"
        assert _parse_value("4.7 uH") == "4.7u"

    def test_standalone_farad(self):
        assert _parse_value("100F") == "100"

    def test_empty(self):
        assert _parse_value("") == "0"


class TestPassiveToSpice:
    def test_resistor(self):
        comp = _make_passive("R1", "10k", "NET_A", "NET_B")
        line = _passive_to_spice(comp)
        assert line == "R1 NET_A NET_B 10k"

    def test_capacitor(self):
        comp = _make_passive("C1", "100nF", "VDD", "GND")
        line = _passive_to_spice(comp)
        assert line == "C1 VDD 0 100n"

    def test_inductor(self):
        comp = _make_passive("L1", "4.7uH", "SW", "VOUT")
        line = _passive_to_spice(comp)
        assert line == "L1 SW VOUT 4.7u"

    def test_non_passive_returns_none(self):
        comp = _make_passive("U1", "ESP32", "VDD", "GND")
        assert _passive_to_spice(comp) is None

    def test_insufficient_pins_returns_none(self):
        comp = _make_passive("R1", "10k", "VDD", "GND")
        comp.pin_nets = {"1": "VDD"}
        assert _passive_to_spice(comp) is None


class TestAnalysisCards:
    def test_tran(self):
        cards = _generate_analysis_cards("tran")
        assert any(".tran" in c for c in cards)

    def test_ac(self):
        cards = _generate_analysis_cards("ac")
        assert any(".ac" in c for c in cards)

    def test_dc(self):
        cards = _generate_analysis_cards("dc", {"source": "V1", "start": "0", "stop": "3.3"})
        assert any(".dc" in c for c in cards)

    def test_op(self):
        cards = _generate_analysis_cards("op")
        assert cards == [".op"]

    def test_custom_tran_params(self):
        cards = _generate_analysis_cards("tran", {"step": "10n", "stop": "1m"})
        assert ".tran 10n 1m 0" in cards[0]

    def test_rejects_analysis_card_injection(self):
        import pytest

        with pytest.raises(ValueError, match="Unsafe or unsupported"):
            _generate_analysis_cards("ac", {"fstart": "1\n.control"})


class TestExportSpiceNetlist:
    def test_generates_cir_file(self, tmp_path):
        components = [
            _make_passive("R1", "10k", "VIN", "VOUT"),
            _make_passive("C1", "100nF", "VOUT", "GND"),
        ]
        out = export_spice_netlist(components, tmp_path / "test.cir")
        assert out.exists()
        content = out.read_text()
        assert "R1" in content
        assert "C1" in content
        assert ".tran" in content
        assert ".end" in content
        assert "set filetype=ascii" in content

    def test_skips_components_without_models(self, tmp_path):
        ic = _make_passive("U1", "TPS62300", "VIN", "VOUT")
        ic.source_ref = "U1"
        ic.mpn = "TPS62300"
        components = [ic, _make_passive("R1", "10k", "VIN", "VOUT")]
        out = export_spice_netlist(components, tmp_path / "test.cir")
        content = out.read_text()
        assert "Skipped U1" in content
        assert "R1" in content

    def test_ac_analysis(self, tmp_path):
        components = [_make_passive("R1", "1k", "IN", "OUT")]
        out = export_spice_netlist(
            components,
            tmp_path / "test.cir",
            analysis_type="ac",
        )
        content = out.read_text()
        assert ".ac" in content

    def test_explicit_voltage_testbench_source(self, tmp_path):
        components = [
            _make_passive("R1", "1k", "VIN", "VOUT"),
            _make_passive("C1", "100n", "VOUT", "GND"),
        ]
        out = export_spice_netlist(
            components,
            tmp_path / "testbench.cir",
            analysis_type="ac",
            analysis_params={"variation": "dec", "points": 40, "fstart": "10", "fstop": "100k"},
            sources=[{"ref": "VTEST", "type": "V", "positive": "VIN", "negative": "GND", "dc": 0, "ac": 1}],
        )
        content = out.read_text()
        assert "VTEST VIN 0 DC 0 AC 1" in content
        assert ".ac dec 40 10 100k" in content

    def test_structured_pulse_source_is_rendered_and_validated(self, tmp_path):
        components = [_make_passive("R1", "1k", "VIN", "VOUT")]
        source = {
            "ref": "VTEST",
            "type": "V",
            "positive": "VIN",
            "negative": "GND",
            "pulse": {"v1": 0, "v2": 3.3, "delay": 0, "rise": 1e-9, "fall": 1e-9, "width": 1e-6, "period": 2e-6},
        }
        out = export_spice_netlist(components, tmp_path / "pulse.cir", sources=[source])
        assert "VTEST VIN 0 PULSE(0 3.2999999999999998 0 1.0000000000000001e-09" in out.read_text()
        source["pulse"]["period"] = 0
        with pytest.raises(ValueError, match="period must be positive"):
            export_spice_netlist(components, tmp_path / "bad-pulse.cir", sources=[source])

    def test_explicit_source_rejects_unconnected_or_colliding_nodes(self, tmp_path):
        import pytest

        components = [_make_passive("R1", "1k", "IN?", "OUT")]
        with pytest.raises(ValueError, match="unconnected net"):
            export_spice_netlist(
                components,
                tmp_path / "floating.cir",
                sources=[{"ref": "VTEST", "type": "V", "positive": "MISSING", "negative": "OUT", "ac": 1}],
            )
        colliding = [
            _make_passive("R1", "1k", "IN?", "OUT"),
            _make_passive("R2", "1k", "IN_", "GND"),
        ]
        with pytest.raises(ValueError, match="normalize to the same SPICE node"):
            export_spice_netlist(colliding, tmp_path / "collision.cir")
        case_colliding = [
            _make_passive("R1", "1k", "VIN", "OUT"),
            _make_passive("R2", "1k", "vin", "GND"),
        ]
        with pytest.raises(ValueError, match="normalize to the same SPICE node"):
            export_spice_netlist(case_colliding, tmp_path / "case-collision.cir")

    def test_one_megohm_one_picofarad_export_uses_mega_suffix(self, tmp_path):
        components = [
            _make_passive("R1", "1M", "IN", "OUT"),
            _make_passive("C1", "1pF", "OUT", "GND"),
        ]
        out = export_spice_netlist(components, tmp_path / "rc.cir")
        content = out.read_text()
        assert "R1 IN OUT 1Meg" in content
        assert "C1 OUT 0 1p" in content

    def test_path_only_ic_model_is_reported_as_unsupported(self, tmp_path):
        ic = _make_passive("U1", "TPS62300", "VIN", "VOUT")
        ic.source_ref = "U1"
        ic.mpn = "TPS62300"
        out = export_spice_netlist(
            [ic], tmp_path / "model.cir", model_manifest={"TPS62300": str(tmp_path / "model.lib")}
        )
        assert "Skipped U1" in out.read_text()

    def test_model_instance_uses_explicit_pin_order_and_subckt_name(self, tmp_path):
        ic = _make_passive("U1", "TPS62300", "VIN", "VOUT")
        ic.source_ref = "U1"
        ic.mpn = "TPS62300"
        model = tmp_path / "model.lib"
        model.write_text(".subckt device VIN VOUT\nR1 VIN VOUT 1k\n.ends device\n")
        out = export_spice_netlist(
            [ic],
            tmp_path / "model.cir",
            model_manifest={"TPS62300": {"path": str(model), "subckt": "device", "pin_order": ["1", "2"]}},
        )
        content = out.read_text()
        assert "XU1 VIN VOUT device" in content
        assert f'.include "{model}"' in content
