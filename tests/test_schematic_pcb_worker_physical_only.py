from __future__ import annotations

import unittest

from circuit_weaver._schematic_pcb_worker import (
    _assert_physical_only_unconnected,
    _physical_only_identity,
    _validate_physical_only_request,
)


class FakePad:
    def __init__(self, number="", netname="", netcode=0):
        self.number = number
        self.netname = netname
        self.netcode = netcode

    def GetNumber(self):
        return self.number

    def GetNetname(self):
        return self.netname

    def GetNetCode(self):
        return self.netcode


class FakeFootprint:
    def __init__(self, reference, pads):
        self.reference = reference
        self.pads = pads

    def GetReference(self):
        return self.reference

    def Pads(self):
        return self.pads


class FakeBoard:
    def __init__(self, footprints):
        self.footprints = footprints

    def GetFootprints(self):
        return self.footprints


class PhysicalOnlyPadGuardTests(unittest.TestCase):
    def test_normal_floating_pads_are_retained_including_unnumbered(self):
        footprint = FakeFootprint("ID1", [FakePad("1"), FakePad("")])
        rows = _assert_physical_only_unconnected(footprint, "ID1")
        self.assertEqual(rows, [("", "", 0), ("1", "", 0)])
        self.assertEqual(_physical_only_identity(FakeBoard([footprint]), {"ID1"}), {"ID1": rows})

    def test_any_assigned_name_or_code_is_rejected_for_numbered_and_unnumbered_pads(self):
        cases = (
            FakePad("1", netname="N1", netcode=1),
            FakePad("1", netcode=1),
            FakePad("", netname="N1", netcode=1),
            FakePad("", netcode=1),
        )
        for pad in cases:
            with self.subTest(number=pad.GetNumber(), name=pad.GetNetname(), code=pad.GetNetCode()):
                with self.assertRaisesRegex(ValueError, "stale net metadata"):
                    _assert_physical_only_unconnected(FakeFootprint("ID1", [pad]), "ID1")

    def test_physical_only_request_rejects_symbol_pins_or_net_assignments(self):
        _validate_physical_only_request({"physical_only": True, "symbol_pins": [], "nets": {}}, "ID1")
        for item in (
            {"physical_only": True, "symbol_pins": ["1"], "nets": {}},
            {"physical_only": True, "symbol_pins": [], "nets": {"1": "/N1"}},
        ):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, "must not declare"):
                _validate_physical_only_request(item, "ID1")

    def test_roundtrip_identity_requires_every_marker_reference(self):
        with self.assertRaisesRegex(ValueError, "references changed"):
            _physical_only_identity(FakeBoard([]), {"ID1"})
        with self.assertRaisesRegex(ValueError, "duplicate physical-only"):
            _physical_only_identity(FakeBoard([FakeFootprint("ID1", []), FakeFootprint("ID1", [])]), {"ID1"})


if __name__ == "__main__":
    unittest.main()
