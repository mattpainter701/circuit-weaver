from __future__ import annotations

import math

from circuit_weaver.route_quality import evaluate_route_quality


def inventory(pads, tracks=(), vias=(), **extra):
    return {
        "status": "ok",
        "footprints": [
            {"ref": ref,
             "pads": [{"number": number, "net": net, "x": x, "y": y,
                       "copper_layers": layers}]}
             for ref, number, net, x, y, layers in pads
        ],
        "tracks": list(tracks),
        "vias": list(vias),
        "track_count": len(tracks) + len(vias),
        "zone_count": 0,
        **extra,
    }


def track(net, a, b, layer=0, kind="segment"):
    return {"net": net, "start_mm": a, "end_mm": b, "layer_id": layer, "kind": kind}


def route(net, start, end, **limits):
    return {"net": net, "from": start, "to": end, "source": "AUTHORED", **limits}


def test_disconnected_route_fails_and_branch_cycle_uses_shortest_centerline():
    pads = [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 10, 0, [0])]
    tracks = [
        track("CLK", [0, 0], [4, 0]), track("CLK", [4, 0], [8, 0]),
        track("CLK", [8, 0], [10, 0]),
        # A cycle/branch creates longer copper that must not be summed.
        track("CLK", [4, 0], [4, 3]), track("CLK", [4, 3], [8, 3]),
        track("CLK", [8, 3], [8, 0]),
    ]
    result = evaluate_route_quality(inventory(pads, tracks), {
        "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                  {"ref": "U1", "pad": "1"})}
    })
    measured = result["routes"]["clock"]
    assert measured["status"] == "verified"
    assert measured["length_mm"] == 10
    assert measured["via_count"] == 0

    disconnected = evaluate_route_quality(inventory(pads, [track("CLK", [0, 0], [4, 0]),
                                                           track("CLK", [7, 0], [10, 0])]), {
        "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                  {"ref": "U1", "pad": "1"})}
    })["routes"]["clock"]
    assert disconnected["status"] == "failed"

    missing_contact = evaluate_route_quality(
        inventory(pads, [track("CLK", [0, 0], [4, 0])]),
        {"routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                    {"ref": "U1", "pad": "1"})}},
    )["routes"]["clock"]
    assert missing_contact["status"] == "unverified"


def test_same_net_decoy_cannot_replace_explicit_route_or_add_length():
    pads = [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 4, 0, [0])]
    tracks = [track("CLK", [0, 0], [4, 0]), track("OTHER", [0, 0], [4, 0])]
    measured = evaluate_route_quality(inventory(pads, tracks), {
        "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                  {"ref": "U1", "pad": "1"})}
    })["routes"]["clock"]
    assert measured["status"] == "verified"
    assert measured["length_mm"] == 4


def test_via_transitions_are_counted_from_explicit_layer_span():
    pads = [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 4, 0, [2])]
    tracks = [track("CLK", [0, 0], [4, 0], 0), track("CLK", [2, -2], [2, 0], 2),
              track("CLK", [2, 0], [4, 0], 2)]
    vias = [{"net": "CLK", "x_mm": 2, "y_mm": 0, "layer_ids": [0, 2], "kind": "via"}]
    measured = evaluate_route_quality(inventory(pads, tracks, vias), {
        "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                  {"ref": "U1", "pad": "1"}, max_vias=0)}
    })["routes"]["clock"]
    assert measured["status"] == "failed"
    assert measured["length_mm"] == 4
    assert measured["via_count"] == 1


def test_differential_skew_is_checked_against_authored_limit():
    pads = [("P", "1", "CLK_P", 0, 0, [0]), ("P", "2", "CLK_N", 0, 2, [0]),
            ("N", "1", "CLK_P", 5, 0, [0]), ("N", "2", "CLK_N", 7, 2, [0])]
    tracks = [track("CLK_P", [0, 0], [5, 0]), track("CLK_N", [0, 2], [7, 2])]
    result = evaluate_route_quality(inventory(pads, tracks), {
        "routes": {
            "p": route("CLK_P", {"ref": "P", "pad": "1"}, {"ref": "N", "pad": "1"}),
            "n": route("CLK_N", {"ref": "P", "pad": "2"}, {"ref": "N", "pad": "2"}),
        },
        "pairs": [{"id": "clock", "positive_route": "p", "negative_route": "n",
                   "source": "AUTHORED", "max_skew_mm": 1}],
    })
    assert result["differential_pairs"] == [{
        "id": "clock", "status": "failed", "skew_mm": 2.0,
        "reason": "measured differential-pair skew exceeds AUTHORED limit",
    }]


def test_missing_model_plane_only_unknown_arc_and_invalid_limits_stay_unverified():
    pads = [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 1, 0, [0])]
    line = track("CLK", [0, 0], [1, 0])
    base = route("CLK", {"ref": "J1", "pad": "1"}, {"ref": "U1", "pad": "1"})
    cases = [
        (inventory(pads, [line]), {**base, "model": "unknown"}),
        (inventory(pads, [], zone_count=1), base),
        (inventory(pads, [track("CLK", [0, 0], [1, 0], kind="arc")]), base),
        (inventory(pads, [line]), {**base, "max_length_mm": True}),
        (inventory(pads, [line]), {**base, "max_length_mm": math.inf}),
        (inventory(pads, [line]), {**base, "max_vias": True}),
    ]
    for board, constraint in cases:
        measured = evaluate_route_quality(board, {"routes": {"clock": constraint}})["routes"]["clock"]
        assert measured["status"] == "unverified"
        assert measured["length_mm"] is None


