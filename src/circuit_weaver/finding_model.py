"""T253 unified finding model for generated and imported design analysis.

Existing producers keep their native result types while they migrate.  This
module is the one versioned boundary used to normalize those results for
review, deduplication, repair planning, JSON, and SARIF output.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .evidence_policy import EVIDENCE_ID_PATTERN
from .finding_contract import RULE_ID_PATTERN

FINDING_SCHEMA_VERSION = "circuit-weaver-findings/v2"
SARIF_VERSION = "2.1.0"
SARIF_SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"

_ARTIFACT_KINDS = frozenset(
    {
        "design",
        "schematic",
        "pcb",
        "gerber",
        "erc",
        "drc",
        "dfm",
        "sourcing",
        "evidence_conflict",
    }
)
_SEVERITIES = frozenset({"blocker", "major", "minor", "info"})
_SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
_CONFIDENCES = frozenset({"verified", "corroborated", "single_source", "heuristic", "stub", "conflicting"})
# Deduplication is deliberately conservative: another weak observation cannot
# silently promote the normalized root cause.
_CONFIDENCE_RANK = {
    "conflicting": 0,
    "stub": 1,
    "heuristic": 2,
    "single_source": 3,
    "corroborated": 4,
    "verified": 5,
}
_VERIFICATION_STATUSES = frozenset({"unverified", "verified", "failed", "not_applicable"})
_REMEDIATION_KINDS = frozenset({"manual", "repair_plan", "unsupported"})
_RISKS = frozenset({"low", "medium", "high"})
_SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
_REMEDIATION_ID_PATTERN = re.compile(r"^REM-[a-z0-9][a-z0-9-]{2,63}$")
# Native suppressions use SUP-* while authoritative DRC overrides use OVR-*.
# Both are stable, audited identifiers and must survive normalization intact.
_SUPPRESSION_ID_PATTERN = re.compile(r"^(?:SUP-[a-z0-9][a-z0-9-]{2,63}|OVR-[A-Za-z0-9][A-Za-z0-9_.:-]{0,63})$")


class FindingModelError(ValueError):
    """A finding cannot cross the T253 normalization boundary."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _portable_path(value: str) -> str:
    if not isinstance(value, str):
        raise FindingModelError("artifact_path must be a string")
    normalized = value.replace("\\", "/").strip()
    if not normalized:
        return ""
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:/", normalized):
        raise FindingModelError("artifact_path must be portable and project-relative")
    return path.as_posix()


def _evidence_ids(values: Iterable[str]) -> tuple[str, ...]:
    materialized = tuple(values)
    if any(not isinstance(value, str) or not EVIDENCE_ID_PATTERN.fullmatch(value) for value in materialized):
        raise FindingModelError("evidence_ids must contain valid evidence IDs")
    return tuple(sorted(set(materialized)))


