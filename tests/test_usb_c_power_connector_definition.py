"""Regression tests for the built-in USB-C power receptacle pin contract."""

from circuit_weaver.component_db import BUILTIN_REGISTRY


def test_usb_c_power_definition_covers_every_footprint_pad():
    component = BUILTIN_REGISTRY.get("USB-C-PWR")
    assert component is not None

    expected_pad_ids = {
        "A1",
        "A4",
        "A5",
        "A6",
        "A7",
        "A8",
        "A9",
        "A12",
        "B1",
        "B4",
        "B5",
        "B6",
        "B7",
        "B8",
        "B9",
        "B12",
        "SH",
    }

    assert {pin.number for pin in component.pins} == expected_pad_ids
    assert component.power_pins == {
        "A4": "VBUS_5V",
        "A9": "VBUS_5V",
        "B4": "VBUS_5V",
        "B9": "VBUS_5V",
        "A1": "GND",
        "A12": "GND",
        "B1": "GND",
        "B12": "GND",
    }
    assert component.pin_nets == {"A5": "USB_CC1", "B5": "USB_CC2"}
    assert component.explicit_no_connects == {"A6", "A7", "A8", "B6", "B7", "B8", "SH"}

    handled_pad_ids = set(component.pin_nets) | set(component.power_pins) | component.explicit_no_connects
    assert handled_pad_ids == expected_pad_ids
