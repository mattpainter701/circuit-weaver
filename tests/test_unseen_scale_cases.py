from __future__ import annotations

from pathlib import Path

from benchmarks.unseen_scale.build_stress_fixtures import (
    _relay_paths,
    build_serial_relay_output_card,
    build_specs,
    build_suite,
)
from circuit_weaver.assembly_manifest import build_assembly_manifest
from circuit_weaver.design_loader import compile_design_ir


def _load_suite():
    suite = build_suite()
    generated = build_specs()
    return suite, {
        case["id"]: generated[Path(case["spec"]).name]
        for case in suite["cases"]
    }


def _physical_count(spec):
    compiled = compile_design_ir(spec)
    return len(compiled.components) + sum(
        len(component.bypass_caps) + len(component.straps) for component in compiled.components
    )


def _pad_nets(spec):
    compiled = compile_design_ir(spec)
    nets = {
        (component.source_ref, str(pad)): net
        for component in compiled.components
        for pad, net in {**component.power_pins, **component.pin_nets}.items()
    }
    manifest = build_assembly_manifest(compiled.components)
    for item in manifest.items:
        if item.source_kind == "strap":
            nets[(item.reference, "1")] = item.net1
            nets[(item.reference, "2")] = item.net2
        elif item.source_kind == "bypass":
            # Generated capacitor symbols are reversed in the emitted netlist.
            nets[(item.reference, "1")] = item.net2
            nets[(item.reference, "2")] = item.net1
    return nets


def test_unseen_suite_covers_three_distinct_scale_archetypes():
    suite, specs = _load_suite()
    cases = suite["cases"]
    assert len(cases) == 3
    assert len({case["family"] for case in cases}) == 3
    assert [case["minimum_physical_parts"] for case in cases] == [180, 250, 350]
    assert all(case["minimum_physical_parts"] > 155 for case in cases)
    assert set(specs) == {
        "distributed_facility_gateway",
        "serial_relay_output_card",
        "multichannel_analog_setpoint_card",
    }


def test_all_required_path_endpoints_are_real_connected_pads():
    suite, specs = _load_suite()
    for case in suite["cases"]:
        nets = _pad_nets(specs[case["id"]])
        paths = case["required_paths"]
        assert paths, case["id"]
        for path in paths:
            assert len(path["segments"]) >= 1
            assert len(path["through"]) == len(path["segments"]) - 1
            for segment in path["segments"]:
                refs = set()
                for endpoint in segment["endpoints"]:
                    key = (endpoint["ref"], str(endpoint["pad"]))
                    refs.add(endpoint["ref"])
                    assert nets.get(key) == segment["net"], (case["id"], path["id"], key, segment["net"])
                assert len(refs) >= 2, (case["id"], path["id"])
            assert (path["source"]["ref"], str(path["source"]["pad"])) in {
                (endpoint["ref"], str(endpoint["pad"])) for endpoint in path["segments"][0]["endpoints"]
            }
            final_endpoints = {
                (endpoint["ref"], str(endpoint["pad"])) for endpoint in path["segments"][-1]["endpoints"]
            }
            assert all((load["ref"], str(load["pad"])) in final_endpoints for load in path["loads"])
            for index, bridge in enumerate(path["through"]):
                left = path["segments"][index]["endpoints"]
                right = path["segments"][index + 1]["endpoints"]
                assert (bridge["ref"], str(bridge["input_pad"])) in {
                    (endpoint["ref"], str(endpoint["pad"])) for endpoint in left
                }
                assert (bridge["ref"], str(bridge["output_pad"])) in {
                    (endpoint["ref"], str(endpoint["pad"])) for endpoint in right
                }


