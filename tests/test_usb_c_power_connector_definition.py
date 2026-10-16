"""Regression tests for the built-in USB-C power receptacle pin contract."""

from circuit_weaver.component_db import BUILTIN_REGISTRY


def test_usb_c_power_definition_covers_every_footprint_pad():
    component = BUILTIN_REGISTRY._components["USB-C-PWR"]
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


def _legacy_library(tmp_path, monkeypatch, *, extra_pad=None):
    canonical = BUILTIN_REGISTRY._components["USB-C-PWR"]
    numbers = ({pin.number for pin in canonical.pins} - {"SH"}) | {"S1"}
    if extra_pad:
        numbers.add(extra_pad)
    pretty = tmp_path / "Connector_USB.pretty"
    pretty.mkdir()
    (pretty / "USB_C_Receptacle_GCT_USB4085.kicad_mod").write_text(
        '(footprint "USB4085" ' + " ".join(f'(pad "{number}" smd rect)' for number in sorted(numbers)) + ")",
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path))
    return canonical


def test_builtin_uses_exact_legacy_shield_name_without_mutating_registry(tmp_path, monkeypatch):
    canonical = _legacy_library(tmp_path, monkeypatch)

    bound = BUILTIN_REGISTRY.get("USB-C-PWR")

    assert {pin.number for pin in bound.pins} == ({pin.number for pin in canonical.pins} - {"SH"}) | {"S1"}
    assert "S1" in bound.explicit_no_connects and "SH" not in bound.explicit_no_connects
    assert bound.pin_nets == canonical.pin_nets
    assert bound.power_pins == canonical.power_pins
    assert "SH" in canonical.explicit_no_connects and "S1" not in canonical.explicit_no_connects
    assert {pin.number for pin in BUILTIN_REGISTRY.get("USB-C-PWR-ND").pins} == {pin.number for pin in bound.pins}


def test_unknown_legacy_pad_set_is_not_silently_reconciled(tmp_path, monkeypatch):
    canonical = _legacy_library(tmp_path, monkeypatch, extra_pad="UNEXPECTED")

    assert BUILTIN_REGISTRY.get("USB-C-PWR") is canonical


def test_connected_shield_is_never_implicitly_renumbered(tmp_path, monkeypatch):
    import copy

    from circuit_weaver.component_db import _bind_builtin_connector_shield

    connected = copy.deepcopy(_legacy_library(tmp_path, monkeypatch))
    connected.pin_nets["SH"] = "GND"

    assert _bind_builtin_connector_shield(connected) is connected
