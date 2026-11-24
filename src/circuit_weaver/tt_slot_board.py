"""Source-grounded physical mapping for the Tiny Tapeout ETR v3.3 board.

The physical pin numbers below are transcribed from the official ETR v3.3
production schematic, rather than inferred from RP2350 GPIO numbering.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROFILE_ID = "tt-etr-v3p3-sky130"
BOARD_ID = PROFILE_ID
PROVENANCE = [
    "https://github.com/TinyTapeout/pcb-files/blob/4d6aa7d91db2b9f4f0ac8b83af80e99ee1a25cc9/ETR/demoboard/v3p3/tinytapeout-demo.pdf",
    "https://github.com/TinyTapeout/tt-demo-pcb/tree/bbf3dd1f771d5aed249b07ddf130b3ddc8261d60",
    "https://www.tinytapeout.com/specs/pcb-etr/",
    "https://tinytapeout.com/guides/get-started-demoboard-etr/",
]

_CONNECTORS = {
    "ui": ("J14", "Input PMOD", "input"),
    "uio": ("J15", "Bidir PMOD", "bidirectional"),
    "uo": ("J16", "Output PMOD", "output"),
}


def _read_info(info: str | Path) -> dict[str, Any]:
    content = info
    if isinstance(info, Path) or ("\n" not in info and Path(info).is_file()):
        content = Path(info).read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError("Tiny Tapeout info.yaml contains invalid YAML") from exc
    if not isinstance(data, dict):
        raise ValueError("Tiny Tapeout info.yaml must contain a YAML mapping")
    return data


def _pin_function(pinout: dict[str, Any], bus: str, bit: int) -> str:
    aliases = {
        "ui": (f"ui[{bit}]", f"ui_in[{bit}]", f"ui_in{bit}"),
        "uo": (f"uo[{bit}]", f"uo_out[{bit}]", f"uo_out{bit}"),
        "uio": (f"uio[{bit}]", f"uio{bit}"),
    }[bus]
    for key in aliases:
        if key in pinout:
            value = pinout[key]
            return str(value).strip() if value not in (None, "") else "Unlabeled"
    return "Unlabeled"


def map_tt_board(info: str | Path, *, macro: str, board: str = BOARD_ID) -> dict[str, Any]:
    """Map a TT SKY130 ``info.yaml`` to ETR v3.3 PMOD connector pins.

    Result rows describe physical connector contacts, with explicit separate
    rows for shared power/ground. ``uio_oe_pico`` is preserved as project
    configuration metadata and never guessed from a pin's displayed name.
    """
    if board != BOARD_ID:
        raise ValueError(f"unsupported Tiny Tapeout board {board!r}; supported: {BOARD_ID}")
    data = _read_info(info)
    project = data.get("project") or {}
    if not isinstance(project, dict):
        raise ValueError("info.yaml project must be a mapping")
    top_module = str(project.get("top_module", "")).strip()
    if not top_module.startswith("tt_um_"):
        raise ValueError("info.yaml project.top_module must identify a tt_um_* project")
    if macro != top_module:
        raise ValueError(f"requested macro {macro!r} does not match info.yaml project.top_module {top_module!r}")
    pinout = data.get("pinout") or {}
    if not isinstance(pinout, dict):
        raise ValueError("info.yaml pinout must be a mapping")

    rows: list[dict[str, Any]] = []
    breakout_rows: list[dict[str, Any]] = []
    for bus, (connector, label, direction) in _CONNECTORS.items():
        for bit in range(8):
            physical_pin = bit + 1 if bit < 4 else bit + 3
            notes = ""
            if bus == "uio":
                notes = "Dynamic: ASIC direction from project RTL uio_oe; RP2350 direction from uio_oe_pico."
            rows.append(
                {
                    "signal": f"{bus}[{bit}]",
                    "function": _pin_function(pinout, bus, bit),
                    "connector": f"{connector} ({label})",
                    "pin": physical_pin,
                    "direction": direction if bus != "uio" else "dynamic",
                    "notes": notes,
                }
            )
            # Same project nets are also available at the keyed ASIC breakout
            # connector. L/R labels are the KiCad connector-side pad IDs in
            # the v3.3 production schematic, not RP2350 GPIO numbers.
            breakout_pad = {
                "ui": ("L31", "L33", "L37", "L39", "L41", "L45", "L47", "L49"),
                "uio": ("L5", "L7", "L9", "L13", "L15", "L17", "L21", "L23"),
                "uo": ("R19", "R17", "R15", "R13", "R9", "R7", "R5", "R3"),
            }[bus][bit]
            breakout_rows.append(
                {
                    "signal": f"{bus}[{bit}]",
                    "function": _pin_function(pinout, bus, bit),
                    "connector": "J5C (ASIC I/O breakout connector)",
                    "pin": breakout_pad,
                    "direction": direction if bus != "uio" else "dynamic",
                    "notes": "KiCad pad ID on keyed connector; L/R is side label, not PMOD number.",
                }
            )
        rows.extend(
            [
                {
                    "signal": "GND",
                    "function": "Ground",
                    "connector": f"{connector} ({label})",
                    "pin": 5,
                    "direction": "power",
                    "notes": "Shared ground contact.",
                },
                {
                    "signal": "+3V3",
                    "function": "3.3 V supply",
                    "connector": f"{connector} ({label})",
                    "pin": 6,
                    "direction": "power",
                    "notes": "3.3 V rail; check attached module load.",
                },
                {
                    "signal": "GND",
                    "function": "Ground",
                    "connector": f"{connector} ({label})",
                    "pin": 11,
                    "direction": "power",
                    "notes": "Shared ground contact.",
                },
                {
                    "signal": "+3V3",
                    "function": "3.3 V supply",
                    "connector": f"{connector} ({label})",
                    "pin": 12,
                    "direction": "power",
                    "notes": "3.3 V rail; check attached module load.",
                },
            ]
        )

    # The management PMOD is separate from project I/O. Signal names follow
    # the schematic's connector nets and are not project bus pins.
    management = [
        ("VBUS", 1, "USB bus supply"),
        ("GND", 2, "Ground"),
        ("+3V3", 3, "Board 3.3 V rail"),
        ("GND", 4, "Ground"),
        ("res4", 5, "Reserved breakout management net"),
        ("res3", 6, "Reserved breakout management net"),
        ("res2", 7, "Reserved breakout management net"),
        ("res1", 8, "Reserved breakout management net"),
        ("res0", 9, "Reserved breakout management net"),
        ("GND", 10, "Ground"),
        ("ctrl_sel_rst", 11, "Active-low project MUX selection/reset control"),
        ("ctrl_ena", 12, "Project MUX enable control"),
        ("ctrl_sel_inc", 13, "Project MUX selection/increment control"),
        ("GND", 14, "Ground"),
        ("+3V3", 15, "Board 3.3 V rail"),
        ("GND", 16, "Ground"),
    ]
    for signal, pin, function in management:
        rows.append(
            {
                "signal": signal,
                "function": function,
                "connector": "J7 (Management PMOD)",
                "pin": pin,
                "direction": "control" if signal.startswith("ctrl_") else "power/shared",
                "notes": (
                    "Dedicated control header, not project I/O. These nets also connect to RP2350 GPIOs; "
                    "avoid external drive while the controller is active."
                ),
            }
        )

    # Clock/reset are available to users on the high-speed 3-pin header J9
    # and also appear at the ASIC breakout connector J5. The J9 clock is the
    # external side of JP1; its default solder bridge joins it to the project
    # clock net shared with RP2350 GPIO16.
    rows.extend(
        [
            {
                "signal": "clk",
                "function": "Project clock / external clock net",
                "connector": "J9 (HighSpeed header)",
                "pin": 3,
                "direction": "shared clock input",
                "notes": (
                    "J9 pin 3 is ext_project_clk on JP1 pin 2. JP1 pin 1 is project_clk, shared with "
                    "RP2350 GPIO16 and ASIC J5C L57; JP1 is bridged by default. Do not drive J9 while "
                    "GPIO16 is active. Opening JP1 isolates J9; this map does not prescribe external-clock setup."
                ),
            },
            {
                "signal": "rst_n",
                "function": "Project reset, active low",
                "connector": "J9 (HighSpeed header)",
                "pin": 2,
                "direction": "input",
                "notes": (
                    "Board-conditioned active-low net; RP2350 GPIO14 connects through a resistor. "
                    "Coordinate external drive with board reset."
                ),
            },
            {
                "signal": "uio[3]",
                "function": _pin_function(pinout, "uio", 3),
                "connector": "J9 (HighSpeed header)",
                "pin": 1,
                "direction": "dynamic",
                "notes": "Same project uio[3] net as J15 pin 4 and ASIC breakout J5C L13; J9 has no ground contact.",
            },
            {
                "signal": "clk",
                "function": "Project clock input",
                "connector": "J5C (ASIC I/O breakout connector)",
                "pin": "L57",
                "direction": "input",
                "notes": "project_clk also connects to RP2350 GPIO16 and JP1 pin 1; board clock control is default.",
            },
            {
                "signal": "rst_n",
                "function": "Project reset, active low",
                "connector": "J5C (ASIC I/O breakout connector)",
                "pin": "L53",
                "direction": "input",
                "notes": "Same active-low net as J9 pin 2; default inactive high from board reset circuit.",
            },
            {
                "signal": "ctrl_ena",
                "function": "Project MUX enable",
                "connector": "J5C (ASIC control breakout connector)",
                "pin": "R46",
                "direction": "control input",
                "notes": "Chip project MUX control, separate from project-module ena.",
            },
            {
                "signal": "ctrl_sel_rst",
                "function": "Project MUX selection/reset",
                "connector": "J5C (ASIC control breakout connector)",
                "pin": "R43",
                "direction": "control input",
                "notes": "Active-low MUX selection/reset (`~{ctrl_sel_rst}`); separate from project reset.",
            },
            {
                "signal": "ctrl_sel_inc",
                "function": "Project MUX selection increment",
                "connector": "J5C (ASIC control breakout connector)",
                "pin": "R47",
                "direction": "control input",
                "notes": "MUX selection control, separate from project inputs.",
            },
            {
                "signal": "ena",
                "function": "Project module enable",
                "connector": None,
                "pin": None,
                "direction": "internal input",
                "notes": "Internal project control; no board pad. Separate from carrier MUX control ctrl_ena.",
            },
        ]
    )

    rows.extend(breakout_rows)
    project_config = {key: data[key] for key in ("uio_oe_pico", "uio_in") if key in data}
    if "clock_hz" in project:
        project_config["clock_hz"] = project["clock_hz"]

    return {
        "profile_id": PROFILE_ID,
        "board": BOARD_ID,
        "board_revision": "v3.3",
        "macro": macro,
        "top_module": top_module,
        "provenance": PROVENANCE.copy(),
        "orientation": (
            "Locate pin 1 on the ETR board silkscreen before counting PMOD contacts. "
            "J9 is an unkeyed 3-pin header without ground."
        ),
        "project_config": project_config,
        "pins": rows,
        "notes": [
            "The table uses physical connector contacts. The L/R labels are keyed breakout pad IDs.",
            (
                "JP1 bridges J9's ext_project_clk to the board project clock/RP2350 GPIO16. "
                "Do not drive the shared net while GPIO16 is active."
            ),
        ],
    }
