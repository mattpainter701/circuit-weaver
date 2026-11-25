import pytest

from circuit_weaver.tt_slot_board import BOARD_ID, PROFILE_ID, map_tt_board

INFO = """
yaml_version: 6
project:
  title: Example
  top_module: tt_um_example
  clock_hz: 1000000
uio_oe_pico: 0b11110000
uio_in: 0b00000000
pinout:
  ui[0]: Button
  ui[1]: Mode
  uo[0]: Status
  uio[7]: Shared bus
"""


def _rows(result, signal, connector):
    return [
        r for r in result["pins"] if r["signal"] == signal and r["connector"] and r["connector"].startswith(connector)
    ]


def test_maps_project_buses_to_verified_etr_v33_pmods_and_breakout():
    result = map_tt_board(INFO, macro="tt_um_example", board=PROFILE_ID)

    assert result["profile_id"] == PROFILE_ID == "tt-etr-v3p3-sky130"
    assert result["board"] == BOARD_ID
    assert result["board_revision"] == "v3.3"
    assert result["top_module"] == "tt_um_example"
    assert "tinytapeout-demo.pdf" in result["provenance"][0]
    assert result["project_config"] == {"uio_oe_pico": 0b11110000, "uio_in": 0, "clock_hz": 1000000}

    ui0 = _rows(result, "ui[0]", "J14")
    assert [(r["pin"], r["function"], r["direction"]) for r in ui0] == [(1, "Button", "input")]
    assert _rows(result, "ui[7]", "J14")[0]["pin"] == 10
    assert _rows(result, "uio[0]", "J15")[0]["pin"] == 1
    assert _rows(result, "uio[0]", "J15")[0]["direction"] == "dynamic"
    assert _rows(result, "uo[0]", "J16")[0]["pin"] == 1
    assert _rows(result, "uo[0]", "J16")[0]["function"] == "Status"
    assert _rows(result, "uio[7]", "J5C")[0]["pin"] == "L23"
    assert _rows(result, "uo[7]", "J5C")[0]["pin"] == "R3"


def test_includes_shared_pmod_power_and_separate_clock_reset_management():
    result = map_tt_board(INFO, macro="tt_um_example", board=PROFILE_ID)

    assert [
        (r["signal"], r["pin"])
        for r in result["pins"]
        if r["connector"] and r["connector"].startswith("J14") and r["direction"] == "power"
    ] == [("GND", 5), ("+3V3", 6), ("GND", 11), ("+3V3", 12)]
    clk = _rows(result, "clk", "J9")
    rst = _rows(result, "rst_n", "J9")
    assert clk[0]["pin"] == 3
    assert "Opening JP1 isolates J9" in clk[0]["notes"]
    assert rst[0]["pin"] == 2 and "active low" in rst[0]["function"]
    assert _rows(result, "clk", "J5C")[0]["pin"] == "L57"
    assert _rows(result, "rst_n", "J5C")[0]["pin"] == "L53"
    assert _rows(result, "ctrl_ena", "J7")[0]["pin"] == 12
    assert "not project I/O" in _rows(result, "ctrl_ena", "J7")[0]["notes"]


def test_rejects_unverified_board_revision_and_non_tt_top_module():
    with pytest.raises(ValueError, match="unsupported Tiny Tapeout board"):
        map_tt_board(INFO, macro="tt_um_example", board="tt-etr-v3p2")
    with pytest.raises(ValueError, match="top_module"):
        map_tt_board(INFO.replace("tt_um_example", "example"), macro="example", board=PROFILE_ID)
    with pytest.raises(ValueError, match="does not match"):
        map_tt_board(INFO, macro="tt_um_different", board=PROFILE_ID)
