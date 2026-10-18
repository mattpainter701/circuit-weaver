"""Conservative native copper geometry graph used by cleanup workers.

The graph intentionally models only track centerlines and native copper-shape
contacts. It does not infer electrical contact from distance alone.
"""
from __future__ import annotations

import heapq
import math
from collections import defaultdict, deque

EPS = 1e-7


def mm(pcbnew, iu):
    return float(pcbnew.ToMM(iu))


def point_mm(pcbnew, point):
    return mm(pcbnew, point.x), mm(pcbnew, point.y)


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def node_key(x, y, layer):
    return (round(x, 6), round(y, 6), str(layer))


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def segment_intersections(a, b, c, d):
    """Return parameters on AB where two finite segments intersect/overlap."""
    r, s = (b[0] - a[0], b[1] - a[1]), (d[0] - c[0], d[1] - c[1])
    den, q = _cross(r, s), (c[0] - a[0], c[1] - a[1])
    if abs(den) > EPS:
        t, u = _cross(q, s) / den, _cross(q, r) / den
        return [max(0.0, min(1.0, t))] if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS else []
    if abs(_cross(q, r)) > EPS:
        return []
    rr = r[0] * r[0] + r[1] * r[1]
    if rr <= EPS:
        return [0.0] if distance(a, c) <= EPS else []
    vals = [((p[0] - a[0]) * r[0] + (p[1] - a[1]) * r[1]) / rr for p in (c, d)]
    lo, hi = max(0.0, min(vals)), min(1.0, max(vals))
    return [max(0.0, min(1.0, lo)), max(0.0, min(1.0, hi))] if lo <= hi + EPS else []


def project_to_segment(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / denom)) if denom > EPS else 0.0
    return t, (a[0] + t * dx, a[1] + t * dy)


def _closest_segments(a, b, c, d):
    candidates = []
    for t, p in ((0.0, a), (1.0, b)):
        u, q = project_to_segment(p, c, d)
        candidates.append((distance(p, q), t, u, p, q))
    for u, q in ((0.0, c), (1.0, d)):
        t, p = project_to_segment(q, a, b)
        candidates.append((distance(p, q), t, u, p, q))
    _, t, u, p, q = min(candidates, key=lambda row: row[0])
    return t, u, p, q


def shape_collides(first, second):
    if first is None or second is None:
        return False
    try:
        return bool(first.Collide(second, 0))
    except (TypeError, RuntimeError, AttributeError):
        return False