@dataclass(frozen=True)
class FindingLocation:
    """Exact project-relative artifact and object location for a finding."""

    artifact_kind: str
    artifact_path: str = ""
    object_type: str = ""
    object_id: str = ""
    ref: str = ""
    net: str = ""
    sheet: str = ""
    layer: str = ""
    line: int | None = None
    column: int | None = None
    x_mm: float | None = None
    y_mm: float | None = None

    def __post_init__(self) -> None:
        if self.artifact_kind not in _ARTIFACT_KINDS:
            raise FindingModelError(f"unsupported artifact_kind: {self.artifact_kind!r}")
        object.__setattr__(self, "artifact_path", _portable_path(self.artifact_path))
        for field in ("object_type", "object_id", "ref", "net", "sheet", "layer"):
            value = getattr(self, field)
            if not isinstance(value, str):
                raise FindingModelError(f"location {field} must be a string")
        if not any(
            (
                self.artifact_path,
                self.object_id,
                self.ref,
                self.net,
                self.sheet,
                self.layer,
                self.line,
                self.x_mm is not None,
            )
        ):
            raise FindingModelError("finding location must identify an artifact or exact object")
        if self.line is not None and (not isinstance(self.line, int) or self.line < 1):
            raise FindingModelError("location line must be a positive integer")
        if self.column is not None and (not isinstance(self.column, int) or self.column < 1):
            raise FindingModelError("location column must be a positive integer")
        if self.column is not None and self.line is None:
            raise FindingModelError("location column requires line")
        if (self.x_mm is None) != (self.y_mm is None):
            raise FindingModelError("location coordinates require both x_mm and y_mm")
        if self.x_mm is not None and not all(isinstance(value, (int, float)) for value in (self.x_mm, self.y_mm)):
            raise FindingModelError("location coordinates must be numeric")

    def identity_scope(self) -> dict[str, object]:
        """Return the root-cause scope, excluding analyzer-specific presentation."""

        scope = {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "ref": self.ref,
            "net": self.net,
            "sheet": self.sheet,
            "layer": self.layer,
        }
        if not any(scope.values()):
            scope["artifact_path"] = self.artifact_path
        return scope

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "artifact_path": self.artifact_path,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "ref": self.ref,
            "net": self.net,
            "sheet": self.sheet,
            "layer": self.layer,
            "line": self.line,
            "column": self.column,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
        }


@dataclass(frozen=True)
class RemediationOption:
    """A bounded next action; T254 may later turn supported options into plans."""

    id: str
    summary: str
    kind: str = "manual"
    risk: str = "medium"
    supported: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _REMEDIATION_ID_PATTERN.fullmatch(self.id):
            raise FindingModelError("remediation id must be a stable REM- identifier")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise FindingModelError("remediation summary is required")
        if self.kind not in _REMEDIATION_KINDS:
            raise FindingModelError(f"unsupported remediation kind: {self.kind!r}")
        if self.risk not in _RISKS:
            raise FindingModelError(f"unsupported remediation risk: {self.risk!r}")
        if not isinstance(self.supported, bool):
            raise FindingModelError("remediation supported must be boolean")
        if self.kind == "unsupported" and self.supported:
            raise FindingModelError("unsupported remediation cannot be marked supported")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "summary": self.summary,
            "kind": self.kind,
            "risk": self.risk,
            "supported": self.supported,
        }


@dataclass(frozen=True)
class FindingObservation:
    """One analyzer's complete supporting observation for a root cause."""

    source: str
    source_finding_id: str
    message: str
    severity: str
    detection_confidence: str
    location: FindingLocation
    evidence_ids: tuple[str, ...] = ()
    observed_value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not _SOURCE_PATTERN.fullmatch(self.source):
            raise FindingModelError("observation source must be a stable analyzer identifier")
        if not isinstance(self.source_finding_id, str) or not self.source_finding_id.strip():
            raise FindingModelError("observation source_finding_id is required")
        if not isinstance(self.message, str) or not self.message.strip():
            raise FindingModelError("observation message is required")
        if self.severity not in _SEVERITIES:
            raise FindingModelError(f"unsupported finding severity: {self.severity!r}")
        if self.detection_confidence not in _CONFIDENCES:
            raise FindingModelError(f"unsupported finding detection_confidence: {self.detection_confidence!r}")
        object.__setattr__(self, "evidence_ids", _evidence_ids(self.evidence_ids))
        if self.observed_value is not None and not isinstance(self.observed_value, str):
            raise FindingModelError("observation observed_value must be a string or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_finding_id": self.source_finding_id,
            "message": self.message,
            "severity": self.severity,
            "detection_confidence": self.detection_confidence,
            "location": self.location.to_dict(),
            "evidence_ids": list(self.evidence_ids),
            "observed_value": self.observed_value,
        }


