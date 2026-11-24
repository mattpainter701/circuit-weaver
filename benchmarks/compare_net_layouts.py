"""Draw two native PCB snapshots on one scale. Run with KiCad's Python."""

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def read_layout(path, net):
    import pcbnew

    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise ValueError(f"KiCad could not read {path}")

    def xy(vector):
        return pcbnew.ToMM(vector.x), pcbnew.ToMM(vector.y)

    bodies, context, pads, tracks, vias, extents = [], [], [], [], [], []
    for footprint in board.GetFootprints():
        box = footprint.GetBoundingBox(False, False)
        bodies.append((xy(box.GetOrigin()), xy(box.GetEnd())))
        for pad in footprint.Pads():
            point = xy(pad.GetPosition())
            context.append(point)
            if pad.GetNetname() == net:
                pads.append((f"{footprint.GetReference()}.{pad.GetNumber()}", point))
                extents.append(point)
    for item in board.GetTracks():
        if item.GetNetname() != net:
            continue
        if isinstance(item, pcbnew.PCB_VIA):
            point = xy(item.GetPosition())
            vias.append(point)
            extents.append(point)
            continue
        start, end = xy(item.GetStart()), xy(item.GetEnd())
        arc = None
        if isinstance(item, pcbnew.PCB_ARC):
            mid = xy(item.GetMid())
            radius = pcbnew.ToMM(item.GetRadius())
            # Native y increases downwards, as does the SVG viewport.
            cross = ((mid[0] - start[0]) * (end[1] - mid[1])
                     - (mid[1] - start[1]) * (end[0] - mid[0]))
            arc = (radius, int(abs(item.GetAngle().AsDegrees()) > 180), int(cross > 0))
            box = item.GetBoundingBox()
            extents.extend([xy(box.GetOrigin()), xy(box.GetEnd())])
        tracks.append((board.GetLayerName(item.GetLayer()), start, end, arc, pcbnew.ToMM(item.GetLength())))
        extents.extend([start, end])
    if not extents:
        raise ValueError(f"No pads, traces or vias found for {net!r} in {path.name}")
    return dict(bodies=bodies, context=context, pads=sorted(pads), tracks=tracks, vias=vias, extents=extents)


def element(parent, tag, **attrs):
    return ET.SubElement(parent, tag, {k.replace("_", "-"): str(v) for k, v in attrs.items()})


def label(parent, x, y, value, size=15, color="#25394a", **attrs):
    element(parent, "text", x=x, y=y, fill=color, font_size=size, **attrs).text = value


