from __future__ import annotations

from types import SimpleNamespace

import pytest

from circuit_weaver._schematic_pcb_worker import _assert_npth_geometry, _validate_npth_request
from circuit_weaver.schematic_pcb import _footprint_pad_records, _parse_netlist

FOOTPRINT = """(footprint "TEST"
 (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
 (pad "2" np_thru_hole circle (at 2 3) (size 0.6 0.6) (drill 0.6) (layers "*.Cu" "*.Mask")))"""


def parse(tmp_path, *, nc=True, shared=False, node=True, footprint=FOOTPRINT, symbol_type="no_connect"):
    fp = tmp_path / "TEST.kicad_mod"
    fp.write_text(footprint)
    xml = tmp_path / "net.xml"
    second = (
        (
            f'<net name="NC"><node ref="U1" pin="2" pintype="{"no_connect" if nc else "passive"}"/>'
            + ('<node ref="U1" pin="1"/>' if shared else "")
            + "</net>"
        )
        if node
        else ""
    )
    xml.write_text(
        '<export><components><comp ref="U1"><libsource lib="Test" part="TEST"/><footprint>Test:TEST</footprint>'
        '<units><unit><pins><pin num="1"/><pin num="2"/></pins></unit></units></comp></components>'
        '<libparts><libpart lib="Test" part="TEST"><pins><pin num="1" type="passive"/>'
        f'<pin num="2" type="{symbol_type}"/></pins></libpart></libparts>'
        '<nets><net name="SIG"><node ref="U1" pin="1"/></net>' + second + "</nets></export>"
    )
    return _parse_netlist(xml, SimpleNamespace(resolve=lambda reference: fp))


def test_numbered_npth_requires_explicit_nc_and_retains_source_pin_and_geometry(tmp_path):
    result = parse(tmp_path)
    item = result["components"][0]
    assert item["symbol_pins"] == ["1", "2"]
    assert item["pad_numbers"] == ["1"]
    assert item["normalized_npth_pin_numbers"] == ["2"]
    assert item["nets"] == {"1": "SIG"}
    assert item["npth_pad_records"][0]["geometry"] == ["circle", 2, 3, 0.6, 0.6, 0.6, 0.6, 0, 0, 0]
    assert result["explicit_no_connects"] == [{"reference": "U1", "pin": "2"}]


@pytest.mark.parametrize("kwargs", [{"nc": False}, {"shared": True}, {"node": False}])
def test_connected_shared_or_implicit_npth_is_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError, match="explicit singleton no-connect"):
        parse(tmp_path, **kwargs)


def test_mixed_electrical_and_npth_number_is_rejected(tmp_path):
    text = FOOTPRINT.replace('(pad "2" np_thru_hole', '(pad "1" np_thru_hole')
    with pytest.raises(ValueError):
        parse(tmp_path, footprint=text)


def test_pad_text_inside_properties_is_not_an_electrical_pad():
    records = _footprint_pad_records(
        '(footprint "X" (property "note" "(pad fake)") (pad "1" smd rect (at 0 0) (size 1 1)))'
    )
    assert [x["number"] for x in records] == ["1"]


@pytest.mark.parametrize(
    "text",
    [
        FOOTPRINT.replace("np_thru_hole circle", "np_thru_hole custom"),
        FOOTPRINT.replace("(drill 0.6)", "(drill nan)"),
        FOOTPRINT.replace("(drill 0.6)", "(drill 0)"),
    ],
)
def test_unverifiable_hole_geometry_is_rejected(text):
    with pytest.raises(ValueError):
        _footprint_pad_records(text)


class FakePad:
    def __init__(self, number="", x=2, net="", code=0):
        self.number, self.x, self.net, self.code = number, x, net, code

    def GetAttribute(self):
        return 99

    def GetNumber(self):
        return self.number

    def GetNetname(self):
        return self.net

    def GetNetCode(self):
        return self.code

    def GetFPRelativePosition(self):
        return SimpleNamespace(x=self.x, y=3)

    def GetSize(self):
        return SimpleNamespace(x=0.6, y=0.6)

    def GetDrillSize(self):
        return SimpleNamespace(x=0.6, y=0.6)

    def GetOffset(self):
        return SimpleNamespace(x=0, y=0)

    def GetShape(self):
        return 0

    def GetOrientation(self):
        return SimpleNamespace(AsDegrees=lambda: 0)


def check(pads):
    records = _footprint_pad_records(FOOTPRINT)[1:]
    pcbnew = SimpleNamespace(PAD_ATTRIB_NPTH=99, PAD_SHAPE_CIRCLE=0, PAD_SHAPE_OVAL=1, ToMM=lambda x: x)
    fp = SimpleNamespace(
        Pads=lambda: pads, GetReference=lambda: "U1", GetOrientation=lambda: SimpleNamespace(AsDegrees=lambda: 0)
    )
    _assert_npth_geometry(fp, records, pcbnew)


@pytest.mark.parametrize("number", ["", "2"])
def test_native_hole_may_retain_or_clear_only_the_declared_nc_number(number):
    check([FakePad(number=number)])


@pytest.mark.parametrize(
    "pads",
    [[], [FakePad(), FakePad()], [FakePad(number="3")], [FakePad(x=2.1)], [FakePad(net="GND")], [FakePad(code=1)]],
)
def test_hole_loss_addition_geometry_number_or_net_change_fails(pads):
    with pytest.raises(ValueError):
        check(pads)


def test_worker_requires_explicit_nc_and_forbids_net_assignment():
    item = dict(
        npth_pad_records=_footprint_pad_records(FOOTPRINT)[1:],
        normalized_npth_pin_numbers=["2"],
        symbol_pins=["1", "2"],
        pad_numbers=["1"],
        nets={"1": "SIG"},
    )
    _validate_npth_request(item, "U1", {("U1", "2")})
    with pytest.raises(ValueError, match="no-connect"):
        _validate_npth_request(item, "U1", set())
    item["nets"]["2"] = "GND"
    with pytest.raises(ValueError, match="electrical net"):
        _validate_npth_request(item, "U1", {("U1", "2")})


def test_unused_electrical_symbol_pin_cannot_be_normalized_to_a_hole(tmp_path):
    with pytest.raises(ValueError, match="no-connect symbol pin"):
        parse(tmp_path, symbol_type="passive")


def test_unnumbered_holes_do_not_change_electrical_import_rules(tmp_path):
    fp=FOOTPRINT.replace('(pad "2" np_thru_hole circle','(pad "" np_thru_hole custom')
    records=_footprint_pad_records(fp)
    assert records[1]['number']=='' and 'geometry' not in records[1]
