# Tiny Tapeout ETR v3.3 board pin mapping

`circuit_weaver.tt_slot_board.map_tt_board(info, macro=...)` supports the
SKY130 ETR profile `tt-etr-v3p3-sky130`. Its physical connector map is grounded
in Tiny Tapeout's [ETR v3.3 production schematic](https://github.com/TinyTapeout/pcb-files/blob/4d6aa7d91db2b9f4f0ac8b83af80e99ee1a25cc9/ETR/demoboard/v3p3/tinytapeout-demo.pdf),
dated 2026-06-23, plus the official [ETR board documentation](https://www.tinytapeout.com/specs/pcb-etr/).
The implementation deliberately rejects other board identifiers because
connector numbering and hardware revisions must be verified separately.

## Project I/O PMOD contacts

The board schematic identifies the three standard PMODs as J14 (input), J15
(bidirectional), and J16 (output). Each carries the same four power contacts:
pin 5 GND, pin 6 +3V3, pin 11 GND, and pin 12 +3V3. Data contacts are:

| Signal bit | Input J14 | Bidir J15 | Output J16 |
|---|---:|---:|---:|
| 0 | 1 | 1 | 1 |
| 1 | 2 | 2 | 2 |
| 2 | 3 | 3 | 3 |
| 3 | 4 | 4 | 4 |
| 4 | 7 | 7 | 7 |
| 5 | 8 | 8 | 8 |
| 6 | 9 | 9 | 9 |
| 7 | 10 | 10 | 10 |

The numbers above are connector contact numbers as printed in the schematic;
they do not encode a view direction. Orient the mating PMOD using its key and
the board silkscreen. J14 contacts carry `ui[0..7]`, J15 carries `uio[0..7]`,
and J16 carries `uo[0..7]`. The profile maps these as separate physical rows
and also includes the corresponding keyed ASIC breakout connector pad IDs.

The +3V3 contacts are the board's 3.3 V rail, not a generic supply input.
Check the attached module's load and power requirements before using them.
The analog and management headers are separate board interfaces and are not
project `ui`, `uo`, or `uio` pins.

## Clock, reset, and project selection

The 3-pin HighSpeed header J9 exposes the clock net at pin 3 and active-low
project reset at pin 2. The same project signals reach the ASIC breakout
connector J5C at `L57` (`proj_clk`) and `L53` (`~{project_rst}`). J9 pin 1 is
`uio[3]`; J9 does not expose a ground contact. The carrier schematic shows
JP1 pin 1 (`project_clk`, shared with RP2350 GPIO16 and J5C L57) and JP1 pin 2
(`ext_project_clk`, J9 pin 3). JP1 is bridged in the production schematic.
Opening JP1 disconnects J9 pin 3 from the project clock. Do not drive J9 pin 3
while the RP2350 is actively driving GPIO16; the mapper does not prescribe an
external-clock setup. The board SDK also supports board-controlled automatic
and manual clocking.

Project reset is active low. The schematic shows board reset conditioning and
an RP2350 connection through a resistor; the RP2350 normally leaves this line
high impedance. Coordinate external reset drive with the onboard reset
circuit. Do not confuse project reset with the RP2350's USB boot/run controls.
The [ETR guide](https://tinytapeout.com/guides/get-started-demoboard-etr/)'s hardware-in-the-loop path selects a shuttle project through
Commander or the SDK, then controls project clock/reset separately. In the
MicroPython SDK, the documented selector syntax is
`tt.shuttle.tt_um_myproject.enable()`; replace the example name with the
project's official module name.

The separate management PMOD J7 carries `ctrl_ena` on pin 12, active-low
`ctrl_sel_rst` on pin 11, and `ctrl_sel_inc` on pin 13. These operate the chip's
project mux. In particular, `ctrl_ena` is not the Tiny Tapeout project module's
`ena` input. The project `ena` is internal to the chip interface and has no ETR
carrier header pad. The mapper does not guess which project is selected from
`info.yaml`; select the submitted project through the official Tiny Tapeout
SDK/Commander, using the project's actual shuttle entry.

The RP2350 GPIO mapping in the board README is controller-side information,
not physical header numbering. `uio_oe_pico` controls RP2350 GPIO direction;
the project's RTL controls ASIC `uio_oe`. The profile preserves the project
configuration and reports bidirectional direction as dynamic rather than
inferring it from a signal name.