def test_gateway_retains_all_eight_analog_frontend_to_adc_channels():
    suite, specs = _load_suite()
    case = next(case for case in suite["cases"] if case["id"] == "distributed_facility_gateway")
    spec = specs[case["id"]]
    nets = _pad_nets(spec)
    channels = case["required_channels"]
    assert len(channels) == 8
    manifest = build_assembly_manifest(compile_design_ir(spec).components)
    filters = {
        item.owner_pin: item
        for item in manifest.items
        if item.source_kind == "strap" and item.owner_ref == "U8" and item.owner_pin.startswith("RFILT_CH")
    }
    for channel in channels:
        channel_index = channel["adc_channel"]
        filter_item = filters[f"RFILT_CH{channel_index}"]
        assert channel["adc_filter_ref"] == filter_item.reference
        assert filter_item.net1 == channel["frontend_out_net"]
        assert filter_item.net2 == channel["adc_net"]
        assert nets[(channel["input_connector_ref"], str(channel["input_positive_pad"]))] == channel["sensor_p_net"]
        assert nets[(channel["input_connector_ref"], str(channel["input_negative_pad"]))] == channel["sensor_n_net"]
        assert nets[(channel["frontend_ref"], str(channel["frontend_p_pad"]))] == channel["sensor_p_net"]
        assert nets[(channel["frontend_ref"], str(channel["frontend_n_pad"]))] == channel["sensor_n_net"]
        assert nets[(channel["frontend_ref"], str(channel["frontend_ref_pad"]))] == channel["reference_net"]
        assert nets[(channel["reference_ref"], str(channel["reference_output_pad"]))] == channel["reference_net"]
        assert nets[(channel["frontend_ref"], str(channel["frontend_out_pad"]))] == channel["frontend_out_net"]
        assert nets[(channel["adc_filter_ref"], str(channel["adc_filter_input_pad"]))] == channel["frontend_out_net"]
        assert nets[(channel["adc_filter_ref"], str(channel["adc_filter_output_pad"]))] == channel["adc_net"]
        assert nets[(channel["adc_ref"], str(channel["adc_pad"]))] == channel["adc_net"]
        providers = [block for block in spec.get("analog", []) if block.get("ref") == channel["reference_ref"]]
        assert len(providers) == 1
        assert any(
            interface.get("name") == channel["reference_net"] for interface in providers[0].get("interfaces", [])
        )


def test_analog_contracts_use_compiled_support_ownership_and_pad_convention():
    suite, specs = _load_suite()
    case = next(item for item in suite["cases"] if item["id"] == "multichannel_analog_setpoint_card")
    generated_case = next(item for item in build_suite()["cases"] if item["id"] == case["id"])
    assert case["required_paths"] == generated_case["required_paths"]
    spec = specs[case["id"]]
    manifest = build_assembly_manifest(compile_design_ir(spec).components)
    output_caps = {
        (item.owner_ref, item.net1): item.reference
        for item in manifest.items
        if item.source_kind == "bypass" and item.role == "output_filter"
    }
    output_paths = [path for path in case["required_paths"] if path["id"].startswith("setpoint_output_")]
    assert len(output_paths) == 72
    assert all(len(path.get("return_assertions", [])) == 1 for path in output_paths)
    assert all(path["return_assertions"][0]["net"] == "GND" for path in output_paths)
    for path in output_paths:
        signal = path["segments"][-1]["net"]
        capacitor = next(endpoint for endpoint in path["segments"][-1]["endpoints"] if endpoint["ref"].startswith("C"))
        owner = next(item.owner_ref for item in manifest.items if item.reference == capacitor["ref"])
        assert output_caps[(owner, signal)] == capacitor["ref"]
        assert capacitor["pad"] == "2"


def test_expanded_physical_counts_clear_each_case_floor():
    suite, specs = _load_suite()
    expected_counts = {
        "distributed_facility_gateway": 187,
        "serial_relay_output_card": 268,
        "multichannel_analog_setpoint_card": 438,
    }
    for case in suite["cases"]:
        count = _physical_count(specs[case["id"]])
        assert count == expected_counts[case["id"]], (case["id"], count)
        assert count >= case["minimum_physical_parts"], (case["id"], count)


def test_controller_extra_pin_maps_do_not_overlap_no_connects():
    _, specs = _load_suite()
    for case_id, spec in specs.items():
        for block in spec.get("digital", []):
            mapped = {str(pin) for pin in block.get("pin_nets_extra", {})}
            no_connects = {str(pin) for pin in block.get("no_connects", [])}
            assert mapped.isdisjoint(no_connects), (case_id, block.get("ref"), mapped & no_connects)
        for block in spec.get("communication", []):
            mapped = {str(pin) for pin in block.get("pin_nets_extra", {})}
            no_connects = {str(pin) for pin in block.get("no_connects", [])}
            assert mapped.isdisjoint(no_connects), (case_id, block.get("ref"), mapped & no_connects)


