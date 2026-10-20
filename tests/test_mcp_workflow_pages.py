"""The workflow must retrieve every page and reject incomplete MCP evidence."""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from circuit_weaver.mcp_server import _validation_page

_spec = importlib.util.spec_from_file_location(
    "paged_workflow", Path(__file__).resolve().parents[1] / "scripts/validate_app_workflow.py",
)
_workflow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_workflow)


def _report():
    return {
        "valid": False, "profile": "standard", "summary": {"electrical": 37, "structural": 0},
        "categories": {"electrical": [
            {"code": "voltage", "subject": f"U{i}", "message": "Check voltage.",
             "evidence_ids": [f"EV-{i}"], "calculation": {"actual": 5, "maximum": 3.3}}
            for i in range(37)
        ], "structural": []},
        "metadata": {"project": "ManySources", "component_count": 350},
        "evidence_ids": [f"EV-{i:05d}" for i in range(1237)],
        "evidence_manifest": "evidence_manifest.json", "detail_level": "summary",
        "omitted_fields": ["metadata.evidence_manifest", "metadata.evidence_ids_by_reference"],
    }


class _Session:
    def __init__(self, report, tamper=None):
        self.report = report
        self.tamper = tamper
        self.calls = []

    async def call_tool(self, name, arguments):
        assert name == "validate_design"
        self.calls.append(arguments)
        page = json.loads(_validation_page(
            self.report, arguments["collection"], arguments["offset"], arguments["limit"],
            arguments["expected_report_sha256"],
        ))
        if self.tamper:
            self.tamper(page)
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps(page))])


def test_collects_all_findings_and_all_provenance_ids_with_digest_verification():
    report = _report()
    original = copy.deepcopy(report)
    session = _Session(report)
    rebuilt, pages = asyncio.run(_workflow.collect_mcp_validation_pages(session, "{}"))
    assert rebuilt == original == report
    assert len(pages) == 17  # Four finding pages and thirteen provenance-ID pages.
    assert max(len(json.dumps(page["result"]).encode()) for page in pages) <= 16000
    assert all(call["expected_report_sha256"] for call in session.calls[1:])


@pytest.mark.parametrize("mode, message", [
    ("alter_item", "mismatched digest"),
    ("skip_items", "skipped items"),
    ("changed_report", "report changed"),
    ("changed_header", "header changed"),
])
def test_rejects_changed_or_incomplete_validation_pages(mode, message):
    def tamper(page):
        if mode == "alter_item" and page["collection"] == "evidence_ids":
            page["items"][0] = "EV-TAMPERED"
        elif mode == "skip_items":
            page["items"] = []
        elif mode == "changed_report" and page["offset"]:
            page["report_sha256"] = "f" * 64
        elif mode == "changed_header" and page["offset"]:
            page["valid"] = True

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(_workflow.collect_mcp_validation_pages(_Session(_report(), tamper), "{}"))
