from __future__ import annotations

import math
from types import SimpleNamespace

from circuit_weaver import _kicad_worker
from circuit_weaver.route_quality import evaluate_route_quality


def inventory(pads, tracks=(), vias=(), **extra):
    return {
        "status": "ok",
        "footprints": [
            {"ref": ref,
             "pads": [{"number": number, "net": net, "x": x, "y": y,
                       "copper_layers": layers, "enabled_copper_layers": layers}]}
             for ref, number, net, x, y, layers in pads
        ],
        "tracks": list(tracks),
        "vias": list(vias),
        "track_count": len(tracks) + len(vias),
        "zone_count": 0,
        "copper_layer_inventory_complete": True,
        "copper_layer_count": 2,
        "copper_layers": [{"id": 0, "name": "F.Cu"}, {"id": 2, "name": "B.Cu"}],
        "pad_connectivity_inventory_complete": True,
        **extra,
    }


def track(net, a, b, layer=0, kind="segment"):
    return {"net": net, "start_mm": a, "end_mm": b, "layer_id": layer, "kind": kind}


def route(net, start, end, **limits):
    return {"net": net, "from": start, "to": end, "source": "AUTHORED", **limits}


def test_legacy_planar_inventory_does_not_require_layer_transition_metadata():
    board = inventory(
        [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 4, 0, [0])],
        [track("CLK", [0, 0], [4, 0])],
    )
    for key in ("copper_layer_count", "copper_layers", "copper_layer_inventory_complete"):
        board.pop(key)
    for footprint in board["footprints"]:
        footprint["pads"][0].pop("enabled_copper_layers")
    specs = {"clock": route("CLK", {"ref": "J1", "pad": "1"}, {"ref": "U1", "pad": "1"})}
    assert evaluate_route_quality(board, {"routes": specs})["routes"]["clock"]["status"] == "verified"

    board["footprints"][0]["pads"][0]["copper_layers"] = [0, 2]
    assert evaluate_route_quality(board, {"routes": specs})["routes"]["clock"]["status"] == "unverified"


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
    vias = [{"net": "CLK", "x_mm": 2, "y_mm": 0, "layer_ids": [0, 2],
             "layer_span_complete": True, "kind": "via"}]
    measured = evaluate_route_quality(inventory(pads, tracks, vias), {
        "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                  {"ref": "U1", "pad": "1"}, max_vias=0)}
    })["routes"]["clock"]
    assert measured["status"] == "failed"
    assert measured["length_mm"] == 4
    assert measured["via_count"] == 1


def test_native_via_span_preserves_physical_stack_order_and_blind_spans():
    through = SimpleNamespace(TopLayer=lambda: 0, BottomLayer=lambda: 2)
    blind = SimpleNamespace(TopLayer=lambda: 0, BottomLayer=lambda: 6)
    buried = SimpleNamespace(TopLayer=lambda: 4, BottomLayer=lambda: 6)
    stack = [0, 4, 6, 2]
    assert _kicad_worker._native_via_layer_span(through, stack, True) == [0, 4, 6, 2]
    assert _kicad_worker._native_via_layer_span(through, [0, 2], True) == [0, 2]
    assert _kicad_worker._native_via_layer_span(blind, stack, True) == [0, 4, 6]
    assert _kicad_worker._native_via_layer_span(buried, stack, True) == [4, 6]
    assert _kicad_worker._native_via_layer_span(through, stack, False) is None
    assert _kicad_worker._native_via_layer_span(SimpleNamespace(TopLayer=lambda: 0, BottomLayer=lambda: 31),
                                                stack, True) is None


def test_native_pad_plating_uses_native_attribute_constants():
    pcbnew = SimpleNamespace(PAD_ATTRIB_PTH=0, PAD_ATTRIB_SMD=1,
                             PAD_ATTRIB_CONN=2, PAD_ATTRIB_NPTH=3)
    assert _kicad_worker._native_pad_is_plated_through(
        SimpleNamespace(GetAttribute=lambda: 0), pcbnew
    ) is True
    for value in (1, 2, 3):
        assert _kicad_worker._native_pad_is_plated_through(
            SimpleNamespace(GetAttribute=lambda value=value: value), pcbnew
        ) is False
    assert _kicad_worker._native_pad_is_plated_through(
        SimpleNamespace(GetAttribute=lambda: 99), pcbnew
    ) is None


