"""Regression coverage for the public MCP server boundary."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import pytest

from circuit_weaver.dispatcher import ValidationMessage, ValidationReport
from circuit_weaver.mcp_server import (
    _as_mcp_tool_result,
    _call_without_stdout,
    _discover_projects_tool,
    _generate_artifacts_tool,
    _manufacturing_readiness_tool,
    _parse_port,
    _validate_design_tool,
    main,
)


def test_engine_stdout_is_suppressed(capsys: pytest.CaptureFixture[str]) -> None:
    def noisy_engine() -> str:
        print("engine progress that would corrupt JSON-RPC")
        return "ok"

    assert _call_without_stdout(noisy_engine) == "ok"
    assert capsys.readouterr().out == ""


def test_inline_generation_keeps_sibling_projects_unchanged(tmp_path: Path) -> None:
    import yaml

    from circuit_weaver.project_state import ProjectState, save_project_state

    siblings = [save_project_state(tmp_path / name, ProjectState(name=name)) for name in ("first", "second")]
    before = {path: path.read_bytes() for path in siblings}
    sample = Path(__file__).resolve().parents[1] / "samples/led_power_indicator/led_power_indicator.yaml"
    spec = yaml.safe_load(sample.read_text(encoding="utf-8"))
    output = tmp_path / "new-project"
    for _ in range(2):
        result = json.loads(_generate_artifacts_tool(json.dumps(spec), str(output)))
        assert result["status"] == "ok", result
        assert Path(result["root_schematic"]).is_file()
    state = json.loads((output / ".circuit-weaver/project.json").read_text(encoding="utf-8"))
    assert state["name"] == spec["project"]
    assert state["workflow"]["generate"]["output_dir"] == str(output.resolve())
    assert {path: path.read_bytes() for path in siblings} == before


def test_invalid_json_has_a_stable_error_envelope() -> None:
    result = json.loads(_validate_design_tool("{not-json"))

    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_json"
    assert "line 1" in result["error"]["message"]


@pytest.mark.parametrize("envelope", [
    {"status": "error", "error": {"code": "invalid_json", "message": "Invalid input"}},
    {"ok": False, "error": {"code": "project_not_found", "message": "Missing project"}},
])
def test_mcp_error_result_preserves_structured_and_text_envelopes(envelope) -> None:
    payload = json.dumps(envelope)

    result = _as_mcp_tool_result(payload)

    assert result.isError is True
    assert result.structuredContent == {"result": payload}
    assert json.loads(result.structuredContent["result"]) == envelope
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert result.content[0].text == payload


def test_mcp_error_result_satisfies_sdk_wrapped_string_output_schema() -> None:
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    def json_string_tool() -> str:
        return ""

    metadata = func_metadata(json_string_tool)
    assert metadata.wrap_output is True
    payload = json.dumps({"status": "error", "error": {"code": "invalid_json", "message": "Invalid input"}})
    result = _as_mcp_tool_result(payload)

    assert metadata.convert_result(result) is result
    assert metadata.output_model.model_validate(result.structuredContent).model_dump() == {"result": payload}


@pytest.mark.parametrize("payload", ['{"status": "ok", "value": 1}', "plain text"])
def test_mcp_normal_result_preserves_original_payload(payload) -> None:
    assert _as_mcp_tool_result(payload) == payload


def test_validate_tool_serializes_evidence_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from circuit_weaver import dispatcher

    monkeypatch.setattr(
        dispatcher,
        "validate_design",
        lambda *_args, **_kwargs: ValidationReport(
            profile="standard",
            valid=True,
            categories={
                "electrical": [
                    ValidationMessage("electrical", "rule", "warning", "U1", "finding", evidence_ids=["EV-USER-1"])
                ]
            },
            evidence_manifest="evidence_manifest.json",
        ),
    )

    result = json.loads(_validate_design_tool("{}"))

    assert result["evidence_ids"] == ["EV-USER-1"]
    assert result["evidence_manifest"] == "evidence_manifest.json"
    assert result["detail_level"] == "summary"
    assert result["omitted_fields"] == [
        "metadata.evidence_manifest",
        "metadata.evidence_ids_by_reference",
    ]


def test_validate_summary_is_compact_and_preserves_audit_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from circuit_weaver import dispatcher

    manifest = {
        "schema_version": "circuit-weaver-evidence-manifest/v1",
        "records": [
            {"id": f"EV-{index}", "claim": "evidence details " * 40}
            for index in range(500)
        ],
    }
    references = {f"param:U1.{index}": [f"EV-{index}"] for index in range(500)}
    finding = ValidationMessage(
        category="electrical",
        code="voltage-out-of-range",
        level="warning",
        subject="U1",
        message="Observed voltage exceeds the declared range.",
        suggestion="Check the supply rail.",
        evidence_ids=["EV-1", "EV-2"],
        calculation={"observed": 5.2, "allowed_max": 5.0},
        is_validator_finding=True,
        rule_id="voltage-range",
        observed_value="5.2 V",
        expected_constraint="<= 5.0 V",
        safest_next_action="Verify the source voltage.",
        detection_confidence="high",
        severity="warning",
    )
    report = ValidationReport(
        profile="standard",
        valid=True,
        categories={"electrical": [finding]},
        summary={"electrical": 1},
        metadata={
            "project": "AuditFixture",
            "component_count": 1,
            "evidence_manifest": manifest,
            "evidence_ids_by_reference": references,
        },
        evidence_ids=["EV-1"],
        evidence_manifest="evidence_manifest.json",
    )
    before = copy.deepcopy(report.to_dict())
    monkeypatch.setattr(dispatcher, "validate_design", lambda *_args, **_kwargs: report)

    summary = json.loads(_validate_design_tool("{}"))
    full = json.loads(_validate_design_tool("{}", detail_level="full"))

    assert len(json.dumps(summary, separators=(",", ":")).encode("utf-8")) < 10_000
    assert summary["profile"] == "standard"
    assert summary["valid"] is True
    assert summary["summary"] == {"electrical": 1}
    assert summary["evidence_ids"] == ["EV-1", "EV-2"]
    assert summary["evidence_manifest"] == "evidence_manifest.json"
    assert summary["metadata"] == {"project": "AuditFixture", "component_count": 1}
    assert summary["omitted_fields"] == [
        "metadata.evidence_manifest",
        "metadata.evidence_ids_by_reference",
    ]
    assert summary["categories"]["electrical"] == [finding.__dict__]
    assert full == before
    assert report.to_dict() == before


@pytest.mark.parametrize("detail_level", ["expanded", None, False, []])
def test_validate_tool_rejects_invalid_detail_level(detail_level) -> None:
    result = json.loads(_validate_design_tool("{}", detail_level=detail_level))

    assert result == {
        "status": "error",
        "error": {
            "code": "invalid_detail_level",
            "message": "detail_level must be 'summary', 'full', or 'page'",
        },
    }


def test_generate_reports_skipped_kicad_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from circuit_weaver import dispatcher

    manifest = tmp_path / "artifact_manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    evidence_manifest = tmp_path / "evidence_manifest.json"
    evidence_manifest.write_text("{}\n", encoding="utf-8")
    current_schematic = tmp_path / "unverified-project.kicad_sch"
    current_schematic.write_text("(kicad_sch)\n", encoding="utf-8")
    stale_schematic = tmp_path / "stale.kicad_sch"
    stale_schematic.write_text("(kicad_sch)\n", encoding="utf-8")
    monkeypatch.setattr(
        dispatcher,
        "generate_artifacts",
        lambda *_args, **_kwargs: {
            "project": "unverified-project",
            "root_schematic": str(current_schematic),
            "files": [str(current_schematic), str(manifest)],
            "valid": True,
            "kicad_verified": False,
            "verification_status": "unverified",
            "erc": {
                "status": "skipped",
                "errors": 0,
                "warnings": 0,
                "skip_reason": "KiCad CLI not available",
            },
            "artifact_manifest": str(manifest),
            "evidence_manifest": str(evidence_manifest),
        },
    )

    result = json.loads(_generate_artifacts_tool("{}", str(tmp_path)))

    assert result["status"] == "ok"
    assert result["valid"] is True
    assert result["kicad_verified"] is False
    assert result["verification_status"] == "unverified"
    assert result["erc"]["status"] == "skipped"
    assert result["artifact_manifest"] == str(manifest)
    assert result["evidence_manifest"] == str(evidence_manifest)
    assert result["files"] == [str(current_schematic), str(manifest)]
    assert str(stale_schematic) not in result["files"]


def test_discover_projects_uses_max_depth_and_serializes_dataclass(tmp_path: Path) -> None:
    project_dir = tmp_path / "board"
    project_dir.mkdir()
    (project_dir / "design.yaml").write_text(
        "metadata:\n  project: board\nblocks:\n  - id: U1\n    type: mcu\n",
        encoding="utf-8",
    )

    result = json.loads(_discover_projects_tool(str(tmp_path), depth=1))

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["projects"][0]["name"] == "board"
    assert result["projects"][0]["type"] == "circuit_weaver"
    assert result["projects"][0]["project_type"] == "circuit_weaver"


def test_manufacturing_readiness_tool_reads_exact_artifact(tmp_path: Path) -> None:
    path = tmp_path / "manufacturing_readiness.json"
    payload = {
        "state": "drc_pending",
        "blockers": ["drc_not_run"],
        "evidence_ids": [],
        "next_actions": ["Run DRC."],
        "blocked_reason": None,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = json.loads(_manufacturing_readiness_tool(str(path)))

    assert result == {"status": "ok", **payload}


@pytest.mark.parametrize("raw_port, expected", [("0", 0), ("5000", 5000), ("65535", 65535)])
def test_parse_port_accepts_valid_values(raw_port: str, expected: int) -> None:
    assert _parse_port(raw_port) == expected


@pytest.mark.parametrize("raw_port", ["abc", "-1", "65536"])
def test_parse_port_rejects_invalid_values(raw_port: str) -> None:
    with pytest.raises(ValueError):
        _parse_port(raw_port)


def test_main_reports_bad_port_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CIRCUIT_WEAVER_MCP_PORT", "not-a-port")

    with pytest.raises(SystemExit) as exc_info:
        main([])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert captured.out == ""
    assert "must be an integer" in captured.err
    assert "Traceback" not in captured.err


def test_main_help_does_not_require_optional_mcp_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from circuit_weaver import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "_create_app",
        lambda: pytest.fail("--help must exit before importing the optional MCP runtime"),
    )

    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "Circuit Weaver MCP server" in captured.out
    assert captured.err == ""


def test_stdio_server_handshake_and_tool_call(tmp_path: Path) -> None:
    project_dir = tmp_path / "mcp-project"
    project_dir.mkdir()
    (project_dir / "design.yaml").write_text("metadata:\n  project: mcp-project\nblocks: []\n", encoding="utf-8")

    async def exercise_server() -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        environment = dict(os.environ)
        environment["CIRCUIT_WEAVER_MCP_PORT"] = "0"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "circuit_weaver.mcp_server"],
            env=environment,
        )

        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                required_tools = {
                    "validate_design",
                    "generate_artifacts",
                    "discover_projects",
                    "research_component",
                    "manufacturing_readiness",
                    "runtime_info",
                    "library_access",
                    "start_project",
                    "open_project",
                    "plan_workflow",
                    "run_workflow",
                    "workflow_status",
                    "resume_workflow",
                    "cancel_workflow",
                }
                assert required_tools <= {tool.name for tool in tools.tools}

                response = await session.call_tool(
                    "discover_projects",
                    {"root_dir": str(tmp_path), "depth": 1},
                )
                text_blocks = [block.text for block in response.content if hasattr(block, "text")]
                assert len(text_blocks) == 1
                payload = json.loads(text_blocks[0])
                assert payload["status"] == "ok"
                assert payload["count"] == 1

                import yaml

                sample = Path(__file__).resolve().parents[1] / "src/circuit_weaver/examples/iot_sensor.yaml"
                spec = yaml.safe_load(sample.read_text(encoding="utf-8"))
                validation_response = await session.call_tool(
                    "validate_design",
                    {"spec_json": json.dumps(spec)},
                )
                validation_text = "\n".join(
                    block.text for block in validation_response.content if hasattr(block, "text")
                )
                assert validation_response.isError is False
                assert len(validation_text.encode("utf-8")) < 20_000
                validation_payload = json.loads(validation_text)
                assert validation_payload["valid"] is True
                assert validation_payload["detail_level"] == "summary"
                assert validation_payload["metadata"]["project"] == "IoT_Sensor"
                assert validation_payload["metadata"]["component_count"] == 6
                assert validation_payload["summary"] == {
                    category: len(findings)
                    for category, findings in validation_payload["categories"].items()
                }

                invalid_detail_response = await session.call_tool(
                    "validate_design",
                    {"spec_json": "{}", "detail_level": "expanded"},
                )
                assert invalid_detail_response.isError is True

                invalid_response = await session.call_tool("validate_design", {"spec_json": "{not-json"})
                invalid_text = "\n".join(
                    block.text for block in invalid_response.content if hasattr(block, "text")
                )
                assert invalid_response.isError is True
                assert "invalid_json" in invalid_text

    asyncio.run(asyncio.wait_for(exercise_server(), timeout=30))
