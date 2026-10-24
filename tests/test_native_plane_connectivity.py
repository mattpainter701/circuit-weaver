"""Fail-closed tests for native plane-to-pad connectivity checks."""
from types import SimpleNamespace

import pytest

from circuit_weaver import _native_ground_plane as ground


class Item:
    def __init__(self, uuid, cls="PAD", net="GND", *, connected=True):
        self.uuid = uuid
        self.m_Uuid = SimpleNamespace(AsString=lambda: self.uuid)
        self.cls = cls
        self.net = net
        self.connected = connected

    def GetClass(self):
        return self.cls

    def GetNetname(self):
        return self.net

    def IsConnected(self):
        # This only represents BOARD_CONNECTED_ITEM type membership in KiCad.
        return self.connected


class Pad(Item):
    def __init__(self, uuid, ref, number, net="GND", *, connected=True):
        super().__init__(uuid, "PAD", net, connected=connected)
        self.ref, self.number = ref, number

    def GetParentFootprint(self):
        return SimpleNamespace(GetReference=lambda: self.ref)

    def GetNumber(self):
        return self.number


class Zone(Item):
    def __init__(self, uuid, net="GND", layer="In1.Cu", *, rule=False, filled=True, polys=True):
        super().__init__(uuid, "ZONE", net)
        self.layer, self.rule, self.filled, self.polys = layer, rule, filled, polys

    def GetIsRuleArea(self):
        return self.rule

    def GetLayerName(self):
        return self.layer

    def IsFilled(self):
        return self.filled

    def HasFilledPolysForLayer(self, _layer):
        return self.polys

    def GetLayer(self):
        return {"F.Cu": 0, "In1.Cu": 4, "In2.Cu": 6, "B.Cu": 2, "F.SilkS": 5}[self.layer]


class Footprint:
    def __init__(self, ref, pads):
        self.ref, self.pads = ref, pads

    def GetReference(self):
        return self.ref

    def Pads(self):
        return self.pads


class Connectivity:
    def __init__(self, clusters):
        self.clusters = clusters
        self.calls = []

    def GetConnectedItems(self, pad):
        self.calls.append(pad.uuid)
        return self.clusters.get(pad.uuid, [pad])


class Board:
    def __init__(self, pads, zones, clusters):
        self.pads, self.zones = pads, zones
        self.connectivity = Connectivity(clusters)
        self.build_count = 0

    def GetFootprints(self):
        grouped = {}
        for pad in self.pads:
            grouped.setdefault(pad.ref, []).append(pad)
        return [Footprint(ref, pads) for ref, pads in grouped.items()]

    def Zones(self):
        return self.zones

    def GetConnectivity(self):
        return self.connectivity

    def GetLayerName(self, layer):
        return {0: "F.Cu", 4: "In1.Cu", 6: "In2.Cu", 2: "B.Cu", 5: "F.SilkS"}[layer]

    def BuildConnectivity(self):
        self.build_count += 1


def pending(board, nets=("GND",), *, rebuild=True):
    return ground._unconnected_plane_pads_by_net(board, nets, rebuild=rebuild)


def test_isconnected_true_isolated_pad_is_still_pending():
    pad = Pad("pad-a", "J1", "1", connected=True)
    zone = Zone("zone-gnd")
    board = Board([pad], [zone], {pad.uuid: [pad]})

    assert pending(board) == {"GND": ["J1.1"]}
    assert board.build_count == 1


def test_same_net_local_pad_pair_does_not_count_without_zone_in_component():
    first, second = Pad("p1", "J1", "1"), Pad("p2", "J2", "1")
    zone = Zone("zone-gnd")
    board = Board([first, second], [zone], {
        first.uuid: [first, second], second.uuid: [first, second],
    })

    assert pending(board) == {"GND": ["J1.1", "J2.1"]}


def test_transitive_track_via_component_reaching_selected_zone_is_connected():
    pad = Pad("pad-a", "J1", "1")
    via = Item("via-a", "PCB_VIA")
    track = Item("track-a", "PCB_TRACK")
    zone = Zone("zone-gnd")
    board = Board([pad], [zone], {pad.uuid: [pad, track, via, zone]})

    assert pending(board) == {"GND": []}


@pytest.mark.parametrize("zone", [
    Zone("wrong-net", net="VCC"),
    Zone("rule-area", rule=True),
    Zone("non-copper", layer="F.SilkS"),
    Zone("unfilled", filled=False),
    Zone("no-polys", polys=False),
])
def test_wrong_net_keepout_non_copper_or_unfilled_zone_cannot_anchor(zone):
    pad = Pad("pad-a", "J1", "1")
    board = Board([pad], [zone], {pad.uuid: [pad, zone]})

    assert pending(board) == {"GND": ["J1.1"]}