def test_native_pad_copper_layers_are_intersected_with_enabled_physical_stack():
    all_known_copper = list(range(0, 64, 2))
    enabled_stack = [0, 4, 6, 2]
    assert _kicad_worker._native_pad_enabled_copper_layers(
        all_known_copper, enabled_stack, True
    ) == [0, 2, 4, 6]
    assert _kicad_worker._native_pad_enabled_copper_layers(
        all_known_copper, enabled_stack, False
    ) is None


def test_four_layer_via_connects_inner_copper_and_counts_one_physical_via():
    stack = [{"id": layer, "name": name} for layer, name in
             ((0, "F.Cu"), (4, "In1.Cu"), (6, "In2.Cu"), (2, "B.Cu"))]
    vias = [{"net": "CLK", "x_mm": 2, "y_mm": 0, "layer_ids": [0, 4, 6, 2],
             "layer_span_complete": True, "kind": "via"}]
    for start_layer, end_layer in ((0, 6), (6, 2)):
        pads = [("J1", "1", "CLK", 0, 0, [start_layer]), ("U1", "1", "CLK", 4, 0, [end_layer])]
        tracks = [track("CLK", [0, 0], [2, 0], start_layer), track("CLK", [2, 0], [4, 0], end_layer)]
        board = inventory(pads, tracks, vias, copper_layer_count=4, copper_layers=stack)
        measured = evaluate_route_quality(board, {
            "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                       {"ref": "U1", "pad": "1"})}
        })["routes"]["clock"]
        assert measured["status"] == "verified"
        assert measured["length_mm"] == 4
        assert measured["via_count"] == 1


def test_via_span_must_be_complete_and_match_enabled_physical_stack():
    stack = [{"id": layer, "name": name} for layer, name in
             ((0, "F.Cu"), (4, "In1.Cu"), (6, "In2.Cu"), (2, "B.Cu"))]
    pads = [("J1", "1", "CLK", 0, 0, [0]), ("U1", "1", "CLK", 4, 0, [6])]
    tracks = [track("CLK", [0, 0], [2, 0], 0), track("CLK", [2, 0], [4, 0], 6)]
    for via in (
        {"net": "CLK", "x_mm": 2, "y_mm": 0, "layer_ids": [0, 2], "kind": "via",
         "layer_span_complete": True},
        {"net": "CLK", "x_mm": 2, "y_mm": 0, "layer_ids": [0, 2, 4, 6], "kind": "via",
         "layer_span_complete": True},
        {"net": "CLK", "x_mm": 2, "y_mm": 0, "layer_ids": [0, 4, 6, 2], "kind": "via",
         "layer_span_complete": False},
    ):
        board = inventory(pads, tracks, [via], copper_layer_count=4, copper_layers=stack)
        measured = evaluate_route_quality(board, {
            "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                       {"ref": "U1", "pad": "1"})}
        })["routes"]["clock"]
        assert measured["status"] == "unverified"
        assert measured["via_count"] is None
    via = {"net": "CLK", "x_mm": 2, "y_mm": 0, "layer_ids": [0, 4, 6, 2],
           "kind": "via", "layer_span_complete": True}
    board = inventory(pads, tracks, [via], copper_layer_count=4, copper_layers=stack,
                      copper_layer_inventory_complete=False)
    measured = evaluate_route_quality(board, {
        "routes": {"clock": route("CLK", {"ref": "J1", "pad": "1"},
                                   {"ref": "U1", "pad": "1"})}
    })["routes"]["clock"]
    assert measured["status"] == "unverified"
    assert measured["via_count"] is None


