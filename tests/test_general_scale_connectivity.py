from __future__ import annotations

from copy import deepcopy

import pytest

from benchmarks.general_scale.connectivity import (
    ConnectivityContractError,
    verify_native_connectivity,
)


def _channel(i: int, *, post_stage: bool = False) -> dict:
    row = {
        "frontend_ref": f"U{i + 1}",
        "frontend_p_pad": "2",
        "sensor_p_net": f"IN{i}_P",
        "frontend_n_pad": "3",
        "sensor_n_net": f"IN{i}_N",
        "frontend_ref_pad": "5",
        "reference_net": f"REF{i}",
        "reference_ref": f"U{100 + i}",
        "reference_output_pad": "1",
        "frontend_out_pad": "6",
        "frontend_out_net": f"AMP{i}",
        "adc_ref": "U90",
        "adc_channel": i,
        "adc_pad": str(i + 1),
        "adc_net": f"ADC{i}",
        "adc_filter_ref": f"R{200 + i}",
        "adc_filter_input_pad": "1",
        "adc_filter_output_pad": "2",
        "input_connector_ref": f"J{i + 1}",
        "input_positive_pad": "1",
        "input_positive_net": f"IN{i}_P",
        "input_negative_pad": "2",
        "input_negative_net": f"IN{i}_N",
    }
    if post_stage:
        row.update({"post_stage_ref": f"U{30 + i}", "post_stage_input_pad": "2",
                    "post_stage_output_pad": "1", "post_stage_output_net": f"STAGE{i}"})
    else:
        row["frontend_out_net"] = f"AMP{i}"
    return row


def _native(contracts: list[dict]) -> dict:
    by_ref: dict[str, dict[str, object]] = {}

    def add(ref: str, pad: str, net: str) -> None:
        footprint = by_ref.setdefault(ref, {"ref": ref, "footprint": "Test:Package", "pads": []})
        footprint["pads"].append({"number": pad, "net": net})

    for channel in contracts:
        i = channel["adc_channel"]
        add(channel["input_connector_ref"], "1", channel["input_positive_net"])
        add(channel["input_connector_ref"], "2", channel["input_negative_net"])
        add(channel["frontend_ref"], channel["frontend_p_pad"], channel["sensor_p_net"])
        add(channel["frontend_ref"], channel["frontend_n_pad"], channel["sensor_n_net"])
        add(channel["frontend_ref"], channel["frontend_ref_pad"], channel["reference_net"])
        add(channel["reference_ref"], channel["reference_output_pad"], channel["reference_net"])
        if "post_stage_ref" in channel:
            add(channel["frontend_ref"], channel["frontend_out_pad"], channel["frontend_out_net"])
            add(channel["post_stage_ref"], channel["post_stage_input_pad"], channel["frontend_out_net"])
            add(channel["post_stage_ref"], channel["post_stage_output_pad"], channel["post_stage_output_net"])
            filter_input_net = channel["post_stage_output_net"]
        else:
            add(channel["frontend_ref"], channel["frontend_out_pad"], channel["frontend_out_net"])
            filter_input_net = channel["frontend_out_net"]
        add(channel["adc_filter_ref"], channel["adc_filter_input_pad"], filter_input_net)
        add(channel["adc_filter_ref"], channel["adc_filter_output_pad"], channel["adc_net"])
        add("U90", str(i + 1), channel["adc_net"])
    return {"status": "ok", "footprints": list(by_ref.values())}


