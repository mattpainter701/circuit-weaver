"""Contract-level tests for the pure four-layer acceptance corpus."""

from __future__ import annotations

from benchmarks.four_layer_acceptance.corpus import build_cases


def test_corpus_is_deterministic_and_has_new_connected_family_coverage():
    first = build_cases()
    second = build_cases()

    assert first == second
    assert len(first) >= 8
    assert len({case["case_id"] for case in first}) == len(first)
    assert len({case["family"] for case in first}) >= 5
    assert {case["family"] for case in first} >= {
        "iot_sensor_controller",
        "serialized_relay_output",
        "analog_setpoint_output",
        "fieldbus_gateway",
        "precision_acquisition",
        "clock_distribution_control",
    }


def test_each_case_declares_four_layer_plane_and_bounded_manifest_target():
    for case in build_cases():
        assert case["copper_layers"] == 4
        assert case["prototype_profile"]["clearance_mm"] == 0.127
        assert case["prototype_profile"]["minimum_drill_mm"] == 0.2
        assert case["ground_plane"]["net"] == "GND"
        assert case["ground_plane"]["layers"] in (["In1.Cu"], ["In1.Cu", "In2.Cu"])
        assert 25 <= case["component_count_target"] < 500
        assert case["expectations"]["board_constraints"]["width_mm"] > 0
        assert case["expectations"]["board_constraints"]["height_mm"] > 0
        assert case["expectations"]["required_nets"]
        assert case["expectations"]["required_endpoints"]
        assert case["expectations"]["required_components"]
        assert case["expectations"]["required_library_parts"]
        assert (
            case["declared_limits"]["declared"]["input_voltage_v"][0]
            < case["declared_limits"]["declared"]["input_voltage_v"][1]
        )
        assert (
            case["declared_limits"]["declared"]["ambient_temperature_c"][1]
            < case["declared_limits"]["declared"]["max_board_temperature_c"]
        )


def test_endpoint_expectations_name_real_nets_and_include_connected_pairs():
    for case in build_cases():
        nets = set(case["expectations"]["required_nets"])
        endpoint_nets = [endpoint["net"] for endpoint in case["expectations"]["required_endpoints"]]
        assert set(endpoint_nets) <= nets
        assert any(endpoint_nets.count(net) >= 2 for net in nets)
        assert all(endpoint["ref"] and endpoint["pad"] for endpoint in case["expectations"]["required_endpoints"])


def test_negative_controls_are_actionable_and_ground_bootstrap_case_is_explicit():
    cases = build_cases()
    control_kinds = {control["kind"] for case in cases for control in case["controls"]}
    assert {"connectivity", "library", "stackup", "limits"} <= control_kinds
    for case in cases:
        for control in case["controls"]:
            if control["kind"] == "connectivity":
                mutation = control["mutation"]
                assert mutation["source"] != mutation["endpoint"]
                assert mutation["replace_net"].endswith("_WRONG")
            if control["kind"] == "limits":
                assert control["expected_result"] == "report_as_unverified"

    bootstrap = next(case for case in cases if case["case_id"] == "iot_environment_node_ground_anchorless")
    mechanical = bootstrap["expectations"]["mechanical_requirements"]
    assert mechanical["plated_through_hole_gnd_anchors"] == 0
    assert "POWER_RETURN" in bootstrap["expectations"]["required_nets"]


def test_dual_inner_ground_and_reused_controls_are_labeled():
    cases = build_cases()
    assert any(case["ground_plane"]["layers"] == ["In1.Cu", "In2.Cu"] for case in cases)
    assert any(case["comparison_fixture_reuse"] for case in cases)
    assert not all(case["comparison_fixture_reuse"] for case in cases)
    assert any(case["case_id"] == "sdr_clock_carrier_control" and case["comparison_fixture_reuse"] for case in cases)


def test_generated_filter_supports_are_described_by_semantic_ownership():
    cases = {case["case_id"]: case for case in build_cases()}
    for case_id, owner, role, first_net, second_net in (
        ("analog_setpoint_24", "U30", "output_filter", "VOUTA_RAW_U30", "SETPOINT_01"),
        ("analog_setpoint_80", "U30", "output_filter", "VOUTA_RAW_U30", "SETPOINT_01"),
        ("precision_acquisition_control", "U5", "input_filter", "CH0_U5", "FILT_CH0_U5"),
    ):
        supports = cases[case_id]["expectations"]["required_supports"]
        assert any(
            item
            == {
                "owner_ref": owner,
                "role": role,
                "source_kind": "strap",
                "net1": first_net,
                "net2": second_net,
            }
            for item in supports
        )

    for case_id, owner, role, signal in (
        ("analog_setpoint_24", "U30", "output_filter", "SETPOINT_01"),
        ("analog_setpoint_80", "U30", "output_filter", "SETPOINT_01"),
        ("precision_acquisition_control", "U5", "input_filter", "FILT_CH0_U5"),
    ):
        supports = cases[case_id]["expectations"]["required_supports"]
        assert any(
            item
            == {
                "owner_ref": owner,
                "role": role,
                "source_kind": "bypass",
                "net1": signal,
                "net2": "GND",
            }
            for item in supports
        )