@dataclass(frozen=True)
class UnifiedFinding:
    """Versioned T253 root cause retaining every supporting observation."""

    rule_id: str
    root_cause_key: str
    message: str
    severity: str
    detection_confidence: str
    location: FindingLocation
    observations: tuple[FindingObservation, ...]
    evidence_ids: tuple[str, ...] = ()
    remediation_options: tuple[RemediationOption, ...] = ()
    verification_status: str = "unverified"
    suppressed: bool = False
    suppression_id: str | None = None
    # Keep every applied suppression for auditability.  suppression_id remains
    # as the backwards-compatible primary identifier.
    suppression_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not RULE_ID_PATTERN.fullmatch(self.rule_id):
            raise FindingModelError("rule_id must be a stable CW-<DOMAIN>-<NNN> ID")
        if not isinstance(self.root_cause_key, str) or not self.root_cause_key.strip():
            raise FindingModelError("root_cause_key is required")
        if not isinstance(self.message, str) or not self.message.strip():
            raise FindingModelError("finding message is required")
        if self.severity not in _SEVERITIES:
            raise FindingModelError(f"unsupported finding severity: {self.severity!r}")
        if self.detection_confidence not in _CONFIDENCES:
            raise FindingModelError(f"unsupported finding detection_confidence: {self.detection_confidence!r}")
        if self.verification_status not in _VERIFICATION_STATUSES:
            raise FindingModelError(f"unsupported finding verification_status: {self.verification_status!r}")
        if not isinstance(self.observations, tuple) or not self.observations:
            raise FindingModelError("finding requires at least one supporting observation")
        if not all(isinstance(item, FindingObservation) for item in self.observations):
            raise FindingModelError("finding observations must use FindingObservation")
        if not isinstance(self.remediation_options, tuple) or not all(
            isinstance(item, RemediationOption) for item in self.remediation_options
        ):
            raise FindingModelError("finding remediation_options must use RemediationOption")
        object.__setattr__(self, "evidence_ids", _evidence_ids(self.evidence_ids))
        raw_suppression_ids = tuple(self.suppression_ids)
        if any(
            not isinstance(value, str) or not _SUPPRESSION_ID_PATTERN.fullmatch(value) for value in raw_suppression_ids
        ):
            raise FindingModelError("suppression_ids must contain stable SUP-/OVR- identifiers")
        suppression_ids = tuple(sorted(set(raw_suppression_ids)))
        if self.suppression_id is not None:
            if not isinstance(self.suppression_id, str) or not _SUPPRESSION_ID_PATTERN.fullmatch(self.suppression_id):
                raise FindingModelError("suppression_id must be a stable SUP-/OVR- identifier")
            suppression_ids = tuple(sorted(set(suppression_ids + (self.suppression_id,))))
        object.__setattr__(self, "suppression_ids", suppression_ids)
        if self.suppressed and not suppression_ids:
            raise FindingModelError("suppressed finding requires suppression_id")
        # A merged root cause stays actionable when any observation is
        # unsuppressed, but IDs applied to sibling observations remain audit
        # evidence.  ``suppression_id`` is only the active aggregate override;
        # ``suppression_ids`` is the complete historical set.
        if not self.suppressed and self.suppression_id is not None:
            raise FindingModelError("unsuppressed finding cannot carry an active suppression_id")

        expected_severity = max((item.severity for item in self.observations), key=_SEVERITY_RANK.__getitem__)
        expected_confidence = min(
            (item.detection_confidence for item in self.observations),
            key=_CONFIDENCE_RANK.__getitem__,
        )
        if self.severity != expected_severity:
            raise FindingModelError("finding severity does not match supporting observations")
        if self.detection_confidence != expected_confidence:
            raise FindingModelError("finding detection_confidence does not match supporting observations")

    @property
    def id(self) -> str:
        identity = {
            "rule_id": self.rule_id,
            "root_cause_key": self.root_cause_key,
            "scope": self.location.identity_scope(),
        }
        digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()[:12]
        return f"FND-{digest}"

    @property
    def content_integrity(self) -> str:
        """Digest of mutable finding content, deliberately separate from ``id``."""

        payload = self._serialized_content()
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def _serialized_content(self) -> dict[str, object]:
        return {
            "schema_version": FINDING_SCHEMA_VERSION,
            "rule_id": self.rule_id,
            "root_cause_key": self.root_cause_key,
            "message": self.message,
            "severity": self.severity,
            "detection_confidence": self.detection_confidence,
            "location": self.location.to_dict(),
            "evidence_ids": list(self.evidence_ids),
            "remediation_options": [item.to_dict() for item in self.remediation_options],
            "verification_status": self.verification_status,
            "observations": [item.to_dict() for item in self.observations],
            "suppressed": self.suppressed,
            "suppression_id": self.suppression_id,
            "suppression_ids": list(self.suppression_ids),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._serialized_content(), "id": self.id, "content_integrity": self.content_integrity}


