"""Create real, deliberately unplaced circuits with KiCad's installed libraries.

Executed by the native KiCad interpreter, never imported by the application.
"""

import json
import sys
from pathlib import Path

import pcbnew

root, output, case = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
board = pcbnew.BOARD()
for a, b in (((100, 70), (150, 70)), ((150, 70), (150, 105)),
             ((150, 105), (100, 105)), ((100, 105), (100, 70))):
    edge = pcbnew.PCB_SHAPE()
    edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
    edge.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
    edge.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(pcbnew.FromMM(0.05))
    board.Add(edge)

header = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
resistor = "Resistor_SMD:R_0603_1608Metric"
capacitor = "Capacitor_SMD:C_0603_1608Metric"
parts = [("J1", header, ["VIN", "GND"])]
if case == "led":
    parts += [("R1", resistor, ["VIN", "LED"]),
              ("D1", "LED_SMD:LED_0603_1608Metric", ["GND", "LED"])]
elif case in {"divider", "rc", "back"}:
    parts += [("J2", header, ["OUT", "GND"]), ("R1", resistor, ["VIN", "OUT"]),
              ("R2" if case == "divider" else "C1", resistor if case == "divider" else capacitor, ["OUT", "GND"])]
elif case == "soic":
    parts += [("U1", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", ["A", "B", "C", "GND", "D", "E", "F", "VIN"])]
    parts += [(f"R{i}", resistor, [net, "VIN" if i % 2 else "GND"])
              for i, net in enumerate("ABCDEF", 1)]
    parts += [("C1", capacitor, ["VIN", "GND"])]
else:
    raise ValueError(case)

nets = {}
for _, _, names in parts:
    for name in names:
        if name not in nets:
            net = pcbnew.NETINFO_ITEM(board, name)
            board.Add(net)
            nets[name] = net
for ref, library_id, names in parts:
    library, name = library_id.split(":")
    footprint = pcbnew.FootprintLoad(str(root / f"{library}.pretty"), name)
    if footprint is None:
        raise RuntimeError(f"Missing test footprint: {library_id}")
    board.Add(footprint)
    footprint.SetReference(ref)
    x, y = (104, 80) if ref == "J1" else (144, 80) if ref == "J2" else (125, 87)
    footprint.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    if case == "back" and ref == "C1":
        footprint.Flip(footprint.GetPosition(), False)
    footprint.SetOrientationDegrees(90 if ref == "J2" else 0)
    footprint.SetLocked(ref.startswith("J"))
    for pad in footprint.Pads():
        if pad.GetNumber().isdigit() and int(pad.GetNumber()) > 0:
            pad.SetNet(nets[names[int(pad.GetNumber()) - 1]])
output.parent.mkdir(parents=True, exist_ok=True)
assert pcbnew.SaveBoard(str(output), board)
if case == "rc":
    # Preserve the installed KiCad version's native project/schema metadata.
    # A hand-written partial project triggers different migrations in 8/9/10.
    project_path = output.with_suffix(".kicad_pro")
    if not project_path.is_file():
        # Some KiCad builds do not create the sidecar as a SaveBoard effect.
        # Ask the installed native settings manager to serialize the board's
        # real project settings rather than synthesizing a version-specific JSON.
        if not pcbnew.GetSettingsManager().SaveProject(str(project_path), board.GetProject()):
            raise RuntimeError(f"KiCad could not save native project settings: {project_path}")
        if not project_path.is_file():
            raise RuntimeError(f"KiCad did not create native project settings: {project_path}")
    project = json.loads(project_path.read_text())
    project["board"]["design_settings"]["rules"].update(min_track_width=0.3, min_clearance=0.25)
    default_class = next(c for c in project["net_settings"]["classes"] if c["name"] == "Default")
    default_class.update(track_width=0.35, clearance=0.25, via_diameter=0.7, via_drill=0.3)
    project_path.write_text(json.dumps(project))
    output.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "min-track" (constraint track_width (min 0.3mm)))\n')
print(json.dumps({"board": str(output), "case": case, "version": pcbnew.Version()}))
