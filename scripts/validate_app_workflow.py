"""Run a real CLI/optional MCP demo and retain inspectable evidence.

Requires KiCad with pcbnew and Freerouting. Outputs are prototype artifacts;
the manufacturing export gate must still refuse an unapproved delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def exception_message(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", ())
    return "; ".join(exception_message(child) for child in nested) if nested else str(exc)


def verify_routing(payload: dict, board: Path) -> None:
    """Accept only native DRC evidence bound to the exact final board bytes."""
    drc = payload.get("drc", {})
    require(payload.get("routing_complete") is True, "Routing is incomplete")
    require(payload.get("output_kind") == "routed_kicad_board", "Router returned no native board")
    require(drc.get("status") == "ok" and drc.get("passed") is True, "Native DRC did not pass")
    require(drc.get("blocker_count") == 0, "Native DRC has blockers")
    require(Path(drc.get("board", "")).resolve() == board.resolve(), "DRC refers to a different board")
    require(drc.get("board_sha256") == digest(board), "DRC does not match final board bytes")


class Demo:
    def __init__(self, output: Path, timeout: int, *, installed: bool = False):
        self.output = output
        self.timeout = timeout
        self.steps: list[dict] = []
        self.cwd = output if installed else REPO
        self.env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO / "src")}
        if installed:
            self.env.pop("PYTHONPATH", None)

    def command(self, name: str, arguments: list[str], *, json_output=True, exit_code=0) -> dict:
        command = [sys.executable, "-m", "circuit_weaver", *map(str, arguments)]
        return self.external(name, command, json_output=json_output, exit_code=exit_code)

    def external(self, name: str, command: list[str], *, json_output=False, exit_code=0) -> dict:
        print(f"[{name}] starting", flush=True)
        step = {"name": name, "command": command, "passed": False}
        self.steps.append(step)
        started = time.monotonic()
        try:
            process = subprocess.run(command, cwd=self.cwd, env=self.env, capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=self.timeout)
            step.update(exit_code=process.returncode, seconds=round(time.monotonic() - started, 2))
            (self.output / f"{name}.stdout.log").write_text(process.stdout, encoding="utf-8")
            (self.output / f"{name}.stderr.log").write_text(process.stderr, encoding="utf-8")
            expected_codes = {exit_code} if isinstance(exit_code, int) else set(exit_code)
            require(process.returncode in expected_codes,
                    f"{name}: exit {process.returncode}, expected {exit_code}; see stderr/stdout logs")
            payload = json.loads(process.stdout) if json_output else {}
            step["passed"] = True
            print(f"[{name}] passed ({step['seconds']}s)", flush=True)
            return payload
        except Exception as exc:
            step["error"] = str(exc)
            raise

    def report(self, summary: dict) -> None:
        summary["steps"] = self.steps
        (self.output / "evidence.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        rows = "".join(
            f"<tr><td>{html.escape(s['name'])}</td><td>{'PASS' if s['passed'] else 'FAIL'}</td>"
            f"<td>{s.get('seconds', '')}</td><td><a href='{s['name']}.stdout.log'>output</a> "
            f"<a href='{s['name']}.stderr.log'>diagnostics</a></td></tr>" for s in self.steps
        )
        previews = "".join(
            f"<figure><figcaption>{html.escape(p.name)}</figcaption>"
            f"<object type='image/svg+xml' data='{p.relative_to(self.output).as_posix()}'></object></figure>"
            for p in (self.output / "board-preview.svg",) if p.is_file()
        )
        schematic = "".join(
            f"<li><a href='{p.relative_to(self.output).as_posix()}'>{html.escape(p.name)}</a></li>"
            for p in sorted((self.output / "generated").rglob("*.svg"))
        )
        page = f"""<!doctype html><meta charset="utf-8"><title>Circuit Weaver workflow demo</title>
