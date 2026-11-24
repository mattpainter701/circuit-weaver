import asyncio
import json
import subprocess
import sys

import klayout.db as kdb
from mcp.shared.memory import create_connected_server_and_client_session

from circuit_weaver import mcp_server


def test_silicon_cli_reports_incomplete_handoff(tmp_path):
    manifest = tmp_path / "handoff.json"
    manifest.write_text(json.dumps({
        "schema": 1, "provider": None, "run": None, "declared_top": "chip", "roles": [],
    }))
    output = tmp_path / "review.zip"
    result = subprocess.run(
        [sys.executable, "-m", "circuit_weaver", "silicon", "handoff", str(manifest), "--bundle", str(output)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 3, result.stderr + result.stdout
    assert json.loads(result.stdout)["status"] == "incomplete"
    assert not output.exists()


def test_mcp_silicon_can_render_and_requires_absolute_paths(tmp_path):
    source, image = tmp_path / "demo.gds", tmp_path / "demo.png"
    layout = kdb.Layout()
    top = layout.create_cell("demo")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 1000, 500))
    layout.write(str(source))

    async def exercise():
        async with create_connected_server_and_client_session(mcp_server._create_app()) as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {"silicon_preview", "silicon_handoff"} <= tools
            invalid = await session.call_tool("silicon_handoff", {"manifest": "relative.json"})
            assert invalid.isError
            preview = await session.call_tool("silicon_preview", {"layout": str(source), "output": str(image)})
            assert not preview.isError, preview.content
            assert json.loads(preview.content[0].text)["top"] == "demo"

    asyncio.run(exercise())
    assert image.read_bytes().startswith(b"\x89PNG")