class CopperGraph:
    """A rebuildable graph over copper on explicitly selected nets."""

    def __init__(self, board, pcbnew, nets=None, excluded_uuids=None):
        self.board, self.pcbnew = board, pcbnew
        self.selected = set(nets) if nets is not None else None
        excluded_uuids = set(excluded_uuids or ())
        self.adj = defaultdict(list)
        self.net_pads = defaultdict(list)
        self.pad_nodes = {}
        self.net_lengths = defaultdict(float)
        self.net_track_count = defaultdict(int)
        self.net_via_count = defaultdict(int)
        self.items = defaultdict(list)
        # Parallel provenance for each graph edge keeps the adjacency tuple
        # contract stable for existing callers while allowing cleanup tools
        # to freeze the concrete copper objects on a selected route.
        self.edge_items = defaultdict(set)
        tracks, vias = [], []
        for item in board.GetTracks():
            try:
                item_uuid = str(item.m_Uuid.AsString())
            except (AttributeError, RuntimeError):
                item_uuid = None
            if item_uuid in excluded_uuids:
                continue
            net = str(item.GetNetname())
            if not net or (self.selected is not None and net not in self.selected):
                continue
            self.items[net].append(item)
            if "VIA" in type(item).__name__.upper():
                vias.append(item)
                self.net_via_count[net] += 1
                continue
            a, b = point_mm(pcbnew, item.GetStart()), point_mm(pcbnew, item.GetEnd())
            layer_id = item.GetLayer()
            rec = {"item": item, "net": net, "a": a, "b": b,
                   "layer": str(item.GetLayerName()), "layer_id": layer_id,
                   "shape": item.GetEffectiveShape(layer_id), "splits": [0.0, 1.0]}
            tracks.append(rec)
            self.net_lengths[net] += distance(a, b)
            self.net_track_count[net] += 1

        grouped = defaultdict(list)
        for i, seg in enumerate(tracks):
            grouped[(seg["net"], seg["layer"])].append(i)
        for (net, _layer), ids in grouped.items():
            for off, i in enumerate(ids):
                a = tracks[i]
                for j in ids[off + 1:]:
                    b = tracks[j]
                    its = segment_intersections(a["a"], a["b"], b["a"], b["b"])
                    for t in its:
                        a["splits"].append(t)
                    for t in segment_intersections(b["a"], b["b"], a["a"], a["b"]):
                        b["splits"].append(t)

        track_contacts = []
        for (net, _layer), ids in grouped.items():
            for off, i in enumerate(ids):
                a = tracks[i]
                for j in ids[off + 1:]:
                    b = tracks[j]
                    if not shape_collides(a["shape"], b["shape"]):
                        continue
                    if segment_intersections(a["a"], a["b"], b["a"], b["b"]):
                        continue
                    t, u, pa, pb = _closest_segments(a["a"], a["b"], b["a"], b["b"])
                    a["splits"].append(t)
                    b["splits"].append(u)
                    track_contacts.append((i, t, j, u, distance(pa, pb), net))

        pad_contacts = []
        for fp in board.GetFootprints():
            ref = str(fp.GetReferenceAsString())
            for pad in fp.Pads():
                net, number = str(pad.GetNetname()), str(pad.GetNumber())
                if not net or (self.selected is not None and net not in self.selected):
                    continue
                pxy = point_mm(pcbnew, pad.GetPosition())
                pnode = ("PAD", ref, number, net)
                self.pad_nodes[(ref, number)] = (net, pnode)
                self.net_pads[net].append({"ref": ref, "pad": number, "xy": pxy, "node": pnode})
                for idx, seg in enumerate(tracks):
                    if seg["net"] != net or not pad.IsOnLayer(seg["layer_id"]):
                        continue
                    if shape_collides(pad.GetEffectiveShape(seg["layer_id"]), seg["shape"]):
                        t, contact = project_to_segment(pxy, seg["a"], seg["b"])
                        seg["splits"].append(t)
                        pad_contacts.append((pnode, idx, t, distance(pxy, contact), net))

        via_contacts = []
        for via in vias:
            net, center = str(via.GetNetname()), point_mm(pcbnew, via.GetPosition())
            layers = (int(via.TopLayer()), int(via.BottomLayer()))
            for idx, seg in enumerate(tracks):
                if seg["net"] != net or seg["layer_id"] not in layers:
                    continue
                if shape_collides(via.GetEffectiveShape(seg["layer_id"]), seg["shape"]):
                    t, contact = project_to_segment(center, seg["a"], seg["b"])
                    seg["splits"].append(t)
                    via_contacts.append((via, seg["layer_id"], idx, t, distance(contact, center), net))

        track_nodes = {}
        for idx, seg in enumerate(tracks):
            ts = sorted(set(round(t, 10) for t in seg["splits"]))
            nodes = []
            for t in ts:
                x = seg["a"][0] + t * (seg["b"][0] - seg["a"][0])
                y = seg["a"][1] + t * (seg["b"][1] - seg["a"][1])
                node = node_key(x, y, seg["layer"])
                nodes.append(node)
                track_nodes[(idx, t)] = node
            for u, v in zip(nodes, nodes[1:]):
                self._add_edge(u, v, distance(u[:2], v[:2]), seg["net"], item_ids=(
                    self._item_uuid(seg["item"]),
                ))
        for i, t, j, u, gap, net in track_contacts:
            self._add_edge(track_nodes[(i, round(t, 10))], track_nodes[(j, round(u, 10))], gap, net,
                           item_ids=(self._item_uuid(tracks[i]["item"]), self._item_uuid(tracks[j]["item"])))

        via_nodes = {}
        for via in vias:
            net, center = str(via.GetNetname()), point_mm(pcbnew, via.GetPosition())
            for layer_id in (int(via.TopLayer()), int(via.BottomLayer())):
                via_nodes[(id(via), layer_id)] = node_key(*center, board.GetLayerName(layer_id))
            top, bottom = int(via.TopLayer()), int(via.BottomLayer())
            if top != bottom:
                self._add_edge(via_nodes[(id(via), top)], via_nodes[(id(via), bottom)], 0.0, net, via=1,
                               item_ids=(self._item_uuid(via),))
        for via, layer_id, idx, t, access, net in via_contacts:
            self._add_edge(track_nodes[(idx, round(t, 10))], via_nodes[(id(via), layer_id)], access, net,
                           item_ids=(self._item_uuid(tracks[idx]["item"]), self._item_uuid(via)))

        # Native pad-to-via shape contacts (important for traces absent on one side).
        for fp in board.GetFootprints():
            ref = str(fp.GetReferenceAsString())
            for pad in fp.Pads():
                net = str(pad.GetNetname())
                if not net or (self.selected is not None and net not in self.selected):
                    continue
                pnode = self.pad_nodes[(ref, str(pad.GetNumber()))][1]
                pxy = point_mm(pcbnew, pad.GetPosition())
                for via in vias:
                    if str(via.GetNetname()) != net:
                        continue
                    center = point_mm(pcbnew, via.GetPosition())
                    for layer_id in (int(via.TopLayer()), int(via.BottomLayer())):
                        if pad.IsOnLayer(layer_id) and shape_collides(
                            pad.GetEffectiveShape(layer_id), via.GetEffectiveShape(layer_id)
                        ):
                            self._add_edge(pnode, via_nodes[(id(via), layer_id)], distance(pxy, center), net,
                                           item_ids=(self._item_uuid(via),))
        for pnode, idx, t, access, net in pad_contacts:
            self._add_edge(pnode, track_nodes[(idx, round(t, 10))], access, net,
                           item_ids=(self._item_uuid(tracks[idx]["item"]),))

        for node, edges in self.adj.items():
            unique, seen = [], set()
            for edge in edges:
                key = edge[:4]
                if key not in seen:
                    seen.add(key)
                    unique.append(edge)
            self.adj[node] = unique

    def _add_edge(self, a, b, length, net, via=0, item_ids=()):
        if not hasattr(self, "edge_items"):
            self.edge_items = defaultdict(set)
        self.adj[a].append((b, length, via, net))
        self.adj[b].append((a, length, via, net))
        key = (a, b, length, via, net)
        self.edge_items[key].update(item_id for item_id in item_ids if item_id)
        self.edge_items[(b, a, length, via, net)].update(item_id for item_id in item_ids if item_id)

    @staticmethod
    def _item_uuid(item):
        try:
            return str(item.m_Uuid.AsString())
        except (AttributeError, RuntimeError):
            return ""

    def shortest(self, net, source, destinations):
        if source is None or not destinations:
            return None
        targets = set(destinations)
        best, prev = {source: (0.0, 0)}, {}
        # The string tiebreaker prevents Python comparing mixed PAD and
        # coordinate node tuples when two candidates have equal costs.
        heap = [(0.0, 0, str(source), source)]
        found = None
        while heap:
            length, vias, _stable_node, node = heapq.heappop(heap)
            if best.get(node) != (length, vias):
                continue
            if node in targets:
                found = node
                break
            for nxt, weight, via, edge_net in self.adj.get(node, ()):
                if edge_net != net:
                    continue
                candidate = (length + weight, vias + via)
                if nxt not in best or candidate < best[nxt]:
                    best[nxt], prev[nxt] = candidate, (node, (node, nxt, weight, via, edge_net))
                    heapq.heappush(heap, (*candidate, str(nxt), nxt))
        if found is None:
            return None
        path = [found]
        route_edges = []
        while path[-1] != source:
            previous, edge_key = prev[path[-1]]
            route_edges.append(edge_key)
            path.append(previous)
        path.reverse()
        item_uuids = sorted({item_id for edge_key in route_edges for item_id in self.edge_items.get(edge_key, ())})
        return {"length_mm": best[found][0], "vias": best[found][1], "path": path,
                "item_uuids": item_uuids}

    def route(self, from_ref, from_pad, to_ref, to_pad):
        first = self.pad_nodes.get((str(from_ref), str(from_pad)))
        last = self.pad_nodes.get((str(to_ref), str(to_pad)))
        if not first or not last or first[0] != last[0]:
            return None
        return self.shortest(first[0], first[1], [last[1]])

    def pad_components_connected(self, net):
        pads = self.net_pads.get(net, ())
        if len(pads) <= 1:
            return True
        start = pads[0]["node"]
        seen, queue = {start}, deque([start])
        while queue:
            node = queue.popleft()
            for nxt, _weight, _via, edge_net in self.adj.get(node, ()):
                if edge_net == net and nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return all(pad["node"] in seen for pad in pads)

    def connected(self, net, ref, pad):
        node = self.pad_nodes.get((str(ref), str(pad)))
        if not node:
            return False
        seen, queue = {node[1]}, deque([node[1]])
        while queue:
            current = queue.popleft()
            for nxt, _weight, _via, edge_net in self.adj.get(current, ()):
                if edge_net == net and nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return all(p["node"] in seen for p in self.net_pads.get(net, ()))

    def padless_component_count(self, net):
        """Count native copper graph islands with no footprint pad attached."""
        nodes = {node for node, edges in self.adj.items()
                 if any(edge[3] == net for edge in edges)}
        nodes.update(p["node"] for p in self.net_pads.get(net, ()))
        pad_nodes = {p["node"] for p in self.net_pads.get(net, ())}
        seen, count = set(), 0
        for start in nodes:
            if start in seen:
                continue
            component, queue = {start}, deque([start])
            seen.add(start)
            while queue:
                current = queue.popleft()
                for nxt, _weight, _via, edge_net in self.adj.get(current, ()):
                    if edge_net == net and nxt not in seen:
                        seen.add(nxt)
                        component.add(nxt)
                        queue.append(nxt)
            if not component.intersection(pad_nodes):
                count += 1
        return count