def _remediation_id(summary: str) -> str:
    digest = hashlib.sha256(summary.strip().encode("utf-8")).hexdigest()[:12]
    return f"REM-{digest}"


def from_validation_issue(
    issue: Any,
    *,
    source: str,
    artifact_kind: str,
    artifact_path: str = "",
    object_type: str = "",
    object_id: str = "",
    verification_status: str = "unverified",
) -> UnifiedFinding:
    """Normalize the shared T248 ValidationIssue seed without trusting presentation fields."""

    rule_id = getattr(issue, "rule_id", None)
    if not isinstance(rule_id, str) or not RULE_ID_PATTERN.fullmatch(rule_id):
        raise FindingModelError("ValidationIssue must carry a stable rule_id before normalization")
    code = getattr(issue, "code", "")
    ref = getattr(issue, "ref", "") or ""
    net = getattr(issue, "net", "") or ""
    mpn = getattr(issue, "mpn", "") or ""
    if not object_type:
        object_type = "component" if ref else "net" if net else "finding"
    if not object_id:
        object_id = ref or net or str(code)
    location = FindingLocation(
        artifact_kind=artifact_kind,
        artifact_path=artifact_path,
        object_type=object_type,
        object_id=object_id,
        ref=ref,
        net=net,
    )
    message = getattr(issue, "message", "")
    severity = getattr(issue, "severity", "")
    confidence = getattr(issue, "detection_confidence", "")
    evidence_ids = tuple(getattr(issue, "evidence_ids", ()) or ())
    observation = FindingObservation(
        source=source,
        source_finding_id=str(code),
        message=message,
        severity=severity,
        detection_confidence=confidence,
        location=location,
        evidence_ids=evidence_ids,
        observed_value=getattr(issue, "observed_value", None),
    )
    action = getattr(issue, "safest_next_action", None) or getattr(issue, "suggestion", None)
    remediation_options: tuple[RemediationOption, ...] = ()
    if isinstance(action, str) and action.strip():
        remediation_options = (
            RemediationOption(
                id=_remediation_id(action),
                summary=action,
                kind="manual",
                risk="high" if severity == "blocker" else "medium",
                supported=False,
            ),
        )
    root_cause_key = "|".join(str(value) for value in (code, ref, net, mpn) if value)
    return UnifiedFinding(
        rule_id=rule_id,
        root_cause_key=root_cause_key,
        message=message,
        severity=severity,
        detection_confidence=confidence,
        location=location,
        observations=(observation,),
        evidence_ids=evidence_ids,
        remediation_options=remediation_options,
        verification_status=verification_status,
        suppressed=bool(getattr(issue, "suppressed", False)),
        suppression_id=getattr(issue, "suppression_id", None),
    )


_PCB_DFM_RULES = {
    "track_width": "CW-DFM-001",
    "track_spacing": "CW-DFM-002",
    "via_drill": "CW-DFM-003",
    "annular_ring": "CW-DFM-004",
    "board_size": "CW-DFM-005",
    "board_size_small": "CW-DFM-006",
}


