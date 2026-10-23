"""MCP server for AI-agent access to Circuit Weaver.

The default transport is stdio for Claude, Codex, and other MCP clients::

    circuit-weaver-mcp

Set ``CIRCUIT_WEAVER_MCP_PORT`` to run the legacy SSE transport on localhost.
Circuit Weaver's engine predates MCP and some paths still write progress to
stdout, so tool calls are isolated to keep the stdio protocol stream clean.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

_logger = logging.getLogger(__name__)
_STDOUT_LOCK = threading.RLock()
_STDOUT_CONTEXT = threading.local()
_STDOUT_INSTALL_LOCK = threading.Lock()
_T = TypeVar("_T")


class _ThreadRoutedStdout:
    """Route each worker thread's tool output to its own capture buffer."""

    def __init__(self, original: Any):
        self._original = original

    def write(self, value: str) -> int:
        destination = getattr(_STDOUT_CONTEXT, "capture", None) or self._original
        return destination.write(value)

    def writelines(self, lines: Any) -> None:
        destination = getattr(_STDOUT_CONTEXT, "capture", None) or self._original
        destination.writelines(lines)

    def flush(self) -> None:
        destination = getattr(_STDOUT_CONTEXT, "capture", None) or self._original
        destination.flush()

    def isatty(self) -> bool:
        return bool(self._original.isatty())

    def fileno(self) -> int:
        return self._original.fileno()

    @property
    def buffer(self) -> Any:
        return self._original.buffer

    @property
    def encoding(self) -> str | None:
        return self._original.encoding

    @property
    def errors(self) -> str | None:
        return self._original.errors

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


def _install_stdout_router() -> None:
    """Install a process-wide stdout proxy once, preserving the MCP transport."""
    if isinstance(sys.stdout, _ThreadRoutedStdout):
        return
    with _STDOUT_INSTALL_LOCK:
        if not isinstance(sys.stdout, _ThreadRoutedStdout):
            sys.stdout = _ThreadRoutedStdout(sys.stdout)  # type: ignore[assignment]


