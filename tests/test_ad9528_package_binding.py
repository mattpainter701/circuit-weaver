from __future__ import annotations

import re

import pytest

from circuit_weaver.footprint_lib import KiCadFootprintLibrary
from circuit_weaver.ic_data import get_ic_data

AD9528_FOOTPRINT = "Package_CSP:LFCSP-72-1EP_10x10mm_P0.5mm_EP5.3x5.3mm"


def test_ad9528_uses_exact_cp72_6_stock_package_binding() -> None:
    data = get_ic_data("AD9528")

    assert data is not None
    assert data["footprint"] == AD9528_FOOTPRINT
    assert data["pins"][-1]["number"] == "73"
    assert data["pins"][-1]["name"] == "EPAD"
    assert data["pins"][-1]["type"] == "power_in"
    assert "73" in data["pin_gnd"]


def test_ad9528_binding_resolves_to_72_leads_and_5p3mm_ep() -> None:
    library = KiCadFootprintLibrary()
    path = library.resolve(AD9528_FOOTPRINT)
    if path is None:
        # KiCad is optional in CI; the binding regression above is portable.
        pytest.skip("local KiCad footprint libraries are not installed")

    text = path.read_text(encoding="utf-8")
    pad_blocks = re.findall(r'\(pad\s+"([^"]+)"[\s\S]*?\(size\s+([\d.]+)\s+([\d.]+)\)', text)
    numbered_pads = {number for number, _, _ in pad_blocks}
    assert {str(number) for number in range(1, 73)} <= numbered_pads
    assert "73" in numbered_pads

    ep_size = next((float(width), float(height)) for number, width, height in pad_blocks if number == "73")
    assert ep_size == (5.3, 5.3)
