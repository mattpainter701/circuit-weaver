"""Read-only, evidence-bounded measurements of explicitly constrained PCB routes.

Input geometry is expressed in millimetres and native layer IDs. This module
does not infer electrical contact from copper width or shape: line centerlines
must meet exactly, and endpoint pads must lie on a same-net centerline. Missing
geometry, same-net zones, arcs, and unsupported constraints therefore remain
unverified. A complete inventory of unrelated zone nets does not substitute
for centerline connectivity and does not block an explicit trace measurement.
"""
from __future__ import annotations

import heapq
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

_MAX_ROUTE_TRACKS = 2000
_MAX_ROUTE_VIAS = 2000
_ROUTE_FIELDS = {"net", "from", "to", "source", "max_length_mm", "max_vias"}
_PAIR_FIELDS = {"id", "positive_route", "negative_route", "source", "max_skew_mm"}


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _point(value):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        return None
    if not all(_number(part) for part in value):
        return None
    return float(value[0]), float(value[1])


def _key(point, layer):
    # Native KiCad coordinates are integer IU; six decimal places in mm keeps
    # shared vertices stable after conversion while avoiding broad snapping.
    return (round(point[0], 6), round(point[1], 6), layer)


def _on_segment(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.dist(point, a) <= 1e-6
    t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length2
    if t < -1e-9 or t > 1 + 1e-9:
        return False
    return math.dist(point, (a[0] + t * dx, a[1] + t * dy)) <= 1e-6


def _intersection(a, b, c, d):
    """Return a finite segment intersection, including collinear endpoints."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    qx, qy = c[0] - a[0], c[1] - a[1]
    if abs(den) > 1e-12:
        t, u = (qx * sy - qy * sx) / den, (qx * ry - qy * rx) / den
        if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
            return (a[0] + min(1, max(0, t)) * rx,
                    a[1] + min(1, max(0, t)) * ry)
        return None
    if abs(qx * ry - qy * rx) > 1e-9:
        return None
    for p in (a, b, c, d):
        if _on_segment(p, a, b) and _on_segment(p, c, d):
            return p
    return None


def _endpoint(inspection, endpoint, net):
    if not isinstance(endpoint, Mapping):
        return None
    ref, pad_number = endpoint.get("ref"), str(endpoint.get("pad", ""))
    if not ref or not pad_number:
        return None
    matches = []
    footprints = inspection.get("footprints")
    if not isinstance(footprints, Sequence) or isinstance(footprints, (str, bytes)):
        return None
    for footprint in footprints:
        if not isinstance(footprint, Mapping):
            return None
        if footprint.get("ref") != ref:
            continue
        pads = footprint.get("pads")
        if not isinstance(pads, Sequence) or isinstance(pads, (str, bytes)):
            return None
        for pad in pads:
            if not isinstance(pad, Mapping):
                return None
            if str(pad.get("number", "")) == pad_number and pad.get("net") == net:
                point = _point((pad.get("x"), pad.get("y")))
                layers = pad.get("enabled_copper_layers")
                stack = _native_copper_stack_ids(inspection)
                if "enabled_copper_layers" not in pad:
                    # Older planar inventories explicitly identified one pad
                    # layer but had no stack metadata. No layer transition is
                    # inferred from this backward-compatible contact proof.
                    raw = pad.get("copper_layers")
                    if (point is not None and isinstance(raw, Sequence)
                            and not isinstance(raw, (str, bytes)) and len(raw) == 1
                            and type(raw[0]) is int and (stack is None or raw[0] in stack)):
                        matches.append((point, tuple(raw)))
                    continue
                if (point is not None and isinstance(layers, Sequence)
                        and not isinstance(layers, (str, bytes)) and layers
                        and all(isinstance(layer, int) and not isinstance(layer, bool)
                                for layer in layers)
                        and len(layers) == len(set(layers)) and stack is not None
                        and all(layer in stack for layer in layers)):
                    matches.append((point, tuple(layers)))
    return matches[0] if len(matches) == 1 else None


def _valid_limit(value):
    return _number(value) and value >= 0


def _valid_id(value):
    return isinstance(value, str) and bool(value.strip())


def _native_copper_stack_ids(inspection):
    """Read a complete native physical copper order, never sort layer IDs."""
    rows = inspection.get("copper_layers")
    count = inspection.get("copper_layer_count")
    if (inspection.get("copper_layer_inventory_complete") is not True
            or not isinstance(rows, Sequence) or isinstance(rows, (str, bytes))
            or type(count) is not int or count < 2 or len(rows) != count):
        return None
    ids, names = [], []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        layer_id, name = row.get("id"), row.get("name")
        if (not isinstance(layer_id, int) or isinstance(layer_id, bool)
                or not isinstance(name, str) or not name.endswith(".Cu")):
            return None
        ids.append(layer_id)
        names.append(name)
    if (len(ids) != len(set(ids)) or len(names) != len(set(names))
            or names[0] != "F.Cu" or names[-1] != "B.Cu"):
        return None
    return ids


def _via_layer_span(inspection, via):
    """Validate the worker's ordered via span against the native board stack."""
    layers = via.get("layer_ids")
    stack = _native_copper_stack_ids(inspection)
    if (via.get("kind") != "via" or via.get("layer_span_complete") is not True
            or stack is None or not isinstance(layers, Sequence) or isinstance(layers, (str, bytes))
            or len(layers) < 2
            or any(not isinstance(layer, int) or isinstance(layer, bool) for layer in layers)
            or len(layers) != len(set(layers))):
        return None
    if any(layer not in stack for layer in layers):
        return None
    first, last = stack.index(layers[0]), stack.index(layers[-1])
    if first == last:
        return None
    low, high = min(first, last), max(first, last)
    expected = stack[low:high + 1]
    return list(layers) if list(layers) == expected else None


def _route(inspection, route_id, spec):
    result = {"id": route_id, "status": "unverified", "length_mm": None,
              "via_count": None, "reason": None}
    if not isinstance(spec, Mapping):
        result["reason"] = "route constraint is missing or malformed"
        return result
    if set(spec) - _ROUTE_FIELDS:
        result["reason"] = "route constraint contains unsupported fields"
        return result
    if spec.get("source") != "AUTHORED":
        result["reason"] = "route limits are not marked AUTHORED"
        return result
    if spec.get("model") is not None:
        result["reason"] = "electrical model claims are outside the measured route evidence"
        return result
    net = spec.get("net")
    start, finish = _endpoint(inspection, spec.get("from"), net), _endpoint(
        inspection, spec.get("to"), net)
    if not net or start is None or finish is None:
        result["reason"] = "explicit same-net endpoint pad evidence is incomplete"
        return result
    max_length, max_vias = spec.get("max_length_mm"), spec.get("max_vias")
    if max_length is not None and not _valid_limit(max_length):
        result["reason"] = "max_length_mm must be a finite nonnegative number"
        return result
    if max_vias is not None and (not isinstance(max_vias, int) or isinstance(max_vias, bool)
                                 or max_vias < 0):
        result["reason"] = "max_vias must be a nonnegative integer"
        return result

    zone_count = inspection.get("zone_count")
    if not isinstance(zone_count, int) or isinstance(zone_count, bool) or zone_count < 0:
        result["reason"] = "native inspection has no valid copper-zone inventory"
        return result
    zone_nets = inspection.get("zone_nets")
    if zone_count:
        if (inspection.get("zone_net_inventory_complete") is not True
                or not isinstance(zone_nets, Sequence) or isinstance(zone_nets, (str, bytes))
                or len(zone_nets) != zone_count
                or any(not isinstance(zone_net, str) or not zone_net for zone_net in zone_nets)):
            result["reason"] = "native inspection has no complete copper-zone net inventory"
            return result
        if net in zone_nets:
            result["reason"] = "same-net copper zone continuity is not measured"
            return result
    raw_tracks = inspection.get("tracks")
    if not isinstance(raw_tracks, Sequence) or isinstance(raw_tracks, (str, bytes)):
        result["reason"] = "native inspection has no complete track geometry inventory"
        return result
    tracks = []
    for row in raw_tracks:
        if not isinstance(row, Mapping) or "net" not in row:
            result["reason"] = "native inspection contains malformed track geometry"
            return result
        if row.get("net") != net:
            continue
        if row.get("kind", "segment") != "segment":
            result["reason"] = "route contains unsupported non-segment copper geometry"
            return result
        a, b, layer = _point(row.get("start_mm")), _point(row.get("end_mm")), row.get("layer_id")
        if a is None or b is None or not isinstance(layer, int) or isinstance(layer, bool):
            result["reason"] = "same-net track geometry is incomplete"
            return result
        tracks.append((a, b, layer))
    if not tracks:
        result["reason"] = "no explicit same-net track centerlines are available"
        return result
    if len(tracks) > _MAX_ROUTE_TRACKS:
        result["reason"] = f"same-net route exceeds the {_MAX_ROUTE_TRACKS}-segment analysis bound"
        return result

    adj = defaultdict(list)
    split_points = [[a, b] for a, b, _layer in tracks]
    via_contacts = []
    plated_pad_contacts = []
    raw_vias = inspection.get("vias")
    if not isinstance(raw_vias, Sequence) or isinstance(raw_vias, (str, bytes)):
        result["reason"] = "native inspection has no complete via geometry inventory"
        return result
    track_count = inspection.get("track_count")
    if (not isinstance(track_count, int) or isinstance(track_count, bool)
            or track_count != len(raw_tracks) + len(raw_vias)):
        result["reason"] = "native track geometry inventory is incomplete"
        return result
    route_vias = [via for via in raw_vias
                  if isinstance(via, Mapping) and via.get("net") == net]
    if any(not isinstance(via, Mapping) or "net" not in via for via in raw_vias):
        result["reason"] = "native inspection contains malformed via geometry"
        return result
    if len(route_vias) > _MAX_ROUTE_VIAS:
        result["reason"] = f"same-net route exceeds the {_MAX_ROUTE_VIAS}-via analysis bound"
        return result
    for via in route_vias:
        center = _point((via.get("x_mm"), via.get("y_mm")))
        layers = _via_layer_span(inspection, via)
        if center is None or layers is None:
            result["reason"] = "same-net via layer transition evidence is incomplete or unsupported"
            return result
        contacts = set()
        for index, (a, b, layer) in enumerate(tracks):
            if layer in layers and _on_segment(center, a, b):
                contacts.add(_key(center, layer))
                split_points[index].append(center)
        via_contacts.append(contacts)

    # A plated through-hole pad joins its native copper layers without adding
    # a routed via. Use only exact pad-center contacts: this is conservative
    # and avoids inferring connectivity from pad bounding boxes or shapes.
    footprints = inspection.get("footprints")
    if not isinstance(footprints, Sequence) or isinstance(footprints, (str, bytes)):
        result["reason"] = "native inspection has no complete footprint pad inventory"
        return result
    stack = _native_copper_stack_ids(inspection)
    for footprint in footprints:
        if not isinstance(footprint, Mapping):
            result["reason"] = "native inspection contains malformed footprint pad inventory"
            return result
        pads = footprint.get("pads")
        if not isinstance(pads, Sequence) or isinstance(pads, (str, bytes)):
            result["reason"] = "native inspection has incomplete footprint pad inventory"
            return result
        for pad in pads:
            if not isinstance(pad, Mapping):
                result["reason"] = "native inspection contains malformed pad inventory"
                return result
            if pad.get("net") != net or pad.get("plated_through") is not True:
                continue
            center = _point((pad.get("x"), pad.get("y")))
            layers = pad.get("enabled_copper_layers")
            if (center is None or stack is None
                    or not isinstance(layers, Sequence) or isinstance(layers, (str, bytes))
                    or len(layers) < 2
                    or any(not isinstance(layer, int) or isinstance(layer, bool) for layer in layers)
                    or len(layers) != len(set(layers))
                    or any(layer not in stack for layer in layers)):
                result["reason"] = "plated through-hole pad layer evidence is incomplete"
                return result
            contacts = set()
            for index, (a, b, layer) in enumerate(tracks):
                if layer in layers and _on_segment(center, a, b):
                    contacts.add(_key(center, layer))
                    split_points[index].append(center)
            if len(contacts) > 1:
                plated_pad_contacts.append(contacts)
    for i, (a, b, layer) in enumerate(tracks):
        for j in range(i):
            c, d, other_layer = tracks[j]
            if layer == other_layer:
                cross = _intersection(a, b, c, d)
                if cross is not None:
                    split_points[i].append(cross)
                    split_points[j].append(cross)
    for (a, b, layer), points in zip(tracks, split_points):
        for endpoint, allowed_layers in (start, finish):
            if layer in allowed_layers and _on_segment(endpoint, a, b):
                points.append(endpoint)
        points = sorted({_key(p, layer) for p in points},
                        key=lambda p: ((p[0] - a[0]) * (b[0] - a[0])
                                       + (p[1] - a[1]) * (b[1] - a[1])))
        for first, second in zip(points, points[1:]):
            length = math.dist(first[:2], second[:2])
            adj[first].append((second, length, 0))
            adj[second].append((first, length, 0))

    # One physical via can connect tracks on any layers in its verified span.
    # Every cross-layer edge has unit cost, so traversing a 4-layer through via
    # still counts as one via rather than three layer transitions.
    for contacted in via_contacts:
        for index, first in enumerate(sorted(contacted)):
            for second in sorted(contacted)[index + 1:]:
                adj[first].append((second, 0.0, 1))
                adj[second].append((first, 0.0, 1))
    for contacted in plated_pad_contacts:
        for index, first in enumerate(sorted(contacted)):
            for second in sorted(contacted)[index + 1:]:
                adj[first].append((second, 0.0, 0))
                adj[second].append((first, 0.0, 0))

    sources = {_key(start[0], layer) for layer in start[1]}
    targets = {_key(finish[0], layer) for layer in finish[1]}
    # Endpoint pads count as connected only when their exact center lies on a
    # same-net track centerline on one of the pad's explicit copper layers.
    if not any(node in adj for node in sources) or not any(node in adj for node in targets):
        result["reason"] = "endpoint pad center does not contact measured track centerlines"
        return result
    best = {node: (0.0, 0) for node in sources if node in adj}
    heap = [(0.0, 0, str(node), node) for node in best]
    heapq.heapify(heap)
    found = None
    while heap:
        length, vias, _stable, node = heapq.heappop(heap)
        if best.get(node) != (length, vias):
            continue
        if node in targets:
            found = node
            break
        for nxt, weight, via_cost in adj[node]:
            candidate = (length + weight, vias + via_cost)
            if nxt not in best or candidate < best[nxt]:
                best[nxt] = candidate
                heapq.heappush(heap, (*candidate, str(nxt), nxt))
    if found is None:
        if inspection.get("pad_connectivity_inventory_complete") is not True:
            result["reason"] = "native inspection has incomplete plated-pad connectivity evidence"
            return result
        result["status"], result["reason"] = "failed", "endpoint pads are disconnected in explicit copper graph"
        return result
    result.update(length_mm=best[found][0], via_count=best[found][1], reason=None)
    violated = ((max_length is not None and result["length_mm"] > max_length)
                or (max_vias is not None and result["via_count"] > max_vias))
    result["status"] = "failed" if violated else "verified"
    if violated:
        result["reason"] = "measured route exceeds an AUTHORED length or via limit"
    return result


def evaluate_route_quality(inspection, constraints):
    """Measure explicit endpoint routes and authored differential-pair skew.

    ``inspection`` contains ``footprints`` in native inspect format, plus
    ``tracks`` with ``net``, ``start_mm``, ``end_mm``, ``layer_id`` and optional
    ``kind`` (only ``segment`` is supported), and ``vias`` with ``net``,
    ``x_mm``, ``layer_ids`` in native physical copper order, and
    ``layer_span_complete=True`` backed by a complete native copper stack.
    ``constraints.routes`` maps
    route IDs to ``net``, ``from``/``to`` ``{ref, pad}``, ``source:
    "AUTHORED"``, and optional ``max_length_mm``/``max_vias``. ``pairs`` is a
    list of ``{id, positive_route, negative_route, source: "AUTHORED",
    max_skew_mm}`` records. Results are measurements and limit checks only;
    they do not establish impedance, plane continuity, RF performance, or
    qualification.
    """
    if not isinstance(inspection, Mapping) or not isinstance(constraints, Mapping):
        raise TypeError("inspection and constraints must be mappings")
    inspection_ok = inspection.get("status") == "ok"
    unsupported_contract_fields = set(constraints) - {"schema_version", "routes", "pairs"}
    contract_version = constraints.get("schema_version", 1)
    unsupported_contract_version = (
        not isinstance(contract_version, int) or isinstance(contract_version, bool)
        or contract_version != 1
    )
    routes_spec = constraints.get("routes", {})
    valid_routes_spec = isinstance(routes_spec, Mapping)
    if not valid_routes_spec:
        routes_spec = {}
    routes = {}
    for route_id, spec in routes_spec.items():
        if not _valid_id(route_id):
            diagnostic_id = "__invalid_route_id_0"
            suffix = 0
            while diagnostic_id in routes or diagnostic_id in routes_spec:
                suffix += 1
                diagnostic_id = f"__invalid_route_id_{suffix}"
            routes[diagnostic_id] = {
                "id": None, "status": "unverified", "length_mm": None,
                "via_count": None, "reason": "route ID must be a nonempty string",
            }
            continue
        if not inspection_ok:
            result = {"id": route_id, "status": "unverified", "length_mm": None,
                      "via_count": None, "reason": "native inspection status is missing or not ok"}
        elif unsupported_contract_fields or unsupported_contract_version:
            result = {"id": route_id, "status": "unverified", "length_mm": None,
                      "via_count": None, "reason": "route contract contains unsupported top-level fields"}
        else:
            result = _route(inspection, route_id, spec)
        routes[route_id] = result
    if not valid_routes_spec:
        diagnostic_id = "__invalid_routes_collection"
        suffix = 0
        while diagnostic_id in routes:
            suffix += 1
            diagnostic_id = f"__invalid_routes_collection_{suffix}"
        routes[diagnostic_id] = {
            "id": None, "status": "unverified", "length_mm": None,
            "via_count": None, "reason": "route collection is malformed",
        }
    pairs = []
    raw_pairs = constraints.get("pairs", ())
    if not isinstance(raw_pairs, Sequence) or isinstance(raw_pairs, (str, bytes)):
        pairs.append({"id": None, "status": "unverified", "skew_mm": None,
                      "reason": "differential-pair collection is malformed"})
        raw_pairs = ()
    for pair in raw_pairs:
        pair_id = pair.get("id") if isinstance(pair, Mapping) else None
        row = {"id": pair_id,
               "status": "unverified", "skew_mm": None, "reason": None}
        if not isinstance(pair, Mapping):
            row["reason"] = "differential-pair entry is malformed"
        elif not _valid_id(pair_id):
            row["reason"] = "differential-pair ID must be a nonempty string"
        elif pair.get("source") != "AUTHORED":
            row["reason"] = "differential-pair limit is missing or not AUTHORED"
        elif set(pair) - _PAIR_FIELDS:
            row["reason"] = "differential-pair constraint contains unsupported fields"
        elif not _valid_limit(pair.get("max_skew_mm")):
            row["reason"] = "max_skew_mm must be a finite nonnegative number"
        else:
            positive_id, negative_id = pair.get("positive_route"), pair.get("negative_route")
            if not _valid_id(positive_id) or not _valid_id(negative_id):
                row["reason"] = "differential route selectors must be nonempty strings"
                pairs.append(row)
                continue
            positive = routes.get(positive_id)
            negative = routes.get(negative_id)
            if not positive or not negative or positive["status"] != "verified" or negative["status"] != "verified":
                row["reason"] = "both differential routes must have verified centerline measurements"
            elif (positive_id == negative_id
                  or routes_spec[positive_id].get("net") == routes_spec[negative_id].get("net")):
                row["reason"] = "differential members must be distinct routes on distinct nets"
            else:
                row["skew_mm"] = abs(positive["length_mm"] - negative["length_mm"])
                row["status"] = "failed" if row["skew_mm"] > pair["max_skew_mm"] else "verified"
                if row["status"] == "failed":
                    row["reason"] = "measured differential-pair skew exceeds AUTHORED limit"
        pairs.append(row)
    return {"schema_version": 1, "routes": routes, "differential_pairs": pairs,
            "limitations": ["No impedance or field-solver model is evaluated.",
                            "Copper-zone continuity and RF qualification are not evaluated."]}


def inspect_route_quality(board_path, contracts, python_path=None):
    """Run the existing native read-only inspector, then evaluate contracts."""
    from .kicad_bridge import run_kicad

    inspection = run_kicad("inspect", board=str(board_path), python_path=python_path,
                           include_route_geometry=True)
    return evaluate_route_quality(inspection, contracts)


# Kept as a descriptive alias for early adopters of the module API.
measure_route_quality = evaluate_route_quality