def _format_mm(value: object) -> str | None:
    if isinstance(value, (int, float)):
        return f"{value:g} mm"
    if isinstance(value, list) and value and all(isinstance(item, (int, float)) for item in value):
        return " x ".join(f"{item:g}" for item in value) + " mm"
    return None


def _imported_finding(
    *,
    rule_id: str,
    root_cause_key: str,
    source: str,
    source_finding_id: str,
    message: str,
    severity: str,
    detection_confidence: str,
    location: FindingLocation,
    observed_value: str | None,
    remediation: str,
) -> UnifiedFinding:
    observation = FindingObservation(
        source=source,
        source_finding_id=source_finding_id,
        message=message,
        severity=severity,
        detection_confidence=detection_confidence,
        location=location,
        observed_value=observed_value,
    )
    return UnifiedFinding(
        rule_id=rule_id,
        root_cause_key=root_cause_key,
        message=message,
        severity=severity,
        detection_confidence=detection_confidence,
        location=location,
        observations=(observation,),
        remediation_options=(
            RemediationOption(
                id=_remediation_id(remediation),
                summary=remediation,
                kind="manual",
                risk="medium",
                supported=False,
            ),
        ),
    )


def from_import_analysis(
    kind: str,
    payload: Mapping[str, Any],
    *,
    artifact_path: str,
) -> tuple[UnifiedFinding, ...]:
    """Normalize supported imported-analyzer observations without inventing facts.

    T253 starts with the PCB DFM and Gerber alignment paths because both
    already emit explicit adverse observations. Unknown analyzer dictionaries
    remain visible in their native reports rather than being guessed into a
    rule or silently treated as clean.
    """

    if not isinstance(payload, Mapping):
        raise FindingModelError("import analysis payload must be an object")
    findings: list[UnifiedFinding] = []
    if kind == "pcb":
        dfm = payload.get("dfm")
        if not isinstance(dfm, Mapping):
            return ()
        violations = dfm.get("violations", [])
        if not isinstance(violations, list):
            raise FindingModelError("PCB DFM violations must be a list")
        for row in violations:
            if not isinstance(row, Mapping):
                raise FindingModelError("PCB DFM violation must be an object")
            parameter = str(row.get("parameter") or "")
            rule_id = _PCB_DFM_RULES.get(parameter)
            if rule_id is None:
                # An unregistered producer shape is not silently assigned a
                # generic rule; it stays in the native analyzer report.
                continue
            tier = str(row.get("tier_required") or "standard")
            message = str(row.get("message") or f"PCB DFM {parameter} finding")
            confidence = "heuristic" if parameter == "track_spacing" else "single_source"
            severity = "major" if tier == "challenging" else "minor"
            observed = _format_mm(row.get("actual_mm"))
            location = FindingLocation(
                artifact_kind="pcb",
                artifact_path=artifact_path,
                object_type="dfm_parameter",
                object_id=parameter,
            )
            findings.append(
                _imported_finding(
                    rule_id=rule_id,
                    root_cause_key=f"pcb-dfm|{parameter}",
                    source="import.pcb.dfm",
                    source_finding_id=parameter,
                    message=message,
                    severity=severity,
                    detection_confidence=confidence,
                    location=location,
                    observed_value=observed,
                    remediation=(
                        f"Review {parameter.replace('_', ' ')} against the selected fabricator profile "
                        "before creating an approved repair plan."
                    ),
                )
            )
    elif kind == "gerbers":
        alignment = payload.get("alignment")
        if not isinstance(alignment, Mapping):
            return ()
        raw_issues = alignment.get("issues", [])
        if not isinstance(raw_issues, list):
            raise FindingModelError("Gerber alignment issues must be a list")
        method = str(alignment.get("method") or "unknown")
        confidence = "conflicting" if method == "conflicting_x2_metadata" else "single_source"
        location = FindingLocation(
            artifact_kind="gerber",
            artifact_path=artifact_path,
            object_type="fabrication_set",
            object_id="alignment",
        )
        for index, raw_issue in enumerate(raw_issues, 1):
            if not isinstance(raw_issue, str) or not raw_issue.strip():
                raise FindingModelError("Gerber alignment issue must be non-empty text")
            findings.append(
                _imported_finding(
                    rule_id="CW-DFM-007",
                    root_cause_key=f"gerber-alignment|{method}",
                    source="import.gerber.alignment",
                    source_finding_id=f"{method}:{index}",
                    message=raw_issue,
                    severity="major",
                    detection_confidence=confidence,
                    location=location,
                    observed_value=None,
                    remediation=(
                        "Regenerate Gerber and drill files from one coordinate origin with consistent "
                        "X2 SameCoordinates metadata."
                    ),
                )
            )
    elif kind != "schematic":
        raise FindingModelError(f"unsupported import analyzer kind: {kind!r}")
    return deduplicate_findings(findings)


