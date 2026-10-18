"""Small, conservative native router for explicitly requested return links.

This module deliberately imports no KiCad bindings.  The caller supplies the
pcbnew module from the same interpreter that loaded the private board copy.
Only newly added tracks and vias are created here; board edits are transactional
at the worker boundary (the caller must discard its private copy on error).
"""

import math


def _mm(pcbnew, p):
    return (pcbnew.ToMM(p.x), pcbnew.ToMM(p.y))


def _point(pcbnew, xy):
    return pcbnew.VECTOR2I(round(xy[0] * 1_000_000), round(xy[1] * 1_000_000))


def _dist_point_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    t = 0 if den == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_distance(a, b, c, d):
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    eps = 1e-12

    def on_segment(p, q, r):
        return (
            min(p[0], r[0]) - eps <= q[0] <= max(p[0], r[0]) + eps
            and min(p[1], r[1]) - eps <= q[1] <= max(p[1], r[1]) + eps
        )

    if (
        (o1 * o2 < 0 and o3 * o4 < 0)
        or (abs(o1) <= eps and on_segment(a, c, b))
        or (abs(o2) <= eps and on_segment(a, d, b))
        or (abs(o3) <= eps and on_segment(c, a, d))
        or (abs(o4) <= eps and on_segment(c, b, d))
    ):
        return 0.0
    return min(
        _dist_point_segment(a, c, d),
        _dist_point_segment(b, c, d),
        _dist_point_segment(c, a, b),
        _dist_point_segment(d, a, b),
    )


def _segment_box_distance(a, b, box):
    x0, y0, x1, y1 = box
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    return min(_segments_distance(a, b, corners[i], corners[(i + 1) % 4]) for i in range(4))


def _net_code(pad):
    return int(pad.GetNetCode())