def comparison(net, layouts, titles):
    layers = sorted({track[0] for layout in layouts for track in layout["tracks"]})
    width, height = 1024, 638 + 21 * ((len(layers) + 2) // 8)
    root = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg", "viewBox": f"0 0 {width} {height}",
                              "width": str(width), "height": str(height), "font-family": "Inter, Arial, sans-serif"})
    element(root, "title").text = f"{net} — placement and routing comparison"
    element(root, "rect", width=width, height=height, fill="#f6f5ef")
    label(root, 30, 30, "CIRCUIT WEAVER   /   NET REVIEW", 13, "#48616a", letter_spacing="1.5")
    label(root, 30, 71, net, 35, font_weight="bold")
    label(root, 30, 98, "Footprint bounds, pad centers and trace centerlines at a common scale", 15)
    xs, ys = zip(*(p for layout in layouts for p in layout["extents"]))
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    scale = min(884 / max(1, max(xs) - min(xs)), 142 / max(1, max(ys) - min(ys)))
    def unique_pads(layout):
        by_name = {}
        for name, point in layout["pads"]:
            by_name[name] = point if name not in by_name else None
        return by_name

    first, last = (unique_pads(layout) for layout in layouts)
    moved = [(first[key], last[key]) for key in sorted(first.keys() & last.keys())
             if first[key] and last[key] and first[key] != last[key]]
    colors = {"F.Cu": "#11746c", "B.Cu": "#a54528"}
    for i, layer in enumerate(k for k in layers if k not in colors):
        colors[layer] = f"hsl({(230 + 47 * i) % 360}, 55%, 38%)"
    for index, (layout, title) in enumerate(zip(layouts, titles)):
        top = 116 + index * 231
        element(root, "rect", x=30, y=top, width=964, height=218, rx=8, fill="#ffffff", stroke="#cfdbd7")
        label(root, 48, top + 27, title, 19, font_weight="bold")
        length = sum(track[4] for track in layout["tracks"])
        via_count = len(layout["vias"])
        via_unit = "via" if via_count == 1 else "vias"
        pieces = len(layout["tracks"])
        piece_unit = "track piece" if pieces == 1 else "track pieces"
        summary = f"{length:.2f} mm   ·   {pieces} {piece_unit}   ·   {via_count} {via_unit}"
        label(root, 976, top + 27, summary, 15, text_anchor="end")

        def project(point):
            return 512 + (point[0] - cx) * scale, top + 124 + (point[1] - cy) * scale

        for start, end in layout["bodies"]:
            x1, y1 = project(start)
            x2, y2 = project(end)
            x1, x2 = max(48, x1), min(976, x2)
            y1, y2 = max(top + 45, y1), min(top + 208, y2)
            if x1 < x2 and y1 < y2:
                element(root, "rect", x=x1, y=y1, width=x2-x1, height=y2-y1, fill="#f6f8f7",
                        stroke="#d7dfdd", stroke_width=0.7)
        for point in layout["context"]:
            x, y = project(point)
            if 48 < x < 976 and top + 47 < y < top + 204:
                element(root, "circle", cx=f"{x:.2f}", cy=f"{y:.2f}", r=1.6, fill="#c9d1d2")
        if index == 1:
            for old, new in moved:
                x1, y1 = project(old)
                x2, y2 = project(new)
                element(root, "line", x1=x1, y1=y1, x2=x2, y2=y2, stroke="#716193",
                        stroke_width=1.5, stroke_dasharray="5 3")
                element(root, "circle", cx=x1, cy=y1, r=3, fill="white", stroke="#716193")
        for layer, start, end, arc, _length in layout["tracks"]:
            x1, y1 = project(start)
            x2, y2 = project(end)
            cmd = "L" if arc is None else f"A {arc[0] * scale:.4f} {arc[0] * scale:.4f} 0 {arc[1]} {arc[2]}"
            element(root, "path", d=f"M {x1:.3f} {y1:.3f} {cmd} {x2:.3f} {y2:.3f}",
                    fill="none", stroke=colors[layer], stroke_width=2.4, stroke_linecap="round")
        for point in layout["vias"]:
            x, y = project(point)
            element(root, "circle", cx=f"{x:.2f}", cy=f"{y:.2f}", r=3.2, fill="white",
                    stroke="#2b4256", stroke_width=1.4)
        for name, point in layout["pads"]:
            x, y = project(point)
            marker = element(root, "rect", x=f"{x-3.5:.2f}", y=f"{y-3.5:.2f}", width=7, height=7,
                             fill="#25394a", stroke="white")
            element(marker, "title").text = name
            if len(layout["pads"]) <= 4:
                align = "start" if x <= width / 2 else "end"
                label(root, x + (7 if align == "start" else -7), y - 9, name, 13, text_anchor=align,
                      stroke="white", stroke_width=3, paint_order="stroke", font_weight="bold")
    legend = ["Pad", "Via", *layers]
    if moved:
        legend.append("Pad motion")
    for i, key in enumerate(legend):
        x, y = 46 + (i % 8) * 120, 601 + (i // 8) * 21
        if key == "Pad":
            element(root, "rect", x=x-3, y=y-4, width=7, height=7, fill="#25394a")
        elif key == "Via":
            element(root, "circle", cx=x, cy=y, r=4, stroke="#2b4256", fill="white")
        elif key == "Pad motion":
            element(root, "path", d=f"M {x-6} {y} h 13", stroke="#716193", stroke_dasharray="3 2")
        else:
            element(root, "path", d=f"M {x-6} {y} h 13", stroke=colors[key], stroke_width=3)
        label(root, x + 12, y + 5, key, 14)
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("boards", nargs=2, type=Path, metavar="BOARD")
    parser.add_argument("--net", required=True, help="Exact net name in both boards")
    parser.add_argument("--labels", nargs=2, default=["Before", "After"])
    parser.add_argument("--output", "-o", required=True, type=Path)
    args = parser.parse_args()
    if args.output.suffix.lower() != ".svg" or any(
            path.suffix.lower() != ".kicad_pcb" or not path.is_file() for path in args.boards):
        parser.error("Supply two existing KiCad PCBs and an .svg output path")
    try:
        layouts = [read_layout(path.resolve(), args.net) for path in args.boards]
        document = ET.tostring(comparison(args.net, layouts, args.labels), encoding="utf-8", xml_declaration=True)
        with args.output.open("xb") as stream:
            stream.write(document)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    sys.exit(main())
