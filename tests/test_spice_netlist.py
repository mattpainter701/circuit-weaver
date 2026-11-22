"""Tests for SPICE netlist generation."""



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

    def test_empty_is_ground(self):
        assert _normalize_node("") == "0"

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
        assert "set filetype=ascii" in content
        assert ".end" in content

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
            components, tmp_path / "test.cir",
            analysis_type="ac",
        )
        content = out.read_text()
        assert ".ac" in content
