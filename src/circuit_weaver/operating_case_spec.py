"""Resolve authored current cases through the existing evidence ledger."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .evidence import EvidenceLedger
from .operating_cases import CurrentLimit, Evidence, Load, OperatingCase, check_operating_case


def _object(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) - keys:
        raise ValueError(f"{label} must be an object with only {', '.join(sorted(keys))}")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
        raise ValueError("operating case and load/boundary names must be identifiers of 1 to 64 characters")
    return value


def _rows(value: Any, label: str, maximum: int) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a list of at most {maximum} items")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate operating-current evidence claim key")
        result[key] = value
    return result


def evaluate_spec_operating_cases(spec: dict) -> tuple[dict[str, Any], EvidenceLedger]:
    """Evaluate optional spec cases without trusting inline confidence labels.

    Ledger records are validated and exactly matched; this does not authenticate
    the supplied sources or infer any physical circuit/rail attachment.
    """
    engine = spec.get("engine")
    if isinstance(engine, dict) and "operating_cases" in engine:
        if "operating_cases" in spec and spec["operating_cases"] != engine["operating_cases"]:
            raise ValueError("outer and nested engine operating_cases disagree")
        if "operating_cases" not in spec:
            spec = engine
    if "operating_cases" not in spec:
        return {"status": "not_requested", "cases": []}, EvidenceLedger()
    document = _object(spec["operating_cases"], {"cases", "evidence_manifest"}, "operating_cases")
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    ledger = EvidenceLedger()
    if "evidence_manifest" in document:
        manifest = _object(document["evidence_manifest"], {"schema_version", "records"}, "evidence_manifest")
        _rows(manifest.get("records"), "evidence records", 8192)
        ledger = EvidenceLedger.from_manifest(manifest)
    superseded = {record["supersedes"] for record in ledger.to_manifest()["records"] if record["supersedes"]}
    cases = _rows(document.get("cases"), "operating cases", 128)
    if not cases:
        raise ValueError("operating_cases must declare at least one case")
    results = []
    names: set[str] = set()
    for raw in cases:
        row = _object(raw, {
            "name", "max_simultaneous", "loads", "source_limit", "connector_limits", "distribution_limits",
        }, "operating case")
        name = _name(row.get("name"))
        if name in names:
            raise ValueError("duplicate operating case name")
        names.add(name)
        referenced: set[str] = set()

        def evidence(item: dict, subject_ref: str, subject: str) -> Evidence | None:
            evidence_id = item.get("evidence_id")
            if evidence_id is None:
                return None
            if not isinstance(evidence_id, str) or not evidence_id:
                raise ValueError("evidence_id must be a nonempty string or null")
            record = ledger.get(evidence_id)
            if record is None:
                return None
            referenced.add(record.id)
            if record.subject_ref != subject_ref or record.id in superseded:
                return None
            try:
                claim = json.loads(record.claim, object_pairs_hook=_unique_object)
                if not isinstance(claim, dict) or set(claim) != {"value", "unit"}:
                    return None
                # A hash identifies the underlying source; it is not source authentication.
                if not record.source.content_hash:
                    return None
                return Evidence(record.id, record.confidence, record.freshness, subject,
                                claim["value"], claim["unit"], record.kind, record.conflicts)
            except (ValueError, TypeError):
                return None

        loads = []
        for raw_load in _rows(row.get("loads"), "loads", 2048):
            item = _object(raw_load, {"name", "current_ma", "max_concurrent", "evidence_id", "unit"}, "load")
            load_name = _name(item.get("name"))
            loads.append(Load(load_name, item.get("current_ma"), item.get("max_concurrent"),
                              evidence(item, f"param:{name}.current.{load_name}", load_name), item.get("unit", "mA")))

        def limit(raw_limit: Any, category: str) -> CurrentLimit | None:
            if raw_limit is None:
                return None
            item = _object(raw_limit, {"name", "current_ma", "evidence_id", "unit"}, "current limit")
            limit_name = _name(item.get("name"))
            subject = "source" if category == "source" else f"{category}:{limit_name}"
            key = "source" if category == "source" else limit_name
            return CurrentLimit(limit_name, item.get("current_ma"),
                                evidence(item, f"param:{name}.{category}.{key}", subject), item.get("unit", "mA"))

        connectors = _rows(row.get("connector_limits", []), "connector limits", 256)
        distribution = _rows(row.get("distribution_limits", []), "distribution limits", 256)
        if any(item is None for item in connectors + distribution):
            raise ValueError("series boundary lists cannot contain null entries")
        case = OperatingCase(
            name, row.get("max_simultaneous"), tuple(loads), limit(row.get("source_limit"), "source"),
            tuple(limit(item, "connector") for item in connectors),
            tuple(limit(item, "distribution") for item in distribution),
        )
        result = check_operating_case(case)
        results.append({**result.to_dict(), "result_sha256": result.result_hash, "evidence_ids": sorted(referenced)})
    statuses = {row["status"] for row in results}
    return {
        "status": "failed" if "failed" in statuses else "unverified" if "unverified" in statuses else "passed",
        "result_scope": "authored_current_budget_only",
        "input_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "cases": results,
    }, ledger
