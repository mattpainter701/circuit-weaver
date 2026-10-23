"""Thin terminal adapters for the shared project workflow API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


def json_options(value: str) -> dict:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"Invalid options JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise argparse.ArgumentTypeError("Options must be a JSON object")
    return data


def add_parsers(subparsers) -> None:
    start = subparsers.add_parser("start", help="Create a draft project without replacing existing files")
    start.add_argument("project", nargs="?", default=".")
    start.add_argument("--spec", help="Existing YAML/JSON design to copy into the new project")
    start.add_argument("--name")
    start.add_argument("--json", dest="json_output", action="store_true")
    opening = subparsers.add_parser("open", help="Discover or open a project from a circuit directory")
    opening.add_argument("project", nargs="?", default=".")
    opening.add_argument("--json", dest="json_output", action="store_true")
    adopt = subparsers.add_parser("adopt", help="Track an existing circuit without replacing source files")
    adopt.add_argument("project", nargs="?", default=".")
    adopt.add_argument("--name")
    adopt.add_argument("--json", dest="json_output", action="store_true")
    runtime = subparsers.add_parser("runtime-info", help="Identify the exact CLI, skill and MCP runtime")
    runtime.add_argument("--json", dest="json_output", action="store_true")
    runtime.add_argument("--audit-external", action="store_true")
    libraries = subparsers.add_parser("library-access", help="Audit accessible CAD assets and functional support")
    libraries.add_argument("query", nargs="?", default="")
    libraries.add_argument("--project-root", default=".")
    libraries.add_argument("--json", dest="json_output", action="store_true")
    bundle = subparsers.add_parser("library-bundle", help="Plan or apply a local project library bundle")
    bundle_actions = bundle.add_subparsers(dest="bundle_action", required=True)
    for name in ("plan", "apply"):
        action = bundle_actions.add_parser(name)
        action.add_argument("document", help="Manifest JSON for plan; saved plan JSON for apply")
        action.add_argument("--project-root", default=".")
        action.add_argument("--json", dest="json_output", action="store_true")
    workflow = subparsers.add_parser("workflow", help="Plan, execute and resume checkpointed prototype work")
    actions = workflow.add_subparsers(dest="workflow_action", required=True)
    for name in ("plan", "run", "status", "resume", "cancel"):
        action = actions.add_parser(name)
        action.add_argument("project", nargs="?", default=".")
        action.add_argument("--json", dest="json_output", action="store_true")
        if name in {"plan", "run", "resume"}:
            action.add_argument("--options-json", type=json_options, default=None)
        if name in {"plan", "run"}:
            action.add_argument(
                "--stages", nargs="+", choices=("validate", "generate", "build", "place", "route", "preview")
            )


def dispatch(args) -> dict[str, Any] | None:
    from . import project_workflow as workflow

    if args.command == "library-bundle":
        from .library_bundle import apply_library_bundle, plan_library_bundle

        document = json.loads(Path(args.document).read_text(encoding="utf-8-sig"))
        operation = plan_library_bundle if args.bundle_action == "plan" else apply_library_bundle
        result = operation(Path(args.project_root).resolve(), document)
        return result
    if args.command == "library-access":
        from .library_access import audit_library_access

        return audit_library_access(args.query or None, project_root=Path(args.project_root).resolve())
    if args.command == "runtime-info":
        from .runtime_info import runtime_info

        return runtime_info(audit_external=args.audit_external)
    if args.command == "start":
        spec = None
        if args.spec:
            import yaml

            spec = yaml.safe_load(Path(args.spec).read_text(encoding="utf-8-sig"))
            if not isinstance(spec, dict):
                raise ValueError("Design spec must be a YAML/JSON object")
        return workflow.start_project(args.project, spec, name=args.name)
    if args.command == "open":
        return workflow.open_project(args.project)
    if args.command == "adopt":
        return workflow.adopt_project(args.project, name=args.name)
    if args.command == "resume" and getattr(args, "execute", False):
        return workflow.resume_workflow(args.project, options=args.options_json)
    if args.command != "workflow":
        return None
    options = getattr(args, "options_json", None)
    if args.workflow_action == "plan":
        return workflow.plan_workflow(args.project, stages=args.stages, options=options)
    if args.workflow_action == "run":
        return workflow.run_workflow(args.project, stages=args.stages, options=options)
    if args.workflow_action == "resume":
        return workflow.resume_workflow(args.project, options=options)
    if args.workflow_action == "cancel":
        return workflow.cancel_workflow(args.project)
    return workflow.workflow_status(args.project)


def welcome() -> int:
    """List the current workspace without running expensive work implicitly."""
    from . import __version__
    from .project_discovery import discover_projects, format_project_table

    projects = discover_projects(Path.cwd())
    print(f"Circuit Weaver {__version__}\nDirectory: {Path.cwd()}\nPython: {sys.executable}\n")
    if projects:
        print(format_project_table(projects))
    print("\n[1] Design a new circuit\n[2] Open an existing circuit\n[3] Start from the bundled example")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("\nUse design-wizard, open ., or start <new-directory> to continue.")
        return 0
    choice = input("Choice [1/2/3]: ").strip()
    if choice == "1":
        from .dispatcher import _handle_design_workflow

        _handle_design_workflow()
        return 0
    from .project_workflow import open_project, start_project

    if choice == "2":
        for index, project in enumerate(projects, 1):
            print(f"{index}. {project.name}: {project.path}")
        target = input("Project number or path [.]: ").strip() or "."
        if target.isdigit() and 1 <= int(target) <= len(projects):
            target = str(projects[int(target) - 1].path)
        result = open_project(target)
    elif choice == "3":
        target = input("New project directory: ").strip()
        if not target:
            return 1
        result = start_project(target)
    else:
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def native_stage(
    stage: str, source: str | Path, output: str | Path | None, options: dict, action: Callable[[], dict]
) -> dict:
    """Checkpoint legacy native CLI commands without modifying source folders."""
    if os.environ.get("CIRCUIT_WEAVER_WORKFLOW_CHILD") == "1":
        return action()
    from .project_state import project_state_path, resolve_project_root
    from .project_workflow import run_stage

    source = Path(source).resolve()
    destination = Path(output).resolve() if output is not None else source.with_suffix(".ses")
    source_root = resolve_project_root(source)
    root = (
        source_root
        if project_state_path(source_root).is_file() and destination.is_relative_to(source_root)
        else destination.parent
    )
    inputs = [source]
    if stage == "place" and options.get("design"):
        inputs.append(Path(options["design"]).resolve())
    for suffix in (".kicad_pro", ".kicad_dru"):
        sidecar = source.with_suffix(suffix)
        if sidecar.is_file():
            inputs.append(sidecar)
    if stage == "build":
        inputs.extend(path for path in source.parent.glob("*.kicad_sch") if path != source)
        inputs.extend(source.parent.glob("*.pretty/*.kicad_mod"))
        table = source.parent / "fp-lib-table"
        if table.is_file():
            inputs.append(table)
    outputs = [destination]
    if stage == "route" and options.get("routed_board") and options.get("output"):
        outputs.append(Path(options["output"]).resolve())
    if stage == "preview" and options.get("render_3d") and options.get("appearance") != "assembly":
        outputs.append(destination.with_suffix(".png"))

    def checked_action():
        value = action()
        if destination.suffix == ".kicad_pcb":
            outputs.extend(
                path
                for path in (destination.with_suffix(".kicad_pro"), destination.with_suffix(".kicad_dru"))
                if path.is_file()
            )
        return value

    result = run_stage(root, stage, checked_action, inputs=inputs, outputs=outputs, options=options)
    value = result.get("result")
    if isinstance(value, dict) and (
        result.get("ok") or value.get("status") in {"error", "failed", "unavailable", "blocked"}
    ):
        return value
    error = result.get("error") or {}
    return {"status": "error", "message": error.get("message", str(error)), "workflow": result}
