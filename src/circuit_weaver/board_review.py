"""Shared, read-only native board review for CLI, Python and MCP clients."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .kicad_bridge import run_kicad
from .placement_quality import review_placement_inventory
from .project_state import file_sha256
from .route_quality import evaluate_route_quality


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate review-plan key: {key}")
        result[key] = value
    return result


def parse_review_plan(document: str) -> dict[str, Any]:
    """Decode a plan without silently replacing duplicate JSON fields."""
    def reject_constant(value: str) -> None:
        raise ValueError(f"Nonfinite JSON constant in review plan: {value}")

    result = json.loads(document, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    if not isinstance(result, dict):
        raise ValueError("Review plan must be a JSON object")
    return result


def review_pcb(
    board_path: str | Path, plan: dict[str, Any] | None = None, *,
    kicad_python_path: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect actual geometry once; report authored limits without editing it.

    Plan keys are ``placement`` (arguments for review_placement_inventory) and
    optional ``routing`` (explicit route_quality contracts). No DRC, impedance,
    plane continuity or engineering approval is inferred from this review.
    """
    if plan is None:
        plan = {}
    if not isinstance(plan, dict) or set(plan) - {"placement", "routing"}:
        raise ValueError("Review plan supports only placement and routing objects")
    placement = plan.get("placement", {})
    if not isinstance(placement, dict) or set(placement) - {"reserved_regions", "cell_size_mm", "max_empty_fraction"}:
        raise ValueError("Unsupported placement review option")
    if "routing" in plan and not isinstance(plan["routing"], dict):
        raise ValueError("routing must contain an explicit contract object")
    source = Path(board_path).expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".kicad_pcb":
        raise ValueError("Board review requires an existing .kicad_pcb file")
    context_paths = (
        source, source.with_suffix(".kicad_pro"), source.with_suffix(".kicad_dru")
    )

    def snapshot() -> dict[Path, dict[str, Any]]:
        return {path: {"exists": path.exists(), "sha256": file_sha256(path) if path.is_file() else None}
                for path in context_paths}

    snapshots = snapshot()
    inventory = run_kicad("inspect", board=str(source), python_path=kicad_python_path,
                          include_route_geometry="routing" in plan)
    if snapshot() != snapshots:
        raise ValueError("Source board or native settings changed during read-only review")
    result: dict[str, Any] = {
        "schema_version": 1, "status": "unverified", "source": str(source),
        "source_sha256": snapshots[source]["sha256"], "sources_unchanged": True,
        "electrical_or_manufacturing_approval": False,
        "routing": {"status": "not_requested"},
    }
    if inventory.get("status") != "ok":
        return {**result, "reason": inventory.get("message", "Native board inspection failed")}
    result["placement"] = review_placement_inventory(inventory, **placement)
    states = [result["placement"]["status"]]
    if "routing" in plan:
        routing = evaluate_route_quality(inventory, plan["routing"])
        route_states = [row["status"] for row in routing["routes"].values()]
        route_states.extend(row["status"] for row in routing["differential_pairs"])
        routing["status"] = (
            "failed" if "failed" in route_states else
            "unverified" if not route_states or "unverified" in route_states else "verified"
        )
        result["routing"] = routing
        states.append(routing["status"])
    result["status"] = (
        "review_required" if any(state in {"failed", "review_required"} for state in states) else
        "unverified" if "unverified" in states else "verified"
    )
    return result
