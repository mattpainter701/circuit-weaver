from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from circuit_weaver.schematic_pcb import _parse_netlist


class FakeLibrary:
    def __init__(self, path: Path):
        self.path = path

    def resolve(self, reference: str) -> Path | None:
        return self.path if reference == "Fixture:Physical" else None


def make_netlist(path: Path, lib: str, part: str, nodes: str = "", symbol_pins: str = "") -> None:
    path.write_text(
        f'''<?xml version="1.0"?>
<export>
 <components>
  <comp ref="H1"><value>test</value><footprint>Fixture:Physical</footprint>
   <libsource lib="{lib}" part="{part}"/><sheetpath names="/" tstamps="/"/>
   <units><unit name="A" variant=""><pins>{symbol_pins}</pins></unit></units>
  </comp>
 </components>
 <libparts><libpart lib="{lib}" part="{part}"><pins>{symbol_pins}</pins></libpart></libparts>
 <nets><net code="1" name="/N1">{nodes}</net></nets>
</export>''',
        encoding="utf-8",
    )


class PinlessMechanicalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.netlist = self.root / "net.xml"
        self.footprint = self.root / "fixture.kicad_mod"
        self.footprint.write_text('''(footprint "Physical" (version 20240108) (generator pcbnew)
 (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
 (pad "2" thru_hole circle (at 2 0) (size 1 1) (drill 0.5) (layers "*.Cu" "*.Mask")))''', encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def parse(self, lib: str = "Mechanical", part: str = "MountingHole", nodes: str = "", pins: str = ""):
        make_netlist(self.netlist, lib, part, nodes, pins)
        return _parse_netlist(self.netlist, FakeLibrary(self.footprint))

    @staticmethod
    def component(parsed):
        return next(row for row in parsed["components"] if row["reference"] == "H1")

    def test_mounting_hole_retains_all_physical_pad_numbers(self):
        item = self.component(self.parse())
        self.assertTrue(item["physical_only"])
        self.assertEqual(item["symbol_pins"], [])
        self.assertEqual(item["pad_numbers"], ["1", "2"])
        self.assertEqual(item["nets"], {})

    def test_fiducial_is_the_only_other_explicit_physical_only_type(self):
        item = self.component(self.parse(part="Fiducial"))
        self.assertTrue(item["physical_only"])
        self.assertEqual(item["pad_numbers"], ["1", "2"])

    def test_pinless_physical_component_with_any_net_node_fails_closed(self):
        node = '<node ref="H1" pin="1" />'
        with self.assertRaisesRegex(ValueError, "no symbol pin numbers"):
            self.parse(nodes=node)

    def test_electrical_or_unknown_pinless_symbol_fails_closed(self):
        for lib, part in (("Device", "R"), ("Custom", "MountingHole")):
            with self.subTest(symbol=(lib, part)), self.assertRaisesRegex(ValueError, "no symbol pin numbers"):
                self.parse(lib=lib, part=part)

    def test_electrical_symbol_pin_footprint_pad_identity_remains_exact(self):
        pins = '<pin num="1" name="A" type="passive" />'
        one_pad = '(footprint "Physical" (pad "1" smd rect (at 0 0) '
        one_pad += '(size 1 1) (layers "F.Cu")))'
        self.footprint.write_text(one_pad, encoding="utf-8")
        item = self.component(self.parse(lib="Device", part="R", pins=pins))
        self.assertFalse(item["physical_only"])
        self.assertEqual(item["symbol_pins"], ["1"])
        self.assertEqual(item["pad_numbers"], ["1"])
        node = '<node ref="H1" pin="1" pintype="passive" />'
        parsed = self.parse(lib="Device", part="R", pins=pins, nodes=node)
        self.assertEqual(self.component(parsed)["nets"], {"1": "/N1"})
        two_pads = '(footprint "Physical" (pad "1" smd rect (at 0 0) '
        two_pads += '(size 1 1) (layers "F.Cu")) '
        two_pads += '(pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu")))'
        self.footprint.write_text(two_pads, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "do not exactly match"):
            self.parse(lib="Device", part="R", pins=pins)


if __name__ == "__main__":
    unittest.main()
