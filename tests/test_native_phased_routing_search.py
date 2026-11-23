"""Half-cell lattice phases for candidate-only bidirectional native routing."""

import pytest

from circuit_weaver import _native_routing_repair as kernel


def _search(start, goal, clear_segment, *, phase=None, cap=5_000, layers=1,
            targets=None, origins=None, can_change=lambda _point: False,
            bounds=(-1, -1, 5, 2), step=1.0):
    return kernel._bidirectional_astar_search(
        start,
        goal,
        layers,
        {0} if targets is None else targets,
        {(0, 0, 0)} if origins is None else origins,
        bounds,
        step,
        cap,
        clear_segment,
        can_change,
        grid_phase=phase,
    )


def _thin_diagonal_gap(a, b, _layer):
    """A wall at x=2 has a narrow opening centered on y=0.5."""
    dx = b[0] - a[0]
    if abs(dx) < 1e-12:
        if abs(a[0] - 2.0) >= 1e-12:
            return True
        return 0.49 <= min(a[1], b[1]) and max(a[1], b[1]) <= 0.51
    t = (2.0 - a[0]) / dx
    if not 0.0 <= t <= 1.0:
        return True
    cross_y = a[1] + t * (b[1] - a[1])
    return 0.49 <= cross_y <= 0.51


def _physical_points(path, start, goal, step, phase):
    origin = (start[0] + phase[0] * step, start[1] + phase[1] * step)
    points = [(start, path[0][2])]
    points.extend(((origin[0] + node[0] * step, origin[1] + node[1] * step), node[2]) for node in path)
    points.append((goal, path[-1][2]))
    return points


def test_half_cell_phase_finds_legal_corridor_missed_by_unshifted_lattice():
    start, goal = (0.0, 0.0), (4.0, 0.0)
    unshifted, unshifted_expanded = _search(start, goal, _thin_diagonal_gap, phase=(0.0, 0.0))
    shifted, shifted_expanded = _search(start, goal, _thin_diagonal_gap, phase=(0.0, 0.5))

    assert unshifted is None
    assert 0 < unshifted_expanded <= 5_000
    assert shifted and 0 < shifted_expanded <= 5_000
    points = _physical_points(shifted, start, goal, 1.0, (0.0, 0.5))
    assert all(_thin_diagonal_gap(a[0], b[0], a[1]) for a, b in zip(points, points[1:]))
    assert points[0] == (start, 0)
    assert points[-1] == (goal, 0)


def test_phase_zero_preserves_original_root_order_without_start_connector_query():
    start, goal = (0.0, 0.0), (3.5, 0.0)
    calls = []

    def clear(a, b, _layer):
        calls.append((a, b))
        return True

    implicit, implicit_expanded = _search(start, goal, clear, phase=None)
    implicit_calls = list(calls)
    calls.clear()
    explicit, explicit_expanded = _search(start, goal, clear, phase=(0.0, 0.0))

    assert implicit == explicit == [(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)]
    assert implicit_expanded == explicit_expanded
    assert implicit_calls == calls
    assert all(a != b for a, b in calls)


def test_phased_search_keeps_layer_change_pockets_and_track_edges_legal():
    start, goal = (0.0, 0.0), (4.0, 0.0)
    pockets = {(1.25, 0.0), (3.25, 0.0)}

    def legal_corridors(a, b, layer):
        if abs(a[1]) > 1e-12 or abs(b[1]) > 1e-12:
            return False
        lo, hi = sorted((a[0], b[0]))
        if layer == 0:
            return hi <= 1.25 + 1e-12 or lo >= 3.25 - 1e-12
        if layer == 1:
            return lo >= 1.25 - 1e-12 and hi <= 3.25 + 1e-12
        return False

    path, expanded = _search(
        start,
        goal,
        legal_corridors,
        phase=(0.5, 0.0),
        layers=2,
        targets={0},
        origins={(0, 0, 0)},
        can_change=lambda point: point in pockets,
        bounds=(-1, -0.25, 5, 0.25),
        step=0.5,
    )

    assert path and 0 < expanded <= 5_000
    points = _physical_points(path, start, goal, 0.5, (0.5, 0.0))
    transitions = [(a, b) for a, b in zip(points, points[1:]) if a[1] != b[1]]
    assert len(transitions) == 2
    assert [a[0] for a, _ in transitions] == [(1.25, 0.0), (3.25, 0.0)]
    for a, b in zip(points, points[1:]):
        if a[1] == b[1]:
            assert legal_corridors(a[0], b[0], a[1])
        else:
            assert a[0] == b[0] and a[0] in pockets


def test_phase_search_is_deterministic_and_uses_one_shared_state_cap():
    start, goal = (0.0, 0.0), (4.0, 0.0)
    first = _search(start, goal, _thin_diagonal_gap, phase=(0.0, 0.5))
    second = _search(start, goal, _thin_diagonal_gap, phase=(0.0, 0.5))
    assert first == second

    def separated(a, b, _layer):
        return a == b or not (min(a[0], b[0]) < 2.0 <= max(a[0], b[0]))

    path, expanded = _search(start, goal, separated, phase=(0.5, 0.5), cap=17)
    assert path is None
    assert expanded == 17


@pytest.mark.parametrize("phase", [True, (True, 0), (0,), (0, 0, 0), (0.25, 0),
                                    (0, float("nan")), (float("inf"), 0), "0.5,0"])
def test_phase_validation_rejects_malformed_or_unsupported_offsets(phase):
    with pytest.raises(ValueError, match="grid phase"):
        _search((0.0, 0.0), (2.0, 0.0), lambda _a, _b, _layer: True, phase=phase)


def test_standard_search_rejects_nonzero_phase():
    with pytest.raises(ValueError, match="only for bidirectional"):
        kernel._validated_search_grid_phase((0.5, 0.0), "standard")
