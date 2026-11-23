"""Check connected-copper identity, geometry, and shared search bounds."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from circuit_weaver import _native_routing_repair as kernel


class Item:
    def __init__(self, uid, item_class, net_code=7, net_name="SIG", *, layers=(0,)):
        self.uid = uid
        self.item_class = item_class
        self.net_code = net_code
        self.net_name = net_name
        self.layers = set(layers)
        self.m_Uuid = SimpleNamespace(AsString=lambda: self.uid)

    def GetClass(self):
        return self.item_class

    def GetNetCode(self):
        return self.net_code

    def GetNetname(self):
        return self.net_name

    def IsOnLayer(self, layer):
        return layer in self.layers


class Pad(Item):
    def __init__(self, uid, xy, **kwargs):
        super().__init__(uid, "PAD", **kwargs)
        self.position = xy

    def GetPosition(self):
        return SimpleNamespace(x=self.position[0], y=self.position[1])


class Track(Item):
    def __init__(self, uid, start, end, **kwargs):
        super().__init__(uid, "PCB_TRACK", **kwargs)
        self.start, self.end = start, end

    def GetStart(self):
        return SimpleNamespace(x=self.start[0], y=self.start[1])

    def GetEnd(self):
        return SimpleNamespace(x=self.end[0], y=self.end[1])


class Via(Item):
    def __init__(self, uid, xy, **kwargs):
        super().__init__(uid, "PCB_VIA", **kwargs)
        self.position = xy

    def GetPosition(self):
        return SimpleNamespace(x=self.position[0], y=self.position[1])


class Connectivity:
    def __init__(self, clusters):
        self.clusters = clusters
        self.calls = []

    def GetConnectedItems(self, seed):
        self.calls.append(seed.uid)
        return self.clusters[seed.uid]


class Board:
    def __init__(self, clusters, source_items=None):
        self.connectivity = Connectivity(clusters)
        self.source_items = source_items or {
            item.uid: item for cluster in clusters.values() for item in cluster
            if item.GetClass() != "ZONE"
        }

    def GetConnectivity(self):
        return self.connectivity


class Pcbnew:
    @staticmethod
    def ToMM(value):
        return value / 1_000_000


def _anchors(board, seed, opposite=(10.0, 0.0), *, limit=32):
    return kernel._connected_component_anchor_vertices(
        board, seed, board.source_items, 7, "SIG", [0, 1], Pcbnew, opposite, limit=limit,
    )


def test_cluster_anchor_collection_preserves_selected_item_and_enabled_layers():
    pad = Pad("pad-a", (0, 0), layers=(0,))
    stub = Track("track-a", (1_000_000, 0), (2_000_000, 0), layers=(0,))
    via = Via("via-a", (3_000_000, 0), layers=(0, 1))
    board = Board({"pad-a": [pad, stub, via]}, source_items={
        "pad-a": pad, "track-a": stub, "via-a": via,
    })

    anchors, cluster_ids = _anchors(board, pad)

    assert board.connectivity.calls == ["pad-a"]
    assert cluster_ids == {"pad-a", "track-a", "via-a"}
    assert {anchor["uuid"] for anchor in anchors} == cluster_ids
    assert {(anchor["uuid"], anchor["layer"]) for anchor in anchors if anchor["uuid"] == "via-a"} == {
        ("via-a", 0), ("via-a", 1),
    }


def test_anchor_enumeration_is_deterministic_bounded_and_keeps_selected_endpoint():
    seed = Track("seed", (99_000_000, 0), (100_000_000, 0))
    tracks = [Track(f"t-{i:02d}", (i * 1_000_000, 0), ((i + 1) * 1_000_000, 0)) for i in range(40)]
    cluster = [seed, *tracks]
    board = Board({"seed": cluster})

    first, ids1 = _anchors(board, seed, opposite=(0.0, 0.0), limit=32)
    second, ids2 = _anchors(board, seed, opposite=(0.0, 0.0), limit=32)

    assert len(first) == len(second) == 32
    assert ids1 == ids2
    assert first == second
    assert {anchor["uuid"] for anchor in first} & {"seed"} == {"seed"}


@pytest.mark.parametrize("bad_member", [
    Item("foreign", "PCB_TRACK", net_code=8, net_name="OTHER"),
    Item("zone", "ZONE"),
])
def test_cluster_rejects_foreign_net_or_zone_member(bad_member):
    seed = Pad("pad-a", (0, 0))
    board = Board({"pad-a": [seed, bad_member]})

    with pytest.raises(ValueError, match="foreign net|contains a zone"):
        _anchors(board, seed)


def test_cluster_adds_known_seed_omitted_from_native_query():
    seed = Pad("pad-a", (0, 0))
    other = Pad("pad-b", (1_000_000, 0))
    board = Board({"pad-a": [other]}, source_items={"pad-a": seed, "pad-b": other})

    anchors, cluster_ids = _anchors(board, seed)

    assert {anchor["uuid"] for anchor in anchors} == {"pad-a", "pad-b"}
    assert cluster_ids == {"pad-a", "pad-b"}


def test_empty_native_query_is_valid_for_isolated_selected_item():
    seed = Pad("pad-a", (0, 0))
    board = Board({"pad-a": []}, source_items={"pad-a": seed})

    anchors, cluster_ids = _anchors(board, seed)

    assert anchors == [{
        "uuid": "pad-a", "point": (0.0, 0.0), "layer": 0, "item_class": "PAD",
    }]
    assert cluster_ids == {"pad-a"}


def test_generic_connectivity_wrapper_uses_canonical_concrete_source_geometry():
    class ConnectedView:
        def __init__(self, uid):
            self.uid = uid
            self.m_Uuid = SimpleNamespace(AsString=lambda: self.uid)

        def GetClass(self):
            return "BOARD_CONNECTED_ITEM"

        def GetNetCode(self):
            return 7

        def GetNetname(self):
            return "SIG"

    pad = Pad("pad-a", (0, 0))
    track = Track("track-a", (1_000_000, 0), (2_000_000, 0))
    board = Board({"pad-a": [ConnectedView("pad-a"), ConnectedView("track-a")]},
                  source_items={"pad-a": pad, "track-a": track})

    anchors, cluster_ids = _anchors(board, pad)

    assert cluster_ids == {"pad-a", "track-a"}
    assert {anchor["uuid"] for anchor in anchors} == cluster_ids
    assert any(anchor["point"] == (1.0, 0.0) for anchor in anchors)


def test_cluster_rejects_query_uuid_missing_from_source_inventory():
    seed = Pad("pad-a", (0, 0))
    unknown = Pad("unknown", (1_000_000, 0))
    board = Board({"pad-a": [seed, unknown]}, source_items={"pad-a": seed})

    with pytest.raises(ValueError, match="unknown source UUID"):
        _anchors(board, seed)


def test_pair_choice_is_stable_and_prefers_same_layer_for_equal_distance():
    first = [
        {"uuid": "a-top", "point": (0.0, 0.0), "layer": 0},
        {"uuid": "a-back", "point": (0.0, 0.0), "layer": 1},
    ]
    last = [
        {"uuid": "b-top", "point": (2.0, 0.0), "layer": 0},
        {"uuid": "b-back", "point": (2.0, 0.0), "layer": 1},
    ]

    chosen = kernel._choose_connected_anchor_pair(first, last)
    again = kernel._choose_connected_anchor_pair(first, last)

    assert chosen == again
    assert chosen[0]["layer"] == chosen[1]["layer"]


def test_local_fine_escape_finds_checked_coarse_roots_when_nearest_connectors_are_blocked():
    anchor = {"uuid": "pad", "point": (0.12, 0.12), "layer": 0}
    lattice_origin = (0.0, 0.0)
    coarse_vertices = {(0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0)}

    def clear(a, b, layer):
        assert layer == 0
        return not (a == anchor["point"] and b in coarse_vertices)

    roots, expanded = kernel._connected_anchor_lattice_roots(
        anchor, lattice_origin, 1.0, [0], (-4.0, -4.0, 4.0, 4.0),
        clear, lambda _point: False, 250,
    )

    assert 0 < expanded <= 250
    assert roots
    for node, connector in roots.items():
        path = connector["connector_path"]
        assert path[0] == (anchor["point"], 0)
        assert path[-1] == ((node[0] * 1.0, node[1] * 1.0), 0)
        assert len(path) > 2
        assert all(clear(a[0], b[0], a[1]) for a, b in zip(path, path[1:]))


def test_isolated_pad_expands_beyond_exact_coarse_root_to_find_local_escape():
    anchor = {"uuid": "pad", "point": (0.0, 0.0), "layer": 0, "item_class": "PAD"}
    blocked_direct = {(0.0, 1.0), (1.0, 0.0), (1.0, 1.0)}

    def clear(a, b, layer):
        assert layer == 0
        return not (a == anchor["point"] and b in blocked_direct)

    roots, expanded = kernel._connected_anchor_lattice_roots(
        anchor, (0.0, 0.0), 1.0, [0], (-2.0, -2.0, 3.0, 3.0),
        clear, lambda _point: False, 300,
    )

    assert 0 < expanded <= 300
    assert (0, 0, 0) in roots  # Retain the exact endpoint as a valid root.
    assert any(node != (0, 0, 0) and len(row["connector_path"]) > 2 for node, row in roots.items())


def test_local_fine_escape_can_change_to_another_copper_layer_before_coarse_root():
    anchor = {"uuid": "pad", "point": (0.12, 0.12), "layer": 0}
    coarse_vertices = {(0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0)}

    def clear(a, b, layer):
        if a == anchor["point"] and b in coarse_vertices:
            return False
        if layer == 0:
            return max(a[0], b[0]) <= 0.51 and abs(a[1] - 0.12) < 1e-9 and abs(b[1] - 0.12) < 1e-9
        return layer == 2

    def via_allowed(point):
        return abs(point[0] - 0.495) < 1e-9 and abs(point[1] - 0.12) < 1e-9

    roots, expanded = kernel._connected_anchor_lattice_roots(
        anchor, (0.0, 0.0), 1.0, [0, 2], (-2.0, -2.0, 3.0, 3.0),
        clear, via_allowed, 2_000,
    )

    assert 0 < expanded <= 2_000
    back_root = next(
        row for node, row in roots.items()
        if node[2] == 1 and any(layer == 2 for _point, layer in row["connector_path"])
    )
    path = back_root["connector_path"]
    transitions = [(a, b) for a, b in zip(path, path[1:]) if a[1] != b[1]]
    assert len(transitions) == 1
    assert transitions[0][0][0] == transitions[0][1][0]
    assert via_allowed(transitions[0][0][0])


def test_connected_physical_reconstruction_retains_local_start_and_goal_escape_paths():
    start = {
        "uuid": "start", "point": (0.12, 0.12), "layer": 0,
        "connector_path": [((0.12, 0.12), 0), ((0.2, 0.2), 0), ((1.0, 0.0), 0)],
    }
    goal = {
        "uuid": "goal", "point": (4.12, 0.12), "layer": 0,
        "connector_path": [((4.12, 0.12), 0), ((4.2, 0.2), 0), ((4.0, 0.0), 0)],
    }

    physical = kernel._physical_connected_search_points(
        [(1, 0, 0), (2, 0, 0), (3, 0, 0)], 1.0, (0.0, 0.0), start, goal, [0],
    )

    assert physical == [
        ((0.12, 0.12), 0), ((0.2, 0.2), 0), ((1.0, 0.0), 0),
        ((2.0, 0.0), 0), ((3.0, 0.0), 0), ((4.0, 0.0), 0),
        ((4.2, 0.2), 0), ((4.12, 0.12), 0),
    ]


def test_multiroot_search_escapes_blocked_nearest_anchor_and_returns_used_anchors():
    starts = [
        {"uuid": "a-nearest-trapped", "point": (0.0, 0.0), "layer": 0},
        {"uuid": "b-escape-track", "point": (0.0, 2.0), "layer": 0},
    ]
    goals = [{"uuid": "goal", "point": (5.0, 2.0), "layer": 0}]

    def clear(a, b, layer):
        assert layer == 0
        return a != (0.0, 0.0) and b != (0.0, 0.0)

    result = kernel._bidirectional_connected_anchor_search(
        starts, goals, [0], (-1.0, -1.0, 6.0, 4.0), 1.0, 100,
        clear, lambda _point: False,
    )
    path, expanded, start_anchor, goal_anchor, origin = result

    assert path
    assert expanded <= 100
    assert start_anchor["uuid"] == "b-escape-track"
    assert goal_anchor["uuid"] == "goal"
    assert origin == (0.0, 0.0)
    physical = kernel._physical_connected_search_points(path, 1.0, origin,
                                                        start_anchor, goal_anchor, [0])
    assert physical[0] == ((0.0, 2.0), 0)
    assert physical[-1] == ((5.0, 2.0), 0)


def test_multiroot_search_also_skips_blocked_nearest_goal_anchor():
    starts = [{"uuid": "start", "point": (0.0, 2.0), "layer": 0}]
    goals = [
        {"uuid": "a-nearest-trapped", "point": (5.0, 0.0), "layer": 0},
        {"uuid": "b-escape-track", "point": (5.0, 2.0), "layer": 0},
    ]

    def clear(a, b, layer):
        assert layer == 0
        return a != (5.0, 0.0) and b != (5.0, 0.0)

    path, expanded, start_anchor, goal_anchor, origin = kernel._bidirectional_connected_anchor_search(
        starts, goals, [0], (-1.0, -1.0, 6.0, 4.0), 1.0, 100,
        clear, lambda _point: False,
    )

    assert path
    assert expanded <= 100
    assert start_anchor["uuid"] == "start"
    assert goal_anchor["uuid"] == "b-escape-track"
    physical = kernel._physical_connected_search_points(path, 1.0, origin,
                                                        start_anchor, goal_anchor, [0])
    assert physical[0] == ((0.0, 2.0), 0)
    assert physical[-1] == ((5.0, 2.0), 0)


def test_multiroot_physical_choice_is_stable_when_uuids_are_renamed():
    def run(starts, goals):
        def clear(a, b, layer):
            assert layer == 0
            return a != (0.0, 0.0) and b != (0.0, 0.0)

        result = kernel._bidirectional_connected_anchor_search(
            starts, goals, [0], (-1.0, -1.0, 6.0, 4.0), 1.0, 100,
            clear, lambda _point: False,
        )
        path, expanded, start_anchor, goal_anchor, origin = result
        physical = kernel._physical_connected_search_points(
            path, 1.0, origin, start_anchor, goal_anchor, [0],
        )
        return path, expanded, origin, physical, start_anchor["uuid"], goal_anchor["uuid"]

    first = run(
        [{"uuid": "a-nearest", "point": (0.0, 0.0), "layer": 0},
         {"uuid": "z-escape", "point": (0.0, 2.0), "layer": 0}],
        [{"uuid": "z-goal", "point": (5.0, 2.0), "layer": 0}],
    )
    renamed = run(
        [{"uuid": "z-nearest", "point": (0.0, 0.0), "layer": 0},
         {"uuid": "a-escape", "point": (0.0, 2.0), "layer": 0}],
        [{"uuid": "a-goal", "point": (5.0, 2.0), "layer": 0}],
    )

    assert first[:4] == renamed[:4]
    assert first[4:] != renamed[4:]


def test_multiroot_search_is_deterministic_and_uses_one_shared_state_cap():
    starts = [
        {"uuid": "start-b", "point": (0.25, 0.0), "layer": 0},
        {"uuid": "start-a", "point": (0.0, 0.0), "layer": 0},
    ]
    goals = [{"uuid": "goal", "point": (4.0, 0.0), "layer": 0}]
    legal_segments = []

    def clear(a, b, layer):
        legal_segments.append((a, b, layer))
        return True

    args = (starts, goals, [0], (-1.0, -1.0, 5.0, 2.0), 1.0, 1, clear, lambda _point: False)
    first = kernel._bidirectional_connected_anchor_search(*args, grid_phase=(0.5, 0.0))
    second = kernel._bidirectional_connected_anchor_search(*args, grid_phase=(0.5, 0.0))

    assert first == second
    assert first[1] <= 1
    assert any(a != b for a, b, _layer in legal_segments)


@pytest.mark.parametrize("bad_anchor", [
    {"uuid": "", "point": (0.0, 0.0), "layer": 0},
    {"uuid": "s", "point": (float("nan"), 0.0), "layer": 0},
    {"uuid": "s", "point": (0.0, 0.0), "layer": True},
    {"uuid": "s", "point": (0.0, 0.0), "layer": 9},
])
def test_multiroot_search_rejects_invalid_anchor_identity_geometry_or_layer(bad_anchor):
    with pytest.raises(ValueError, match="anchor"):
        kernel._bidirectional_connected_anchor_search(
            [bad_anchor], [{"uuid": "g", "point": (2.0, 0.0), "layer": 0}],
            [0], (-1.0, -1.0, 3.0, 1.0), 1.0, 10,
            lambda _a, _b, _layer: True, lambda _point: False,
        )


def test_multiroot_connector_budget_reserves_escape_for_goal_after_two_start_anchors(monkeypatch):
    requests = []
    lattice_origin = (0.25, 0.0)
    node_by_uuid = {"start-a": (0, 0, 0), "start-b": (0, 1, 0), "goal": (4, 0, 0)}

    def fake_roots(anchor, origin, step, stack, bounds, clear_segment, can_change_layer,
                   state_budget, **_kwargs):
        requests.append((anchor["uuid"], state_budget))
        if state_budget <= 0:
            return {}, 0
        node = node_by_uuid[anchor["uuid"]]
        target = (origin[0] + node[0] * step, origin[1] + node[1] * step)
        return {
            node: {
                "anchor": anchor,
                "cost": 0.0,
                "connector_path": [(anchor["point"], stack[0]), (target, stack[0])],
            },
        }, min(4_000, state_budget)

    monkeypatch.setattr(kernel, "_connected_anchor_lattice_roots", fake_roots)
    path, expanded, start_anchor, goal_anchor, _origin = kernel._bidirectional_connected_anchor_search(
        [
            {"uuid": "start-a", "point": lattice_origin, "layer": 0},
            {"uuid": "start-b", "point": (0.25, 1.0), "layer": 0},
        ],
        [{"uuid": "goal", "point": (4.25, 0.0), "layer": 0}],
        [0], (-2.0, -2.0, 8.0, 8.0), 1.0, 32_000,
        lambda *_args: True, lambda _point: False,
    )

    assert path
    assert start_anchor["uuid"] == "start-a"
    assert goal_anchor["uuid"] == "goal"
    assert requests[:3] == [("start-a", 4_000), ("start-b", 0), ("goal", 4_000)]
    assert expanded <= 32_000


def test_multiroot_connector_order_prefers_distinct_xy_before_duplicate_layer(monkeypatch):
    calls = []

    def fake_roots(anchor, *_args, **_kwargs):
        calls.append(anchor["uuid"])
        return {}, 0

    monkeypatch.setattr(kernel, "_connected_anchor_lattice_roots", fake_roots)
    kernel._bidirectional_connected_anchor_search(
        [
            {"uuid": "pad-front", "point": (0.25, 0.0), "layer": 0},
            {"uuid": "pad-back", "point": (0.25, 0.0), "layer": 1},
            {"uuid": "track-end", "point": (0.25, 1.0), "layer": 0},
        ],
        [{"uuid": "goal", "point": (4.25, 0.0), "layer": 0}],
        [0, 1], (-2.0, -2.0, 8.0, 8.0), 1.0, 32_000,
        lambda *_args: True, lambda _point: False,
    )

    assert calls[:3] == ["pad-front", "track-end", "pad-back"]
