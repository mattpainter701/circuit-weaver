"""Create and round-trip-check a prototype board in KiCad's own Python."""

from __future__ import annotations

import json
import math
import sys


def _point(pcbnew, x_mm: float, y_mm: float):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm))


def _assert_physical_only_unconnected(footprint, reference, *, npth_attribute=None):
    """Return every physical pad identity, rejecting any assigned net metadata."""
    result = []
    for pad in footprint.Pads():
        number = str(pad.GetNumber())
        if npth_attribute is not None and pad.GetAttribute() == npth_attribute:
            number = ""
        net_name = str(pad.GetNetname() or "")
        get_net_code = getattr(pad, "GetNetCode", None)
        if not callable(get_net_code):
            raise ValueError(f"{reference}: cannot verify physical-only pad net code")
        try:
            net_code = int(get_net_code())
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError(f"{reference}: cannot verify physical-only pad net code") from exc
        if net_name or net_code != 0:
            raise ValueError(
                f"{reference}: physical-only pad {number!r} carries stale net metadata "
                f"(name={net_name!r}, code={net_code})"
            )
        result.append((number, net_name, net_code))
    return sorted(result)


def _physical_only_identity(board, references, *, npth_refs=(), npth_attribute=None):
    expected_refs = set(references)
    result = {}
    for footprint in board.GetFootprints():
        reference = footprint.GetReference()
        if reference in expected_refs:
            if reference in result:
                raise ValueError(f"{reference}: duplicate physical-only footprint")
            result[reference] = _assert_physical_only_unconnected(
                footprint, reference, npth_attribute=npth_attribute if reference in npth_refs else None
            )
    if set(result) != expected_refs:
        raise ValueError("Physical-only footprint references changed during board round trip")
    return result


def _validate_physical_only_request(item, reference):
    if item.get("physical_only") and (item.get("symbol_pins") or item.get("nets")):
        raise ValueError(
            f"{reference}: physical-only component must not declare symbol pins or net assignments"
        )


def _assert_npth_geometry(footprint, records, pcbnew):
    """Verify every non-plated hole, allowing only KiCad's loss of its NC number."""
    expected = sorted(tuple(x["geometry"]) for x in records)
    allowed = {}
    for record in records:
        allowed.setdefault(tuple(record["geometry"]), set()).add(record["number"])
    actual = []
    for pad in footprint.Pads():
        if pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH:
            continue
        position, size, drill, offset = pad.GetFPRelativePosition(), pad.GetSize(), pad.GetDrillSize(), pad.GetOffset()
        shape = {pcbnew.PAD_SHAPE_CIRCLE: "circle", pcbnew.PAD_SHAPE_OVAL: "oval"}.get(pad.GetShape())
        angle = (pad.GetOrientation().AsDegrees() - footprint.GetOrientation().AsDegrees()) % 360
        values = [position.x, position.y, size.x, size.y, drill.x, drill.y]
        geometry = (shape, *(round(pcbnew.ToMM(v), 6) for v in values), round(angle, 6),
                    round(pcbnew.ToMM(offset.x), 6), round(pcbnew.ToMM(offset.y), 6))
        if geometry not in allowed or (str(pad.GetNumber()) and str(pad.GetNumber()) not in allowed[geometry]):
            raise ValueError(f"{footprint.GetReference()}: native NPTH geometry or source number changed")
        if str(pad.GetNetname() or "") or int(pad.GetNetCode()) != 0:
            raise ValueError(f"{footprint.GetReference()}: NPTH hole carries electrical net metadata")
        actual.append(geometry)
    if sorted(actual) != expected:
        raise ValueError(f"{footprint.GetReference()}: native NPTH hole inventory changed")


def _validate_npth_request(item, reference, explicit_no_connects):
    records = item.get("npth_pad_records", [])
    if not records and not item.get("normalized_npth_pin_numbers"):
        return
    numbers = {x["number"] for x in records if x["number"]}
    if numbers != set(item.get("normalized_npth_pin_numbers", [])):
        raise ValueError(f"{reference}: NPTH normalization declaration is inconsistent")
    if numbers & set(item.get("nets", {})):
        raise ValueError(f"{reference}: NPTH hole cannot receive an electrical net")
    if not item.get("physical_only"):
        if any((reference, n) not in explicit_no_connects for n in numbers):
            raise ValueError(f"{reference}: NPTH hole lacks an explicit no-connect proof")
        if set(item.get("symbol_pins", [])) - numbers != set(item["pad_numbers"]):
            raise ValueError(f"{reference}: electrical pin/pad identity differs after NPTH normalization")


