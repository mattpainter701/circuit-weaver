from circuit_weaver._native_reference_repair import apply_candidate_positions, solve_bounded_assignment


def box(x, y=0, width=10):
    return (x, y, x + width, y + width)


def option(x, y=0):
    return {"box": box(x, y), "pos": (x, y)}


def test_most_constrained_first_escapes_greedy_trap_deterministically():
    domains = {"A": [option(0), option(20)], "B": [option(0)]}
    kwargs = {"layer_by_ref": {"A": "F", "B": "F"}, "fixed_boxes_by_layer": {}, "max_nodes": 20}
    first = solve_bounded_assignment(domains, **kwargs)
    second = solve_bounded_assignment(domains, **kwargs)
    assert first == second
    assert first["status"] == "solved"
    assert first["assignment"]["B"]["pos"] == (0, 0)
    assert first["assignment"]["A"]["pos"] == (20, 0)


def test_fixed_boxes_keep_locked_and_outside_fields_immovable():
    result = solve_bounded_assignment(
        {"conflict": [option(0), option(20)]},
        layer_by_ref={"conflict": "F"},
        fixed_boxes_by_layer={"F": [box(20)]},
        max_nodes=5,
    )
    assert result["status"] == "solved"
    assert result["assignment"]["conflict"]["pos"] == (0, 0)


def test_unsatisfiable_assignment_fails_closed():
    result = solve_bounded_assignment(
        {"A": [option(0)], "B": [option(0)]},
        layer_by_ref={"A": "F", "B": "F"},
        fixed_boxes_by_layer={},
        max_nodes=10,
    )
    assert result["status"] == "no_solution"
    assert result["assignment"] == {}


def test_budget_exhaustion_and_native_rejection_restore_positions():
    exhausted = solve_bounded_assignment(
        {"A": [option(0), option(20)], "B": [option(0)]},
        layer_by_ref={"A": "F", "B": "F"},
        fixed_boxes_by_layer={},
        max_nodes=1,
    )
    assert exhausted["status"] == "budget_exhausted"
    assert exhausted["search_nodes"] == 1

    class Field:
        def __init__(self, x, y):
            self.pos = (x, y)

        def GetTextPos(self):
            return type("Vector", (), {"x": self.pos[0], "y": self.pos[1]})()

        def SetTextPos(self, value):
            self.pos = (value.x, value.y)

    field = Field(3, 4)
    applied = apply_candidate_positions(
        {"A": field}, {"A": {"pos": (30, 40)}},
        lambda x, y: type("Vector", (), {"x": x, "y": y})(),
        lambda: False,
    )
    assert not applied["applied"]
    assert field.pos == (3, 4)