def deduplicate_findings(findings: Iterable[UnifiedFinding]) -> tuple[UnifiedFinding, ...]:
    """Merge identical root causes deterministically while retaining all observations."""

    grouped: dict[str, list[UnifiedFinding]] = {}
    for finding in findings:
        if not isinstance(finding, UnifiedFinding):
            raise FindingModelError("deduplication accepts only UnifiedFinding values")
        grouped.setdefault(finding.id, []).append(finding)

    merged: list[UnifiedFinding] = []
    for finding_id in sorted(grouped):
        group = grouped[finding_id]
        base = min(group, key=lambda item: _canonical_json(item.location.to_dict()))
        observations_by_payload = {
            _canonical_json(observation.to_dict()): observation
            for finding in group
            for observation in finding.observations
        }
        options_by_payload = {
            _canonical_json(option.to_dict()): option for finding in group for option in finding.remediation_options
        }
        evidence_ids = tuple(sorted({item for finding in group for item in finding.evidence_ids}))
        severity = max((finding.severity for finding in group), key=_SEVERITY_RANK.__getitem__)
        confidence = min(
            (finding.detection_confidence for finding in group),
            key=_CONFIDENCE_RANK.__getitem__,
        )
        statuses = {finding.verification_status for finding in group}
        if "failed" in statuses:
            verification_status = "failed"
        elif statuses == {"verified"}:
            verification_status = "verified"
        elif statuses == {"not_applicable"}:
            verification_status = "not_applicable"
        else:
            verification_status = "unverified"
        selected = min(
            group,
            key=lambda item: (
                -_SEVERITY_RANK[item.severity],
                _canonical_json(item.to_dict()),
            ),
        )
        suppressed = all(finding.suppressed for finding in group)
        suppression_ids = {
            suppression_id
            for finding in group
            for suppression_id in (
                finding.suppression_ids or ((finding.suppression_id,) if finding.suppression_id else ())
            )
        }
        primary_suppression_id = min(suppression_ids) if suppressed and suppression_ids else None
        merged.append(
            UnifiedFinding(
                rule_id=base.rule_id,
                root_cause_key=base.root_cause_key,
                message=selected.message,
                severity=severity,
                detection_confidence=confidence,
                location=base.location,
                observations=tuple(observations_by_payload[key] for key in sorted(observations_by_payload)),
                evidence_ids=evidence_ids,
                remediation_options=tuple(options_by_payload[key] for key in sorted(options_by_payload)),
                verification_status=verification_status,
                suppressed=suppressed,
                suppression_id=primary_suppression_id,
                suppression_ids=tuple(sorted(suppression_ids)),
            )
        )
    return tuple(merged)


def findings_document(findings: Iterable[UnifiedFinding]) -> dict[str, object]:
    """Return the deterministic versioned JSON finding document."""

    normalized = deduplicate_findings(findings)
    return {
        "schema_version": FINDING_SCHEMA_VERSION,
        "finding_count": len(normalized),
        "findings": [finding.to_dict() for finding in normalized],
    }