def _identity(board, *, npth_refs=(), npth_attribute=None):
    result = {}
    for footprint in board.GetFootprints():
        reference = footprint.GetReference()
        if not reference or reference in result:
            raise ValueError("Board footprints require unique, nonempty references")
        pads = []
        for pad in footprint.Pads():
            if reference in npth_refs and pad.GetAttribute() == npth_attribute:
                continue
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
    minimum_drill_mm = request.get("minimum_drill_mm")
    if minimum_drill_mm is not None:
        minimum_drill_mm = float(minimum_drill_mm)
        if not math.isfinite(minimum_drill_mm) or minimum_drill_mm <= 0:
            raise ValueError("Minimum drill must be a finite positive millimeter value")
        board.GetDesignSettings().m_MinThroughDrill = pcbnew.FromMM(minimum_drill_mm)
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
    physical_only_refs = set()
    npth_by_ref = {}
    explicit_ncs = {(x['reference'], x['pin']) for x in request.get('explicit_no_connects', [])}
    loaded_physical_only_identity = {}
    for item in request["components"]:
        reference = str(item["reference"])
        if not reference or reference in expected:
            raise ValueError("Netlist contains an empty or duplicate board reference")
        _validate_physical_only_request(item, reference)
        _validate_npth_request(item, reference, explicit_ncs)
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
        is_physical_only = bool(item.get("physical_only"))
        if is_physical_only:
            loaded_physical_only_identity[reference] = _assert_physical_only_unconnected(
                footprint, reference, npth_attribute=pcbnew.PAD_ATTRIB_NPTH if item.get('npth_pad_records') else None
            )
            physical_only_refs.add(reference)
        if item.get('npth_pad_records'):
            _assert_npth_geometry(footprint, item['npth_pad_records'], pcbnew)
            npth_by_ref[reference] = item['npth_pad_records']
        pads = []
        pad_numbers = set()
        for pad in footprint.Pads():
            if reference in npth_by_ref and pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                continue
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
    if physical_only_refs and _physical_only_identity(
        board, physical_only_refs, npth_refs=npth_by_ref, npth_attribute=pcbnew.PAD_ATTRIB_NPTH
    ) != loaded_physical_only_identity:
        raise ValueError("Physical-only footprint pad identity changed before board save")
    for footprint in board.GetFootprints():
        if footprint.GetReference() in npth_by_ref:
            _assert_npth_geometry(footprint, npth_by_ref[footprint.GetReference()], pcbnew)
    if not pcbnew.SaveBoard(output, board):
        raise ValueError("KiCad could not save the prototype board")
    reloaded = pcbnew.LoadBoard(output)
    if reloaded is None:
        raise ValueError("KiCad could not reload the saved prototype board")
    for footprint in reloaded.GetFootprints():
        if footprint.GetReference() in npth_by_ref:
            _assert_npth_geometry(footprint, npth_by_ref[footprint.GetReference()], pcbnew)
    actual = _identity(reloaded, npth_refs=npth_by_ref, npth_attribute=pcbnew.PAD_ATTRIB_NPTH)
    if physical_only_refs and _physical_only_identity(
        reloaded, physical_only_refs, npth_refs=npth_by_ref, npth_attribute=pcbnew.PAD_ATTRIB_NPTH
    ) != loaded_physical_only_identity:
        raise ValueError("Saved board changed physical-only pad/net identity during round trip")
    if actual != expected:
        differing = {
            reference: {"expected": expected.get(reference), "actual": actual.get(reference)}
            for reference in sorted(set(expected) | set(actual))
            if expected.get(reference) != actual.get(reference)
        }
        raise ValueError(f"Saved board changed footprint pad/net identity during round trip: {differing}")
    result = {
        "status": "ok",
        "footprints": len(actual),
        "pads": sum(len(pads) for pads in actual.values()),
        "nets": len(net_by_name),
    }
    if physical_only_refs:
        result["physical_only_footprints"] = len(physical_only_refs)
        result["physical_only_pad_count"] = sum(
            len(pads) for pads in loaded_physical_only_identity.values()
        )
    return result


if __name__ == "__main__":
    try:
        # Retain the app for the native worker's lifetime; create no window.
        if sys.platform == "win32":
            import wx
            _native_wx_app = wx.GetApp() or wx.App(False)
            _native_wx_log = wx.LogStderr()
            wx.Log.SetActiveTarget(_native_wx_log)
        result = execute(json.load(sys.stdin))
    except Exception as exc:
        result = {"status": "error", "message": str(exc)}
    print("CW_SCHEMATIC_PCB_RESULT=" + json.dumps(result, ensure_ascii=True))
    raise SystemExit(0 if result.get("status") == "ok" else 1)