def test_native_four_layer_iot_routes_remain_connected_with_physical_via_spans():
    """Trimmed native regression geometry from the IoT inspection.

    Inspection SHA-256: 68240260889dd625a73009d62a10004fca605a7825670750c5ac6e742eea219a.

    Source board SHA-256: 8d8910b6bde3f3ae9de263213caaa51fa728d2f32333afcfdfdbe61cba6738cf.

    The original exporter reported [F.Cu, B.Cu] for all four through vias,
    hiding the actual In1/In2 signal tracks. This fixture records the physical
    enabled stack and corrected ordered spans expected from the worker.
    """
    stack = [{"id": layer, "name": name} for layer, name in
             ((0, "F.Cu"), (4, "In1.Cu"), (6, "In2.Cu"), (2, "B.Cu"))]
    pads = [
        ("U2", "10", "I2C_SCL", 27.3375, 19.75, [0]),
        ("U3", "4", "I2C_SCL", 44.7525, 31.1175, [0]),
        ("U2", "11", "I2C_SDA", 27.3375, 20.25, [0]),
        ("U3", "3", "I2C_SDA", 44.1025, 31.1175, [0]),
        ("U2", "34", "SWDIO", 35.6625, 16.25, [0]),
        ("J3", "1", "SWDIO", 37.5, 30.57, [0, 2, 4, 6]),
        ("U2", "37", "SWCLK", 34.25, 13.8375, [0]),
        ("J3", "2", "SWCLK", 37.5, 33.11, [0, 2, 4, 6]),
    ]
    # Full signal-net segments from the native worker inspection, represented
    # as (net, layer, start, end) to keep the evidence fixture auditable.
    segments = [
        ("I2C_SCL", 0, (26.3188,20.0745),(26.6433,19.75)),
        ("I2C_SCL", 0, (22.9399,27.1785),(26.7205,27.1785)),
        ("I2C_SCL", 0, (21.99,25.05),(21.4213,25.6187)),
        ("I2C_SCL", 0, (14.6188,25.6599),(21.4213,25.6599)),
        ("I2C_SCL", 0, (26.3188,21.7419),(26.3188,20.0745)),
        ("I2C_SCL", 0, (27.0,22.4231),(26.3188,21.7419)),
        ("I2C_SCL", 0, (26.7205,27.1785),(27.0,26.899)),
        ("I2C_SCL", 0, (21.4213,25.6187),(21.4213,25.6599)),
        ("I2C_SCL", 0, (26.6433,19.75),(27.3375,19.75)),
        ("I2C_SCL", 0, (27.0,26.899),(27.0,22.4231)),
        ("I2C_SCL", 0, (44.7525,30.3919),(44.7525,31.1175)),
        ("I2C_SCL", 0, (9.62,29.57),(10.7087,29.57)),
        ("I2C_SCL", 0, (10.7087,29.57),(14.6188,25.6599)),
        ("I2C_SCL", 0, (21.4213,25.6599),(22.9399,27.1785)),
        ("I2C_SCL", 6, (43.4242,29.0636),(11.2151,29.0636)),
        ("I2C_SCL", 6, (11.2151,29.0636),(10.7087,29.57)),
        ("I2C_SCL", 6, (44.7525,30.3919),(43.4242,29.0636)),
        ("I2C_SCL", 6, (9.62,29.57),(10.7087,29.57)),
        ("I2C_SDA", 0, (10.71,31.02),(9.62,32.11)),
        ("I2C_SDA", 0, (17.1622,29.4488),(15.591,31.02)),
        ("I2C_SDA", 0, (28.3544,20.5246),(28.3544,25.7056)),
        ("I2C_SDA", 0, (15.591,31.02),(10.71,31.02)),
        ("I2C_SDA", 0, (28.3544,25.7056),(27.51,26.55)),
        ("I2C_SDA", 0, (28.0798,20.25),(28.3544,20.5246)),
        ("I2C_SDA", 0, (27.3375,20.25),(28.0798,20.25)),
        ("I2C_SDA", 0, (24.9737,29.4488),(17.1622,29.4488)),
        ("I2C_SDA", 0, (44.1025,31.1175),(44.1025,30.6288)),
        ("I2C_SDA", 0, (27.51,26.9125),(24.9737,29.4488)),
        ("I2C_SDA", 0, (27.51,26.55),(27.51,26.9125)),
        ("I2C_SDA", 0, (43.9834,30.5097),(44.1025,30.6288)),
        ("I2C_SDA", 0, (43.9834,30.3912),(43.9834,30.5097)),
        ("I2C_SDA", 6, (17.1088,32.11),(19.7547,29.4641)),
        ("I2C_SDA", 6, (43.0563,29.4641),(43.9834,30.3912)),
        ("I2C_SDA", 6, (19.7547,29.4641),(43.0563,29.4641)),
        ("I2C_SDA", 6, (9.62,32.11),(17.1088,32.11)),
        ("SWDIO", 0, (35.6625,16.25),(36.3937,16.25)),
        ("SWDIO", 0, (36.9423,16.7986),(36.9423,21.6875)),
        ("SWDIO", 0, (36.3937,16.25),(36.9423,16.7986)),
        ("SWDIO", 4, (36.9423,21.6875),(36.9423,28.9236)),
        ("SWDIO", 4, (37.5,30.57),(37.5,29.4813)),
        ("SWDIO", 4, (36.9423,28.9236),(37.5,29.4813)),
        ("SWCLK", 0, (33.311,15.6592),(33.311,18.1861)),
        ("SWCLK", 0, (34.25,14.7202),(33.311,15.6592)),
        ("SWCLK", 0, (34.25,13.8375),(34.25,14.7202)),
        ("SWCLK", 4, (36.0472,20.9223),(36.0472,31.6572)),
        ("SWCLK", 4, (33.311,18.1861),(36.0472,20.9223)),
        ("SWCLK", 4, (36.0472,31.6572),(37.5,33.11)),
    ]
    tracks = [track(net, list(a), list(b), layer) for net, layer, a, b in segments]
    via_points = {
        "I2C_SCL": (44.7525, 30.3919),
        "I2C_SDA": (43.9834, 30.3912),
        "SWDIO": (36.9423, 21.6875),
        "SWCLK": (33.311, 18.1861),
    }
    vias = [{"net": net, "x_mm": point[0], "y_mm": point[1],
             "layer_ids": [0, 4, 6, 2], "layer_span_complete": True,
             "kind": "via"} for net, point in via_points.items()]
    board = inventory(pads, tracks, vias, zone_count=1, zone_nets=["GND"],
                      zone_net_inventory_complete=True,
                      copper_layer_count=4, copper_layers=stack)
    board["footprints"].append({"ref": "J2", "pads": [
        {"number": "1", "net": "I2C_SCL", "x": 9.62, "y": 29.57,
         "copper_layers": list(range(0, 64, 2)),
         "enabled_copper_layers": [0, 2, 4, 6], "plated_through": True},
        {"number": "2", "net": "I2C_SDA", "x": 9.62, "y": 32.11,
         "copper_layers": list(range(0, 64, 2)),
         "enabled_copper_layers": [0, 2, 4, 6], "plated_through": True},
    ]})
    specs = {
        "scl": route("I2C_SCL", {"ref": "U2", "pad": "10"}, {"ref": "U3", "pad": "4"}),
        "sda": route("I2C_SDA", {"ref": "U2", "pad": "11"}, {"ref": "U3", "pad": "3"}),
        "swdio": route("SWDIO", {"ref": "U2", "pad": "34"}, {"ref": "J3", "pad": "1"}),
        "swclk": route("SWCLK", {"ref": "U2", "pad": "37"}, {"ref": "J3", "pad": "2"}),
    }
    measured = evaluate_route_quality(board, {"routes": specs})["routes"]
    assert set(measured) == set(specs)
    assert all(row["status"] == "verified" for row in measured.values())
    assert all(row["via_count"] == 1 for row in measured.values())