<style>body{{font:16px system-ui;background:#101827;color:#e3ecf7;margin:32px auto;max-width:1100px}}
a{{color:#79d3ff}}h1{{font-size:30px}}.badge{{color:#8ce8b0}}table{{width:100%;border-collapse:collapse}}
td,th{{padding:12px;border-bottom:1px solid #33445c;text-align:left}}object{{width:100%;height:420px;background:white}}
figure{{margin:24px 0}}code{{overflow-wrap:anywhere}}p{{line-height:1.6}}</style>
<h1>Circuit Weaver · automated workflow</h1><p class="badge">{html.escape(summary['status'])}</p>
<p>Real KiCad schematic export, library footprints, placement, Freerouting, native DRC and delivery gates.
Manufacturing authorization remains <strong>not ready</strong>: identity evidence and placement approval
have not been supplied. A blocked manufacturing export is an expected successful gate check.</p>
<p>Source revision: <code>{html.escape(summary.get('revision', 'unknown'))}</code>.
<a href="evidence.json">Machine-readable evidence</a> · <a href="routed.kicad_pcb">Routed PCB</a> ·
<a href="generated/placement_editor.html">Placement review</a> ·
<a href="generated/confidence_report.html">Confidence report</a> ·
<a href="delivery/delivery_manifest.json">Delivery manifest</a> ·
<a href="prototype-positions.csv">Prototype positions (review only)</a></p>
<table><tr><th>Step</th><th>Check result</th><th>Seconds</th><th>Evidence</th></tr>{rows}</table>
{previews}<h2>Schematic sheets</h2><ul>{schematic}</ul>
<p>{html.escape(summary.get('error', ''))}</p>"""
        (self.output / "index.html").write_text(page, encoding="utf-8")


async def mcp_smoke(spec: Path, output: Path, env: dict) -> dict:
    """Exercise the real JSON-RPC stdio server, including error propagation."""
    import yaml
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    transcript = []
    parameters = StdioServerParameters(command=sys.executable,
                                      args=["-m", "circuit_weaver.mcp_server"], cwd=str(output), env=env)
    with (output / "mcp.stderr.log").open("w", encoding="utf-8") as errors:
        async with stdio_client(parameters, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                inventory = await session.list_tools()
                names = sorted(tool.name for tool in inventory.tools)
                require(set(names) == {"generate_artifacts", "validate_design", "discover_projects",
                                       "research_component", "manufacturing_readiness"}, "MCP inventory mismatch")
                spec_json = json.dumps(yaml.safe_load(spec.read_text(encoding="utf-8")))
                for name, arguments in (
                    ("validate_design", {"spec_json": spec_json}),
                    ("generate_artifacts", {"spec_json": spec_json, "output_dir": str(output / "mcp-generated")}),
                    ("discover_projects", {"root_dir": str(output), "depth": 2}),
                ):
                    response = await session.call_tool(name, arguments)
                    require(not response.isError, f"MCP {name} failed: {response.content}")
                    payload = json.loads("\n".join(b.text for b in response.content if hasattr(b, "text")))
                    transcript.append({"tool": name, "result": payload})
                    if name in {"validate_design", "generate_artifacts"}:
                        require(payload.get("valid") is True, f"MCP {name} returned invalid design")
                    if name == "generate_artifacts":
                        require(payload.get("kicad_verified") is True, "MCP generation skipped native KiCad")
                bad = await session.call_tool("validate_design", {"spec_json": "{invalid"})
                require(bad.isError is True, "MCP invalid input did not set isError")
                research = await session.call_tool("research_component", {"query": ""})
                require(research.isError is True, "MCP empty research query did not set isError")
                readiness = await session.call_tool("manufacturing_readiness", {
                    "readiness_path": str(output / "missing-readiness.json")
                })
                require(readiness.isError is True, "MCP missing readiness was treated as manufacturing approval")
    result = {"tools": names, "calls": transcript, "invalid_input_is_error": True,
              "empty_research_is_error": True, "missing_readiness_is_error": True}
    (output / "mcp-transcript.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=REPO / "samples/led_power_indicator/led_power_indicator.yaml")
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory; existing output is refused")
    parser.add_argument("--iterations", type=int, default=1500)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--route-timeout", type=int, default=180)
    parser.add_argument("--clearance", type=float, default=0.127,
                        help="Prototype default clearance in mm (0.127 accommodates the sample USB connector)")
    parser.add_argument("--mcp-smoke", action="store_true")
    parser.add_argument("--installed", action="store_true",
                        help="Test this Python's installed package, not checkout src/")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_spec = args.spec.resolve()
    spec = output / "design.yaml"
    demo = Demo(output, max(300, args.route_timeout + 120), installed=args.installed)
    summary = {"status": "FAILED", "spec": str(source_spec), "fabrication_ready": False}
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True)
        summary["revision"] = revision.stdout.strip()
        changes = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                                 capture_output=True, text=True, check=True)
        summary["source_dirty"] = bool(changes.stdout.strip())
        package_probe = subprocess.run(
            [sys.executable, "-c", "import circuit_weaver; print(circuit_weaver.__file__)"],
            cwd=demo.cwd, env=demo.env, capture_output=True, text=True, check=True,
        )
        package_root = Path(package_probe.stdout.strip()).parent
        require(not args.installed or not package_root.resolve().is_relative_to(REPO / "src"),
                "Installed-mode probe imported checkout source")
        summary["package_path"] = str(package_root)
        summary["package_mode"] = "installed" if args.installed else "source"
        source_files = [p for p in package_root.rglob("*")
                        if p.suffix in {".py", ".json"}]
        package_hash = hashlib.sha256()
        for path in sorted(source_files):
            package_hash.update(f"{path.relative_to(package_root).as_posix()}:{digest(path)}\n".encode())
        summary["package_source_sha256"] = package_hash.hexdigest()
        summary["runner_sha256"] = digest(Path(__file__))
        original = digest(source_spec)
        shutil.copy2(source_spec, spec)
        demo.command("doctor", ["doctor"], json_output=False)
        valid = demo.command("validate", ["validate", str(spec)])
        require(valid.get("valid") is True, "Static design validation failed")
        generated = demo.command("generate", ["generate", str(spec), "--require-kicad",
                                              "-o", str(output / "generated")])
        require(generated.get("kicad_verified") is True, "Generation skipped native KiCad verification")
        root = Path(generated["root_schematic"])
        demo.command("confidence", ["confidence", str(spec), "--run-sims", "--json",
                                    "-o", str(output / "generated/confidence_report.html")])
        built = demo.command("build", ["build-pcb", str(root), "-o", str(output / "unplaced.kicad_pcb"),
                                      "--clearance", str(args.clearance)])
        require(built.get("fabrication_ready") is False, "Prototype board incorrectly claims fabrication readiness")
        summary["prototype_clearance_mm"] = args.clearance
        demo.command("place", ["place-pcb", str(output / "unplaced.kicad_pcb"), "-o", str(output / "placed.kicad_pcb"),
                               "--iterations", str(args.iterations), "--seed", "7"])
        routed = output / "routed.kicad_pcb"
        routing = demo.command("route", ["autoroute", str(output / "placed.kicad_pcb"), "--routed-board", str(routed),
                                         "--attempts", str(args.attempts), "--timeout", str(args.route_timeout),
                                         "--max-passes", "20", "--optimization-threads", "0"])
        verify_routing(routing, routed)
        summary["drc"] = routing["drc"]
        delivery = demo.command("assembly-gate", ["export-jlcpcb", str(spec), "--pcb", str(routed),
                                            "-o", str(output / "delivery")], exit_code=(0, 1))
        require(delivery.get("status") == "blocked", "Prototype assembly export was unexpectedly authorized")
        require(delivery.get("fabrication_ready") is False, "Assembly export overclaimed fabrication readiness")
        reasons = delivery.get("blocked_reasons", [])
        require(bool(reasons) and all("identity handoff bundle" in r or "no LCSC part number" in r
                                     for r in reasons), f"Unexpected assembly blocker: {reasons}")
        require(not delivery.get("cpl") and not list((output / "delivery").glob("cpl*.csv")),
                "Unapproved assembly export published a CPL")
        summary["assembly_delivery"] = delivery
        refused = demo.command("manufacturing-gate", ["export-gerbers", str(routed),
                                                     "-o", str(output / "manufacturing")], exit_code=1)
        require(refused.get("status") == "blocked", "Unapproved manufacturing export was not blocked")
        require(not (output / "manufacturing").exists(), "Blocked export created a manufacturing directory")
        status = demo.command("status", ["status", str(output / "generated"), "--json"])
        resumed = demo.command("resume", ["resume", str(output / "generated"), "--json"])
        require(status.get("has_manifest") is True and status.get("validation", {}).get("passed") is True,
                "Generation project state was not persisted")
        require(bool(status.get("project_id")) and resumed.get("project_id") == status["project_id"],
                "Resume did not restore the generated project")
        cli = shutil.which("kicad-cli") or next(
            (str(p) for version in ("10.0", "9.0", "8.0")
             if (p := Path(f"C:/Program Files/KiCad/{version}/bin/kicad-cli.exe")).is_file()), None
        )
        require(cli is not None, "KiCad CLI disappeared")
        demo.external("prototype-positions", [str(cli), "pcb", "export", "pos", "--format", "csv",
                                               "--units", "mm", "-o", str(output / "prototype-positions.csv"),
                                               str(routed)])
        demo.external("board-preview", [str(cli), "pcb", "export", "svg", "--mode-single",
                                        "--exclude-drawing-sheet", "--page-size-mode", "2",
                                        "--layers", "F.Cu,B.Cu,F.SilkS,Edge.Cuts",
                                        "-o", str(output / "board-preview.svg"), str(routed)])
        preview = output / "board-preview.svg"
        require(preview.is_file() and "<svg" in preview.read_text(encoding="utf-8"),
                "KiCad board preview SVG was not produced")
        if args.mcp_smoke:
            step = {"name": "mcp", "passed": False}
            demo.steps.append(step)
            started = time.monotonic()
            result = asyncio.run(asyncio.wait_for(mcp_smoke(spec, output, demo.env), timeout=120))
            step.update(passed=True, seconds=round(time.monotonic() - started, 2))
            (output / "mcp.stdout.log").write_text(json.dumps(result, indent=2), encoding="utf-8")
        require(digest(source_spec) == original, "Workflow modified the input design")
        verify_routing(routing, routed)
        summary.update(status="PASS — prototype workflow validated; manufacturing export correctly blocked",
                       source_unchanged=True, board_sha256=digest(routed))
    except Exception as exc:
        summary["error"] = exception_message(exc)
        print(f"FAILED: {summary['error']}", file=sys.stderr)
    finally:
        demo.report(summary)
    print(f"Evidence: {output / 'index.html'}", flush=True)
    return 0 if summary["status"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
