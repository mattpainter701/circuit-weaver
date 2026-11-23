from __future__ import annotations

import copy
import json

import pytest

from circuit_weaver import dispatcher, mcp_server


class _Report:
    def __init__(self, payload: dict):
        self.payload = payload
        self.valid = payload.get("valid")

    def to_dict(self) -> dict:
        return copy.deepcopy(self.payload)


def _payload(*, findings: int = 0, evidence_ids: int = 0) -> dict:
    rows = [
        {
            "code": f"CW-TEST-{index:05d}",
            "level": "warning",
            "subject": f"U{index + 1}",
            "message": f"Synthetic finding {index}: inspect this component and preserve its evidence.",
            "evidence_ids": [f"EV-TEST-{index:05d}"],
        }
        for index in range(findings)
    ]
    return {
        "profile": "standard",
        "valid": True,
        "categories": {"electrical": rows, "implementation": []},
        "summary": {"electrical": findings, "implementation": 0},
        "metadata": {
            "project": "synthetic",
            "evidence_manifest": {"items": ["large-ledger-placeholder"]},
            "evidence_ids_by_reference": {"U1": ["EV-TEST-00000"]} if evidence_ids else {},
        },
        "evidence_ids": [f"EV-SOURCE-{index:05d}" for index in range(evidence_ids)],
    }


def _install_report(monkeypatch: pytest.MonkeyPatch, report: _Report) -> None:
    monkeypatch.setattr(dispatcher, "validate_design", lambda _spec: report)


def _call(*, collection: str, offset: int = 0, limit: int = 100,
          expected_report_sha256: str = "") -> dict:
    result = mcp_server._validate_design_tool(
        "{}", detail_level="page", collection=collection, offset=offset,
        limit=limit, expected_report_sha256=expected_report_sha256,
    )
    return json.loads(result)


def _reassemble(monkeypatch: pytest.MonkeyPatch, collection: str, report: _Report) -> tuple[list, dict]:
    _install_report(monkeypatch, report)
    first = _call(collection=collection, limit=73)
    assert first["detail_level"] == "page"
    items = list(first["items"])
    offset = first["next_offset"]
    while offset is not None:
        page = _call(
            collection=collection,
            offset=offset,
            limit=73,
            expected_report_sha256=first["report_sha256"],
        )
        assert page["report_sha256"] == first["report_sha256"]
        items.extend(page["items"])
        offset = page["next_offset"]
    return items, first


def test_pages_reassemble_all_large_findings_and_evidence_ids_losslessly(monkeypatch) -> None:
    report = _Report(_payload(findings=1205, evidence_ids=1379))
    findings, finding_header = _reassemble(monkeypatch, "findings", report)
    evidence_ids, evidence_header = _reassemble(monkeypatch, "evidence_ids", report)

    expected_findings = [
        {"category": category, "finding": finding}
        for category, rows in report.payload["categories"].items()
        for finding in rows
    ]
    assert findings == expected_findings
    assert evidence_ids == report.payload["evidence_ids"]
    assert len(set(evidence_ids)) == 1379
    assert finding_header["collections"]["findings"]["total"] == 1205
    assert evidence_header["collections"]["evidence_ids"]["total"] == 1379
    assert finding_header["collections"]["findings"]["sha256"] == mcp_server._validation_digest(findings)
    assert evidence_header["collections"]["evidence_ids"]["sha256"] == mcp_server._validation_digest(evidence_ids)
    assert finding_header["report_sha256"] == evidence_header["report_sha256"]
    assert finding_header["category_order"] == ["electrical", "implementation"]
    assert finding_header["omitted_fields"] == [
        "metadata.evidence_manifest", "metadata.evidence_ids_by_reference",
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"collection": "unknown"}, "collection must be"),
        ({"collection": "findings", "offset": -1}, "offset must be nonnegative"),
        ({"collection": "findings", "offset": True}, "offset must be nonnegative"),
        ({"collection": "findings", "offset": 1.5}, "offset must be nonnegative"),
        ({"collection": "findings", "limit": 0}, "offset must be nonnegative"),
        ({"collection": "findings", "limit": 101}, "offset must be nonnegative"),
        ({"collection": "findings", "limit": False}, "offset must be nonnegative"),
        ({"collection": "findings", "expected_report_sha256": "A" * 64}, "lowercase SHA-256"),
    ],
)
def test_rejects_invalid_page_arguments(monkeypatch, kwargs: dict, message: str) -> None:
    _install_report(monkeypatch, _Report(_payload(findings=120, evidence_ids=120)))
    result = mcp_server._validate_design_tool("{}", detail_level="page", **kwargs)
    assert json.loads(result)["error"]["message"].find(message) >= 0


