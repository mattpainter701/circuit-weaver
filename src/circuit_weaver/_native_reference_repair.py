"""Bounded deterministic solver for local native reference-label repair.

Candidate geometry and KiCad-specific invariants stay in the standalone
``_kicad_worker``. This module only solves a finite assignment problem and
applies a fully validated assignment transactionally.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

Box = tuple[int, int, int, int]


def boxes_overlap(first: Box, second: Box) -> bool:
    return first[0] < second[2] and first[2] > second[0] and first[1] < second[3] and first[3] > second[1]


def solve_bounded_assignment(
    options_by_ref: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    layer_by_ref: Mapping[str, Any],
    fixed_boxes_by_layer: Mapping[Any, Sequence[Box]],
    max_nodes: int,
) -> dict[str, Any]:
    """Find a deterministic non-overlapping assignment within ``max_nodes``.

    Each option contains a native bounding ``box`` and an optional position
    or scoring metadata. Existing labels outside the active conflict group,
    including locked labels, are supplied as fixed boxes and never moved.
    Candidate option order is honored after a stable most-constrained-first
    variable ordering.
    """
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 0:
        raise ValueError("max_nodes must be a nonnegative integer")
    refs = sorted(options_by_ref)
    if any(not isinstance(ref, str) or not ref for ref in refs):
        raise ValueError("reference keys must be nonempty strings")
    if set(refs) != set(layer_by_ref):
        raise ValueError("every reference must have exactly one layer")
    options: dict[str, list[Mapping[str, Any]]] = {}
    for ref in refs:
        rows = options_by_ref[ref]
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ValueError(f"options for {ref} must be a sequence")
        options[ref] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"candidate for {ref} must be a mapping")
            box = row.get("box")
            if (not isinstance(box, (tuple, list)) or len(box) != 4
                    or any(isinstance(value, bool) or not isinstance(value, int) for value in box)):
                raise ValueError(f"candidate for {ref} must contain an integer bounding box")
            options[ref].append(row)

    fixed = {layer: tuple(boxes) for layer, boxes in fixed_boxes_by_layer.items()}
    order = sorted(refs, key=lambda ref: (len(options[ref]), ref))
    assignment: dict[str, Mapping[str, Any]] = {}
    nodes = 0
    exhausted = False

    def search(depth: int) -> bool:
        nonlocal nodes, exhausted
        if depth == len(order):
            return True
        ref = order[depth]
        layer = layer_by_ref[ref]
        for candidate in options[ref]:
            if nodes >= max_nodes:
                exhausted = True
                return False
            nodes += 1
            box = tuple(candidate["box"])
            if any(boxes_overlap(box, other) for other in fixed.get(layer, ())):
                continue
            if any(
                layer_by_ref[other] == layer and boxes_overlap(box, tuple(row["box"]))
                for other, row in assignment.items()
            ):
                continue
            assignment[ref] = candidate
            if search(depth + 1):
                return True
            assignment.pop(ref)
            if exhausted:
                return False
        return False

    solved = search(0) if refs else True
    status = "solved" if solved else "budget_exhausted" if exhausted else "no_solution"
    return {
        "status": status,
        "assignment": dict(assignment) if solved else {},
        "search_nodes": nodes,
        "variable_order": order,
        "option_counts": {ref: len(options[ref]) for ref in refs},
    }


def apply_candidate_positions(
    fields_by_ref: Mapping[str, Any],
    assignment: Mapping[str, Mapping[str, Any]],
    vector_factory: Callable[[int, int], Any],
    validate: Callable[[], bool],
) -> dict[str, Any]:
    """Apply candidate positions atomically, restoring original positions on failure."""
    if set(assignment) - set(fields_by_ref):
        raise ValueError("assignment contains a reference without a native field")
    originals = {
        ref: (int(fields_by_ref[ref].GetTextPos().x), int(fields_by_ref[ref].GetTextPos().y))
        for ref in assignment
    }

    def restore() -> None:
        for ref, position in originals.items():
            fields_by_ref[ref].SetTextPos(vector_factory(*position))

    try:
        for ref in sorted(assignment):
            position = assignment[ref].get("pos")
            if (not isinstance(position, (tuple, list)) or len(position) != 2
                    or any(isinstance(value, bool) or not isinstance(value, int) for value in position)):
                raise ValueError(f"candidate position for {ref} must be an integer pair")
            fields_by_ref[ref].SetTextPos(vector_factory(*position))
        if not validate():
            restore()
            return {"applied": False, "reason": "native_validation_rejected_candidate"}
    except Exception as exc:
        try:
            restore()
        except Exception as restore_exc:  # pragma: no cover - fail-closed diagnostic
            return {"applied": False, "reason": f"apply_failed:{exc};restore_failed:{restore_exc}"}
        return {"applied": False, "reason": f"apply_failed:{exc}"}
    return {"applied": True, "reason": "native_validation_improved_without_new_conflicts"}