def _capture_tool_stdout(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Suppress prints for one tool call without changing process-global stdout."""
    _install_stdout_router()
    captured = io.StringIO()
    previous = getattr(_STDOUT_CONTEXT, "capture", None)
    _STDOUT_CONTEXT.capture = captured
    try:
        result = func(*args, **kwargs)
    finally:
        if previous is None:
            del _STDOUT_CONTEXT.capture
        else:
            _STDOUT_CONTEXT.capture = previous
    output = captured.getvalue().strip()
    if output:
        _logger.debug("Suppressed engine stdout: %s", output[-2000:])
    return result


def _call_without_stdout(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Serialize heavy engine calls while capturing only their worker output."""
    with _STDOUT_LOCK:
        return _capture_tool_stdout(func, *args, **kwargs)


def _error_json(code: str, message: str) -> str:
    """Return the stable error envelope used by every MCP tool."""
    return json.dumps({"status": "error", "error": {"code": code, "message": message}})


def _as_mcp_tool_result(payload: str) -> Any:
    """Preserve machine-readable JSON while marking MCP error envelopes."""
    try:
        decoded = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return payload
    if isinstance(decoded, dict) and (
        decoded.get("status") == "error" or (decoded.get("ok") is False and isinstance(decoded.get("error"), dict))
    ):
        from mcp.types import CallToolResult, TextContent

        return CallToolResult(isError=True, content=[TextContent(type="text", text=payload)])
    return payload


def _load_spec_json(spec_json: str) -> dict[str, Any]:
    """Parse a JSON object passed to a validate or generate tool."""
    spec = json.loads(spec_json)
    if not isinstance(spec, dict):
        raise ValueError("spec_json must contain a JSON object")
    return spec


def _validate_design_tool(
    spec_json: str,
    detail_level: Literal["summary", "full", "page"] = "summary",
    collection: Literal["findings", "evidence_ids"] = "evidence_ids",
    offset: int = 0,
    limit: int = 100,
    expected_report_sha256: str = "",
) -> str:
    """Validate a Circuit Weaver design spec supplied as JSON."""
    if not isinstance(detail_level, str) or detail_level not in {"summary", "full", "page"}:
        return _error_json("invalid_detail_level", "detail_level must be 'summary', 'full', or 'page'")
    if collection not in ("findings", "evidence_ids"):
        return _error_json("invalid_pagination", "collection must be 'findings' or 'evidence_ids'")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
    ):
        return _error_json("invalid_pagination", "offset must be nonnegative and limit an integer from 1 to 100")
    if not isinstance(expected_report_sha256, str) or (
        expected_report_sha256
        and (len(expected_report_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_report_sha256))
    ):
        return _error_json("invalid_pagination", "expected_report_sha256 must be empty or a lowercase SHA-256 digest")
    if detail_level != "page" and (collection != "evidence_ids" or offset or limit != 100 or expected_report_sha256):
        return _error_json("invalid_pagination", "pagination arguments require detail_level='page'")
    if detail_level == "page" and offset and not expected_report_sha256:
        return _error_json("invalid_pagination", "subsequent pages require expected_report_sha256")

    try:
        spec = _load_spec_json(spec_json)
    except json.JSONDecodeError as exc:
        return _error_json("invalid_json", f"spec_json is not valid JSON at line {exc.lineno}, column {exc.colno}")
    except ValueError as exc:
        return _error_json("invalid_spec", str(exc))

    try:
        from circuit_weaver.dispatcher import validate_design

        report = _call_without_stdout(validate_design, spec)
        if detail_level == "page":
            summary = _report_to_json(report, detail_level="summary")
            payload = json.loads(summary)
            if payload.get("status") == "error":
                return summary
            return _validation_page(payload, collection, offset, limit, expected_report_sha256)
        return _report_to_json(report, detail_level=detail_level)
    except Exception as exc:
        _logger.exception("MCP validate_design failed")
        return _error_json("validation_failed", str(exc) or type(exc).__name__)


def _generate_artifacts_tool(spec_json: str, output_dir: str = "") -> str:
    """Generate KiCad artifacts from a design spec supplied as JSON."""
    try:
        spec = _load_spec_json(spec_json)
    except json.JSONDecodeError as exc:
        return _error_json("invalid_json", f"spec_json is not valid JSON at line {exc.lineno}, column {exc.colno}")
    except ValueError as exc:
        return _error_json("invalid_spec", str(exc))

    try:
        from circuit_weaver.dispatcher import generate_artifacts

        out = Path(output_dir) if output_dir else Path.cwd() / "output"
        out.mkdir(parents=True, exist_ok=True)
        result = _call_without_stdout(generate_artifacts, spec, output_dir=out, export_svg=False, project_root=out)
        return json.dumps(
            {
                "status": "ok",
                "project": result.get("project", ""),
                "files": result.get("files", []),
                "root_schematic": result.get("root_schematic", ""),
                "valid": result.get("valid", False),
                "kicad_verified": result.get("kicad_verified", False),
                "verification_status": result.get("verification_status", "unverified"),
                "erc": result.get("erc"),
                "artifact_manifest": result.get("artifact_manifest", ""),
                "evidence_manifest": result.get("evidence_manifest", ""),
            },
            default=str,
        )
    except Exception as exc:
        _logger.exception("MCP generate_artifacts failed")
        return _error_json("generation_failed", str(exc) or type(exc).__name__)


