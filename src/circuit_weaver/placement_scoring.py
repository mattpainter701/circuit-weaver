"""Compile placement intent once and synchronize changed poses with the Rust scorer."""

from __future__ import annotations

import json
import math
import os
import sys


def select_scorer(requested=None):
    mode = requested if requested is not None else os.environ.get("CIRCUIT_WEAVER_PLACEMENT_BACKEND", "auto")
    if mode not in {"auto", "python", "rust"}:
        raise ValueError("Placement scoring backend must be auto, python, or rust")
    if mode == "python":
        return None
    try:
        import circuit_weaver_placement as native
    except ModuleNotFoundError as exc:
        if exc.name != "circuit_weaver_placement":
            raise
        if mode == "auto":
            return None
        raise RuntimeError("Rust placement scoring requires the circuit-weaver-placement wheel") from exc
    if native.MODEL_VERSION != 1:
        raise RuntimeError("Incompatible circuit-weaver-placement scoring model")
    return NativeScorer(native)


def _model(rows, config, pairs, plan):
    from . import placement_optimizer as opt

    indices = {p.ref: i for i, p in enumerate(rows)}
    points = []
    point_ids = {}

    def pin(i, offset):
        key = (i, *offset)
        if key not in point_ids:
            point_ids[key] = len(points)
            points.append(key)
        return point_ids[key]

    def leg(i, j, net, owner_offset=None):
        left = [owner_offset] if owner_offset is not None else (rows[i].net_pad_offsets or {}).get(net, [])
        right = (rows[j].net_pad_offsets or {}).get(net, [])
        return [(pin(i, a), pin(j, b)) for a in left for b in right]

    groups = [[indices[p.ref] for p in group] for group in opt._functional_groups(rows).values() if len(group) > 1]
    peers = [
        (
            [indices[p.ref] for p in group],
            opt._same_owner_pin_supports(group)
            and all(p.placement_role.strip().casefold() in opt._POWER_LOOP_ROLES for p in group),
        )
        for group in opt._support_peer_groups(rows)
    ]
    root_affinity = {}
    edges = []
    for (a, b), weight in (pairs or {}).items():
        if a in indices and b in indices:
            edges.append((indices[a], indices[b], weight))
        if math.isfinite(weight) and weight > 0:
            for ref in (a, b):
                if ref in indices:
                    root_affinity[ref] = root_affinity.get(ref, 0.0) + weight
    for group in groups:
        for i in group:
            ref = rows[i].ref
            root_affinity[ref] = root_affinity.get(ref, 0.0) + min(len(group) - 1, 3)

    parts, families, output_links, nets = [], [], [], {}
    for i, p in enumerate(rows):
        affinity = root_affinity.get(p.parent_ref or p.ref, 0.0)
        pull = 0.3 if p.parent_ref else max(0.3, 1.0 / (1.0 + math.sqrt(affinity)))
        size = p.reference_size_mm
        text = tuple(map(float, size)) if size and len(size) == 2 else None
        parts.append(
            dict(
                layer=p.layer.casefold(),
                surface=opt._proven_native_surface(p),
                prefix=opt._reference_class_prefix(p.ref),
                text=text,
                heat=p.thermal_dissipation_w,
                zone=opt._zone_center_mm(p.category, config),
                pull=pull,
                edge=p.category in {"connector", "usb", "debug", "rf"},
                rf=p.category == "rf",
                locked=p.constraint_locked,
                support=bool(p.parent_ref),
                noisy=not p.parent_ref and p.category in {"power", "regulator", "poe"},
                sensitive=not p.parent_ref and p.category in {"rf", "clock", "analog", "sensor", "sensors"},
            )
        )
        for net, offsets in (p.net_pad_offsets or {}).items():
            nets.setdefault(net, []).extend(pin(i, offset) for offset in offsets)
        if not p.parent_ref:
            continue
        j = indices.get(p.parent_ref)
        family = dict(
            child=i,
            parent=j,
            critical=p.placement_role.strip().casefold() in opt._CRITICAL_SUPPORT_ROLES,
            shared=[],
            owner_pin=[],
            supplies=[],
            grounds=[],
        )
        families.append(family)
        pad_nets = p.net_pad_offsets or {}
        if p.placement_role.strip().casefold() == "output_cap":
            for k, other in enumerate(rows):
                if other.parent_ref == p.parent_ref and other.placement_role.strip().casefold() == "inductor":
                    output_links.append([pair for n in pad_nets if opt._is_power_rail(n) for pair in leg(i, k, n)])
        if j is None:
            continue
        parent = rows[j]
        owner_pin = p.parent_pin.strip()
        owner_net = (parent.pin_nets or {}).get(owner_pin)
        offset = (parent.pin_offsets or {}).get(owner_pin)
        family["shared"] = [leg(j, i, n) for n in opt._shared_support_nets(parent, pad_nets)]
        if owner_pin and owner_net and offset is not None:
            family["owner_pin"] = leg(j, i, owner_net, offset)
        if p.placement_role.strip().casefold() in opt._POWER_LOOP_ROLES:
            rails = [n for n in pad_nets if opt._is_power_rail(n)]
            for n in [owner_net] if owner_net in rails else rails:
                family["supplies"].extend(leg(j, i, n, offset if owner_pin and n == owner_net else None))
            for n in pad_nets:
                if opt._is_ground_net(n):
                    family["grounds"].extend(leg(j, i, n))
    return dict(
        board=[config.board_width_mm, config.board_height_mm],
        margin=config.edge_clearance_mm,
        gap=config.min_component_gap_mm,
        support_gap=config.support_body_clearance_mm,
        strategy=config.strategy,
        accurate_sum=sys.version_info >= (3, 12),
        parts=parts,
        points=points,
        families=families,
        output_links=output_links,
        groups=groups,
        peers=peers,
        pad_mode=all(p.net_pad_offsets is not None for p in rows),
        nets=[(opt._net_weight(n), ids) for n, ids in nets.items() if len(ids) >= 2],
        edges=edges,
        keepouts=[(k["x_mm"], k["y_mm"], k["x_mm"] + k["width_mm"], k["y_mm"] + k["height_mm"]) for k in plan.keepouts],
        edge_rules=[(indices[r], rule["edge"]) for r, rule in plan.edges.items() if r in indices],
        affinities=[
            (indices[a["target"]], indices[a["near"]], a["max_distance_mm"])
            for a in plan.affinities
            if a["target"] in indices and a["near"] in indices
        ],
    )


