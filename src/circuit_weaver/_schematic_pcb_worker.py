"""Create and round-trip-check a prototype board in KiCad's own Python."""

from __future__ import annotations

import json
import math
import sys


def _point(pcbnew, x_mm: float, y_mm: float):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm))


def _identity(board):
    result = {}
    for footprint in board.GetFootprints():
        reference = footprint.GetReference()
        if not reference or reference in result:
            raise ValueError("Board footprints require unique, nonempty references")
        pads = []
        for pad in footprint.Pads():
            number = str(pad.GetNumber())
            if number:
                pads.append((number, str(pad.GetNetname() or "")))
        result[reference] = sorted(pads)
    return result


def _add_outline(board, pcbnew, width_mm, height_mm):
    corners = ((0.0, 0.0), (width_mm, 0.0), (width_mm, height_mm), (0.0, height_mm))
    for index, start in enumerate(corners):
        end = corners[(index + 1) % len(corners)]
        edge = pcbnew.PCB_SHAPE(board)
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetStart(_point(pcbnew, *start))
        edge.SetEnd(_point(pcbnew, *end))
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetWidth(pcbnew.FromMM(0.05))
        board.Add(edge)


def execute(request):
    import pcbnew

    if request.get("operation") != "build":
        raise ValueError("Unknown prototype board operation")
    width, height = float(request["width_mm"]), float(request["height_mm"])
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise ValueError("Board dimensions must be finite positive numbers")
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    clearance_mm = request.get("clearance_mm")
    if clearance_mm is not None:
        clearance_mm = float(clearance_mm)
        if not math.isfinite(clearance_mm) or clearance_mm <= 0:
            raise ValueError("Clearance must be a finite positive millimeter value")
        # This applies only to the implicit default netclass of a newly-created
        # prototype board. The parent rejects explicit source board/rule settings.
        net_settings = board.GetDesignSettings().m_NetSettings
        # KiCad 8 exposes the default class as a member; 9/10 provide a getter.
        get_default = getattr(net_settings, "GetDefaultNetclass", None)
        default_class = get_default() if callable(get_default) else net_settings.m_DefaultNetClass
        default_class.SetClearance(pcbnew.FromMM(clearance_mm))
    title_block = board.GetTitleBlock()
    title_block.SetTitle("Prototype PCB - NOT FOR FABRICATION")
    title_block.SetComment(
        1,
        "Forward-annotated for review; verify identity, pin mapping, constraints, placement, routing, and DRC.",
    )
    _add_outline(board, pcbnew, width, height)

    net_by_name = {}
    for code, name in enumerate(request["nets"], start=1):
        if not name or name in net_by_name:
            raise ValueError("Netlist contains an empty or duplicate net name")
        net = pcbnew.NETINFO_ITEM(board, name, code)
        board.Add(net)
        net_by_name[name] = net

    expected = {}
    for item in request["components"]:
        reference = str(item["reference"])
        if not reference or reference in expected:
            raise ValueError("Netlist contains an empty or duplicate board reference")
        x, y = float(item["x_mm"]), float(item["y_mm"])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError(f"{reference}: invalid placement coordinates")
        footprint = pcbnew.FootprintLoad(item["library_path"], item["footprint_name"])
        if footprint is None:
            raise ValueError(f"{reference}: KiCad could not load {item['footprint']}")
        footprint.SetFPID(pcbnew.LIB_ID(item["library"], item["footprint_name"]))
        footprint.SetReference(reference)
        footprint.SetValue(str(item.get("value", "")))
        footprint.SetPosition(_point(pcbnew, x, y))
        footprint.SetLayer(pcbnew.F_Cu)
        pads = []
        pad_numbers = set()
        for pad in footprint.Pads():
            number = str(pad.GetNumber())
            if not number:
                continue
            pad_numbers.add(number)
            net_name = item["nets"].get(number)
            if net_name:
                if net_name not in net_by_name:
                    raise ValueError(f"{reference}.{number}: unknown net {net_name!r}")
                pad.SetNet(net_by_name[net_name])
            pads.append((number, net_name or ""))
        if pad_numbers != set(item["pad_numbers"]):
            raise ValueError(f"{reference}: loaded footprint pad numbers changed after preflight")
        if set(item["nets"]) - pad_numbers:
            raise ValueError(f"{reference}: net assignment refers to an absent footprint pad")
        expected[reference] = sorted(pads)
        board.Add(footprint)

    output = request["board"]
    if not pcbnew.SaveBoard(output, board):
        raise ValueError("KiCad could not save the prototype board")
    reloaded = pcbnew.LoadBoard(output)
    if reloaded is None:
        raise ValueError("KiCad could not reload the saved prototype board")
    actual = _identity(reloaded)
    if actual != expected:
        differing = {
            reference: {"expected": expected.get(reference), "actual": actual.get(reference)}
            for reference in sorted(set(expected) | set(actual))
            if expected.get(reference) != actual.get(reference)
        }
        raise ValueError(f"Saved board changed footprint pad/net identity during round trip: {differing}")
    return {
        "status": "ok",
        "footprints": len(actual),
        "pads": sum(len(pads) for pads in actual.values()),
        "nets": len(net_by_name),
    }


if __name__ == "__main__":
    try:
        result = execute(json.load(sys.stdin))
    except Exception as exc:
        result = {"status": "error", "message": str(exc)}
    print("CW_SCHEMATIC_PCB_RESULT=" + json.dumps(result, ensure_ascii=True))
    raise SystemExit(0 if result.get("status") == "ok" else 1)