def findings_json(findings: Iterable[UnifiedFinding]) -> str:
    return json.dumps(findings_document(findings), indent=2, sort_keys=True) + "\n"


def _sarif_location(location: FindingLocation) -> dict[str, object] | None:
    result: dict[str, object] = {}
    if location.artifact_path:
        physical: dict[str, object] = {"artifactLocation": {"uri": location.artifact_path, "uriBaseId": "%SRCROOT%"}}
        if location.line is not None:
            region: dict[str, object] = {"startLine": location.line}
            if location.column is not None:
                region["startColumn"] = location.column
            physical["region"] = region
        result["physicalLocation"] = physical
    logical_name = location.ref or location.net or location.object_id
    if logical_name:
        logical: dict[str, object] = {"name": logical_name}
        if location.object_type:
            logical["kind"] = location.object_type
        result["logicalLocations"] = [logical]
    return result or None


def findings_sarif(findings: Iterable[UnifiedFinding]) -> dict[str, object]:
    """Return SARIF 2.1.0 without dropping Circuit Weaver trust metadata."""

    normalized = deduplicate_findings(findings)
    by_rule: dict[str, UnifiedFinding] = {}
    for finding in normalized:
        by_rule.setdefault(finding.rule_id, finding)
    rules = [
        {
            "id": rule_id,
            "name": rule_id,
            "shortDescription": {"text": rule_id},
            "fullDescription": {"text": by_rule[rule_id].message},
        }
        for rule_id in sorted(by_rule)
    ]
    severity_level = {"blocker": "error", "major": "warning", "minor": "note", "info": "note"}
    results: list[dict[str, object]] = []
    for finding in normalized:
        result: dict[str, object] = {
            "ruleId": finding.rule_id,
            "level": severity_level[finding.severity],
            "message": {"text": finding.message},
            "partialFingerprints": {"circuitWeaverFindingId/v1": finding.id},
            "properties": {
                "finding_id": finding.id,
                "content_integrity": finding.content_integrity,
                "schema_version": FINDING_SCHEMA_VERSION,
                "severity": finding.severity,
                "detection_confidence": finding.detection_confidence,
                "verification_status": finding.verification_status,
                "evidence_ids": list(finding.evidence_ids),
                "location": finding.location.to_dict(),
                "remediation_options": [item.to_dict() for item in finding.remediation_options],
                "observations": [item.to_dict() for item in finding.observations],
                "suppression_ids": list(finding.suppression_ids),
            },
        }
        location = _sarif_location(finding.location)
        if location is not None:
            result["locations"] = [location]
        if finding.suppressed:
            result["suppressions"] = [
                {
                    "kind": "external",
                    "status": "accepted",
                    "justification": finding.suppression_id or "approved suppression",
                }
            ]
        results.append(result)
    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Circuit Weaver",
                        "informationUri": "https://github.com/mattpainter701/circuit-weaver",
                        "rules": rules,
                    }
                },
                "originalUriBaseIds": {"%SRCROOT%": {"uri": "./"}},
                "results": results,
            }
        ],
    }


def findings_sarif_json(findings: Iterable[UnifiedFinding]) -> str:
    return json.dumps(findings_sarif(findings), indent=2, sort_keys=True) + "\n"