def test_intermediate_plated_pad_bridges_layers_without_counting_a_via():
    stack = [{"id": layer, "name": name} for layer, name in
             ((0, "F.Cu"), (4, "In1.Cu"), (6, "In2.Cu"), (2, "B.Cu"))]
    pads = [("SRC", "1", "CLK", 0, 0, [0]), ("DST", "1", "CLK", 4, 0, [6])]
    tracks = [track("CLK", [0, 0], [2, 0], 0), track("CLK", [2, 0], [4, 0], 6)]
    board = inventory(pads, tracks, copper_layer_count=4, copper_layers=stack)
    board["footprints"].append({"ref": "J1", "pads": [
        {"number": "1", "net": "CLK", "x": 2, "y": 0,
         "copper_layers": list(range(0, 64, 2)),
         "enabled_copper_layers": [0, 2, 4, 6], "plated_through": True},
    ]})
    measured = evaluate_route_quality(board, {
        "routes": {"clock": route("CLK", {"ref": "SRC", "pad": "1"},
                                   {"ref": "DST", "pad": "1"})}
    })["routes"]["clock"]
    assert measured["status"] == "verified"
    assert measured["length_mm"] == 4
    assert measured["via_count"] == 0

    # A missing native plating verdict cannot be treated as either a bridge or
    # proof of disconnection: leave the route unverified.
    board["footprints"][2]["pads"][0].pop("plated_through")
    board["pad_connectivity_inventory_complete"] = False
    uncertain = evaluate_route_quality(board, {
        "routes": {"clock": route("CLK", {"ref": "SRC", "pad": "1"},
                                   {"ref": "DST", "pad": "1"})}
    })["routes"]["clock"]
    assert uncertain["status"] == "unverified"
    assert uncertain["via_count"] is None


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
