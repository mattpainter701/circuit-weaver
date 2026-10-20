"""Exactness checks for the native silkscreen broadphase index."""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import circuit_weaver

_WORKER_PATH = Path(circuit_weaver.__file__).with_name("_kicad_worker.py")
_SPEC = importlib.util.spec_from_file_location("_kicad_worker_spatial_test", _WORKER_PATH)
_WORKER = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_WORKER)


def _exact_hits(boxes, query):
    return [index for index, box in enumerate(boxes) if _WORKER._boxes_overlap(query, box)]


def test_aabb_index_returns_exact_overlap_set_for_mixed_and_random_geometry():
    rng = random.Random(20261018)
    boxes = [
        (0, 0, 10, 10),
        (10, 10, 20, 20),  # touching edges are not exact overlaps
        (-10000, -1, 10000, 1),  # index overflow entry
        (500, 500, 501, 501),
    ]
    for _ in range(1000):
        x, y = rng.randint(-2000, 2000), rng.randint(-2000, 2000)
        width, height = rng.randint(1, 150), rng.randint(1, 150)
        boxes.append((x, y, x + width, y + height))

    index = _WORKER._AabbIndex(boxes, cell_size=37)
    queries = list(boxes[:4])
    for _ in range(300):
        x, y = rng.randint(-2500, 2500), rng.randint(-2500, 2500)
        width, height = rng.randint(1, 250), rng.randint(1, 250)
        queries.append((x, y, x + width, y + height))

    for query in queries:
        broadphase = set(index.query(query))
        exact = set(_exact_hits(boxes, query))
        assert exact <= broadphase
        assert {i for i in broadphase if _WORKER._boxes_overlap(query, boxes[i])} == exact


def test_aabb_index_keeps_negative_coordinates_and_large_query_fallback_exact():
    boxes = [(-20, -20, -10, -10), (10, 10, 20, 20), (50, 50, 5000, 5000)]
    index = _WORKER._AabbIndex(boxes, cell_size=1)
    for query in [(-15, -15, -14, -14), (500, 500, 600, 600), (-1_000_000, -1_000_000, 1_000_000, 1_000_000)]:
        candidates = index.query(query)
        assert {i for i in candidates if _WORKER._boxes_overlap(query, boxes[i])} == set(
            _exact_hits(boxes, query)
        )


def test_association_broadphase_matches_full_courtyard_scan():
    obstacles = [
        ("courtyard", "U1", (0, 0, 10, 10)),
        ("courtyard", "U2", (30, 0, 40, 10)),
        ("courtyard", "U3", (100, 100, 110, 110)),
        ("courtyard", "R1", (4, 4, 6, 6)),
        ("pad_mask", "U4", (0, 0, 100, 100)),
    ]
    peers = {"U": ([(owner, box) for kind, owner, box in obstacles if kind == "courtyard"
                    and _WORKER._reference_class(owner) == "u"], None)}
    rows, _ = peers["U"]
    peers["u"] = (rows, _WORKER._AabbIndex([box for _owner, box in rows], 5))

    cases = [
        ((12, 5), (0, 0, 10, 10), "U1"),
        ((24, 5), (0, 0, 10, 10), "U1"),
        ((2, 2), (0, 0, 10, 10), "U1"),
        ((50, 50), (0, 0, 10, 10), "U1"),
    ]
    for center, own, ref in cases:
        brute = _WORKER._candidate_associated(center, own, ref, obstacles)
        indexed = _WORKER._candidate_associated(center, own, ref, obstacles, peers)
        assert indexed == brute
        assert _WORKER._association_margin(center, own, ref, obstacles, peers) == (
            _WORKER._association_margin(center, own, ref, obstacles)
        )