class NativeScorer:
    """One instance per optimizer invocation, including its independent restarts."""

    def __init__(self, native):
        self.native = native
        self.inventory = ()
        self.poses = []
        self.state = None
        self.evaluations = self.snapshots = self.pose_updates = 0

    def __call__(self, rows, config, pairs, plan):
        from .placement_optimizer import _effective_dimensions

        inventory = tuple((row.ref, row.layer) for row in rows)
        if inventory != self.inventory or self.state is None:
            # Legalization may reorder owners/children. Recompile the indexed terms
            # so floating-point accumulation follows the reference order exactly.
            self.state = self.native.PlacementState(json.dumps(_model(rows, config, pairs, plan), allow_nan=False))
            self.inventory = inventory
            self.poses = [None] * len(rows)
            self.snapshots += 1
        updates = []
        for i, p in enumerate(rows):
            pose = (p.x, p.y, p.rotation)
            if pose != self.poses[i]:
                angle = math.radians(p.rotation)
                updates.append((i, p.x, p.y, *_effective_dimensions(p), math.cos(angle), math.sin(angle)))

        result = self.state.score(updates)
        for i, *_ in updates:
            p = rows[i]
            self.poses[i] = (p.x, p.y, p.rotation)
        self.pose_updates += len(updates)
        self.evaluations += 1
        return result