def write_findings_json(path: str | Path, findings: Iterable[UnifiedFinding]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(findings_json(findings), encoding="utf-8")
    return destination


def write_findings_sarif(path: str | Path, findings: Iterable[UnifiedFinding]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(findings_sarif_json(findings), encoding="utf-8")
    return destination


def finding_from_dict(payload: Mapping[str, Any]) -> UnifiedFinding:
    """Strictly load the frozen per-finding JSON shape."""

    if not isinstance(payload, Mapping):
        raise FindingModelError("finding JSON must be an object")
    if payload.get("schema_version") != FINDING_SCHEMA_VERSION:
        raise FindingModelError("unsupported finding schema_version")
    required = {
        "schema_version",
        "id",
        "rule_id",
        "root_cause_key",
        "message",
        "severity",
        "detection_confidence",
        "location",
        "evidence_ids",
        "remediation_options",
        "verification_status",
        "observations",
        "suppressed",
        "suppression_id",
        "suppression_ids",
        "content_integrity",
    }
    if set(payload) != required:
        raise FindingModelError("finding JSON does not match the frozen schema")
    string_fields = {
        "id",
        "content_integrity",
        "rule_id",
        "root_cause_key",
        "message",
        "severity",
        "detection_confidence",
        "verification_status",
    }
    if any(not isinstance(payload.get(field), str) for field in string_fields):
        raise FindingModelError("finding JSON string fields must not be coerced")
    if not isinstance(payload.get("suppressed"), bool):
        raise FindingModelError("finding suppressed must be a boolean")
    if payload.get("suppression_id") is not None and not isinstance(payload.get("suppression_id"), str):
        raise FindingModelError("finding suppression_id must be a string or null")
    for field in ("evidence_ids", "remediation_options", "observations", "suppression_ids"):
        if not isinstance(payload.get(field), list):
            raise FindingModelError(f"finding {field} must be a list")
    serialized_content = dict(payload)
    serialized_content.pop("id", None)
    serialized_integrity = serialized_content.pop("content_integrity", None)
    expected_serialized_integrity = hashlib.sha256(_canonical_json(serialized_content).encode("utf-8")).hexdigest()
    if serialized_integrity != expected_serialized_integrity:
        raise FindingModelError("finding content_integrity does not match serialized content")
    raw_location = payload.get("location")
    if not isinstance(raw_location, Mapping):
        raise FindingModelError("finding location must be an object")
    location = FindingLocation(**dict(raw_location))
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, list):
        raise FindingModelError("finding observations must be a list")
    observations: list[FindingObservation] = []
    for item in raw_observations:
        if not isinstance(item, Mapping):
            raise FindingModelError("finding observation must be an object")
        values = dict(item)
        observation_location = values.get("location")
        if not isinstance(observation_location, Mapping):
            raise FindingModelError("observation location must be an object")
        values["location"] = FindingLocation(**dict(observation_location))
        values["evidence_ids"] = tuple(values.get("evidence_ids", ()))
        observations.append(FindingObservation(**values))
    raw_options = payload.get("remediation_options")
    if not isinstance(raw_options, list):
        raise FindingModelError("finding remediation_options must be a list")
    options = tuple(
        RemediationOption(**dict(item))
        if isinstance(item, Mapping)
        else (_ for _ in ()).throw(FindingModelError("remediation option must be an object"))
        for item in raw_options
    )
    serialized_status = payload["verification_status"]
    if serialized_status not in _VERIFICATION_STATUSES:
        raise FindingModelError("unsupported finding verification_status")
    # Verification is an assertion made by the current analysis run, not a
    # fact that can be carried forward from an untrusted export.  Re-reading
    # an export therefore always starts unverified.
    finding = UnifiedFinding(
        rule_id=str(payload["rule_id"]),
        root_cause_key=str(payload["root_cause_key"]),
        message=str(payload["message"]),
        severity=str(payload["severity"]),
        detection_confidence=str(payload["detection_confidence"]),
        location=location,
        observations=tuple(observations),
        evidence_ids=tuple(payload.get("evidence_ids", ())),
        remediation_options=options,
        verification_status="unverified",
        suppressed=bool(payload["suppressed"]),
        suppression_id=payload.get("suppression_id"),
        suppression_ids=tuple(payload.get("suppression_ids", ())),
    )
    if payload.get("id") != finding.id:
        raise FindingModelError("finding id does not match stable identity")
    return finding