def test_other_net_pad_is_ignored_and_requested_nets_are_seeded():
    gnd = Pad("gnd", "J1", "1", "GND")
    vcc = Pad("vcc", "J1", "2", "VCC")
    board = Board([gnd, vcc], [Zone("zone-gnd")], {gnd.uuid: [gnd]})

    assert pending(board, ("GND", "EMPTY")) == {"GND": ["J1.1"], "EMPTY": []}
    assert pending(board, ("VCC",)) == {"VCC": ["J1.2"]}


@pytest.mark.parametrize("bad_uuid", [None, ""])
@pytest.mark.parametrize("missing_item", ["pad", "zone"])
def test_missing_pad_or_zone_uuid_fails_closed(bad_uuid, missing_item):
    pad = Pad("pad-a", "J1", "1")
    zone = Zone("zone-gnd")
    board = Board([pad], [zone], {pad.uuid: [pad, zone]})
    item = pad if missing_item == "pad" else zone
    item.m_Uuid = SimpleNamespace(AsString=lambda: bad_uuid)

    with pytest.raises(ValueError, match="UUID"):
        pending(board)


def test_missing_native_cluster_query_fails_closed():
    pad = Pad("pad-a", "J1", "1")
    board = Board([pad], [Zone("zone-gnd")], {})
    board.connectivity.GetConnectedItems = None

    with pytest.raises(ValueError, match="transitive connectivity"):
        pending(board)


def test_connectivity_is_built_once_and_connected_pad_component_is_cached():
    first, second = Pad("p1", "J1", "1"), Pad("p2", "J2", "1")
    zone = Zone("zone-gnd")
    component = [first, second, zone]
    board = Board([first, second], [zone], {first.uuid: component, second.uuid: component})

    assert pending(board) == {"GND": []}
    assert board.build_count == 1
    assert board.connectivity.calls == ["p1"]


def test_repeated_logical_pad_members_are_counted_as_distinct_connected_pads():
    first = Pad("shield-tab-1", "J1", "SH")
    second = Pad("shield-tab-2", "J1", "SH")
    zone = Zone("zone-gnd")
    cluster = [first, second, zone]
    board = Board([first, second], [zone], {first.uuid: cluster, second.uuid: cluster})

    assert pending(board) == {"GND": []}
    # One cluster query is enough, but both UUIDs are retained as physical members.
    assert board.connectivity.calls == ["shield-tab-1"]


def test_unattached_duplicate_physical_pad_remains_pending_when_sibling_is_attached():
    attached = Pad("shield-tab-1", "J1", "SH")
    floating = Pad("shield-tab-2", "J1", "SH")
    zone = Zone("zone-gnd")
    board = Board([attached, floating], [zone], {
        attached.uuid: [attached, zone], floating.uuid: [floating],
    })

    assert pending(board) == {"GND": ["J1.SH[shield-tab-2]"]}
    assert board.connectivity.calls == ["shield-tab-1", "shield-tab-2"]


def test_repeated_logical_pad_with_conflicting_nets_fails_closed():
    gnd = Pad("shield-gnd", "J1", "SH", "GND")
    vcc = Pad("shield-vcc", "J1", "SH", "VCC")
    board = Board([gnd, vcc], [Zone("zone-gnd")], {})

    with pytest.raises(ValueError, match="conflicting assigned nets"):
        pending(board)


def test_distinct_footprints_with_duplicate_references_remain_invalid():
    first = Pad("first-pad", "J1", "1")
    second = Pad("second-pad", "J1", "2")
    board = Board([first, second], [Zone("zone-gnd")], {})
    board.GetFootprints = lambda: [Footprint("J1", [first]), Footprint("J1", [second])]

    with pytest.raises(ValueError, match="duplicate footprint reference J1"):
        pending(board)


@pytest.mark.parametrize("uuids", [("same", "same"), ("", "second")])
def test_repeated_logical_pad_with_nonunique_or_missing_uuid_fails_closed(uuids):
    first = Pad(uuids[0], "J1", "SH")
    second = Pad(uuids[1], "J1", "SH")
    board = Board([first, second], [Zone("zone-gnd")], {})

    with pytest.raises(ValueError, match="UUID|identities are not unique"):
        pending(board)


def test_rebuild_false_does_not_rebuild_connectivity():
    pad = Pad("pad-a", "J1", "1")
    zone = Zone("zone-gnd")
    board = Board([pad], [zone], {pad.uuid: [pad, zone]})

    assert pending(board, rebuild=False) == {"GND": []}
    assert board.build_count == 0


@pytest.mark.parametrize("actual_layer,convenience_name,expected", [
    ("In1.Cu", "F.Fab", []),
    ("F.SilkS", "F.Cu", ["J1.1"]),
])
def test_plane_filter_uses_native_board_layer_identity(actual_layer, convenience_name, expected):
    pad = Pad("pad-a", "J1", "1")
    zone = Zone("zone-gnd", layer=actual_layer)
    zone.GetLayerName = lambda: convenience_name
    board = Board([pad], [zone], {pad.uuid: [pad, zone]})

    assert pending(board) == {"GND": expected}
