from __future__ import annotations

from pathlib import Path

import yaml

from circuit_weaver.assembly_manifest import build_assembly_manifest
from circuit_weaver.design_loader import compile_design_ir

ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "benchmarks" / "unseen_scale" / "cases.yaml"


def _load_suite():
    suite = yaml.safe_load(SUITE_PATH.read_text(encoding="utf-8"))
    return suite, {
        case["id"]: yaml.safe_load((SUITE_PATH.parent / case["spec"]).read_text(encoding="utf-8"))
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
        for pad, net in component.pin_nets.items()
    }
    manifest = build_assembly_manifest(compiled.components)
    for item in manifest.items:
        if item.source_kind in {"bypass", "strap"}:
            nets[(item.reference, "1")] = item.net1
            nets[(item.reference, "2")] = item.net2
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
    nets = _pad_nets(specs[case["id"]])
    channels = case["required_channels"]
    assert len(channels) == 8
    for channel in channels:
        assert nets[(channel["input_connector_ref"], str(channel["input_positive_pad"]))] == channel["sensor_p_net"]
        assert nets[(channel["input_connector_ref"], str(channel["input_negative_pad"]))] == channel["sensor_n_net"]
        assert nets[(channel["frontend_ref"], str(channel["frontend_p_pad"]))] == channel["sensor_p_net"]
        assert nets[(channel["frontend_ref"], str(channel["frontend_n_pad"]))] == channel["sensor_n_net"]
        assert nets[(channel["frontend_ref"], str(channel["frontend_ref_pad"]))] == channel["reference_net"]
        assert nets[(channel["frontend_ref"], str(channel["frontend_out_pad"]))] == channel["frontend_out_net"]
        assert nets[(channel["adc_ref"], str(channel["adc_pad"]))] == channel["adc_net"]
        providers = [
            block for block in specs[case["id"]].get("analog", []) if block.get("ref") == channel["reference_ref"]
        ]
        assert len(providers) == 1
        assert any(
            interface.get("name") == channel["reference_net"] for interface in providers[0].get("interfaces", [])
        )


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
    assert len(analog_case["required_paths"]) == 110
    assert len({case["family"] for case in suite["cases"]}) == 3
