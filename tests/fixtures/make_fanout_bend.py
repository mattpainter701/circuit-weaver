"""Build a source-empty board and a router candidate with one narrow bend."""

import json
import shutil
import sys
from pathlib import Path

import pcbnew

library, source, candidate = map(Path, sys.argv[1:4])
board = pcbnew.BOARD()


def point(xy):
    return pcbnew.VECTOR2I(*(pcbnew.FromMM(v) for v in xy))


outline = [(5, 3), (25, 3), (25, 17), (5, 17)]
for a, b in zip(outline, outline[1:] + outline[:1]):
    edge = pcbnew.PCB_SHAPE()
    edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetStart(point(a))
    edge.SetEnd(point(b))
    edge.SetWidth(pcbnew.FromMM(0.05))
    board.Add(edge)

nets = {}
for name in ("SIGNAL", "RETURN"):
    nets[name] = pcbnew.NETINFO_ITEM(board, name)
    board.Add(nets[name])

for ref, xy, net in [
    ("TP1", (10, 12), "SIGNAL"), ("TP2", (20, 12), "SIGNAL"),
    ("TP3", (15, 10), "RETURN"), ("TP4", (15, 6), "RETURN"),
]:
    footprint = pcbnew.FootprintLoad(str(library / "TestPoint.pretty"), "TestPoint_Pad_D1.0mm")
    board.Add(footprint)
    footprint.SetReference(ref)
    footprint.SetPosition(point(xy))
    for pad in footprint.Pads():
        pad.SetNet(nets[net])

assert pcbnew.SaveBoard(str(source), board)
settings = source.with_suffix(".kicad_pro")
if not settings.exists():
    assert pcbnew.GetSettingsManager().SaveProject(str(settings), board.GetProject())
project = json.loads(settings.read_text())
project["board"]["design_settings"]["rules"].update(min_clearance=0.127, min_track_width=0.20)
default = next(row for row in project["net_settings"]["classes"] if row["name"] == "Default")
default.update(track_width=0.20, clearance=0.127, via_diameter=0.6, via_drill=0.3)
settings.write_text(json.dumps(project))

# Both 0.15 mm segments clear TP3. Widening them to the requested 0.20 mm
# leaves 0.115 mm of copper clearance at their shared endpoint.
bend = (15, 10.715)
for a, b, width, net in [
    ((10, 12), bend, 0.15, "SIGNAL"), (bend, (20, 12), 0.15, "SIGNAL"),
    ((15, 10), (15, 6), 0.20, "RETURN"),
]:
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(point(a))
    track.SetEnd(point(b))
    track.SetLayer(pcbnew.F_Cu)
    track.SetWidth(pcbnew.FromMM(width))
    track.SetNet(nets[net])
    board.Add(track)
assert pcbnew.SaveBoard(str(candidate), board)
shutil.copyfile(settings, candidate.with_suffix(".kicad_pro"))
