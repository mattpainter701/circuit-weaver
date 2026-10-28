"""Bidirectional native-routing search geometry and budget properties."""

import math

from circuit_weaver import _native_routing_repair as kernel


def search(start, goal, step, clear, **kwargs):
    return kernel._bidirectional_astar_search(
        start,
        goal,
        kwargs.pop("layers", 1),
        kwargs.pop("targets", {0}),
        kwargs.pop("origins", {(0, 0, 0)}),
        kwargs.pop("bounds", (-10, -10, 20, 20)),
        step,
        kwargs.pop("cap", 150_000),
        clear,
        kwargs.pop("can_change", lambda _point: False),
    )


def physical_points(path, start, goal, step):
    points = [(start[0] + node[0] * step, start[1] + node[1] * step, node[2]) for node in path]
    points.append((goal[0], goal[1], path[-1][2]))
    return points


def test_reconstructed_edges_and_goal_connector_are_all_legal():
    start, goal, step = (0.0, 0.0), (4.6, 0.0), 0.5
    def blocked(a, b, _layer):
        return not (
            abs(a[1]) < 1e-9 and abs(b[1]) < 1e-9
            and min(a[0], b[0]) < 3.25 and max(a[0], b[0]) > 0.75
        )
    path, expanded = search(start, goal, step, blocked, bounds=(-1, -2, 6, 2))
    assert path and 0 < expanded <= 150_000
    points = physical_points(path, start, goal, step)
    assert points[0] == (start[0], start[1], 0)
    assert all(blocked(a[:2], b[:2], a[2]) for a, b in zip(points, points[1:]))
    assert all(a[2] == b[2] for a, b in zip(points, points[1:]))
    assert math.dist(points[-2][:2], goal) <= step / math.sqrt(2) + 1e-9


def test_narrow_layer_pockets_require_two_reconstructed_transitions():
    start, goal, step = (0.0, 0.0), (3.0, 0.0), 0.5

    def in_corridor(a, b, layer):
        # Three bounded corridor spans join only at the two legal pockets.
        if layer == 0:
            return abs(a[1]) < 1e-9 and abs(b[1]) < 1e-9 and max(a[0], b[0]) <= 1.0
        if layer == 1:
            return (abs(a[1]) < 1e-9 and abs(b[1]) < 1e-9
                    and min(a[0], b[0]) >= 1.0 and max(a[0], b[0]) <= 2.0)
        if layer == 2:
            return (abs(a[1]) < 1e-9 and abs(b[1]) < 1e-9
                    and min(a[0], b[0]) >= 2.0)
        return False

    pockets = {(1.0, 0.0), (2.0, 0.0)}
    path, expanded = search(
        start, goal, step, in_corridor,
        layers=3, targets={2}, origins={(0, 0, 0)},
        bounds=(-0.5, -0.5, 3.5, 0.5),
        can_change=lambda p: p in pockets,
    )
    assert path and 0 < expanded <= 150_000
    points = physical_points(path, start, goal, step)
    transitions = [(a, b) for a, b in zip(points, points[1:]) if a[2] != b[2]]
    assert len(transitions) == 2
    assert [a[2] for a, _ in transitions] == [0, 1]
    assert [a[:2] for a, _ in transitions] == [(1.0, 0.0), (2.0, 0.0)]
    for a, b in zip(points, points[1:]):
        if a[2] == b[2]:
            assert in_corridor(a[:2], b[:2], a[2])
        else:
            assert a[:2] == b[:2] and a[:2] in pockets
    assert in_corridor(points[-2][:2], points[-1][:2], points[-2][2])


def test_exact_lattice_goal_and_identical_endpoints():
    def clear(_a, _b, _layer):
        return True
    exact, exact_expanded = search((0.0, 0.0), (2.0, 1.0), 0.5, clear)
    assert exact and exact_expanded > 0
    assert exact[-1][:2] == (4, 2)

    same, same_expanded = search((1.0, 1.0), (1.0, 1.0), 0.5, clear)
    assert same == [(0, 0, 0)]
    assert same_expanded > 0


def test_blocked_reverse_connectors_return_bounded_failure():
    path, expanded = search(
        (0.0, 0.0), (4.0, 0.0), 0.5,
        lambda _a, _b, _layer: False,
        cap=17,
    )
    assert path is None
    # No legal reverse root connector exists, so this is a bounded early failure.
    assert expanded == 0


def test_reverse_front_exhaustion_returns_before_the_state_cap():
    def isolated_goal(a, b, _layer):
        return a == b or all(-1 <= value <= 1 for point in (a, b) for value in point)

    path, expanded = search(
        (0.0, 0.0), (4.0, 0.0), 0.5, isolated_goal,
        bounds=(-1, -1, 5, 1), cap=100,
    )
    assert path is None
    assert 0 < expanded < 100


def test_shared_expansion_cap_is_exact_when_both_fronts_are_blocked_after_seeding():
    # A legal goal connector seeds the reverse side, but the wall prevents the
    # two fronts from meeting. The shared cap must count work across both sides.
    def split(a, b, _layer):
        return not (min(a[0], b[0]) < 2.0 <= max(a[0], b[0]))

    path, expanded = search(
        (0.0, 0.0), (4.0, 0.0), 0.5, split,
        bounds=(-1, -2, 5, 2), cap=23,
    )
    assert path is None
    assert expanded == 23
