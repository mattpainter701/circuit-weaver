from __future__ import annotations

import pytest

from benchmarks.general_scale.signal_connectivity import (
    ConnectivityContractError,
    verify_native_signal_paths,
)


def _contract() -> dict:
    return {
        "id": "output_with_return",
        "source": {"ref": "U1", "pad": "1"},
        "loads": [{"ref": "J1", "pad": "1"}],
        "segments": [
            {"net": "RAW", "endpoints": [{"ref": "U1", "pad": "1"}, {"ref": "R1", "pad": "1"}]},
            {
                "net": "FILTERED",
                "endpoints": [
                    {"ref": "R1", "pad": "2"},
                    {"ref": "C1", "pad": "2"},
                    {"ref": "J1", "pad": "1"},
                ],
            },
        ],
        "through": [{"ref": "R1", "input_pad": "1", "output_pad": "2"}],
        "return_assertions": [{"net": "GND", "endpoints": [{"ref": "C1", "pad": "1"}, {"ref": "J1", "pad": "2"}]}],
    }


def _inventory() -> dict:
    return {
        "status": "ok",
        "footprints": [
            {"ref": "U1", "pads": [{"number": "1", "net": "RAW"}]},
            {"ref": "R1", "pads": [{"number": "1", "net": "RAW"}, {"number": "2", "net": "FILTERED"}]},
            {"ref": "C1", "pads": [{"number": "1", "net": "GND"}, {"number": "2", "net": "FILTERED"}]},
            {"ref": "J1", "pads": [{"number": "1", "net": "FILTERED"}, {"number": "2", "net": "GND"}]},
        ],
    }


def test_checks_signal_and_explicit_power_return_endpoints_separately() -> None:
    proof = verify_native_signal_paths(_inventory(), [_contract()])
    assert proof["status"] == "pass"
    assert proof["verified_paths"] == 1
    assert proof["verified_return_assertions"] == 1


def test_wrong_signal_capacitor_pad_fails_even_if_ground_return_is_correct() -> None:
    contract = _contract()
    contract["segments"][1]["endpoints"][1]["pad"] = "1"
    with pytest.raises(ConnectivityContractError, match="on 'GND', expected 'FILTERED'"):
        verify_native_signal_paths(_inventory(), [contract])


def test_return_assertion_cannot_reuse_signal_pad_or_unrelated_reference() -> None:
    contract = _contract()
    contract["return_assertions"][0]["endpoints"][0] = {"ref": "C1", "pad": "2"}
    with pytest.raises(ConnectivityContractError, match="pads distinct"):
        verify_native_signal_paths(_inventory(), [contract])
    contract = _contract()
    contract["return_assertions"][0]["endpoints"][0] = {"ref": "C2", "pad": "1"}
    with pytest.raises(ConnectivityContractError, match="already in the signal path"):
        verify_native_signal_paths(_inventory(), [contract])


def test_power_nets_remain_forbidden_as_signal_segments() -> None:
    contract = _contract()
    contract["segments"][0]["net"] = "GND"
    with pytest.raises(ConnectivityContractError, match="cannot use power-only net"):
        verify_native_signal_paths(_inventory(), [contract])