@pytest.mark.parametrize("count", [8, 6, 8])
def test_verifies_declared_sensor_to_adc_channels_against_native_pads(count: int) -> None:
    contracts = [_channel(i, post_stage=(i % 2 == 0)) for i in range(count)]
    evidence = verify_native_connectivity(_native(contracts), {"required_channels": contracts})
    assert evidence["status"] == "pass"
    assert evidence["verified_channels"] == count


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c, pcb: c[0].pop("adc_pad"), "missing required field adc_pad"),
        (lambda c, pcb: pcb["footprints"][0]["pads"][0].update(net="WRONG"), "expected"),
        (lambda c, pcb: pcb["footprints"].append(deepcopy(pcb["footprints"][0])), "duplicate native footprint"),
        (lambda c, pcb: c.append(deepcopy(c[0])), "duplicate ADC channel"),
        (lambda c, pcb: pcb["footprints"][0]["pads"][0].update(net=""), "unconnected"),
    ],
)
def test_rejects_incomplete_or_false_native_contracts(mutate, message: str) -> None:
    contracts = [_channel(0)]
    inventory = _native(contracts)
    mutate(contracts, inventory)
    with pytest.raises(ConnectivityContractError, match=message):
        verify_native_connectivity(inventory, contracts)


def test_accepts_consistent_duplicate_physical_pad_instances() -> None:
    contracts = [_channel(0)]
    inventory = _native(contracts)
    inventory["footprints"][0]["pads"].append(deepcopy(inventory["footprints"][0]["pads"][0]))
    assert verify_native_connectivity(inventory, contracts)["status"] == "pass"


def test_contract_must_match_consistent_duplicate_pad_net() -> None:
    contracts = [_channel(0)]
    inventory = _native(contracts)
    connector = next(row for row in inventory["footprints"] if row["ref"] == "J1")
    connector["pads"].append(deepcopy(connector["pads"][0]))
    contracts[0]["input_positive_net"] = "OTHER"
    with pytest.raises(ConnectivityContractError, match="expected 'OTHER'"):
        verify_native_connectivity(inventory, contracts)


def test_rejects_conflicting_duplicate_pad_nets() -> None:
    contracts = [_channel(0)]
    inventory = _native(contracts)
    duplicate = deepcopy(inventory["footprints"][0]["pads"][0])
    duplicate["net"] = "WRONG"
    inventory["footprints"][0]["pads"].append(duplicate)
    with pytest.raises(ConnectivityContractError, match="conflicting duplicate pad"):
        verify_native_connectivity(inventory, contracts)


def test_ignores_native_proven_unassigned_paste_only_pad() -> None:
    contracts = [_channel(0)]
    inventory = _native(contracts)
    adc = next(row for row in inventory["footprints"] if row["ref"] == "U90")
    adc["native_known_copper_layer_inventory_complete"] = True
    adc["pads"].append({
        "number": "",
        "net": "",
        "attribute": 1,
        "layers": [13],
        "copper_layers": [],
    })

    evidence = verify_native_connectivity(inventory, contracts)

    assert evidence["status"] == "pass"


@pytest.mark.parametrize(
    ("pad_update", "footprint_update"),
    [
        ({"copper_layers": [0]}, {}),
        ({"layers": None}, {}),
        ({"copper_layers": None}, {}),
        ({"net": "VDD"}, {}),
        ({"attribute": None}, {}),
        ({}, {"native_known_copper_layer_inventory_complete": False}),
    ],
)
def test_unproven_or_electrical_unnumbered_pad_remains_a_contract_error(
    pad_update: dict, footprint_update: dict
) -> None:
    contracts = [_channel(0)]
    inventory = _native(contracts)
    adc = next(row for row in inventory["footprints"] if row["ref"] == "U90")
    adc["native_known_copper_layer_inventory_complete"] = True
    adc.update(footprint_update)
    adc["pads"].append({
        "number": "",
        "net": "",
        "attribute": 1,
        "layers": [13],
        "copper_layers": [],
        **pad_update,
    })

    with pytest.raises(ConnectivityContractError, match="pad without a number"):
        verify_native_connectivity(inventory, contracts)


def test_rejects_connector_nets_that_do_not_match_frontend() -> None:
    contracts = [_channel(0)]
    contracts[0]["input_positive_net"] = "OTHER"
    inventory = _native(contracts)
    with pytest.raises(ConnectivityContractError, match="do not match"):
        verify_native_connectivity(inventory, contracts)