def test_plane_only_net_does_not_appear_as_zero_length_route():
    pads = [("J1", "1", "GND", 0, 0, [0]), ("U1", "1", "GND", 5, 0, [0])]
    result = evaluate_route_quality(inventory(pads, [], zone_count=1), {
        "routes": {"return": route("GND", {"ref": "J1", "pad": "1"},
                                    {"ref": "U1", "pad": "1"})}
    })["routes"]["return"]
    assert result["status"] == "unverified"
    assert result["length_mm"] is None


def test_unrelated_plane_does_not_block_explicit_clock_centerline_measurement():
    pads = [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 2, 0, [0])]
    board = inventory(
        pads,
        [track("CLK", [0, 0], [2, 0])],
        zone_count=1,
        zone_nets=["GND"],
        zone_net_inventory_complete=True,
    )
    measured = evaluate_route_quality(board, {
        "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                   {"ref": "U1", "pad": "1"})}
    })["routes"]["clock"]
    assert measured["status"] == "verified"
    assert measured["length_mm"] == 2


def test_same_net_plane_and_unknown_zone_inventory_remain_unverified():
    pads = [("J1", "1", "GND", 0, 0, [0]), ("U1", "1", "GND", 2, 0, [0])]
    spec = route("GND", {"ref": "J1", "pad": "1"}, {"ref": "U1", "pad": "1"})
    same_net_zone = inventory(
        pads, [], zone_count=1, zone_nets=["GND"], zone_net_inventory_complete=True
    )
    unknown_zone_inventory = inventory(pads, [], zone_count=1)
    for board in (same_net_zone, unknown_zone_inventory):
        result = evaluate_route_quality(board, {"routes": {"return": spec}})["routes"]["return"]
        assert result["status"] == "unverified"
        assert result["length_mm"] is None


def test_inspection_failure_duplicate_pad_and_unknown_contract_fields_fail_closed():
    pads = [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 1, 0, [0])]
    board = inventory(pads, [track("CLK", [0, 0], [1, 0])])
    spec = route("CLK", {"ref": "J1", "pad": "1"}, {"ref": "U1", "pad": "1"})

    for status in (None, "error"):
        bad_board = {**board, "status": status}
        result = evaluate_route_quality(bad_board, {"routes": {"clock": spec}})["routes"]["clock"]
        assert result["status"] == "unverified"

    duplicate_pads = inventory(pads + [("J1", "1", "CLK", 0, 0, [0])],
                               [track("CLK", [0, 0], [1, 0])])
    duplicate = evaluate_route_quality(duplicate_pads, {"routes": {"clock": spec}})["routes"]["clock"]
    assert duplicate["status"] == "unverified"

    unknown_field = evaluate_route_quality(
        board, {"routes": {"clock": {**spec, "impedance_ohms": 50}}}
    )["routes"]["clock"]
    assert unknown_field["status"] == "unverified"

    boolean_schema_version = evaluate_route_quality(
        board, {"schema_version": True, "routes": {"clock": spec}}
    )["routes"]["clock"]
    assert boolean_schema_version["status"] == "unverified"

    malformed_pairs = evaluate_route_quality(board, {"pairs": None})["differential_pairs"]
    assert malformed_pairs == [{
        "id": None, "status": "unverified", "skew_mm": None,
        "reason": "differential-pair collection is malformed",
    }]


def test_oversized_same_net_graph_is_bounded_and_unverified():
    pads = [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 1, 0, [0])]
    tracks = [track("CLK", [float(i), 0], [float(i) + 0.5, 0]) for i in range(2001)]
    result = evaluate_route_quality(inventory(pads, tracks), {
        "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                  {"ref": "U1", "pad": "1"})}
    })["routes"]["clock"]
    assert result["status"] == "unverified"
    assert result["length_mm"] is None


def test_route_and_pair_selectors_require_nonempty_string_ids_without_coercion():
    pads = [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 1, 0, [0])]
    board = inventory(pads, [track("CLK", [0, 0], [1, 0])])
    spec = route("CLK", {"ref": "J1", "pad": "1"}, {"ref": "U1", "pad": "1"})

    result = evaluate_route_quality(board, {
        "routes": {1: spec, "1": spec, "None": spec},
        "pairs": [
            {"id": "bad-selector", "positive_route": None, "negative_route": "1",
             "source": "AUTHORED", "max_skew_mm": 0},
            {"id": 7, "positive_route": "None", "negative_route": "1",
             "source": "AUTHORED", "max_skew_mm": 0},
        ],
    })
    assert result["routes"]["1"]["status"] == "verified"
    invalid_routes = [row for key, row in result["routes"].items() if key != "1" and key != "None"]
    assert len(invalid_routes) == 1
    assert invalid_routes[0]["status"] == "unverified"
    assert result["differential_pairs"][0]["status"] == "unverified"
    assert "selectors" in result["differential_pairs"][0]["reason"]
    assert result["differential_pairs"][1]["status"] == "unverified"
    assert "ID" in result["differential_pairs"][1]["reason"]