def _pad_index(board):
    result = {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            key = (ref, str(pad.GetNumber()))
            if key in result:
                raise ValueError(f"duplicate physical pad {ref}.{key[1]}")
            result[key] = pad
    return result


def _layer_is_copper(pad, pcbnew):
    layers = pad.GetLayerSet()
    return bool(layers.Contains(pcbnew.F_Cu) or layers.Contains(pcbnew.B_Cu))


def _pad_box(pad, pcbnew, clearance):
    b = pad.GetBoundingBox()
    x0, y0 = pcbnew.ToMM(b.GetLeft()), pcbnew.ToMM(b.GetTop())
    x1, y1 = pcbnew.ToMM(b.GetRight()), pcbnew.ToMM(b.GetBottom())
    return (min(x0, x1) - clearance, min(y0, y1) - clearance, max(x0, x1) + clearance, max(y0, y1) + clearance)


def _track_ends(track, pcbnew):
    return _mm(pcbnew, track.GetStart()), _mm(pcbnew, track.GetEnd())


def _candidate_paths(a, b):
    """Generate compact 0/45/90-degree front-layer paths, shortest first."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    sx = 1 if dx >= 0 else -1
    sy = 1 if dy >= 0 else -1
    d = min(abs(dx), abs(dy))
    if abs(dx) < 1e-9 or abs(dy) < 1e-9 or abs(abs(dx) - abs(dy)) < 1e-9:
        return [[a, b]]
    p1 = (a[0] + sx * d, a[1] + sy * d)
    p2 = (b[0] - sx * d, b[1] - sy * d)
    paths = [[a, p1, b], [a, p2, b]]
    # A pair of orthogonal bends is useful where the diagonal corner is blocked.
    paths += [[a, (b[0], a[1]), b], [a, (a[0], b[1]), b]]
    # Sweep modest parallel corridors around local copper.  These still use
    # only horizontal and vertical segments and stay short enough to compare
    # against the caller's explicit length ceiling.
    for offset in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0):
        for sign in (-1, 1):
            y = a[1] + sign * offset
            paths.append([a, (a[0], y), (b[0], y), b])
            x = a[0] + sign * offset
            paths.append([a, (x, a[1]), (x, b[1]), b])
    return sorted(paths, key=lambda p: sum(math.dist(p[i], p[i + 1]) for i in range(len(p) - 1)))


def _bounds(board, pcbnew):
    box = board.GetBoardEdgesBoundingBox()
    if box is None or box.GetWidth() <= 0 or box.GetHeight() <= 0:
        raise ValueError("board requires a closed outline")
    x0, y0 = pcbnew.ToMM(box.GetLeft()), pcbnew.ToMM(box.GetTop())
    x1, y1 = pcbnew.ToMM(box.GetRight()), pcbnew.ToMM(box.GetBottom())
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _valid_path(path, net, endpoint_pads, board, pcbnew, clearance, width, bounds, layer=None):
    layer = pcbnew.F_Cu if layer is None else layer
    margin = clearance + width / 2
    x0, y0, x1, y1 = bounds
    for p in path:
        if p[0] < x0 + margin or p[0] > x1 - margin or p[1] < y0 + margin or p[1] > y1 - margin:
            return False
    tracks = list(board.GetTracks())
    pads = [pad for fp in board.GetFootprints() for pad in fp.Pads()]
    for a, b in zip(path, path[1:]):
        segment = pcbnew.PCB_TRACK(board)
        segment.SetStart(_point(pcbnew, a))
        segment.SetEnd(_point(pcbnew, b))
        segment.SetWidth(pcbnew.FromMM(width))
        segment.SetLayer(layer)
        for pad in pads:
            if _net_code(pad) == net or not pad.GetLayerSet().Contains(layer):
                continue
            if _copper_collision(segment, pad, layer, clearance, pcbnew):
                return False
        for tr in tracks:
            if tr.GetClass() == "PCB_ARC":
                raise ValueError("native return routing does not support existing copper arcs")
            if tr.GetClass() == "PCB_VIA":
                if int(tr.GetNetCode()) != net and _copper_collision(segment, tr, layer, clearance, pcbnew):
                    return False
                continue
            if tr.GetClass() != "PCB_TRACK":
                raise ValueError(f"native return routing cannot safely classify copper object {tr.GetClass()}")
            if int(tr.GetNetCode()) == net or tr.GetLayer() != layer:
                continue
            if _copper_collision(segment, tr, layer, clearance, pcbnew):
                return False
    return True


def _via_clear(point, net, endpoint_pads, board, pcbnew, clearance, diameter, bounds, ignored_uuids=()):
    radius = diameter / 2 + clearance
    x0, y0, x1, y1 = bounds
    if point[0] < x0 + radius or point[0] > x1 - radius or point[1] < y0 + radius or point[1] > y1 - radius:
        return False
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(_point(pcbnew, point))
    via.SetWidth(pcbnew.FromMM(diameter))
    via.SetDrill(pcbnew.FromMM(diameter / 2))
    via.SetViaType(pcbnew.VIATYPE_THROUGH)
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            # Require the full via annulus and clearance outside every pad,
            # including the requested endpoints, to prevent via-in-pad.
            layers = pad.GetLayerSet()
            if not (layers.Contains(pcbnew.F_Cu) or layers.Contains(pcbnew.B_Cu)):
                continue
            if any(
                layers.Contains(layer) and _copper_collision(via, pad, layer, clearance, pcbnew)
                for layer in (pcbnew.F_Cu, pcbnew.B_Cu)
            ):
                return False
        # A through via exposes mask openings on both sides. Preserve every
        # visible field's clearance even though fields themselves do not move.
        if hasattr(fp, "Reference"):
            for field in (fp.Reference(), fp.Value(), *list(fp.GetFields())):
                if field.IsVisible() and field.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS):
                    box = _pad_box(field, pcbnew, diameter / 2 + 0.2)
                    if box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]:
                        return False
    for item in board.GetTracks():
        if ignored_uuids and item.m_Uuid.AsString() in ignored_uuids:
            continue
        if item.GetClass() == "PCB_ARC":
            raise ValueError("native return routing does not support existing copper arcs")
        if item.GetClass() == "PCB_VIA":
            if any(_copper_collision(via, item, layer, clearance, pcbnew) for layer in (pcbnew.F_Cu, pcbnew.B_Cu)):
                return False
            continue
        if item.GetClass() != "PCB_TRACK":
            raise ValueError(f"native return routing cannot safely classify copper object {item.GetClass()}")
        if int(item.GetNetCode()) == net:
            continue
        if item.GetLayer() not in (pcbnew.F_Cu, pcbnew.B_Cu):
            continue
        if _copper_collision(via, item, item.GetLayer(), clearance, pcbnew):
            return False
    return True


def _copper_collision(first, second, layer, clearance, pcbnew):
    """Effective native copper, rather than rectangular pad approximations."""
    try:
        a, b = first.GetEffectiveShape(layer), second.GetEffectiveShape(layer)
        if a is None or b is None:
            raise ValueError("Native copper shape is unavailable")
        return bool(a.Collide(b, pcbnew.FromMM(clearance)))
    except (AttributeError, TypeError, RuntimeError) as exc:
        raise ValueError("Native copper collision verification is unavailable") from exc


def _fanouts(point, toward, away=None):
    result = []
    base = math.degrees(math.atan2(toward[1] - point[1], toward[0] - point[0]))
    bases = [base]
    if away is not None:
        bases.insert(0, math.degrees(math.atan2(away[1] - point[1], away[0] - point[0])))
    for distance in (0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0):
        for root in bases:
            for delta in (0, -15, 15, -30, 30, 45, -45, 90, -90, 135, -135, 180):
                angle = root + delta
                rad = math.radians(angle)
                result.append((point[0] + distance * math.cos(rad), point[1] + distance * math.sin(rad)))
    return result


def _add_via(board, pcbnew, point, net, diameter, drill):
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(_point(pcbnew, point))
    via.SetWidth(pcbnew.FromMM(diameter))
    via.SetDrill(pcbnew.FromMM(drill))
    via.SetViaType(pcbnew.VIATYPE_THROUGH)
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    via.SetNetCode(net)
    board.Add(via)


def _add_path(board, pcbnew, path, net, width, layer=None):
    for a, b in zip(path, path[1:]):
        if math.dist(a, b) <= 1e-6:
            continue
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(_point(pcbnew, a))
        track.SetEnd(_point(pcbnew, b))
        track.SetWidth(pcbnew.FromMM(width))
        track.SetLayer(pcbnew.F_Cu if layer is None else layer)
        track.SetNetCode(net)
        board.Add(track)


def _path_length(path):
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def _escape_paths(point, toward, away, net, pads, board, pcbnew, clearance, width, diameter, bounds):
    """Allow a bend after leaving a fine-pitch pad before the via landing."""
    result = []
    existing = {
        _mm(pcbnew, item.GetPosition())
        for item in board.GetTracks()
        if item.GetClass() == "PCB_VIA" and int(item.GetNetCode()) == net
    }
    for landing in [*sorted(existing), *_fanouts(point, toward, away)]:
        reuse = landing in existing
        if not reuse and not _via_clear(landing, net, pads, board, pcbnew, clearance, diameter, bounds):
            continue
        for path in _candidate_paths(point, landing)[:8]:
            if _valid_path(path, net, pads, board, pcbnew, clearance, width, bounds):
                result.append((landing, path, _path_length(path), reuse))
                break
    return result


def refine(board, pcbnew, connections, options=None):
    """Add explicitly requested, short front-copper return links.

    Returns a native report; raises ValueError if any link cannot be safely
    routed. Existing board objects are never modified.
    """
    options = options or {}
    if int(board.GetDesignSettings().GetCopperLayerCount()) != 2:
        raise ValueError("native return routing supports two-layer boards only")
    if list(board.Zones()):
        raise ValueError("native return routing requires a board without copper zones")
    if not connections:
        return {"status": "ok", "connections": [], "added_tracks": 0, "added_vias": 0}
    pads = _pad_index(board)
    clearance = float(options.get("clearance_mm", 0.127))
    width = float(options.get("width_mm", 0.2))
    via_diameter = float(options.get("via_diameter_mm", 0.6))
    via_drill = float(options.get("via_drill_mm", 0.3))
    bounds = _bounds(board, pcbnew)
    report = []
    for row in connections:
        fr, fp = str(row["from_ref"]), str(row["from_pad"])
        tr, tp = str(row["to_ref"]), str(row["to_pad"])
        try:
            src, dst = pads[(fr, fp)], pads[(tr, tp)]
        except KeyError as exc:
            raise ValueError(f"unknown physical pad {exc.args[0][0]}.{exc.args[0][1]}") from None
        if src is dst:
            raise ValueError(f"connection {fr}.{fp} to {tr}.{tp} names one physical pad twice")
        net = _net_code(src)
        if net <= 0 or _net_code(dst) != net:
            raise ValueError(f"connection {fr}.{fp} to {tr}.{tp} must use the same nonempty net")
        if not _layer_is_copper(src, pcbnew) or not _layer_is_copper(dst, pcbnew):
            raise ValueError(f"connection {fr}.{fp} to {tr}.{tp} includes a non-copper pad")
        if not src.GetLayerSet().Contains(pcbnew.F_Cu) or not dst.GetLayerSet().Contains(pcbnew.F_Cu):
            raise ValueError(f"connection {fr}.{fp} to {tr}.{tp} requires front-copper-accessible pads")
        a, b = _mm(pcbnew, src.GetPosition()), _mm(pcbnew, dst.GetPosition())
        source_fp = board.FindFootprintByReference(fr)
        target_fp = board.FindFootprintByReference(tr)
        source_center = _mm(pcbnew, source_fp.GetPosition())
        target_center = _mm(pcbnew, target_fp.GetPosition())
        source_away = (a[0] + (a[0] - source_center[0]), a[1] + (a[1] - source_center[1]))
        target_away = (b[0] + (b[0] - target_center[0]), b[1] + (b[1] - target_center[1]))
        candidate_count = 0
        chosen = None
        max_length = row.get("max_length_mm", math.inf)
        if isinstance(max_length, bool) or not isinstance(max_length, (int, float)) or max_length <= 0:
            raise ValueError("max_length_mm must be positive")
        if "max_length_mm" in row and not math.isfinite(max_length):
            raise ValueError("max_length_mm must be finite")
        for path in _candidate_paths(a, b):
            candidate_count += 1
            if _path_length(path) <= max_length + 1e-6 and _valid_path(
                path, net, (src, dst), board, pcbnew, clearance, width, bounds
            ):
                chosen = path
                break
        via_points = []
        bridge = None
        layer = "F.Cu"
        if chosen is None:
            starts = _escape_paths(
                a, b, source_away, net, (src, dst), board, pcbnew, clearance, width, via_diameter, bounds
            )
            ends = _escape_paths(
                b, a, target_away, net, (src, dst), board, pcbnew, clearance, width, via_diameter, bounds
            )
            pairs = sorted(
                (
                    (s[2] + math.dist(s[0], t[0]) + t[2], s, t)
                    for s in starts
                    for t in ends
                    if s[2] + math.dist(s[0], t[0]) + t[2] <= max_length + 1e-6
                ),
                key=lambda item: item[0],
            )
            for _, source_escape, target_escape in pairs[:128]:
                s, t = source_escape[0], target_escape[0]
                for path in _candidate_paths(s, t):
                    candidate_count += 1
                    if source_escape[2] + _path_length(path) + target_escape[2] <= max_length + 1e-6 and _valid_path(
                        path, net, (src, dst), board, pcbnew, clearance, width, bounds, pcbnew.B_Cu
                    ):
                        chosen = source_escape[1]
                        return_escape = list(reversed(target_escape[1]))
                        bridge = path
                        via_points = [escape[0] for escape in (source_escape, target_escape) if not escape[3]]
                        layer = "B.Cu"
                        break
                if bridge is not None:
                    break
            if bridge is None:
                raise ValueError(f"no clear front or two-layer path found for {fr}.{fp} to {tr}.{tp}")
        length = (
            _path_length(chosen)
            if bridge is None
            else _path_length(chosen) + _path_length(bridge) + _path_length(return_escape)
        )
        if length > max_length + 1e-6:
            raise ValueError(f"no clear path within max_length_mm for {fr}.{fp} to {tr}.{tp}")
        _add_path(board, pcbnew, chosen, net, width)
        if bridge is not None:
            _add_path(board, pcbnew, bridge, net, width, pcbnew.B_Cu)
            _add_path(board, pcbnew, return_escape, net, width)
            for point in via_points:
                _add_via(board, pcbnew, point, net, via_diameter, via_drill)
            chosen = chosen + bridge[1:] + return_escape[1:]
        track_count = len(chosen) - 1
        report.append(
            {
                "from_ref": fr,
                "from_pad": fp,
                "to_ref": tr,
                "to_pad": tp,
                "net": src.GetNetname(),
                "status": "routed",
                "path_mm": [[round(x, 4), round(y, 4)] for x, y in chosen],
                "layer": layer,
                "planar_length_mm": round(length, 4),
                "candidate_count": candidate_count,
                "added_tracks": track_count,
                "added_vias": len(via_points),
            }
        )
    return {
        "status": "ok",
        "connections": report,
        "added_tracks": sum(r["added_tracks"] for r in report),
        "added_vias": sum(r["added_vias"] for r in report),
    }
