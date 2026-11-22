"""Single, evidence-gated manufacturing-readiness state machine."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .evidence_policy import EvidencePolicyError, require_backing, require_fabrication_evidence


class ReadinessContractError(ValueError):
    """A readiness transition is unsupported or lacks required evidence."""


READINESS_FILENAME = "manufacturing_readiness.json"
EVIDENCE_MANIFEST_FILENAME = "evidence_manifest.json"
_FABRICATION_GATE_TOKEN = object()
_READBACK_TOKEN = object()


class ManufacturingReadinessState(str, Enum):
    NOT_READY = "not_ready"
    NEEDS_REVIEW = "needs_review"
    DRC_PENDING = "drc_pending"
    DRC_CLEAN = "drc_clean"
    FABRICATION_READY = "fabrication_ready"
    BLOCKED = "blocked"


_ORDER = (
    ManufacturingReadinessState.NOT_READY,
    ManufacturingReadinessState.NEEDS_REVIEW,
    ManufacturingReadinessState.DRC_PENDING,
    ManufacturingReadinessState.DRC_CLEAN,
    ManufacturingReadinessState.FABRICATION_READY,
)


@dataclass(frozen=True)
class ManufacturingReadiness:
    """The one readiness payload returned by every product surface."""

    state: ManufacturingReadinessState = ManufacturingReadinessState.NOT_READY
    blockers: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    blocked_reason: str | None = None
    _gate_token: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.state is ManufacturingReadinessState.BLOCKED:
            if not self.blocked_reason or not self.blocked_reason.strip():
                raise ReadinessContractError("blocked readiness requires a reason")
        elif self.blocked_reason is not None:
            raise ReadinessContractError("blocked_reason is valid only for blocked readiness")
        if (
            self.state is ManufacturingReadinessState.FABRICATION_READY
            and self._gate_token not in {_FABRICATION_GATE_TOKEN, _READBACK_TOKEN}
        ):
            raise ReadinessContractError("fabrication_ready can only be produced by the evidence gate")
        for field_name in ("blockers", "evidence_ids", "next_actions"):
            values = getattr(self, field_name)
            if tuple(sorted(set(values))) != values:
                raise ReadinessContractError(f"{field_name} must be sorted and unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "blockers": list(self.blockers),
            "evidence_ids": list(self.evidence_ids),
            "next_actions": list(self.next_actions),
            "blocked_reason": self.blocked_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ManufacturingReadiness":
        if not isinstance(raw, dict):
            raise ReadinessContractError("manufacturing readiness must be an object")
        try:
            state = ManufacturingReadinessState(str(raw.get("state", "")))
        except ValueError as exc:
            raise ReadinessContractError("manufacturing readiness has an unknown state") from exc

        def string_tuple(field_name: str) -> tuple[str, ...]:
            value = raw.get(field_name, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ReadinessContractError(f"{field_name} must be an array of strings")
            return tuple(value)

        blocked_reason = raw.get("blocked_reason")
        if blocked_reason is not None and not isinstance(blocked_reason, str):
            raise ReadinessContractError("blocked_reason must be a string or null")

        return cls(
            state=state,
            blockers=string_tuple("blockers"),
            evidence_ids=string_tuple("evidence_ids"),
            next_actions=string_tuple("next_actions"),
            blocked_reason=blocked_reason,
            _gate_token=(
                _READBACK_TOKEN
                if state is ManufacturingReadinessState.FABRICATION_READY
                else None
            ),
        )

    def write(self, output: str | Path) -> Path:
        target = Path(output)
        if target.suffix.lower() != ".json":
            target = target / READINESS_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
            newline="",
        )
        return target


@dataclass(frozen=True)
class ManufacturingReadinessInputs:
    """Gate observations consumed only by the central readiness assessor."""

    identity_complete: bool = False
    placement_approved: bool = False
    routing_complete: bool = False
    erc_passed: bool = False
    drc_completed: bool = False
    drc_passed: bool = False
    bom_cpl_reconciled: bool = False
    fabrication_artifacts_valid: bool = False


@dataclass(frozen=True)
class ManufacturingReadinessOverride:
    """Explicit, expiring export authorization without changing readiness state."""

    id: str
    reason: str
    expires_at: str


def read_manufacturing_readiness(path: str | Path) -> ManufacturingReadiness:
    source = Path(path)
    if source.is_dir():
        direct = source / READINESS_FILENAME
        source = direct if direct.is_file() else source / "output" / READINESS_FILENAME
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReadinessContractError(f"cannot read manufacturing readiness: {exc}") from exc
    return ManufacturingReadiness.from_dict(raw)


def read_manufacturing_evidence(path: str | Path) -> tuple[dict[str, Any], ...]:
    """Load a fully validated evidence manifest for an export authorization gate."""

    from .evidence import EvidenceLedger

    source = Path(path)
    if source.is_dir():
        source = source / EVIDENCE_MANIFEST_FILENAME
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReadinessContractError(f"cannot read manufacturing evidence: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ReadinessContractError("manufacturing evidence manifest must be an object")
    try:
        records = EvidenceLedger.from_manifest(raw).to_manifest()["records"]
    except (TypeError, ValueError) as exc:
        raise ReadinessContractError(f"invalid manufacturing evidence manifest: {exc}") from exc
    return tuple(dict(record) for record in records if isinstance(record, Mapping))


def _records(records: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(record for record in records if isinstance(record, dict))


def require_manufacturing_evidence(
    records: Iterable[dict[str, Any]],
    *,
    acknowledged_heuristic_ids: Iterable[str] = (),
) -> dict[str, Mapping[str, Any]]:
    """Require identity, real-pad handoff, DRC, then the unchanged T244.4 gate."""

    materialized = _records(records)
    subjects = {str(record.get("subject_ref") or "") for record in materialized}
    if not any(subject.startswith(("comp:", "footprint:")) for subject in subjects):
        raise EvidencePolicyError("manufacturing readiness requires component/footprint identity evidence")
    if "tool:pcb_handoff" not in subjects:
        raise EvidencePolicyError("manufacturing readiness requires pad-bearing board provenance")
    if "tool:drc" not in subjects:
        raise EvidencePolicyError("manufacturing readiness requires DRC tool evidence")
    backing = require_fabrication_evidence(
        materialized,
        acknowledged_heuristic_ids=acknowledged_heuristic_ids,
    )
    backing["tool:pcb_handoff"] = require_backing(
        "tool:pcb_handoff",
        materialized,
        acknowledged_heuristic_ids=acknowledged_heuristic_ids,
    )
    backing["tool:drc"] = require_backing(
        "tool:drc",
        materialized,
        acknowledged_heuristic_ids=acknowledged_heuristic_ids,
    )
    return backing


def assess_manufacturing_readiness(
    inputs: ManufacturingReadinessInputs,
    *,
    evidence_records: Iterable[dict[str, Any]] = (),
) -> ManufacturingReadiness:
    """The single producer of readiness state used by every presentation surface."""

    records = _records(evidence_records)
    evidence_ids = tuple(
        sorted({str(record["id"]) for record in records if isinstance(record.get("id"), str)})
    )
    if inputs.drc_completed and not inputs.drc_passed:
        return block_manufacturing_readiness(
            "KiCad DRC reported unapproved blockers",
            blockers=("drc_failed",),
            evidence_ids=evidence_ids,
            next_actions=("Resolve DRC findings and rerun the exact-board transaction.",),
        )
    early_blockers: list[str] = []
    if not inputs.identity_complete:
        early_blockers.append("identity_incomplete")
    if not inputs.placement_approved:
        early_blockers.append("placement_not_approved")
    if early_blockers:
        return ManufacturingReadiness(
            state=ManufacturingReadinessState.NOT_READY,
            blockers=tuple(sorted(early_blockers)),
            evidence_ids=evidence_ids,
            next_actions=("Resolve identity and approve the exact placement state.",),
        )
    if not inputs.routing_complete:
        return ManufacturingReadiness(
            state=ManufacturingReadinessState.NEEDS_REVIEW,
            blockers=("routing_incomplete",),
            evidence_ids=evidence_ids,
            next_actions=("Complete routing and connectivity closure.",),
        )
    if not inputs.drc_completed:
        return ManufacturingReadiness(
            state=ManufacturingReadinessState.DRC_PENDING,
            blockers=("drc_not_run",),
            evidence_ids=evidence_ids,
            next_actions=("Run KiCad DRC on the exact staged board bytes.",),
        )
    remaining: list[str] = []
    if not inputs.erc_passed:
        remaining.append("erc_not_verified")
    if not inputs.bom_cpl_reconciled:
        remaining.append("bom_cpl_not_reconciled")
    if not inputs.fabrication_artifacts_valid:
        remaining.append("fabrication_artifacts_not_validated")
    if remaining:
        return ManufacturingReadiness(
            state=ManufacturingReadinessState.DRC_CLEAN,
            blockers=tuple(sorted(remaining)),
            evidence_ids=evidence_ids,
            next_actions=("Verify ERC, BOM/CPL reconciliation, and Gerber/drill outputs.",),
        )
    try:
        require_manufacturing_evidence(records)
    except EvidencePolicyError as exc:
        raise ReadinessContractError(
            "fabrication_ready requires identity/pads/DRC evidence and the T244.4 gate"
        ) from exc
    return ManufacturingReadiness(
        state=ManufacturingReadinessState.FABRICATION_READY,
        evidence_ids=evidence_ids,
        _gate_token=_FABRICATION_GATE_TOKEN,
    )


def require_export_authorized(
    readiness: ManufacturingReadiness,
    *,
    evidence_records: Iterable[dict[str, Any]] = (),
    override: ManufacturingReadinessOverride | None = None,
    board_sha256: str | None = None,
    now: datetime | None = None,
) -> None:
    if readiness.state is ManufacturingReadinessState.FABRICATION_READY:
        records = _records(evidence_records)
        try:
            backing = require_manufacturing_evidence(records)
            manifest_ids = {
                str(record["id"])
                for record in records
                if isinstance(record.get("id"), str)
            }
            readiness_ids = set(readiness.evidence_ids)
            backing_ids = {
                str(record["id"])
                for record in backing.values()
                if isinstance(record.get("id"), str)
            }
            if not readiness_ids or readiness_ids - manifest_ids:
                raise EvidencePolicyError(
                    "fabrication_ready evidence IDs must resolve in the evidence manifest"
                )
            if backing_ids - readiness_ids:
                raise EvidencePolicyError(
                    "fabrication_ready evidence IDs do not cover the T244.4 gate results"
                )
            if board_sha256 is not None:
                if not re.fullmatch(r"[a-fA-F0-9]{64}", board_sha256):
                    raise EvidencePolicyError("export board SHA-256 is invalid")
                drc_claim = backing["tool:drc"].get("claim")
                try:
                    claim = json.loads(drc_claim) if isinstance(drc_claim, str) else None
                except json.JSONDecodeError:
                    claim = None
                if not isinstance(claim, Mapping) or claim.get("board_sha256") != board_sha256:
                    raise EvidencePolicyError(
                        "fabrication-ready DRC evidence does not match the exact board bytes being exported"
                    )
                source = backing["tool:drc"].get("source")
                if not isinstance(source, Mapping) or source.get("content_hash") != board_sha256:
                    raise EvidencePolicyError(
                        "fabrication-ready DRC evidence does not bind its source hash to the exported board"
                    )
            return
        except EvidencePolicyError as exc:
            if override is None:
                raise ReadinessContractError(
                    f"fabrication_ready evidence revalidation failed: {exc}"
                ) from exc
    if (
        override is None
        or not isinstance(override.id, str)
        or not override.id.strip()
        or not isinstance(override.reason, str)
        or not override.reason.strip()
    ):
        raise ReadinessContractError("manufacturing export requires fabrication_ready or an explicit override")
    if not isinstance(override.expires_at, str) or not override.expires_at.strip():
        raise ReadinessContractError("manufacturing override expiry must be ISO-8601")
    try:
        expires = datetime.fromisoformat(override.expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReadinessContractError("manufacturing override expiry must be ISO-8601") from exc
    if expires.tzinfo is None or expires.astimezone(timezone.utc) <= (
        now or datetime.now(timezone.utc)
    ).astimezone(timezone.utc):
        raise ReadinessContractError("manufacturing override is expired")


def block_manufacturing_readiness(
    reason: str, *, blockers: Iterable[str] = (), evidence_ids: Iterable[str] = (), next_actions: Iterable[str] = ()
) -> ManufacturingReadiness:
    return ManufacturingReadiness(
        state=ManufacturingReadinessState.BLOCKED,
        blockers=tuple(sorted(set(blockers))),
        evidence_ids=tuple(sorted(set(evidence_ids))),
        next_actions=tuple(sorted(set(next_actions))),
        blocked_reason=reason,
    )


def transition_manufacturing_readiness(
    current: ManufacturingReadiness,
    target: ManufacturingReadinessState,
    *,
    evidence_records: Iterable[dict[str, Any]] = (),
    drc_passed: bool = False,
    blockers: Iterable[str] = (),
    next_actions: Iterable[str] = (),
) -> ManufacturingReadiness:
    """Advance exactly one evidence-gated state; blocked is terminal."""

    if current.state is ManufacturingReadinessState.BLOCKED:
        raise ReadinessContractError("blocked manufacturing readiness is terminal")
    if target is ManufacturingReadinessState.BLOCKED:
        raise ReadinessContractError("use block_manufacturing_readiness to supply a reason")
    current_index = _ORDER.index(current.state)
    target_index = _ORDER.index(target)
    if target_index != current_index + 1:
        raise ReadinessContractError("manufacturing readiness transitions must advance exactly one state")

    records = tuple(evidence_records)
    if target is ManufacturingReadinessState.DRC_CLEAN:
        has_verified_drc = any(
            record.get("subject_ref") == "tool:drc"
            and record.get("kind") == "tool_result"
            and record.get("confidence") in {"verified", "corroborated"}
            and not record.get("conflicts")
            for record in records
        )
        if not drc_passed or not has_verified_drc:
            raise ReadinessContractError("drc_clean requires passing verified tool:drc evidence")
    if target is ManufacturingReadinessState.FABRICATION_READY:
        try:
            require_manufacturing_evidence(records)
        except EvidencePolicyError as exc:
            raise ReadinessContractError(
                "fabrication_ready requires identity/pads/DRC evidence and the T244.4 fabrication-evidence gate"
            ) from exc

    evidence_ids = tuple(
        sorted(
            set(current.evidence_ids)
            | {str(record["id"]) for record in records if isinstance(record.get("id"), str)}
        )
    )
    return ManufacturingReadiness(
        state=target,
        blockers=tuple(sorted(set(blockers))),
        evidence_ids=evidence_ids,
        next_actions=tuple(sorted(set(next_actions))),
        _gate_token=(
            _FABRICATION_GATE_TOKEN
            if target is ManufacturingReadinessState.FABRICATION_READY
            else None
        ),
    )
