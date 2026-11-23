from __future__ import annotations

from copy import deepcopy

import pytest

from benchmarks.general_scale.connectivity import ConnectivityContractError
from benchmarks.general_scale.signal_connectivity import verify_native_signal_paths


def _inventory(rows: list[tuple[str, str, str]]) -> dict:
    pads: dict[str, list[dict[str, str]]] = {}
    for ref, number, net in rows:
        pads.setdefault(ref, []).append({"number": number, "net": net})
    return {
        "status": "ok",
        "footprints": [
            {"ref": ref, "footprint": "Test:Package", "pads": entries}
            for ref, entries in pads.items()
        ],
    }


def _bus() -> tuple[dict, dict]:
    contract = {
        "id": "i2c-sda-sensor-0",
        "source": {"ref": "U1", "pad": "12"},
        "loads": [{"ref": "U8", "pad": "3"}, {"ref": "U9", "pad": "4"}],
        "segments": [{
            "net": "I2C_SDA",
            "endpoints": [
                {"ref": "U1", "pad": "12"}, {"ref": "U8", "pad": "3"}, {"ref": "U9", "pad": "4"},
            ],
        }],
        "through": [],
    }
    inventory = _inventory([
        ("U1", "12", "I2C_SDA"), ("U8", "3", "I2C_SDA"), ("U9", "4", "I2C_SDA"),
    ])
    return inventory, contract


def _series() -> tuple[dict, dict]:
    contract = {
        "id": "mcu-to-driver-enable",
        "source": {"ref": "U1", "pad": "7"},
        "loads": [{"ref": "U5", "pad": "2"}],
        "segments": [
            {"net": "MCU_EN", "endpoints": [{"ref": "U1", "pad": "7"}, {"ref": "R2", "pad": "1"}]},
            {"net": "DRIVER_EN", "endpoints": [{"ref": "R2", "pad": "2"}, {"ref": "U5", "pad": "2"}]},
        ],
        "through": [{"ref": "R2", "input_pad": "1", "output_pad": "2"}],
    }
    inventory = _inventory([
        ("U1", "7", "MCU_EN"), ("R2", "1", "MCU_EN"),
        ("R2", "2", "DRIVER_EN"), ("U5", "2", "DRIVER_EN"),
    ])
    return inventory, contract


def test_verifies_controller_to_load_bus_against_native_pads() -> None:
    inventory, contract = _bus()
    result = verify_native_signal_paths(inventory, {"required_paths": [contract]})
    assert result["status"] == "pass"
    assert result["verified_paths"] == 1
    assert result["paths"][0]["nets"] == ["I2C_SDA"]


def test_verifies_series_path_through_resistor_and_driver() -> None:
    inventory, contract = _series()
    result = verify_native_signal_paths(inventory, [contract])
    assert result["status"] == "pass"
    assert result["paths"][0]["nets"] == ["MCU_EN", "DRIVER_EN"]


@pytest.mark.parametrize("mutation, message", [
    ("wrong_net", "expected 'I2C_SDA'"),
    ("reversed_bridge", "pads must join"),
    ("disconnected_chain", "expected 'DRIVER_EN'"),
    ("missing_pad", "U8 pad 99 is missing"),
    ("duplicate_path", "duplicate required path id"),
    ("empty_paths", "required_paths must be a non-empty sequence"),
    ("power_only", "power-only net"),
    ("one_ref", "different references"),
])
def test_rejects_false_or_malformed_paths(mutation: str, message: str) -> None:
    bus_inventory, bus_contract = _bus()
    series_inventory, series_contract = _series()
    if mutation == "wrong_net":
        bus_inventory["footprints"][0]["pads"][0]["net"] = "WRONG"
        inventory, contracts = bus_inventory, [bus_contract]
    elif mutation == "reversed_bridge":
        series_contract["through"][0]["input_pad"], series_contract["through"][0]["output_pad"] = "2", "1"
        inventory, contracts = series_inventory, [series_contract]
    elif mutation == "disconnected_chain":
        series_inventory["footprints"][2]["pads"][0]["net"] = "ISOLATED"
        inventory, contracts = series_inventory, [series_contract]
    elif mutation == "missing_pad":
        bus_contract["loads"][0]["pad"] = "99"
        bus_contract["segments"][0]["endpoints"][1]["pad"] = "99"
        inventory, contracts = bus_inventory, [bus_contract]
    elif mutation == "duplicate_path":
        inventory, contracts = bus_inventory, [bus_contract, deepcopy(bus_contract)]
    elif mutation == "empty_paths":
        inventory, contracts = bus_inventory, []
    elif mutation == "power_only":
        bus_contract["segments"][0]["net"] = "GND"
        inventory, contracts = bus_inventory, [bus_contract]
    else:
        bus_contract["segments"][0]["endpoints"] = [
            {"ref": "U1", "pad": "12"}, {"ref": "U1", "pad": "12"},
        ]
        inventory, contracts = bus_inventory, [bus_contract]

    with pytest.raises(ConnectivityContractError, match=message):
        verify_native_signal_paths(inventory, contracts)


def test_rejects_conflicting_duplicate_native_pad_instances() -> None:
    inventory, contract = _bus()
    inventory["footprints"][0]["pads"].append({"number": "12", "net": "WRONG"})
    with pytest.raises(ConnectivityContractError, match="conflicting duplicate pad 12"):
        verify_native_signal_paths(inventory, [contract])