def test_channel_topology_is_genuinely_repeated_and_connected():
    suite, specs = _load_suite()
    relay = specs["serial_relay_output_card"]
    analog = specs["multichannel_analog_setpoint_card"]
    relay_case = next(case for case in suite["cases"] if case["id"] == "serial_relay_output_card")
    analog_case = next(case for case in suite["cases"] if case["id"] == "multichannel_analog_setpoint_card")

    relay_outputs = [path for path in relay_case["required_paths"] if path["id"].startswith("relay_output_")]
    assert relay["power"]
    assert len(relay_outputs) == 160
    assert {path["id"] for path in relay_outputs} == {f"relay_output_{i:03d}" for i in range(1, 161)}
    assert {path["loads"][0]["ref"] for path in relay_outputs} == {f"J{i}" for i in range(2, 162)}
    assert len(relay["connectors"]) == 161  # one coil header per output plus the debug header.
    assert len(relay["digital"]) >= 20  # 20 cascaded 595s and an MCU.

    dac_count = len(analog["analog"]) - 1  # one voltage reference, remaining entries are DACs
    assert dac_count == 36
    assert len(analog["connectors"]) == 73  # one connector per setpoint plus the debug header.
    outputs = [path for path in analog_case["required_paths"] if path["id"].startswith("setpoint_output_")]
    sync = [path for path in analog_case["required_paths"] if path["id"].startswith("dac_sync_")]
    assert len(outputs) == 72
    assert {path["loads"][0]["ref"] for path in outputs} == {f"J{i}" for i in range(2, 74)}
    assert len(sync) == dac_count
    assert all(len(path.get("return_assertions", [])) == 1 for path in outputs)
    assert {
        endpoint["ref"]
        for path in outputs
        for endpoint in path["return_assertions"][0]["endpoints"]
        if endpoint["ref"].startswith("J")
    } == {f"J{i}" for i in range(2, 74)}
    assert len(analog_case["required_paths"]) == 110
    assert len({case["family"] for case in suite["cases"]}) == 3


def test_serial_relay_partial_driver_channel_nets_follow_template_single_channel_contract():
    output_pads = ["15", "1", "2", "3", "4", "5", "6", "7"]
    for channels in (7, 8, 13, 15, 288):
        spec = build_serial_relay_output_card(channels)
        if channels >= 161:
            # The corpus adapter gives the SWD header a reference outside its
            # output-connector range before compiling the high-count variant.
            spec["connectors"][0]["ref"] = "J9000"
        nets = _pad_nets(spec)
        paths = _relay_paths(channels)
        assert len(paths) == channels

        for index in range(channels):
            shift_register_index, shift_bit = divmod(index, 8)
            driver_index, driver_channel = divmod(index, 7)
            channels_used = min(7, channels - 7 * driver_index)
            driver_ref = f"U{200 + driver_index}"
            drive_net = f"COIL_DRV_{driver_ref}"
            load_net = f"LOAD_{driver_ref}"
            if channels_used != 1:
                drive_net = f"{drive_net}_{driver_channel + 1}"
                load_net = f"{load_net}_{driver_channel + 1}"
            register_ref = f"U{100 + shift_register_index}"
            connector_ref = f"J{index + 2}"
            input_pad = str(driver_channel + 1)
            output_pad = str(16 - driver_channel)
            register_pad = output_pads[shift_bit]

            assert nets[(register_ref, register_pad)] == drive_net, (channels, index, "register")
            assert nets[(driver_ref, input_pad)] == drive_net, (channels, index, "driver input")
            assert nets[(driver_ref, output_pad)] == load_net, (channels, index, "driver output")
            assert nets[(connector_ref, "2")] == load_net, (channels, index, "connector")

        for path in paths:
            for segment in path["segments"]:
                assert all(nets[(e["ref"], str(e["pad"]))] == segment["net"] for e in segment["endpoints"]), (
                    channels,
                    path["id"],
                )
