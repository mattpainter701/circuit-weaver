"""MCP server for AI-agent access to Circuit Weaver.

The default transport is stdio for Claude, Codex, and other MCP clients::

    circuit-weaver-mcp

Set ``CIRCUIT_WEAVER_MCP_PORT`` to run the legacy SSE transport on localhost.
Circuit Weaver's engine predates MCP and some paths still write progress to
stdout, so tool calls are isolated to keep the stdio protocol stream clean.
"""

from __future__ import annotations

import contextlib
import copy
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
_T = TypeVar("_T")


def _call_without_stdout(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Call an engine function without allowing prints onto MCP stdout."""
    captured = io.StringIO()
    with _STDOUT_LOCK, contextlib.redirect_stdout(captured):
        result = func(*args, **kwargs)
    output = captured.getvalue().strip()
    if output:
        _logger.debug("Suppressed engine stdout: %s", output[-2000:])
    return result


def _error_json(code: str, message: str) -> str:
    """Return the stable error envelope used by every MCP tool."""
    return json.dumps({"status": "error", "error": {"code": code, "message": message}})


def _as_mcp_tool_result(payload: str) -> str:
    """Raise on a low-level error envelope so MCP sets ``isError``."""
    try:
        decoded = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return payload
    if isinstance(decoded, dict) and decoded.get("status") == "error":
        error = decoded.get("error", {})
        code = error.get("code", "tool_error") if isinstance(error, dict) else "tool_error"
        message = error.get("message", "Tool call failed") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"{code}: {message}")
    return payload


def _load_spec_json(spec_json: str) -> dict[str, Any]:
    """Parse a JSON object passed to a validate or generate tool."""
    spec = json.loads(spec_json)
    if not isinstance(spec, dict):
        raise ValueError("spec_json must contain a JSON object")
    return spec


def _validate_design_tool(spec_json: str, detail_level: Literal["summary", "full"] = "summary") -> str:
    """Validate a Circuit Weaver design spec supplied as JSON."""
    if not isinstance(detail_level, str) or detail_level not in {"summary", "full"}:
        return _error_json("invalid_detail_level", "detail_level must be 'summary' or 'full'")

    try:
        spec = _load_spec_json(spec_json)
    except json.JSONDecodeError as exc:
        return _error_json("invalid_json", f"spec_json is not valid JSON at line {exc.lineno}, column {exc.colno}")
    except ValueError as exc:
        return _error_json("invalid_spec", str(exc))

    try:
        from circuit_weaver.dispatcher import validate_design

        report = _call_without_stdout(validate_design, spec)
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


def _create_app() -> Any:
    """Create and return the MCP server application."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("MCP support is unavailable; install circuit-weaver[mcp]") from exc

    mcp = FastMCP("circuit-weaver", log_level="WARNING")

    @mcp.tool()
    def validate_design(spec_json: str, detail_level: Literal["summary", "full"] = "summary") -> str:
        """Validate a design spec; use full detail to include embedded evidence metadata."""
        return _as_mcp_tool_result(_validate_design_tool(spec_json, detail_level))

    @mcp.tool()
    def generate_artifacts(spec_json: str, output_dir: str = "") -> str:
        """Generate KiCad artifacts from a JSON spec into output_dir."""
        return _as_mcp_tool_result(_generate_artifacts_tool(spec_json, output_dir))

    @mcp.tool()
    def discover_projects(root_dir: str = ".", depth: int = 2) -> str:
        """Discover projects beneath root_dir, scanning at most depth levels."""
        return _as_mcp_tool_result(_discover_projects_tool(root_dir, depth))

    @mcp.tool()
    def research_component(query: str) -> str:
        """Look up a component by MPN, description, or keyword."""
        return _as_mcp_tool_result(_research_component_tool(query))

    @mcp.tool()
    def manufacturing_readiness(readiness_path: str) -> str:
        """Read a manufacturing_readiness.json artifact and return its exact state."""

        return _as_mcp_tool_result(_manufacturing_readiness_tool(readiness_path))

    return mcp


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
