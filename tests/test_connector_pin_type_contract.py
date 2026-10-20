from __future__ import annotations

import json
from importlib.resources import files


def test_generic_two_pin_header_uses_passive_contacts() -> None:
    catalog = json.loads(
        files("circuit_weaver").joinpath("ic_data", "connector.json").read_text(encoding="utf-8")
    )
    header = catalog["PIN_HEADER_2P"]

    assert {pin["number"]: pin["type"] for pin in header["pins"]} == {
        "1": "passive",
        "2": "passive",
    }
