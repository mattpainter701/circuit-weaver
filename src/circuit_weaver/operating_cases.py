"""Bounded current-budget checks for explicitly authored operating cases.

This checker only compares a declared simultaneous-load envelope with declared
current limits. It does not infer component ratings or qualify a complete board.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

Status = Literal["passed", "failed", "unverified"]


@dataclass(frozen=True)
class Evidence:
    """Provenance attached to an authored electrical value."""

    evidence_id: str | None
    confidence: str | None
    freshness: str | None
    subject: str | None = None
    value: float | None = None
    unit: str | None = None
    kind: str | None = None
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.conflicts, tuple) or any(not isinstance(item, str) for item in self.conflicts):
            raise ValueError("evidence conflicts must be an immutable tuple of IDs")
        for field_name in ("evidence_id", "confidence", "freshness", "subject", "unit", "kind"):
            field_value = getattr(self, field_name)
            if field_value is not None and not isinstance(field_value, str):
                raise ValueError(f"evidence {field_name} must be a string or None")
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ValueError("evidence value must be numeric or None")
            _finite_nonnegative(self.value, "evidence value")

    def trusted_for(self, subject: str, value: float | int, unit: str) -> bool:
        """Whether provenance supports this exact value and remains current."""
        try:
            exact_match = _current_decimal(self.value, "evidence value") == _current_decimal(value, "authored value")
        except ValueError:
            exact_match = False
        return bool(
            isinstance(self.evidence_id, str)
            and self.evidence_id.strip()
            and isinstance(self.confidence, str)
            and self.confidence in {"verified", "corroborated"}
            and isinstance(self.freshness, str)
            and self.freshness == "current"
            and isinstance(self.subject, str)
            and self.subject == subject
            and not isinstance(self.value, bool)
            and isinstance(self.value, (int, float))
            and exact_match
            and self.unit == unit
            and isinstance(self.kind, str)
            and self.kind in {"datasheet", "calculation", "distributor"}
            and not self.conflicts
        )


@dataclass(frozen=True)
class Load:
    """One load class; current is per active item and expressed in mA."""

    name: str
    current_ma: float | None
    max_concurrent: int | None
    evidence: Evidence | None
    unit: str = "mA"


@dataclass(frozen=True)
class CurrentLimit:
    """A boundary current limit, expressed in mA with provenance."""

    name: str
    current_ma: float | None
    evidence: Evidence | None
    unit: str = "mA"


@dataclass(frozen=True)
class OperatingCase:
    """Authored worst-case simultaneous loads and their current boundaries."""

    name: str
    max_simultaneous: int | None
    loads: tuple[Load, ...]
    source_limit: CurrentLimit | None
    connector_limits: tuple[CurrentLimit, ...] = ()
    distribution_limits: tuple[CurrentLimit, ...] = ()


@dataclass(frozen=True)
class BoundaryResult:
    boundary: str
    required_ma: float | None
    limit_ma: float | None
    status: Status
    evidence_id: str | None
    reason: str | None = None


@dataclass(frozen=True)
class OperatingCaseResult:
    result_scope: str
    input_sha256: str
    functionality_qualification: Status
    thermal_qualification: Status
    case_name: str
    status: Status
    required_ma: float | None
    declared_max_simultaneous: int | None
    boundary_results: tuple[BoundaryResult, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return deterministic JSON-safe result data."""
        payload = asdict(self)
        payload["boundary_results"] = [asdict(item) for item in self.boundary_results]
        payload["reasons"] = list(self.reasons)
        return payload

    @property
    def result_hash(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _finite_nonnegative(value: object, label: str, *, positive: bool = False) -> float:
    # bool is an int subclass, but accepting True as 1 mA is almost certainly
    # an authoring error, so explicitly reject it.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a numeric value in mA")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label} must be a finite numeric value in mA") from None
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        qualifier = "finite and positive" if positive else "finite and non-negative"
        raise ValueError(f"{label} must be {qualifier}")
    return result


_MAX_DECIMAL_PRECISION = 4096


def _current_decimal(value: object, label: str) -> Decimal:
    """Parse the authored numeric spelling after the finite/range guard."""
    _finite_nonnegative(value, label)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a finite decimal value in mA") from None


def _sum_precision(loads: tuple[Load, ...]) -> int:
    """Bound precision for exact decimal products and aligned summation."""
    shapes: list[tuple[int, int, int]] = []
    for load in loads:
        assert load.current_ma is not None and load.max_concurrent is not None
        current = _current_decimal(load.current_ma, f"load {load.name} current")
        count = Decimal(load.max_concurrent)
        exponent = current.as_tuple().exponent + count.as_tuple().exponent
        coefficient_digits = len(current.as_tuple().digits) + len(count.as_tuple().digits)
        adjusted_bound = max(0, current.adjusted()) + max(0, count.adjusted()) + 2
        shapes.append((exponent, coefficient_digits, adjusted_bound))
    min_exponent = min(shape[0] for shape in shapes)
    max_adjusted = max(shape[2] for shape in shapes)
    carry_digits = len(str(len(shapes) + 1))
    needed = max(max(shape[1] for shape in shapes), max_adjusted - min_exponent + carry_digits + 2)
    if needed > _MAX_DECIMAL_PRECISION:
        raise ValueError("operating-case decimal arithmetic exceeds bounded precision")
    return max(28, needed)


def _input_sha256(case: OperatingCase) -> str:
    """Hash every validated authored input, including evidence metadata."""
    canonical = json.dumps(asdict(case), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_limit(
    limit: CurrentLimit | None,
    required_ma: float | None,
    required_decimal: Decimal | None,
    boundary: str,
) -> BoundaryResult:
    if limit is None:
        return BoundaryResult(
            boundary,
            required_ma,
            None,
            "unverified",
            None,
            f"{boundary} current limit or current evidence is missing or untrusted",
        )
    if not isinstance(limit, CurrentLimit):
        raise ValueError(f"{boundary} must be a CurrentLimit")
    if not isinstance(limit.name, str) or not limit.name.strip():
        raise ValueError(f"{boundary} name must be a non-empty string")
    if limit.evidence is not None and not isinstance(limit.evidence, Evidence):
        raise ValueError(f"{boundary} evidence must be an Evidence value or None")
    if limit.unit != "mA":
        raise ValueError(f"{boundary} current limit unit must be 'mA'")
    if limit.current_ma is None:
        return BoundaryResult(
            boundary,
            required_ma,
            None,
            "unverified",
            None,
            f"{boundary} current limit or current evidence is missing or untrusted",
        )
    value = _finite_nonnegative(limit.current_ma, f"{boundary} limit")
    limit_decimal = _current_decimal(limit.current_ma, f"{boundary} limit")
    if limit.evidence is None:
        return BoundaryResult(
            boundary,
            required_ma,
            None,
            "unverified",
            None,
            f"{boundary} current limit or current evidence is missing or untrusted",
        )
    if not isinstance(limit.evidence, Evidence):
        raise ValueError(f"{boundary} evidence must be an Evidence value or None")
    if not limit.evidence.trusted_for(boundary, limit.current_ma, "mA"):
        return BoundaryResult(
            boundary,
            required_ma,
            None,
            "unverified",
            None,
            f"{boundary} current limit evidence does not verify the stated value",
        )
    if required_ma is None or required_decimal is None:
        return BoundaryResult(
            boundary,
            None,
            value,
            "unverified",
            limit.evidence.evidence_id,
            "required current cannot be derived from the authored case",
        )
    status: Status = "passed" if required_decimal <= limit_decimal else "failed"
    reason = None if status == "passed" else f"required {required_decimal} mA exceeds stated limit {limit_decimal} mA"
    return BoundaryResult(boundary, required_ma, value, status, limit.evidence.evidence_id, reason)


def check_operating_case(case: OperatingCase) -> OperatingCaseResult:
    """Check one authored operating case without filling in absent ratings.

    A complete case requires `max_simultaneous` to equal the sum of per-load
    `max_concurrent` counts. Missing concurrency or load-current values yield
    an unverified result with no computed required current. All currents use
    mA. Untrusted load-current evidence leaves arithmetic conditional and
    prevents boundary pass/fail claims.
    """

    if not isinstance(case, OperatingCase):
        raise ValueError("case must be an OperatingCase")
    if not isinstance(case.name, str) or not case.name.strip():
        raise ValueError("case name must be non-empty")
    if case.max_simultaneous is not None and (
        isinstance(case.max_simultaneous, bool)
        or not isinstance(case.max_simultaneous, int)
        or case.max_simultaneous < 0
    ):
        raise ValueError("max_simultaneous must be a non-negative integer or None")
    if not case.loads:
        raise ValueError("operating case must declare at least one load")
    if (
        not isinstance(case.loads, tuple)
        or not isinstance(case.connector_limits, tuple)
        or not isinstance(case.distribution_limits, tuple)
    ):
        raise ValueError("loads and boundary limits must be immutable tuples")

    active_count = 0
    currents: list[tuple[Load, float | None]] = []
    reasons: list[str] = []
    trusted_loads = True
    seen_load_names: set[str] = set()
    for load in case.loads:
        if not isinstance(load, Load) or not isinstance(load.name, str) or not load.name.strip():
            raise ValueError("each load must have a non-empty name")
        if load.name in seen_load_names:
            raise ValueError(f"duplicate load name: {load.name}")
        seen_load_names.add(load.name)
        if load.unit != "mA":
            raise ValueError(f"load {load.name} current unit must be 'mA'")
        if load.max_concurrent is not None and (
            isinstance(load.max_concurrent, bool) or not isinstance(load.max_concurrent, int) or load.max_concurrent < 0
        ):
            raise ValueError(f"load {load.name} max_concurrent must be a non-negative integer or None")
        current = None if load.current_ma is None else _finite_nonnegative(load.current_ma, f"load {load.name} current")
        if load.max_concurrent is not None:
            active_count += load.max_concurrent
        currents.append((load, current))
        if load.evidence is not None and not isinstance(load.evidence, Evidence):
            raise ValueError(f"load {load.name} evidence must be an Evidence value or None")
        if (
            not isinstance(load.evidence, Evidence)
            or current is None
            or not load.evidence.trusted_for(load.name, load.current_ma, "mA")
        ):
            trusted_loads = False
            reasons.append(f"load {load.name} current evidence is missing or untrusted")
    concurrency_known = case.max_simultaneous is not None and all(
        load.max_concurrent is not None for load, _ in currents
    )
    if concurrency_known and active_count != case.max_simultaneous:
        raise ValueError("sum of per-load max_concurrent counts must equal max_simultaneous")
    required_decimal: Decimal | None = None
    required_ma: float | None = None
    if concurrency_known and all(current is not None for _, current in currents):
        try:
            with localcontext() as context:
                context.prec = _sum_precision(case.loads)
                context.Emax = _MAX_DECIMAL_PRECISION
                context.Emin = -_MAX_DECIMAL_PRECISION
                required_decimal = Decimal(0)
                for load in case.loads:
                    assert load.current_ma is not None and load.max_concurrent is not None
                    required_decimal += _current_decimal(load.current_ma, f"load {load.name} current") * Decimal(
                        load.max_concurrent
                    )
            required_ma = float(required_decimal)
        except (OverflowError, InvalidOperation):
            raise ValueError("computed operating-case current is not finite") from None
        if not math.isfinite(required_ma):
            raise ValueError("computed operating-case current is not finite")
    if case.max_simultaneous is None:
        reasons.append("maximum simultaneous load count is not declared")
    if any(current is None for _, current in currents):
        reasons.append("one or more per-load currents are not declared")
    if any(load.max_concurrent is None for load, _ in currents):
        reasons.append("one or more per-load concurrency counts are not declared")

    if not isinstance(case.connector_limits, tuple) or not isinstance(case.distribution_limits, tuple):
        raise ValueError("boundary limits must be immutable tuples")
    for item in (*case.connector_limits, *case.distribution_limits):
        if not isinstance(item, CurrentLimit):
            raise ValueError("connector and distribution boundaries must be CurrentLimit values")
    connector_names = [item.name for item in case.connector_limits]
    distribution_names = [item.name for item in case.distribution_limits]
    if any(not isinstance(name, str) or not name.strip() for name in (*connector_names, *distribution_names)):
        raise ValueError("boundary names must be non-empty strings")
    if len(set(connector_names)) != len(connector_names) or len(set(distribution_names)) != len(distribution_names):
        raise ValueError("duplicate connector or distribution boundary name")
    if case.source_limit is not None and not isinstance(case.source_limit, CurrentLimit):
        raise ValueError("source_limit must be a CurrentLimit or None")
    limits = [("source", case.source_limit)]
    limits.extend((f"connector:{item.name}", item) for item in case.connector_limits)
    limits.extend((f"distribution:{item.name}", item) for item in case.distribution_limits)
    if not case.connector_limits:
        reasons.append("connector current limit is not declared")
        limits.append(("connector", None))
    if not case.distribution_limits:
        reasons.append("distribution current limit is not declared")
        limits.append(("distribution", None))
    if case.source_limit is None:
        reasons.append("source current limit is not declared")

    boundary_results_list = []
    for item, boundary in ((item, name) for name, item in limits):
        result = _check_limit(item, required_ma, required_decimal, boundary)
        if not trusted_loads and result.status != "unverified":
            result = BoundaryResult(
                result.boundary,
                required_ma,
                result.limit_ma,
                "unverified",
                result.evidence_id,
                "load-current evidence is untrusted; comparison is conditional",
            )
        boundary_results_list.append(result)
    boundary_results = tuple(boundary_results_list)
    reasons.extend(result.reason for result in boundary_results if result.reason)
    if any(result.status == "failed" for result in boundary_results):
        status: Status = "failed"
    elif reasons or any(result.status == "unverified" for result in boundary_results):
        status = "unverified"
    else:
        status = "passed"
    return OperatingCaseResult(
        result_scope="authored_current_budget_only",
        input_sha256=_input_sha256(case),
        functionality_qualification="unverified",
        thermal_qualification="unverified",
        case_name=case.name,
        status=status,
        required_ma=required_ma,
        declared_max_simultaneous=case.max_simultaneous,
        boundary_results=boundary_results,
        reasons=tuple(reasons),
    )