def test_subsequent_page_requires_report_digest(monkeypatch) -> None:
    _install_report(monkeypatch, _Report(_payload(findings=120, evidence_ids=120)))
    result = _call(collection="findings", offset=1, limit=20)
    assert result["error"]["code"] == "invalid_pagination"
    assert "require expected_report_sha256" in result["error"]["message"]


def test_rejects_offset_past_collection_length_when_digest_is_bound(monkeypatch) -> None:
    report = _Report(_payload(findings=120, evidence_ids=120))
    _install_report(monkeypatch, report)
    first = _call(collection="findings", limit=20)
    result = _call(
        collection="findings", offset=121, limit=20,
        expected_report_sha256=first["report_sha256"],
    )
    assert result["error"]["code"] == "invalid_pagination"
    assert "offset exceeds collection length" in result["error"]["message"]

def test_non_page_detail_rejects_pagination_arguments(monkeypatch) -> None:
    _install_report(monkeypatch, _Report(_payload()))
    result = mcp_server._validate_design_tool("{}", detail_level="summary", offset=1)
    assert json.loads(result)["error"]["code"] == "invalid_pagination"


def test_rejects_report_mutation_between_pages(monkeypatch) -> None:
    report = _Report(_payload(findings=120, evidence_ids=120))
    _install_report(monkeypatch, report)
    first = _call(collection="findings", limit=20)
    report.payload["categories"]["electrical"][0]["message"] = "changed between page requests"
    result = _call(
        collection="findings", offset=20, limit=20,
        expected_report_sha256=first["report_sha256"],
    )
    assert result["error"]["code"] == "report_changed"
    assert "restart paging" in result["error"]["message"]


def test_validation_exception_is_returned_as_tool_error(monkeypatch) -> None:
    def fail(_spec):
        raise RuntimeError("synthetic validation failure")

    monkeypatch.setattr(dispatcher, "validate_design", fail)
    result = json.loads(mcp_server._validate_design_tool("{}", detail_level="page"))
    assert result["error"]["code"] == "validation_failed"
    assert result["error"]["message"] == "synthetic validation failure"


def test_public_page_payload_for_187_part_shape_stays_under_20kb(monkeypatch) -> None:
    # Mirrors the public 187-part report shape: 558 provenance IDs and two
    # implementation findings, while retaining the small report header.
    report = _Report(_payload(findings=2, evidence_ids=558))
    _install_report(monkeypatch, report)
    for collection in ("findings", "evidence_ids"):
        raw = mcp_server._validate_design_tool(
            "{}", detail_level="page", collection=collection, limit=100,
        )
        assert len(raw.encode("utf-8")) < 20_000
        page = json.loads(raw)
        assert page["collections"][collection]["total"] == (2 if collection == "findings" else 558)


def test_large_findings_page_reduces_item_count_without_losing_rows(monkeypatch) -> None:
    payload = _payload(findings=240, evidence_ids=1100)
    for row in payload["categories"]["electrical"]:
        row["message"] += " " + ("Detailed diagnostic context. " * 12)
    report = _Report(payload)
    _install_report(monkeypatch, report)

    first_raw = mcp_server._validate_design_tool(
        "{}", detail_level="page", collection="findings", limit=100,
    )
    assert len(first_raw.encode("utf-8")) <= 16_384
    first = json.loads(first_raw)
    assert 0 < len(first["items"]) < 100
    assert first["next_offset"] == len(first["items"])

    reassembled = list(first["items"])
    offset = first["next_offset"]
    while offset is not None:
        page = _call(
            collection="findings", offset=offset, limit=100,
            expected_report_sha256=first["report_sha256"],
        )
        reassembled.extend(page["items"])
        offset = page["next_offset"]
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= 16_384
    expected = [
        {"category": category, "finding": finding}
        for category, rows in report.payload["categories"].items()
        for finding in rows
    ]
    assert reassembled == expected
    assert len(reassembled) == 240


def test_single_oversized_finding_returns_explicit_error(monkeypatch) -> None:
    payload = _payload(findings=1, evidence_ids=1)
    payload["categories"]["electrical"][0]["message"] = "oversized " + ("x" * 20_000)
    _install_report(monkeypatch, _Report(payload))

    result = _call(collection="findings", limit=100)
    assert result["error"]["code"] == "page_item_too_large"
    assert "full-detail report" in result["error"]["message"].lower()
