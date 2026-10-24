from types import SimpleNamespace

import pytest

from circuit_weaver import _kicad_worker as worker


class _Seq(list):
    def Seq(self):
        return self


class _Outline:
    def Format(self, _):
        return "zone-outline"


class _Polygons:
    def __init__(self):
        self.text = "filled-polygons"

    def Format(self, _):
        return self.text

    def OutlineCount(self):
        return 1


class _Zone:
    def __init__(self, uuid="zone-1", net="GND", layer_name="In1.Cu", rule_area=False):
        self.m_Uuid = SimpleNamespace(AsString=lambda: uuid)
        self.net = net
        self.layer_name = layer_name
        self.rule_area = rule_area
        self.polygons = _Polygons()

    def GetNetname(self):
        return self.net

    def GetLayer(self):
        return 2

    def GetLayerSet(self):
        return _Seq([2])

    def GetLayerName(self):
        return self.layer_name

    def GetIsRuleArea(self):
        return self.rule_area

    def GetDoNotAllowTracks(self):
        return True

    def GetDoNotAllowVias(self):
        return True

    def GetDoNotAllowPads(self):
        return False

    def GetDoNotAllowZoneFills(self):
        return True

    def GetDoNotAllowFootprints(self):
        return False

    def Outline(self):
        return _Outline()

    def GetLocalClearance(self):
        return 250000

    def GetMinThickness(self):
        return 200000

    def GetThermalReliefGap(self):
        return 250000

    def GetThermalReliefSpokeWidth(self):
        return 300000

    def GetPadConnection(self):
        return 1

    def GetIslandRemovalMode(self):
        return 0

    def GetMinIslandArea(self):
        return 0

    def GetAssignedPriority(self):
        return 0

    def IsFilled(self):
        return True

    def HasFilledPolysForLayer(self, _):
        return True

    def GetFilledPolysList(self, _):
        return self.polygons

    def IsIsland(self, _, __):
        return False

    def CalculateFilledArea(self):
        return 10_000_000_000


class _Board:
    def __init__(self):
        self.zone = _Zone()
        self.zones = [self.zone]

    def GetEnabledLayers(self):
        return SimpleNamespace(CuStack=lambda: _Seq([0, 2, 4, 31]))

    def GetCopperLayerCount(self):
        return 4

    def GetLayerName(self, layer):
        return {0: "F.Cu", 2: "In1.Cu", 4: "In2.Cu", 31: "B.Cu"}[layer]

    def Zones(self):
        return self.zones

    def BuildConnectivity(self):
        pass
    def GetConnectivity(self):
        return SimpleNamespace(GetConnectedPads=lambda _: [], GetConnectedTracks=lambda _: [])
    def GetFootprints(self):
        return []


class _Filler:
    last = None

    def __init__(self, expected):
        self.expected = expected
        self.received = None
        _Filler.last = self

    def Fill(self, zones):
        self.received = zones
        return zones is self.expected


PCBNEW = SimpleNamespace(
    F_Cu=0, In1_Cu=2, In2_Cu=4, B_Cu=31,
    LSET=SimpleNamespace(AllCuMask=lambda: _Seq([0, 2, 4, 31])),
    ZONE_FILLER=lambda board: _Filler(board.Zones()),
)


def test_import_refill_report_proves_saved_geometry_and_stackup(monkeypatch):
    board = _Board()
    monkeypatch.setattr("circuit_weaver._native_ground_plane._connected_ground_pads",
                        lambda board, net, allow_unconnected=False: [])
    definitions = worker._zone_definition_state(board, PCBNEW)
    stack = worker._native_stack_state(board, PCBNEW)

    report = worker._refill_imported_zones(board, PCBNEW, definitions, stack)
    verified = worker._verify_saved_imported_zones(board, PCBNEW, report)

    assert verified["status"] == "verified"
    assert verified["saved_fill_geometry_verified"] is True
    assert verified["copper_layers_preserved"] is True
    assert verified["zone_count"] == 1
    assert verified["zones"][0]["layer_name"] == "In1.Cu"
    assert verified["zones"][0]["polygon_sha256"]
    assert _Filler.last.received is board.zones
    assert verified["unconnected_pads_by_net"] == {"GND": []}


def test_import_refill_rejects_zone_definition_mutation():
    board = _Board()
    definitions = worker._zone_definition_state(board, PCBNEW)
    board.zone.net = "VDD"
    with pytest.raises(ValueError, match="changed existing zone definitions"):
        worker._refill_imported_zones(board, PCBNEW, definitions, worker._native_stack_state(board, PCBNEW))


def test_saved_refill_rejects_changed_fill_geometry(monkeypatch):
    board = _Board()
    monkeypatch.setattr("circuit_weaver._native_ground_plane._connected_ground_pads",
                        lambda board, net, allow_unconnected=False: [])
    report = worker._refill_imported_zones(
        board, PCBNEW, worker._zone_definition_state(board, PCBNEW), worker._native_stack_state(board, PCBNEW),
    )
    board.zone.polygons.text = "stale-or-mutated-fill"
    with pytest.raises(ValueError, match="fill geometry differs"):
        worker._verify_saved_imported_zones(board, PCBNEW, report)


def test_rule_area_and_non_copper_zone_definitions_are_preserved_but_not_filled():
    board = _Board()
    rule_area = _Zone(uuid="keepout-1", rule_area=True)
    non_copper = _Zone(uuid="graphic-1", layer_name="Dwgs.User")
    board.zones.extend([rule_area, non_copper])
    state = worker._zone_fill_state(board, PCBNEW)
    assert state["fillable_zone_count"] == 1
    assert state["keepout_zone_count"] == 1
    assert state["non_copper_zone_count"] == 1
    assert [row["id"] for row in state["rows"]] == ["zone-1"]


def test_saved_report_includes_unconnected_pads_by_net(monkeypatch):
    board = _Board()

    def get_footprints():
        return [SimpleNamespace(
            GetReference=lambda: "J1",
            Pads=lambda: [SimpleNamespace(
                GetNetname=lambda: "GND", GetNumber=lambda: "1", IsConnected=lambda: False
            )],
        )]

    board.GetFootprints = get_footprints
    report = worker._refill_imported_zones(
        board, PCBNEW, worker._zone_definition_state(board, PCBNEW), worker._native_stack_state(board, PCBNEW),
    )
    verified = worker._verify_saved_imported_zones(board, PCBNEW, report)
    assert verified["unconnected_pads_by_net"] == {"GND": ["J1.1"]}
    assert verified["unconnected_ground_pads"] == ["J1.1"]


def test_saved_report_counts_only_fillable_zones_and_preserves_keepout_flags():
    board = _Board()
    board.zones.append(_Zone(uuid="keepout-1", rule_area=True))
    report = worker._refill_imported_zones(
        board, PCBNEW, worker._zone_definition_state(board, PCBNEW), worker._native_stack_state(board, PCBNEW))
    verified = worker._verify_saved_imported_zones(board, PCBNEW, report)
    assert verified["zone_count"] == 1
    assert verified["total_zone_count"] == 2

    def allow_vias():
        return False

    board.zones[1].GetDoNotAllowVias = allow_vias
    with pytest.raises(ValueError, match="changed zone definitions"):
        worker._verify_saved_imported_zones(board, PCBNEW, report)


def test_multi_layer_zone_cannot_claim_complete_saved_fill_evidence():
    board = _Board()

    def get_layer_set():
        return _Seq([2, 4])

    board.zone.GetLayerSet = get_layer_set
    with pytest.raises(ValueError, match="single-layer copper zones"):
        worker._zone_fill_state(board, PCBNEW)
