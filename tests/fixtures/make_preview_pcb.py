"""Create a real, geometry-rich PCB for native SVG preview regressions.

Run this script with KiCad's own Python so loaded library footprints and saved
board geometry are exactly what the native preview exporter will see.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pcbnew

footprint_root, output_path = Path(sys.argv[1]), Path(sys.argv[2])
output_path.parent.mkdir(parents=True, exist_ok=True)
board = pcbnew.BOARD()


def point(x: float, y: float):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


def edge(a: tuple[float, float], b: tuple[float, float]) -> None:
    item = pcbnew.PCB_SHAPE(board)
    item.SetShape(pcbnew.SHAPE_T_SEGMENT)
    item.SetStart(point(*a))
    item.SetEnd(point(*b))
    item.SetLayer(pcbnew.Edge_Cuts)
    item.SetWidth(pcbnew.FromMM(0.05))
    board.Add(item)


# Chamfered, closed outer perimeter plus a separate closed Edge.Cuts loop for
# an actual cutout. These loops exercise polygon-with-holes handling.
outline = [(0, 6), (6, 0), (44, 0), (50, 6), (50, 34), (44, 40), (6, 40), (0, 34)]
for start, end in zip(outline, outline[1:] + outline[:1]):
    edge(start, end)
cutout = [(35, 25), (41, 25), (41, 31), (35, 31)]
for start, end in zip(cutout, cutout[1:] + cutout[:1]):
    edge(start, end)

footprints = [
    ("J1", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", (9, 18), 90, False),
    ("R1", "Resistor_SMD:R_0603_1608Metric", (20, 11), 30, False),
    ("R2", "Resistor_SMD:R_0603_1608Metric", (29, 15), 135, True),
]
net_by_name = {}
for ref, library_id, (x, y), angle, back in footprints:
    library, name = library_id.split(":", 1)
    footprint = pcbnew.FootprintLoad(str(footprint_root / f"{library}.pretty"), name)
    if footprint is None:
        raise RuntimeError(f"Missing required test footprint: {library_id}")
    board.Add(footprint)
    footprint.SetReference(ref)
    footprint.SetValue(name)
    footprint.SetPosition(point(x, y))
    footprint.SetOrientationDegrees(angle)
    if back:
        footprint.Flip(footprint.GetPosition(), False)
    for pad in footprint.Pads():
        net_name = f"{ref}_{pad.GetNumber()}"
        net = pcbnew.NETINFO_ITEM(board, net_name)
        board.Add(net)
        pad.SetNet(net)
        net_by_name[net_name] = net

# A plated through-hole footprint tests native drill rendering. Add a separate
# oblong NPTH pad in a local footprint to ensure slot geometry is preserved.
library, name = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical".split(":", 1)
tht = pcbnew.FootprintLoad(str(footprint_root / f"{library}.pretty"), name)
if tht is None:
    raise RuntimeError(f"Missing required test footprint: {library}:{name}")
board.Add(tht)
tht.SetReference("J2")
tht.SetValue(name)
tht.SetPosition(point(12, 30))
tht.SetOrientationDegrees(0)
for pad in tht.Pads():
    net_name = f"J2_{pad.GetNumber()}"
    net = pcbnew.NETINFO_ITEM(board, net_name)
    board.Add(net)
    pad.SetNet(net)
    net_by_name[net_name] = net

slot_fp = pcbnew.FOOTPRINT(board)
slot_fp.SetReference("MH1")
slot_fp.SetValue("NPTH oblong slot")
slot_fp.SetPosition(point(22, 30))
slot_pad = pcbnew.PAD(slot_fp)
slot_pad.SetNumber("1")
slot_pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
slot_pad.SetShape(pcbnew.PAD_SHAPE_OVAL)
slot_pad.SetSize(point(3.0, 1.6))
slot_pad.SetDrillShape(pcbnew.PAD_DRILL_SHAPE_OBLONG)
slot_pad.SetDrillSize(point(2.6, 1.0))
slot_pad.SetPosition(point(22, 30))
slot_fp.Add(slot_pad)
board.Add(slot_fp)

# Real front/back copper on distinct layers; this fixture is a rendering input,
# not an electrically complete board and must never satisfy fabrication gates.
for net_name, layer, start, end in (
    ("R1_1", pcbnew.F_Cu, (18, 11), (13, 18)),
    ("R2_1", pcbnew.B_Cu, (27, 15), (22, 20)),
):
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(point(*start))
    track.SetEnd(point(*end))
    track.SetWidth(pcbnew.FromMM(0.25))
    track.SetLayer(layer)
    track.SetNetCode(net_by_name[net_name].GetNetCode())
    board.Add(track)

# Shift the complete board to a nonzero origin. Native Edge.Cuts SVG coordinates
# stay local to the board area; the renderer must register them against this
# physical polygon origin without shifting footprint or copper geometry.
board.Move(point(20, 30))

if not pcbnew.SaveBoard(str(output_path), board):
    raise RuntimeError(f"KiCad failed to save fixture board: {output_path}")

print(json.dumps({"board": str(output_path), "version": pcbnew.Version(),
                  "expected_bounds_mm": {"x": 20, "y": 30, "width": 50, "height": 40},
                  "references": ["J1", "J2", "R1", "R2", "MH1"],
                  "features": ["rotated-front", "rotated-back", "smd", "tht",
                               "oblong-npth-slot", "chamfered-outline", "edge-cuts-cutout",
                               "front-copper", "back-copper"]}))