def _discover_projects_tool(root_dir: str = ".", depth: int = 2) -> str:
    """Discover Circuit Weaver and native KiCad projects."""
    if depth < 0:
        return _error_json("invalid_depth", "depth must be zero or greater")

    try:
        from circuit_weaver.project_discovery import discover_projects

        results = _call_without_stdout(discover_projects, root_dir=Path(root_dir), max_depth=depth)
        projects: list[dict[str, Any]] = []
        for project in results:
            if hasattr(project, "to_dict"):
                data = dict(project.to_dict())
            elif isinstance(project, dict):
                data = dict(project)
            else:
                raise TypeError(f"Unsupported discovery result: {type(project).__name__}")
            data["type"] = data.get("project_type", data.get("type", "unknown"))
            projects.append(data)
        return json.dumps({"status": "ok", "projects": projects, "count": len(projects)}, default=str)
    except Exception as exc:
        _logger.exception("MCP discover_projects failed")
        return _error_json("discovery_failed", str(exc) or type(exc).__name__)


def _research_component_tool(query: str) -> str:
    """Look up a component by MPN, description, or keyword."""
    if not query.strip():
        return _error_json("invalid_query", "query must not be empty")

    try:
        from circuit_weaver.parts_lookup import PartsLookup

        result = _call_without_stdout(PartsLookup().lookup, query)
        if not result:
            return json.dumps({"status": "ok", "found": False, "query": query})
        return json.dumps(
            {
                "status": "ok",
                "found": True,
                "mpn": result.get("mpn", ""),
                "manufacturer": result.get("manufacturer", ""),
                "description": result.get("description", ""),
                "package": result.get("package", ""),
                "stock": result.get("stock", 0),
                "datasheet_url": result.get("datasheet_url", ""),
            }
        )
    except Exception as exc:
        _logger.exception("MCP research_component failed")
        return _error_json("lookup_failed", str(exc) or type(exc).__name__)


def _manufacturing_readiness_tool(readiness_path: str) -> str:
    """Read the canonical readiness artifact without deriving a new state."""

    try:
        from circuit_weaver.manufacturing_readiness import read_manufacturing_readiness

        readiness = read_manufacturing_readiness(readiness_path)
        return json.dumps({"status": "ok", **readiness.to_dict()})
    except Exception as exc:
        _logger.exception("MCP manufacturing_readiness failed")
        return _error_json("readiness_failed", str(exc) or type(exc).__name__)


def _workflow_json(value: Any, *, field: str, expected: type) -> Any:
    """Decode an optional JSON object/array argument without accepting coercions."""
    if not value:
        return expected()

    def reject_constant(constant: str) -> None:
        raise ValueError(f"{field} contains non-standard JSON constant {constant}")

    try:
        decoded = json.loads(value, parse_constant=reject_constant)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(decoded, expected):
        label = "object" if expected is dict else "array"
        raise ValueError(f"{field} must contain a JSON {label}")
    return decoded


def _absolute_workflow_path(value: str, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    return path


def _workflow_tool_impl(
    operation: str,
    target: str = "",
    *,
    spec_json: str = "",
    name: str = "",
    stages_json: str = "",
    options_json: str = "",
    resume: bool = False,
) -> str:
    """Call the shared durable workflow API for MCP clients."""
    try:
        from circuit_weaver import project_workflow

        if operation == "start_project":
            project_dir = _absolute_workflow_path(target, "project_dir")
            spec = _workflow_json(spec_json, field="spec_json", expected=dict) if spec_json else None
            result = project_workflow.start_project(project_dir, spec, name=name or None)
        elif operation == "runtime_info":
            from circuit_weaver.runtime_info import runtime_info

            result = runtime_info()
        else:
            project = _absolute_workflow_path(target, "target")
            stages = _workflow_json(stages_json, field="stages_json", expected=list) if stages_json else None
            options = _workflow_json(options_json, field="options_json", expected=dict) if options_json else None
            if stages is not None and any(not isinstance(stage, str) for stage in stages):
                raise ValueError("stages_json must contain only stage names")
            calls = {
                "open_project": lambda: project_workflow.open_project(project),
                "adopt_project": lambda: project_workflow.adopt_project(project, name=name or None),
                "plan_workflow": lambda: project_workflow.plan_workflow(project, stages=stages, options=options),
                "run_workflow": lambda: project_workflow.run_workflow(
                    project,
                    stages=stages,
                    options=options,
                    resume=resume,
                ),
                "workflow_status": lambda: project_workflow.workflow_status(project),
                "resume_workflow": lambda: project_workflow.resume_workflow(project, options=options),
                "cancel_workflow": lambda: project_workflow.cancel_workflow(project),
            }
            if operation not in calls:
                raise ValueError(f"Unsupported workflow operation: {operation}")
            if operation in {"run_workflow", "resume_workflow"}:
                result = _call_without_stdout(calls[operation])
            else:
                result = calls[operation]()
        return json.dumps(result, default=str)
    except (ValueError, TypeError) as exc:
        return _error_json("invalid_workflow_arguments", str(exc))
    except Exception as exc:
        _logger.exception("MCP %s failed", operation)
        return _error_json("workflow_failed", str(exc) or type(exc).__name__)


def _workflow_tool(
    operation: str,
    target: str = "",
    *,
    spec_json: str = "",
    name: str = "",
    stages_json: str = "",
    options_json: str = "",
    resume: bool = False,
) -> str:
    """Run one lifecycle operation with per-thread stdout capture."""
    return _capture_tool_stdout(
        _workflow_tool_impl,
        operation,
        target,
        spec_json=spec_json,
        name=name,
        stages_json=stages_json,
        options_json=options_json,
        resume=resume,
    )


def _library_bundle_tool(operation: str, project_root: str, document_json: str) -> str:
    """Share the local-only plan/apply contract without relying on server cwd."""
    try:
        from .library_bundle import apply_library_bundle, plan_library_bundle

        root = _absolute_workflow_path(project_root, "project_root")
        document = _workflow_json(document_json, field="document_json", expected=dict)
        if operation not in {"plan", "apply"}:
            raise ValueError("operation must be plan or apply")
        function = plan_library_bundle if operation == "plan" else apply_library_bundle
        return json.dumps(_call_without_stdout(function, root, document))
    except (ValueError, TypeError, OSError) as exc:
        return _error_json("library_bundle_failed", str(exc))


def _create_app() -> Any:
    """Create and return the MCP server application."""
    _install_stdout_router()
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("MCP support is unavailable; install circuit-weaver[mcp]") from exc

    mcp = FastMCP("circuit-weaver", log_level="WARNING")

    @mcp.tool()
    async def validate_design(
        spec_json: str,
        detail_level: Literal["summary", "full", "page"] = "summary",
        collection: Literal["findings", "evidence_ids"] = "evidence_ids",
        offset: int = 0,
        limit: int = 100,
        expected_report_sha256: str = "",
    ) -> Any:
        """Validate a design. Summary preserves every finding and evidence ID; full adds the ledger.

        For large reports use page, collection=findings or evidence_ids, and follow next_offset
        for BOTH collections. Bind subsequent pages with expected_report_sha256 from page one.
        Counts and digests allow verification that every item was retrieved without truncation.
        """
        payload = await asyncio.to_thread(
            _capture_tool_stdout,
            _validate_design_tool,
            spec_json,
            detail_level,
            collection,
            offset,
            limit,
            expected_report_sha256,
        )
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    async def generate_artifacts(spec_json: str, output_dir: str = "") -> Any:
        """Generate KiCad artifacts from a JSON spec into output_dir."""
        payload = await asyncio.to_thread(_capture_tool_stdout, _generate_artifacts_tool, spec_json, output_dir)
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    async def discover_projects(root_dir: str = ".", depth: int = 2) -> Any:
        """Discover projects beneath root_dir, scanning at most depth levels."""
        payload = await asyncio.to_thread(_capture_tool_stdout, _discover_projects_tool, root_dir, depth)
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    async def research_component(query: str) -> Any:
        """Look up a component by MPN, description, or keyword."""
        payload = await asyncio.to_thread(_capture_tool_stdout, _research_component_tool, query)
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    async def manufacturing_readiness(readiness_path: str) -> Any:
        """Read a manufacturing_readiness.json artifact and return its exact state."""
        payload = await asyncio.to_thread(_capture_tool_stdout, _manufacturing_readiness_tool, readiness_path)
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    def runtime_info() -> Any:
        """Report the installed runtime, skill installation state, and MCP launch details."""
        return _as_mcp_tool_result(_workflow_tool("runtime_info"))

    @mcp.tool()
    async def library_access(query: str = "", project_root: str = "") -> Any:
        """Audit local CAD libraries separately from functional and SPICE support; no downloads."""
        from .library_access import audit_library_access

        try:
            root = _absolute_workflow_path(project_root, "project_root") if project_root else None
            result = await asyncio.to_thread(
                _capture_tool_stdout, audit_library_access, query or None, project_root=root
            )
            return _as_mcp_tool_result(json.dumps(result, default=str))
        except (OSError, ValueError) as exc:
            return _as_mcp_tool_result(_error_json("library_access_failed", str(exc)))

    @mcp.tool()
    def start_project(project_dir: str, spec_json: str = "", name: str = "") -> Any:
        """Create and initialize a project at an explicit absolute project_dir."""
        return _as_mcp_tool_result(_workflow_tool("start_project", project_dir, spec_json=spec_json, name=name))

    @mcp.tool()
    def open_project(target: str) -> Any:
        """Open or resume project discovery from an explicit absolute path."""
        return _as_mcp_tool_result(_workflow_tool("open_project", target))

    @mcp.tool()
    async def adopt_project(target: str, name: str = "") -> Any:
        """Initialize tracking for existing sources; preserve their exact contents."""
        payload = await asyncio.to_thread(_workflow_tool, "adopt_project", target, name=name)
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    async def plan_library_bundle(project_root: str, manifest_json: str) -> Any:
        """Dry-run local CAD/model imports with provenance, hashes and conflict checks."""
        payload = await asyncio.to_thread(_library_bundle_tool, "plan", project_root, manifest_json)
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    async def apply_library_bundle(project_root: str, plan_json: str) -> Any:
        """Apply a saved local bundle plan after freshness checks; roll back on failure."""
        payload = await asyncio.to_thread(_library_bundle_tool, "apply", project_root, plan_json)
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    def plan_workflow(target: str, stages_json: str = "", options_json: str = "") -> Any:
        """Plan workflow stages for an explicit absolute project path without executing them."""
        return _as_mcp_tool_result(
            _workflow_tool(
                "plan_workflow",
                target,
                stages_json=stages_json,
                options_json=options_json,
            )
        )

    @mcp.tool()
    async def run_workflow(target: str, stages_json: str = "", options_json: str = "", resume: bool = False) -> Any:
        """Execute selected workflow stages and persist resumable checkpoints."""
        payload = await asyncio.to_thread(
            _workflow_tool,
            "run_workflow",
            target,
            stages_json=stages_json,
            options_json=options_json,
            resume=resume,
        )
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    def workflow_status(target: str) -> Any:
        """Read durable workflow status for an explicit absolute project path."""
        return _as_mcp_tool_result(_workflow_tool("workflow_status", target))

    @mcp.tool()
    async def resume_workflow(target: str, options_json: str = "") -> Any:
        """Execute the verified saved workflow from its first incomplete stage."""
        payload = await asyncio.to_thread(_workflow_tool, "resume_workflow", target, options_json=options_json)
        return _as_mcp_tool_result(payload)

    @mcp.tool()
    def cancel_workflow(target: str) -> Any:
        """Request workflow cancellation at the next stage boundary."""
        return _as_mcp_tool_result(_workflow_tool("cancel_workflow", target))

    return mcp


def _validation_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _validation_page(payload: dict, collection: str, offset: int, limit: int, expected_digest: str) -> str:
    """Page growing collections without discarding provenance or finding fields."""
    report_digest = _validation_digest(payload)
    if expected_digest and expected_digest != report_digest:
        return _error_json("report_changed", "Validation report changed; restart paging from offset zero")
    collections = {
        "evidence_ids": payload.get("evidence_ids", []),
        "findings": [
            {"category": category, "finding": finding}
            for category, findings in payload.get("categories", {}).items()
            for finding in findings
        ],
    }
    items = collections[collection]
    if offset > len(items):
        return _error_json("invalid_pagination", "offset exceeds collection length")
    page = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key not in {"evidence_ids", "categories", "detail_level"}
    }
    page.update(
        detail_level="page",
        report_sha256=report_digest,
        collection=collection,
        category_order=list(payload.get("categories", {})),
        offset=offset,
        limit=limit,
        collections={
            name: {"total": len(values), "sha256": _validation_digest(values)} for name, values in collections.items()
        },
    )
    count = min(limit, len(items) - offset)
    while True:
        page["items"] = items[offset : offset + count]
        page["next_offset"] = offset + count if offset + count < len(items) else None
        serialized = json.dumps(page, default=str)
        if len(serialized.encode("utf-8")) <= 16000:
            return serialized
        if count <= 1:
            return _error_json(
                "page_item_too_large",
                "A validation item or header exceeds the 16000-byte page budget; "
                "use the CLI or full-detail report to inspect it without truncation",
            )
        count = max(1, count // 2)


def _report_to_json(report: Any, detail_level: Literal["summary", "full"] = "full") -> str:
    """Serialize a ValidationReport, optionally omitting bulky embedded ledgers."""
    if hasattr(report, "to_dict"):
        payload = report.to_dict()
    else:
        payload = None
    valid = getattr(report, "valid", None)
    if payload is None and valid is not None:
        summary = getattr(report, "summary", {})
        categories = getattr(report, "categories", {})
        payload = {
            "valid": valid,
            "summary": summary,
            "categories": {
                key: [
                    {
                        "code": getattr(value, "code", ""),
                        "level": getattr(value, "level", ""),
                        "subject": getattr(value, "subject", ""),
                        "message": getattr(value, "message", ""),
                    }
                    for value in values
                ]
                for key, values in (categories or {}).items()
            },
        }
    if payload is None:
        return _error_json("invalid_report", "validation returned an unsupported report format")

    if detail_level == "full":
        return json.dumps(payload, default=str)

    payload = copy.deepcopy(payload)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("evidence_manifest", None)
        metadata.pop("evidence_ids_by_reference", None)
    payload["detail_level"] = "summary"
    payload["omitted_fields"] = [
        "metadata.evidence_manifest",
        "metadata.evidence_ids_by_reference",
    ]
    return json.dumps(payload, default=str)


def _parse_port(raw_port: str) -> int:
    """Parse and validate the optional SSE port."""
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("CIRCUIT_WEAVER_MCP_PORT must be an integer") from exc
    if not 0 <= port <= 65535:
        raise ValueError("CIRCUIT_WEAVER_MCP_PORT must be between 1 and 65535, or 0 for stdio")
    return port


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="circuit-weaver-mcp",
        description="Run the Circuit Weaver MCP server over stdio or local SSE.",
    )
    parser.parse_args(argv)

    try:
        port = _parse_port(os.environ.get("CIRCUIT_WEAVER_MCP_PORT", "0"))
        mcp = _create_app()
    except (RuntimeError, ValueError) as exc:
        print(f"circuit-weaver-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    if port:
        _logger.info("Starting Circuit Weaver MCP server on port %d", port)
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = port
        mcp.run(transport="sse")
    else:
        _logger.info("Starting Circuit Weaver MCP server (stdio)")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
