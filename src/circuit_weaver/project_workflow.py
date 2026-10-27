"""Shared, resumable project workflow API for CLI, MCP, and Python callers.

The runner stores JSON-safe plans and content-addressed checkpoints in the
existing ``ProjectState.workflow`` mapping. Native work is performed through
the existing CLI dispatcher in a child Python process; child invocations are
marked so integrations can avoid recording duplicate checkpoints.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib.resources
import json
import math
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import dispatcher
from .project_state import (
    AmbiguousProjectStateError,
    ProjectState,
    _descendant_project_state_roots,
    _load_project_state_file,
    file_sha256,
    get_project_state_summary,
    load_project_state,
    manufacturing_artwork_kind,
    project_state_path,
    resolve_project_root,
    resolve_project_state_root,
    save_project_state,
)

STAGES = ("validate", "generate", "build", "place", "route", "preview")
DEFAULT_STAGES = STAGES
_RUNNER_KEY = "runner"
_LOCK_NAME = "workflow.lock"
_CHILD_ENV = "CIRCUIT_WEAVER_WORKFLOW_CHILD"
# Native PCB generation scales each subprocess budget to the parsed schematic
# size (capped at 600s). Keep a separate 900s hard stage cap for netlist export,
# worker execution, DRC validation and publication together.
# A full router attempt can use 1,800s. Native SES import, zone refill and DRC
# still have to run after it; the parent must not expire at the router deadline.
_ROUTE_NATIVE_POSTPROCESS_SECONDS = 600
_STAGE_TIMEOUTS = {"build": 900, "place": 300, "route": 2400, "preview": 180}
_APPEND_ONLY_OUTPUTS = {"design.log", "circuit-weaver.log"}
_PROJECT_INPUT_SUFFIXES = {".kicad_pro", ".kicad_sch", ".kicad_pcb", ".zip", ".yaml", ".yml", ".json"}
_SPEC_SUFFIXES = {".yaml", ".yml", ".json"}
_GENERATED_REPORT_FILENAMES = {"ground-plane.json", "route-quality.json"}
_PROJECT_MODEL_SUFFIXES = {".step", ".stp", ".wrl", ".wrz", ".iges", ".igs", ".obj", ".stl", ".ply"}


class WorkflowError(RuntimeError):
    def __init__(
        self, code: str, message: str, choices: list[str] | None = None, *, stage_result: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.choices = choices
        self.stage_result = stage_result


def _result(ok: bool, status: str, **values: Any) -> dict[str, Any]:
    return {"ok": ok, "status": status, **values}


def _error(exc: Exception, fallback: str = "workflow_error") -> dict[str, Any]:
    code = getattr(exc, "code", fallback)
    value = {"code": str(code), "message": str(exc)}
    if getattr(exc, "choices", None):
        value["choices"] = list(exc.choices)
    return value


def _root(target: str | Path) -> Path:
    try:
        return resolve_project_state_root(target).resolve()
    except AmbiguousProjectStateError as exc:
        choices = [line.strip()[2:].strip() for line in str(exc).splitlines() if line.strip().startswith("-")]
        raise WorkflowError("ambiguous_project", str(exc), choices=choices) from exc


def _load(root: Path) -> ProjectState:
    state = load_project_state(root)
    if state is None:
        raise WorkflowError("project_not_found", f"No Circuit Weaver project state at {root}")
    return state


def _runner(state: ProjectState) -> dict[str, Any]:
    value = state.workflow.get(_RUNNER_KEY)
    if not isinstance(value, dict):
        value = {"version": 1, "status": "idle", "stages": {}, "cancel_requested": False}
        state.workflow[_RUNNER_KEY] = value
    value.setdefault("stages", {})
    value.setdefault("status", "idle")
    value.setdefault("cancel_requested", False)
    return value


def _touch(run: dict[str, Any]) -> None:
    run["updated_at"] = datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _snapshot(root: Path, paths: list[str | Path]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve(strict=False)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        record: dict[str, Any] = {"path": key, "exists": path.is_file()}
        if path.is_file():
            stat = path.stat()
            record.update({"sha256": file_sha256(path), "size": stat.st_size})
        elif path.is_dir() and path.suffix.casefold() == ".pretty":
            from .kicad_project_context import footprint_library_tree_sha256
            record.update({"exists": True, "kind": "footprint_library_tree",
                           "sha256": footprint_library_tree_sha256(path)})
        records.append(record)
    return records


def _checkpoint_outputs(root: Path, paths: list[str | Path]) -> list[dict[str, Any]]:
    stable = [path for path in paths if Path(path).name.casefold() not in _APPEND_ONLY_OUTPUTS]
    return _snapshot(root, stable)


def _checkpoint_output_paths(checkpoint: dict[str, Any]) -> list[str]:
    return [
        str(item["path"])
        for item in checkpoint.get("outputs", [])
        if item.get("exists") and Path(item.get("path", "")).name.casefold() not in _APPEND_ONLY_OUTPUTS
    ]


def _spec_candidates(root: Path, paths: Any = None) -> list[Path]:
    """Return candidate design files, excluding known generated root reports."""
    candidates = root.iterdir() if paths is None else paths
    return sorted(
        (
            path for path in candidates
            if path.is_file()
            and path.suffix.casefold() in _SPEC_SUFFIXES
            and path.name.casefold() not in _GENERATED_REPORT_FILENAMES
        ),
        key=lambda path: path.name.casefold(),
    )


def _select_spec(root: Path, paths: Any = None, *, required: bool = True) -> Path | None:
    candidates = _spec_candidates(root, paths)
    candidate = root / "design.yaml"
    if candidate.is_file() and candidate in candidates:
        return candidate
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        if not required:
            return None
        raise WorkflowError("spec_missing", f"No design spec found in {root}")
    raise WorkflowError(
        "ambiguous_spec",
        f"Multiple design specs found in {root}; use design.yaml",
        [str(path) for path in candidates],
    )


def _spec_path(root: Path) -> Path:
    selected = _select_spec(root)
    assert selected is not None
    return selected


def _has_spec(root: Path) -> bool:
    return bool(_spec_candidates(root))


def _stage_requires_spec(stage: str) -> bool:
    return stage in {"validate", "generate"}


def _native_boards(root: Path) -> list[Path]:
    return sorted((path for path in root.glob("*.kicad_pcb") if path.is_file()), key=lambda path: path.name.casefold())


def _write_state(root: Path, state: ProjectState) -> None:
    try:
        save_project_state(root, state)
    except Exception as exc:
        raise WorkflowError("state_conflict", f"Project state changed concurrently; retry: {exc}") from exc


def start_project(
    project_dir: str | Path, spec: dict[str, Any] | None = None, *, name: str | None = None
) -> dict[str, Any]:
    """Create a project draft without replacing any existing path or file."""
    root = Path(project_dir).expanduser().resolve(strict=False)
    try:
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise WorkflowError("destination_not_empty", f"Project destination must be empty: {root}")
        if spec is not None and not isinstance(spec, dict):
            raise WorkflowError("invalid_spec", "spec must be a JSON object")
        root.mkdir(parents=True, exist_ok=True)
        if spec is None:
            resource = importlib.resources.files("circuit_weaver").joinpath("examples", "iot_sensor.yaml")
            spec_data = dispatcher._load_spec_file(resource) if isinstance(resource, Path) else None
            if spec_data is None:
                import yaml

                spec_data = yaml.safe_load(resource.read_text(encoding="utf-8"))
        else:
            spec_data = json.loads(json.dumps(spec))
        project_name = name or str(spec_data.get("project") or root.name or "project")
        spec_data["project"] = project_name
        design_file = root / "design.yaml"
        if design_file.exists():
            raise WorkflowError("destination_not_empty", f"Refusing to overwrite {design_file}")
        with design_file.open("x", encoding="utf-8", newline="") as handle:
            handle.write(dispatcher.spec_to_yaml_text(spec_data))
        state = ProjectState(name=project_name, kind="design", status="new", current_phase="draft")
        state.sources.append({"path": "design.yaml", "role": "generation_source", "kind": "design_spec"})
        save_project_state(root, state)
        return _result(
            True,
            "draft",
            project_dir=str(root),
            name=project_name,
            spec=str(design_file),
            next_actions=["plan_workflow"],
        )
    except Exception as exc:
        return _result(False, "error", error=_error(exc), project_dir=str(root))


def open_project(target: str | Path = ".") -> dict[str, Any]:
    """Return project status without changing project files."""
    try:
        root = _root(target)
        state = load_project_state(root)
        if state is None:
            files = (
                sorted(
                    str(path.resolve())
                    for path in root.iterdir()
                    if path.is_file() and _is_adoptable_input(path)
                )
                if root.is_dir()
                else ([str(root)] if root.is_file() else [])
            )
            if not files:
                raise WorkflowError(
                    "project_not_found", f"No project manifest or supported design files found at {root}"
                )
            return _result(
                True,
                "open",
                project_dir=str(root if root.is_dir() else root.parent),
                summary={"kind": "native", "files": files, "read_only": True},
                choices=files,
                next_actions=["adopt_project"],
            )
        summary = get_project_state_summary(root)
        run = _runner(state)
        workflow_view = dict(run)
        persisted_status = str(run.get("status", "idle"))
        activity = _workflow_activity(root, persisted_status)
        if persisted_status == "running":
            # Keep the durable runner/checkpoints available to guarded resume,
            # while making the outward status reflect lock ownership.
            workflow_view["persisted_status"] = persisted_status
            if activity["state"] == "interrupted":
                workflow_view["status"] = "interrupted"
        outward_status = "interrupted" if activity["state"] == "interrupted" else persisted_status
        return _result(
            True,
            str(outward_status or summary.get("status", "open")),
            project_dir=str(root),
            summary=summary,
            workflow=workflow_view,
            workflow_activity=activity,
        )
    except Exception as exc:
        return _result(False, "error", error=_error(exc), choices=getattr(exc, "choices", None))


def adopt_project(target: str | Path, name: str | None = None) -> dict[str, Any]:
    """Create durable Circuit Weaver state for an existing spec or KiCad project.

    Adoption records hashes and sizes for direct project inputs. It never edits
    those files. Existing exact state is returned unchanged, making retries safe.
    Native planning and execution also initialize state automatically; adoption
    is useful when an explicit baseline of existing source hashes is wanted.
    """
    try:
        candidate = Path(target).expanduser().resolve(strict=False)
        if candidate.is_file():
            root = candidate.parent
            if not _is_adoptable_input(candidate):
                raise WorkflowError("unsupported_target", f"Unsupported project file: {candidate}")
        elif candidate.is_dir():
            root = candidate
        else:
            raise WorkflowError(
                "unsupported_target", f"Project target must be an existing directory or file: {candidate}"
            )

        state_path = project_state_path(root)
        if state_path.is_file():
            existing = open_project(root)
            return {**existing, "adopted": False, "message": "Project already has durable state"}
        _require_adoption_root(candidate, root)

        with _RunLock(root):
            # Adoption and workflow initialization must serialize. Another
            # operation may have created the state while this call waited.
            if state_path.is_file():
                existing = open_project(root)
                return {**existing, "adopted": False, "message": "Project already has durable state"}
            if state_path.exists():
                raise WorkflowError(
                    "state_conflict", f"Project state path already exists and is not a manifest: {state_path}"
                )
            _require_adoption_root(candidate, root)
            descendants = _descendant_project_state_roots(root)
            if descendants:
                choices = [str(path) for path in descendants]
                raise WorkflowError(
                    "ambiguous_project",
                    "Target contains existing project state; adopt a specific project directory",
                    choices,
                )

            files = sorted(
                (path for path in root.iterdir() if path.is_file() and _is_adoptable_input(path)),
                key=lambda path: path.name.casefold(),
            )
            specs = _spec_candidates(root, files)
            spec = _select_spec(root, files, required=False)
            if spec is not None:
                files = [path for path in files if path not in specs or path == spec]
            if not files or all(path.name.casefold() in _GENERATED_REPORT_FILENAMES for path in files):
                raise WorkflowError("unsupported_target", f"No supported project inputs found in {root}")

            project_name = root.name or "project"
            if name is not None:
                if not isinstance(name, str) or not name.strip():
                    raise WorkflowError("invalid_name", "name must be a nonempty string")
                project_name = name.strip()
            sources = []
            for path in files:
                if path == spec:
                    kind = "design_spec"
                elif path.suffix.casefold() == ".kicad_pro":
                    kind = "kicad_project"
                elif path.suffix.casefold() == ".kicad_sch":
                    kind = "schematic"
                elif path.suffix.casefold() == ".kicad_pcb":
                    kind = "pcb"
                elif path.suffix.casefold() == ".zip":
                    kind = "archive"
                else:
                    kind = manufacturing_artwork_kind(path) or "project_source"
                stat = path.stat()
                record = {
                    "path": path.relative_to(root).as_posix(),
                    "role": "generation_source" if path == spec else "project_input",
                    "kind": kind,
                    "size": stat.st_size,
                    "sha256": file_sha256(path),
                }
                sources.append(record)

            state = ProjectState(
                name=project_name,
                kind="design" if spec is not None else "native",
                status="open",
                current_phase="adopted",
                next_actions=["plan_workflow"],
                sources=sources,
                workflow={
                    "adoption": {
                        "status": "adopted",
                        "input_snapshot": _snapshot(root, [root / item["path"] for item in sources]),
                    }
                },
            )
            save_project_state(root, state)
        return _result(
            True,
            "adopted",
            adopted=True,
            project_dir=str(root),
            name=project_name,
            sources=sources,
            next_actions=["plan_workflow"],
        )
    except Exception as exc:
        return _result(False, "error", error=_error(exc), choices=getattr(exc, "choices", None))


def _require_adoption_root(candidate: Path, root: Path) -> None:
    resolved_root = resolve_project_root(candidate)
    if resolved_root == root:
        return
    state_owner = project_state_path(resolved_root)
    code = "state_conflict" if state_owner.exists() else "ambiguous_project"
    raise WorkflowError(
        code,
        f"Target is inside a project rooted at {resolved_root}; adopt that project root instead",
        [str(resolved_root)],
    )


def _is_adoptable_input(path: Path) -> bool:
    suffix = path.suffix.casefold()
    return suffix in _PROJECT_INPUT_SUFFIXES or manufacturing_artwork_kind(path) is not None


def _normalize_stages(stages: list[str] | tuple[str, ...] | None) -> list[str]:
    value = list(DEFAULT_STAGES if stages is None else stages)
    if not value or any(not isinstance(stage, str) or stage not in STAGES for stage in value):
        raise WorkflowError("invalid_stages", f"stages must be a nonempty ordered subset of {', '.join(STAGES)}")
    if len(set(value)) != len(value) or value != sorted(value, key=STAGES.index):
        raise WorkflowError("invalid_stages", "stages must be unique and in canonical order")
    return value


def _normalize_options(options: dict[str, Any] | None) -> dict[str, Any]:
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise WorkflowError("invalid_options", "options must be a JSON object")
    raw_contracts = options.get("route_contracts")
    if isinstance(raw_contracts, dict) and isinstance(raw_contracts.get("routes"), dict):
        if any(not isinstance(key, str) or not key.strip() for key in raw_contracts["routes"]):
            raise WorkflowError("invalid_options", "route_contracts route IDs must be nonempty strings")
    try:
        normalized = json.loads(json.dumps(options, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise WorkflowError("invalid_options", "options must contain JSON-safe values") from exc
    if "copper_layers" in normalized:
        from .pcb_stackup import validate_copper_layers
        try:
            normalized["copper_layers"] = validate_copper_layers(normalized["copper_layers"])
        except ValueError as exc:
            raise WorkflowError("invalid_options", str(exc)) from exc
    if "ground_plane" in normalized:
        from .pcb_ground_plane import _validate_plan
        try:
            normalized["ground_plane"] = _validate_plan(normalized["ground_plane"])
        except (TypeError, ValueError) as exc:
            raise WorkflowError("invalid_options", str(exc)) from exc
    for key in ("clearance_mm", "minimum_drill_mm"):
        if key not in normalized:
            continue
        value = normalized[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise WorkflowError("invalid_options", f"{key} must be a finite positive number")
    for key, minimum, maximum in (
        ("route_max_passes", 0, 9999),
        ("route_optimization_threads", 0, 1024),
        ("route_attempts", 1, 32),
    ):
        if key not in normalized:
            continue
        value = normalized[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise WorkflowError("invalid_options", f"{key} must be an integer from {minimum} through {maximum}")
    if "route_effort" in normalized:
        if not isinstance(normalized["route_effort"], str) or normalized["route_effort"] not in {
            "fast", "medium", "high"
        }:
            raise WorkflowError("invalid_options", "route_effort must be fast, medium, or high")
    if "route_contracts" in normalized:
        contracts = normalized["route_contracts"]
        if (not isinstance(contracts, dict) or not isinstance(contracts.get("routes"), dict)
                or not contracts["routes"] or set(contracts) - {"schema_version", "routes", "pairs"}):
            raise WorkflowError("invalid_options", "route_contracts requires a nonempty explicit routes object")
    return normalized


def _plan_data(root: Path, stages: list[str], options: dict[str, Any]) -> dict[str, Any]:
    inputs = _snapshot(root, _stage_inputs(root, stages[0], []))
    return {
        "stages": stages,
        "options": options,
        "inputs": inputs,
        "hash": _digest({"stages": stages, "options": options, "inputs": inputs}),
    }


def plan_workflow(
    target: str | Path = ".", *, stages: list[str] | None = None, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Persist a plan, initializing state for manifestless native projects.

    Workflow execution can initialize the same native state automatically;
    callers may use :func:`adopt_project` first to record source hash baselines.
    """
    try:
        root = _root(target)
        normalized_stages = _normalize_stages(stages)
        normalized_options = _normalize_options(options)
        with _RunLock(root):
            plan = _plan_data(root, normalized_stages, normalized_options)
            state = load_project_state(root)
            if state is None:
                state = ProjectState(name=root.name or "project", kind="native")
                save_project_state(root, state)
            run = _runner(state)
            # Lock ownership proves any persisted running state is stale.
            run.update({"status": "planned", "current_stage": None, "plan": plan, "cancel_requested": False})
            _touch(run)
            _write_state(root, state)
        return _result(True, "planned", project_dir=str(root), plan=plan)
    except Exception as exc:
        return _result(False, "error", error=_error(exc))


class _RunLock:
    """Advisory OS lock: process death releases ownership; metadata is diagnostic only."""

    def __init__(self, root: Path):
        self.path = root / ".circuit-weaver" / _LOCK_NAME
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        deadline = time.monotonic() + 0.2
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise WorkflowError("workflow_busy", "Another workflow run owns this project lock") from exc
                time.sleep(0.025)
        metadata = json.dumps(
            {"run_id": uuid.uuid4().hex, "pid": os.getpid(), "host": socket.gethostname(), "started": time.time()}
        ).encode()
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(metadata)
        self.handle.flush()
        return self

    def __exit__(self, *_):
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def _probe_run_lock(root: Path) -> tuple[str, str | None]:
    """Inspect existing advisory lock ownership without creating or changing files.

    Returns ``("active" | "free" | "unknown", diagnostic)``. Lock metadata
    is deliberately ignored: only the OS lock can establish live ownership.
    """
    path = root / ".circuit-weaver" / _LOCK_NAME
    try:
        handle = path.open("r+b")
    except FileNotFoundError:
        return "free", None
    except OSError as exc:
        return "unknown", f"{type(exc).__name__}: {exc}"

    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return "active", None
                return "unknown", f"{type(exc).__name__}: {exc}"
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    return "active", None
                return "unknown", f"{type(exc).__name__}: {exc}"
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return "free", None
    except Exception as exc:
        return "unknown", f"{type(exc).__name__}: {exc}"
    finally:
        handle.close()


def _workflow_activity(root: Path, persisted_status: str) -> dict[str, Any]:
    if persisted_status != "running":
        return {"state": "idle", "basis": "persisted_status"}
    ownership, diagnostic = _probe_run_lock(root)
    if ownership == "active":
        return {"state": "active", "basis": "advisory_lock"}
    if ownership == "free":
        return {"state": "interrupted", "basis": "advisory_lock"}
    return {"state": "unknown", "basis": "advisory_lock", "diagnostic": diagnostic}


def record_stage(
    target: str | Path,
    stage: str,
    *,
    inputs: list[str | Path],
    outputs: list[str | Path],
    options: dict[str, Any] | None = None,
    status: str = "completed",
    error: dict[str, Any] | str | None = None,
    run_id: str | None = None,
    result: Any = None,
) -> dict[str, Any]:
    """Persist a stage boundary/checkpoint for native CLI and integration callers."""
    if stage not in STAGES or status not in {"running", "completed", "failed", "cancelled"}:
        raise WorkflowError("invalid_checkpoint", "Unknown stage or checkpoint status")
    root = _root(target)
    state = _load(root)
    run = _runner(state)
    entry = {
        "status": status,
        "options": _normalize_options(options),
        "inputs": _snapshot(root, inputs),
        "outputs": _checkpoint_outputs(root, outputs),
        "run_id": run_id or run.get("run_id"),
        "updated_at": time.time(),
    }
    if error is not None:
        entry["error"] = error if isinstance(error, dict) else {"message": str(error)}
    if result is not None:
        try:
            entry["result"] = json.loads(json.dumps(result, allow_nan=False, default=str))
        except (TypeError, ValueError):
            entry["result"] = {"summary": str(result)}
    run["stages"][stage] = entry
    run["current_stage"] = stage if status == "running" else None
    if status == "running":
        run["status"] = "running"
    elif status in {"failed", "cancelled"}:
        run["status"] = status
    _touch(run)
    _write_state(root, state)
    return json.loads(json.dumps(entry))


def _verify_checkpoint(
    root: Path, stage: str, checkpoint: dict[str, Any], inputs: list[str | Path], options: dict[str, Any]
) -> bool:
    if checkpoint.get("status") != "completed" or checkpoint.get("options") != options:
        return False
    if checkpoint.get("inputs") != _snapshot(root, inputs):
        raise WorkflowError("needs_replan", f"Inputs changed since {stage} completed; plan again before continuing")
    saved_outputs = checkpoint.get("outputs", [])
    if not saved_outputs:
        return True
    if any(not item.get("exists") for item in saved_outputs):
        return False
    current = _checkpoint_outputs(root, [item["path"] for item in saved_outputs])
    if current != saved_outputs:
        raise WorkflowError("needs_replan", f"An output from {stage} changed; plan again before continuing")
    return True


def _cli(stage: str, args: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    env = os.environ.copy()
    env[_CHILD_ENV] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "circuit_weaver", *args],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("stage_timeout", f"{stage} exceeded its {timeout}s timeout") from exc
    except OSError as exc:
        raise WorkflowError("stage_launch_failed", str(exc)) from exc
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        if proc.returncode:
            raise WorkflowError(
                "stage_failed", proc.stderr[-4000:] or proc.stdout[-4000:] or f"{stage} exited {proc.returncode}"
            ) from exc
        raise WorkflowError("invalid_stage_output", f"{stage} returned no machine-readable result") from exc
    if not isinstance(result, dict):
        if proc.returncode:
            raise WorkflowError(
                "stage_failed", proc.stderr[-4000:] or proc.stdout[-4000:] or f"{stage} exited {proc.returncode}"
            )
        raise WorkflowError("invalid_stage_output", f"{stage} returned a non-object result")
    if proc.returncode:
        error = result.get("error")
        if isinstance(error, dict):
            code = error.get("code") or result.get("code") or "stage_failed"
            message = error.get("message") or error.get("reason") or result.get("message") or result.get("reason")
        else:
            code = result.get("code") or "stage_failed"
            message = error or result.get("message") or result.get("reason") or result.get("detail")
        message = message or proc.stderr[-4000:] or f"{stage} exited {proc.returncode}"
        raise WorkflowError(str(code), f"{stage} failed: {message}", stage_result=result)
    return result


def _execute_stage(
    root: Path, stage: str, options: dict[str, Any], prior: list[str]
) -> tuple[dict[str, Any], list[str]]:
    spec = _spec_path(root) if _stage_requires_spec(stage) else None
    if stage == "validate":
        design = dispatcher._load_spec_file(spec)
        report = dispatcher.validate_design(
            design, enrich_parts=bool(options.get("enrich_parts", False)), check_determinism=False
        )
        payload = report.to_dict() if hasattr(report, "to_dict") else vars(report)
        if not bool(getattr(report, "valid", payload.get("valid", False))):
            raise WorkflowError("validation_failed", "Design validation reported errors")
        result = {"status": "ok", "valid": True}
        operating_cases = payload.get("metadata", {}).get("operating_cases", {})
        if operating_cases.get("status") != "not_requested" and operating_cases:
            result["operating_cases"] = operating_cases
        return result, []
    if stage == "generate":
        out_dir = root / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        design = dispatcher._load_spec_file(spec)
        result = dispatcher.generate_artifacts(design, output_dir=out_dir, project_root=root, require_kicad=False)
        if not isinstance(result, dict):
            raise WorkflowError("generation_failed", "Generation returned no structured result")
        if result.get("status") in {"error", "failed"} or result.get("valid") is not True:
            raise WorkflowError(
                "generation_failed", str(result.get("message") or "Generation did not produce a valid design")
            )
        outputs = [str(p) for p in out_dir.rglob("*") if p.is_file()]
        if not outputs:
            raise WorkflowError("generation_empty", "Generation completed without publishing any artifacts")
        return result if isinstance(result, dict) else {"status": "ok"}, outputs
    if stage == "build":
        width = options.get("board_width_mm")
        height = options.get("board_height_mm")
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, (int, float))
            or not isinstance(height, (int, float))
            or width <= 0
            or height <= 0
        ):
            raise WorkflowError(
                "dimensions_required", "build requires explicit positive board_width_mm and board_height_mm"
            )
        sch = _root_schematic(root)
        attempt = _attempt_dir(root, stage)
        out = attempt / "prototype.kicad_pcb"
        result = _cli(
            stage,
            [
                "build-pcb",
                str(sch),
                "--output",
                str(out),
                "--project-root",
                str(root),
                *(["--copper-layers", str(options["copper_layers"])] if "copper_layers" in options else []),
                "--width",
                str(width),
                "--height",
                str(height),
                *(["--clearance", str(options["clearance_mm"])] if "clearance_mm" in options else []),
                *(["--minimum-drill", str(options["minimum_drill_mm"])] if "minimum_drill_mm" in options else []),
                *_tool_args(options, "kicad_python_path"),
            ],
            root,
            _STAGE_TIMEOUTS[stage],
        )
        _require_ok(result, stage)
        _require_outputs(stage, result, [out])
        outputs = _publish_attempt(attempt, root, out, root / out.name, result)
        return result, outputs
    if stage in {"place", "route", "preview"}:
        board = _latest_board(root, stage)
        if "copper_layers" in options:
            _require_workflow_stackup(board, options)
        if stage == "place":
            iterations = options.get("iterations", 5000)
            if isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= 100000:
                raise WorkflowError("invalid_iterations", "iterations must be an integer from 1 through 100000")
            attempt = _attempt_dir(root, stage)
            out = attempt / "placed.kicad_pcb"
            args = [
                "place-pcb",
                str(board),
                "--output",
                str(out),
                "--iterations",
                str(iterations),
                *_tool_args(options, "kicad_python_path"),
                "--tidy-references",
            ]
            has_design = _has_spec(root)
            design = _spec_path(root) if has_design else None
            if design is not None:
                args.extend(["--design", str(design)])
            result = _cli(stage, args, root, _STAGE_TIMEOUTS[stage])
            _require_ok(result, stage)
            if "copper_layers" in options:
                _require_workflow_stackup(out, options)
            outputs = _publish_attempt(attempt, root, out, root / out.name, result)
        elif stage == "route":
            attempt = _attempt_dir(root, stage)
            out = attempt / "routed.ses"
            routed_board = attempt / "routed.kicad_pcb"
            router_timeout = min(max(float(options.get("route_timeout", 300)), 1), 1800)
            plane_preparation = None
            if "ground_plane" in options:
                from .kicad_project_context import stage_project_footprint_context
                from .pcb_ground_plane import add_ground_plane
                plane_board = attempt / "plane-input.kicad_pcb"
                plane_preparation = add_ground_plane(
                    board, plane_board, options["ground_plane"], pre_route=True,
                    kicad_python_path=options.get("kicad_python_path"),
                )
                (attempt / "ground-plane-before.json").write_text(
                    json.dumps(plane_preparation, indent=2) + "\n", encoding="utf-8"
                )
                _require_ok(plane_preparation, "ground_plane")
                _require_outputs("ground_plane", plane_preparation, [plane_board])
                # The routed working copy needs the same project footprint table;
                # it remains private and is never published over project assets.
                stage_project_footprint_context(board.parent, attempt)
                board = plane_board
                if "copper_layers" in options:
                    _require_workflow_stackup(board, options)
            args = [
                "autoroute",
                str(board),
                "--output",
                str(out),
                "--routed-board",
                str(routed_board),
                "--timeout",
                str(router_timeout),
            ]
            for key, flag in (
                ("route_max_passes", "--max-passes"),
                ("route_optimization_threads", "--optimization-threads"),
                ("route_attempts", "--attempts"),
                ("route_effort", "--effort"),
            ):
                if key in options:
                    args.extend([flag, str(options[key])])
            args.extend(_tool_args(options, "freerouting_path", "kicad_cli_path", "kicad_python_path"))
            result = _cli(
                stage, args, root,
                min(math.ceil(router_timeout) + _ROUTE_NATIVE_POSTPROCESS_SECONDS, _STAGE_TIMEOUTS[stage]),
            )
            _require_ok(result, stage)
            extras = [(out, root / out.name)]
            publish_hashes = None
            if any(key in options for key in ("route_contracts", "ground_plane", "copper_layers")):
                publish_hashes = {
                    path: file_sha256(path) if path.is_file() else None
                    for path in (routed_board, out, routed_board.with_suffix(".kicad_pro"),
                                 routed_board.with_suffix(".kicad_dru"))
                }
            if "copper_layers" in options:
                result["copper_layers"] = _require_workflow_stackup(routed_board, options)
            if plane_preparation is not None:
                (attempt / "ground-plane-after.json").write_text(
                    json.dumps({"plan": options["ground_plane"], "after_route": result.get("zone_refill")},
                               indent=2) + "\n", encoding="utf-8"
                )
                _require_workflow_planes(result.get("zone_refill"), options["ground_plane"])
                report = attempt / "ground-plane.json"
                plane_report = {
                    "plan": options["ground_plane"], "plan_sha256": _digest(options["ground_plane"]),
                    "pre_route": plane_preparation, "after_route": result["zone_refill"],
                    "board_sha256": publish_hashes[routed_board], "routing_complete": True,
                    "electrical_or_manufacturing_approval": False,
                }
                report.write_text(json.dumps(plane_report, indent=2) + "\n", encoding="utf-8")
                result["ground_plane"] = plane_report
                extras.append((report, root / report.name))
                publish_hashes[report] = file_sha256(report)
            if "route_contracts" in options:
                from .board_review import review_pcb

                review = review_pcb(routed_board, {"routing": options["route_contracts"]},
                                    kicad_python_path=options.get("kicad_python_path"))
                quality = {
                    **review.get("routing", {"status": "unverified"}),
                    "board_sha256": review["source_sha256"],
                    "contract_sha256": _digest(options["route_contracts"]),
                    "sources_unchanged": review["sources_unchanged"],
                }
                report = attempt / "route-quality.json"
                report.write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")
                if quality["board_sha256"] != publish_hashes[routed_board]:
                    raise WorkflowError("stage_output_changed", "Routed candidate changed before quality review")
                if quality["status"] != "verified":
                    raise WorkflowError(
                        "route_contract_failed",
                        f"Authored route checks are {quality['status']}; candidate and report retained in {attempt}",
                    )
                result["route_quality"] = quality
                extras.append((report, root / report.name))
                publish_hashes[report] = file_sha256(report)
            outputs = _publish_attempt(
                attempt, root, routed_board, root / routed_board.name, result, extras=extras,
                expected_hashes=publish_hashes,
            )
        else:
            appearance = options.get("appearance", "layers")
            if appearance not in {"layers", "assembly"}:
                raise WorkflowError("invalid_appearance", "appearance must be layers or assembly")
            attempt = _attempt_dir(root, stage)
            out = attempt / "preview.svg"
            args = [
                "preview-pcb",
                str(board),
                "--output",
                str(out),
                "--appearance",
                str(appearance),
                "--timeout",
                str(min(max(int(options.get("preview_timeout", 120)), 1), 180)),
                *_tool_args(options, "kicad_cli_path", "kicad_python_path"),
            ]
            result = _cli(stage, args, root, _STAGE_TIMEOUTS[stage])
            _require_ok(result, stage)
            if appearance == "assembly" and not _assembly_coverage(result):
                raise WorkflowError("preview_incomplete", "Assembly preview lacks verified component/model coverage")
            outputs = _publish_attempt(attempt, root, out, root / out.name, result)
        _require_outputs(stage, result, [Path(value) for value in outputs])
        return result, outputs
    raise WorkflowError("unknown_stage", stage)


def _require_workflow_stackup(board: Path, options: dict[str, Any]) -> dict[str, Any]:
    from .kicad_bridge import run_kicad
    from .pcb_stackup import expected_copper_layer_names
    inspection = run_kicad("inspect", board=str(board), python_path=options.get("kicad_python_path"))
    expected = expected_copper_layer_names(options["copper_layers"])
    if (inspection.get("status") != "ok" or inspection.get("copper_layer_inventory_complete") is not True
            or type(inspection.get("copper_layer_count")) is not int
            or inspection.get("copper_layer_count") != options["copper_layers"]
            or tuple(inspection.get("copper_layer_names") or ()) != expected):
        raise WorkflowError("stackup_mismatch", "Native board does not verify the explicitly requested copper layers")
    return {"count": len(expected), "ordered_names": list(expected),
            "scope": "copper_count_and_order_only; dielectric_and_impedance_not_assessed"}


def _require_workflow_planes(report: Any, plan: dict[str, Any]) -> None:
    if (not isinstance(report, dict) or report.get("status") != "verified"
            or any(report.get(key) is not True for key in (
                "saved_fill_geometry_verified", "zone_definitions_preserved", "copper_layers_preserved"))
            or not isinstance(report.get("zones"), list)):
        raise WorkflowError("plane_verification_failed", "Routed planes lack fresh native saved-fill evidence")
    if (type(report.get("zone_count")) is not int or report["zone_count"] <= 0
            or report["zone_count"] != len(report["zones"])):
        raise WorkflowError("plane_verification_failed", "Routed plane count is inconsistent")
    remaining = report.get("unconnected_pads_by_net")
    anchors = report.get("connected_copper")
    if not isinstance(remaining, dict) or remaining.get(plan["net"]) != [] or not isinstance(anchors, list):
        raise WorkflowError("plane_verification_failed", "Routed plane net has incomplete native pad/anchor evidence")
    for layer in plan["layers"]:
        rows = [row for row in report["zones"] if isinstance(row, dict)
                and row.get("net") == plan["net"] and row.get("layer_name") == layer]
        if len(rows) != 1:
            raise WorkflowError("plane_verification_failed", "Routed plane inventory does not match the authored plan")
        row = rows[0]
        connections = [item for item in anchors if isinstance(item, dict) and item.get("id") == row.get("id")]
        if (len(connections) != 1 or not row.get("id")
                or type(connections[0].get("connected_copper_count")) is not int
                or connections[0]["connected_copper_count"] <= 0):
            raise WorkflowError("plane_verification_failed", "Routed plane has no proven native copper anchor")
        area, islands, digest = row.get("area_mm2"), row.get("island_count"), row.get("polygon_sha256")
        if (isinstance(area, bool) or not isinstance(area, (int, float)) or not math.isfinite(area) or area <= 0
                or type(islands) is not int or islands != 0 or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest.lower())):
            raise WorkflowError("plane_verification_failed", "Routed planes contain unverified fill or isolated copper")


def _tool_args(options: dict[str, Any], *keys: str) -> list[str]:
    flags = {
        "kicad_python_path": "--kicad-python-path",
        "kicad_cli_path": "--kicad-cli-path",
        "freerouting_path": "--freerouting-path",
    }
    out: list[str] = []
    for key in keys:
        if options.get(key):
            out.extend([flags[key], str(options[key])])
    return out


def _require_ok(result: dict[str, Any], stage: str) -> None:
    if result.get("status") not in {"ok", "success", "completed"}:
        raise WorkflowError(f"{stage}_failed", str(result.get("message") or result.get("reason") or f"{stage} failed"))


def _require_outputs(stage: str, result: dict[str, Any], paths: list[Path]) -> None:
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise WorkflowError("stage_output_missing", f"{stage} did not create expected output(s): {', '.join(missing)}")


def _related_outputs(path: Path) -> list[str]:
    return [
        str(candidate)
        for candidate in (path, path.with_suffix(".kicad_pro"), path.with_suffix(".kicad_dru"))
        if candidate.is_file()
    ]


def _attempt_dir(root: Path, stage: str) -> Path:
    attempt = root / ".circuit-weaver" / "attempts" / f"{stage}-{uuid.uuid4().hex}"
    attempt.mkdir(parents=True, exist_ok=False)
    return attempt


def _publish_attempt(
    attempt: Path,
    root: Path,
    primary: Path,
    destination: Path,
    result: dict[str, Any],
    *,
    extras: list[tuple[Path, Path]] | None = None,
    expected_hashes: dict[Path, str | None] | None = None,
) -> list[str]:
    staged = [(primary, destination)]
    staged.extend(
        (primary.with_suffix(suffix), destination.with_suffix(suffix))
        for suffix in (".kicad_pro", ".kicad_dru")
        if primary.with_suffix(suffix).is_file()
    )
    staged.extend(extras or [])

    def check_hashes() -> None:
        for path, expected in (expected_hashes or {}).items():
            if (file_sha256(path) if path.is_file() else None) != expected:
                raise WorkflowError("stage_output_changed", f"Reviewed stage output changed: {path}")

    check_hashes()
    for source, target in staged:
        if not source.is_file():
            raise WorkflowError("stage_output_missing", f"Expected staged output is missing: {source}")
        if target.exists():
            raise WorkflowError("output_exists", f"Refusing to replace existing workflow output: {target}")
    published: list[Path] = []
    linked_sources: dict[Path, tuple[int, int]] = {}
    linked_targets: dict[Path, tuple[int, int]] = {}
    try:
        for source, target in staged:
            # Hard-link creation is atomic and fails if a concurrent writer
            # created the destination after our preflight check.
            os.link(source, target)
            published.append(target)
            target_stat = target.stat()
            identity = (target_stat.st_dev, target_stat.st_ino)
            linked_sources[source] = identity
            linked_targets[target] = identity
            check_hashes()
            expected = (expected_hashes or {}).get(source)
            if expected is not None and file_sha256(target) != expected:
                raise WorkflowError("stage_output_changed", f"Published bytes do not match reviewed output: {target}")
    except (OSError, WorkflowError) as exc:
        for target in reversed(published):
            try:
                target_stat = target.stat()
                if (target_stat.st_dev, target_stat.st_ino) == linked_targets.get(target):
                    target.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, WorkflowError):
            raise
        raise WorkflowError("publish_failed", f"Could not publish native stage outputs: {exc}") from exc
    for source, target in staged:
        try:
            source_stat = source.stat()
            target_stat = target.stat()
            if (
                linked_sources.get(source) == (source_stat.st_dev, source_stat.st_ino)
                and (target_stat.st_dev, target_stat.st_ino) == linked_sources[source]
            ):
                source.unlink(missing_ok=True)
        except OSError:
            # All targets are already safely published; leftover staging links
            # are harmless and can be recovered with the attempt directory.
            pass
    if all(not source.exists() for source, _ in staged):
        # Preserve diagnostics and native evidence (for example routing logs
        # and DSNs) that were not part of the published artifact set.
        try:
            attempt.rmdir()
        except OSError:
            pass
    replacements = {str(source): str(target) for source, target in staged}

    def remap(value: Any) -> Any:
        if isinstance(value, dict):
            return {remap(key): remap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, tuple):
            return tuple(remap(item) for item in value)
        if isinstance(value, str):
            for source, target in replacements.items():
                value = value.replace(source, target)
        return value

    updated_result = remap(json.loads(json.dumps(result, default=str)))
    result.clear()
    result.update(updated_result)
    return [str(path) for path in published]


def _assembly_coverage(result: dict[str, Any]) -> bool:
    components = result.get("component_coverage")
    models = result.get("model_coverage")
    return (
        isinstance(components, dict)
        and isinstance(models, dict)
        and isinstance(components.get("expected"), int)
        and components.get("expected", 0) > 0
        and components.get("represented") == components.get("expected")
        and components.get("missing") == 0
        and models.get("expected", 0) > 0
        and models.get("resolved") == models.get("expected")
        and models.get("missing") == 0
    )


def _root_schematic(root: Path) -> Path:
    manifest = root / "output" / "artifact_manifest.json"
    if manifest.is_file():
        try:
            relative = json.loads(manifest.read_text(encoding="utf-8")).get("root_schematic")
            if relative:
                candidate = (root / "output" / relative).resolve()
                if candidate.is_file() and candidate.suffix.casefold() == ".kicad_sch":
                    return candidate
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    files = sorted((root / "output").glob("*.kicad_sch"))
    if not files:
        files = sorted(root.glob("*.kicad_sch"))
    if not files:
        raise WorkflowError("schematic_missing", "No generated root schematic is available for PCB build")
    if len(files) != 1:
        raise WorkflowError("ambiguous_schematic", "Multiple schematics exist without a root_schematic manifest entry")
    return files[0]


def _latest_board(root: Path, stage: str) -> Path:
    preferred = {
        "place": [root / "prototype.kicad_pcb"],
        "route": [root / "placed.kicad_pcb", root / "prototype.kicad_pcb"],
        "preview": [root / "routed.kicad_pcb", root / "placed.kicad_pcb", root / "prototype.kicad_pcb"],
    }[stage]
    for path in preferred:
        if path.is_file():
            return path
    candidates = _native_boards(root)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise WorkflowError(
            "ambiguous_board",
            "Multiple native PCB files found; pass a project directory with one board or choose the intended board",
        )
    raise WorkflowError("board_missing", f"No board input is available for {stage}")


def _stage_inputs(root: Path, stage: str, completed_outputs: list[str]) -> list[str | Path]:
    paths: list[str | Path] = []
    if _stage_requires_spec(stage):
        paths.append(_spec_path(root))
    if stage == "generate":
        paths.extend(_custom_footprint_inputs(root, include_output=False))
    elif stage == "build":
        if _project_has_spec(root):
            paths.append(_spec_path(root))
        schematic = _root_schematic(root)
        paths.append(schematic)
        paths.extend(_existing_sidecars(schematic))
        paths.extend(_hierarchical_schematic_inputs(root, schematic))
        paths.extend(_custom_footprint_inputs(root))
    elif stage in {"place", "route", "preview"}:
        try:
            board = _latest_board(root, stage)
            paths.append(board)
            paths.extend(_existing_sidecars(board))
            if stage in {"place", "route"}:
                paths.extend(_native_footprint_context_inputs(board.parent))
            if stage == "place" and _project_has_spec(root):
                paths.append(_spec_path(root))
            if stage == "preview":
                paths.extend(_project_model_inputs(root))
        except WorkflowError:
            raise
    paths.extend(completed_outputs)
    return [path for path in paths if Path(path).name.casefold() not in _APPEND_ONLY_OUTPUTS]


def _native_footprint_context_inputs(project: Path) -> list[Path]:
    from .kicad_project_context import inspect_project_footprint_context
    try:
        context = inspect_project_footprint_context(project)
    except (OSError, ValueError) as exc:
        raise WorkflowError("invalid_library_context", str(exc)) from exc
    return [Path(path) for path in context["watch_paths"]]


def _project_has_spec(root: Path) -> bool:
    return _has_spec(root)


def _hierarchical_schematic_inputs(root: Path, selected_root: Path) -> list[Path]:
    search_root = root / "output" if selected_root.is_relative_to(root / "output") else root
    return sorted(
        (path for path in search_root.rglob("*.kicad_sch") if path.is_file()),
        key=lambda path: str(path).casefold(),
    )


def _custom_footprint_inputs(root: Path, *, include_output: bool = True) -> list[Path]:
    bases = (root, root / "output") if include_output else (root,)
    paths = [path for base in bases for path in base.glob("fp-lib-table")]
    paths.extend(path for base in bases for path in base.glob("sym-lib-table"))
    for base in bases:
        if base.is_dir():
            for pattern in ("*.kicad_mod", "*.kicad_sym"):
                for path in base.rglob(pattern):
                    if not path.is_file() or path.is_symlink():
                        continue
                    try:
                        relative = path.relative_to(root)
                        resolved = path.resolve(strict=False)
                    except (OSError, ValueError):
                        continue
                    if not relative.parts or relative.parts[0].casefold() == ".circuit-weaver":
                        continue
                    if not include_output and relative.parts[0].casefold() == "output":
                        continue
                    if resolved.is_relative_to(root):
                        paths.append(path)
    return sorted(set(paths), key=lambda path: str(path).casefold())


def _project_model_inputs(root: Path) -> list[Path]:
    receipt_dir = (root / "libraries" / ".circuit-weaver-receipts").resolve(strict=False)
    lock_paths = {
        (root / ".circuit-weaver-library-bundle.lock").resolve(strict=False),
        (root / ".circuit-weaver" / _LOCK_NAME).resolve(strict=False),
    }
    paths: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.casefold() not in _PROJECT_MODEL_SUFFIXES:
            continue
        resolved = path.resolve(strict=False)
        if resolved in lock_paths or resolved.is_relative_to(receipt_dir):
            continue
        if resolved.is_relative_to(root):
            paths.append(path)
    return sorted(paths, key=lambda path: str(path).casefold())


def _existing_sidecars(path: Path) -> list[Path]:
    return [
        candidate
        for candidate in (path.with_suffix(".kicad_pro"), path.with_suffix(".kicad_dru"))
        if candidate.is_file()
    ]


def run_workflow(
    target: str | Path = ".",
    *,
    stages: list[str] | None = None,
    options: dict[str, Any] | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    try:
        root = _root(target)
        normalized_stages = _normalize_stages(stages)
        normalized_options = _normalize_options(options)
        with _RunLock(root):
            plan = _plan_data(root, normalized_stages, normalized_options)
            state = load_project_state(root)
            if state is None:
                state = ProjectState(name=root.name or "project", kind="native")
                save_project_state(root, state)
            run = _runner(state)
            old_plan = run.get("plan")
            if resume:
                if not isinstance(old_plan, dict) or old_plan.get("hash") != plan["hash"]:
                    raise WorkflowError(
                        "needs_replan", "Saved plan inputs or settings changed; plan again before resuming"
                    )
                run_id = str(run.get("run_id") or uuid.uuid4())
            else:
                for stage in normalized_stages:
                    if stage == "generate":
                        out_dir = root / "output"
                        if out_dir.is_dir() and any(out_dir.iterdir()):
                            raise WorkflowError(
                                "output_exists", f"Refusing to overwrite existing output directory {out_dir}"
                            )
                    elif any(Path(path).exists() for path in _expected_outputs(root, stage)):
                        raise WorkflowError("output_exists", f"Refusing to overwrite existing output for stage {stage}")
                run_id = str(uuid.uuid4())
                run["stages"] = {}
                run["plan"] = plan
            run.update({"status": "running", "run_id": run_id, "cancel_requested": False, "current_stage": None})
            _touch(run)
            _write_state(root, state)
            completed_outputs: list[str] = []
            checkpoints = _runner(_load(root))["stages"]
            for stage in normalized_stages:
                state = _load(root)
                run = _runner(state)
                if run.get("cancel_requested"):
                    run.update({"status": "cancelled", "current_stage": None})
                    _touch(run)
                    _write_state(root, state)
                    return _result(
                        False, "cancelled", project_dir=str(root), run_id=run_id, completed=list(completed_outputs)
                    )
                inputs = _stage_inputs(root, stage, completed_outputs)
                if stage == normalized_stages[0] and _snapshot(root, inputs) != plan.get("inputs"):
                    raise WorkflowError(
                        "needs_replan", "Workflow inputs changed after planning; plan again before running"
                    )
                checkpoint = checkpoints.get(stage)
                if resume and checkpoint and _verify_checkpoint(root, stage, checkpoint, inputs, normalized_options):
                    completed_outputs.extend(_checkpoint_output_paths(checkpoint))
                    continue
                outputs = _expected_outputs(root, stage)
                recover_generate = bool(
                    resume
                    and stage == "generate"
                    and checkpoint
                    and checkpoint.get("status") in {"running", "failed"}
                    and checkpoint.get("options") == normalized_options
                    and checkpoint.get("inputs") == _snapshot(root, inputs)
                )
                if resume and checkpoint and checkpoint.get("status") in {"running", "failed"}:
                    if checkpoint.get("options") != normalized_options or checkpoint.get("inputs") != _snapshot(
                        root, inputs
                    ):
                        raise WorkflowError(
                            "needs_replan",
                            f"Inputs or settings changed during interrupted {stage}; plan again before continuing",
                        )
                    if stage != "generate" and any(Path(path).exists() for path in outputs):
                        existing = ", ".join(path for path in outputs if Path(path).exists())
                        raise WorkflowError(
                            "needs_recovery",
                            f"A native output from interrupted {stage} already exists: {existing}. "
                            "Move it aside after reviewing it, then resume.",
                        )
                if (
                    stage == "generate"
                    and (root / "output").is_dir()
                    and any((root / "output").iterdir())
                    and not recover_generate
                ):
                    raise WorkflowError(
                        "output_exists",
                        f"Stage generate output already exists; refusing to overwrite {root / 'output'}",
                    )
                if any(Path(path).exists() for path in outputs) and not (
                    resume and checkpoint and checkpoint.get("status") in {"running", "failed"}
                ):
                    raise WorkflowError("output_exists", f"Stage {stage} output already exists; refusing to overwrite")
                input_before = _snapshot(root, inputs)
                record_stage(
                    root,
                    stage,
                    inputs=inputs,
                    outputs=outputs,
                    options=normalized_options,
                    status="running",
                    run_id=run_id,
                )
                result: Any = None
                try:
                    result, outputs = _execute_stage(root, stage, normalized_options, completed_outputs)
                    if _snapshot(root, inputs) != input_before:
                        raise WorkflowError("input_changed", f"Input files changed while {stage} was running")
                    record_stage(
                        root,
                        stage,
                        inputs=inputs,
                        outputs=outputs,
                        options=normalized_options,
                        status="completed",
                        run_id=run_id,
                        result=result,
                    )
                except Exception as exc:
                    if stage == "generate" and (root / "output").is_dir():
                        outputs = [str(path) for path in (root / "output").rglob("*") if path.is_file()]
                    record_stage(
                        root,
                        stage,
                        inputs=inputs,
                        outputs=outputs,
                        options=normalized_options,
                        status="failed",
                        error=_error(exc),
                        run_id=run_id,
                        result=(getattr(exc, "stage_result", None) or result),
                    )
                    state = _load(root)
                    run = _runner(state)
                    run.update({"status": "failed", "current_stage": stage, "last_error": _error(exc)})
                    _touch(run)
                    _write_state(root, state)
                    raise
                checkpoints = _runner(_load(root))["stages"]
                # Match resume behavior exactly: only outputs actually captured
                # in the durable checkpoint are dependencies of later stages.
                completed_outputs.extend(_checkpoint_output_paths(checkpoints[stage]))
                if _finish_if_cancelled(root, run_id):
                    return _result(
                        False,
                        "cancelled",
                        project_dir=str(root),
                        run_id=run_id,
                        completed=list(completed_outputs),
                    )
            if _finish_if_cancelled(root, run_id):
                return _result(
                    False, "cancelled", project_dir=str(root), run_id=run_id, completed=list(completed_outputs)
                )
            state = _load(root)
            run = _runner(state)
            run.update({"status": "completed", "current_stage": None, "cancel_requested": False})
            _touch(run)
            try:
                _write_state(root, state)
            except WorkflowError as exc:
                if exc.code != "state_conflict" or not _finish_if_cancelled(root, run_id):
                    raise
                return _result(
                    False,
                    "cancelled",
                    project_dir=str(root),
                    run_id=run_id,
                    completed=list(completed_outputs),
                )
            return _result(True, "completed", project_dir=str(root), run_id=run_id, stages=normalized_stages)
    except Exception as exc:
        return _result(False, "error", error=_error(exc))


def _expected_outputs(root: Path, stage: str) -> list[str]:
    fixed = {
        "build": [root / "prototype.kicad_pcb"],
        "place": [root / "placed.kicad_pcb"],
        "route": [root / "routed.kicad_pcb", root / "routed.ses"],
        "preview": [root / "preview.svg"],
    }
    if stage in {"validate", "generate"}:
        return []
    return [str(path) for path in fixed[stage]]


def workflow_status(target: str | Path = ".") -> dict[str, Any]:
    return open_project(target)


def _finish_if_cancelled(root: Path, run_id: str) -> bool:
    state = _load(root)
    run = _runner(state)
    if run.get("run_id") != run_id or not run.get("cancel_requested"):
        return False
    run.update({"status": "cancelled", "current_stage": None})
    _touch(run)
    _write_state(root, state)
    return True


def resume_workflow(target: str | Path = ".", *, options: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        root = _root(target)
        run = _runner(_load(root))
        plan = run.get("plan") or {}
        return run_workflow(
            root,
            stages=plan.get("stages"),
            options=options if options is not None else plan.get("options", {}),
            resume=True,
        )
    except Exception as exc:
        return _result(False, "error", error=_error(exc))


def cancel_workflow(target: str | Path = ".") -> dict[str, Any]:
    try:
        root = _root(target)
        for _ in range(4):
            state = _load(root)
            run = _runner(state)
            if run.get("status") != "running":
                return _result(
                    True, str(run.get("status", "idle")), project_dir=str(root), message="No active run to cancel"
                )
            run["cancel_requested"] = True
            run["cancel_requested_at"] = time.time()
            _touch(run)
            try:
                _write_state(root, state)
                return _result(
                    True,
                    "cancellation_requested",
                    project_dir=str(root),
                    run_id=run.get("run_id"),
                    message="Cancellation will take effect at the next stage boundary",
                )
            except WorkflowError as exc:
                if exc.code != "state_conflict":
                    raise
        raise WorkflowError("state_conflict", "Could not record cancellation because project state kept changing")
    except Exception as exc:
        return _result(False, "error", error=_error(exc))


@contextlib.contextmanager
def workflow_lock(target: str | Path = "."):
    """Acquire the project workflow lock for an external native operation."""
    root = _root(target)
    with _RunLock(root):
        yield root


def run_stage(
    target: str | Path,
    stage: str,
    action: Callable[[], Any],
    *,
    inputs: list[str | Path],
    outputs: list[str | Path],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run and checkpoint one standalone stage while owning the shared lock."""
    try:
        # Native CLI hooks pass the exact selected input/output root. Resolving
        # descendants here can accidentally attach a vendor/source directory to
        # an unrelated nested Circuit Weaver project.
        root = Path(target).expanduser().resolve(strict=False)
        if root.exists() and not root.is_dir():
            raise WorkflowError("invalid_project_root", f"Project root is not a directory: {root}")
        if stage not in STAGES:
            raise WorkflowError("invalid_stage", f"Unknown workflow stage: {stage}")
        normalized_options = _normalize_options(options)
        missing_inputs = [path["path"] for path in _snapshot(root, inputs) if not path["exists"]]
        if missing_inputs:
            raise WorkflowError("input_missing", f"Stage input files are missing: {', '.join(missing_inputs)}")
        # Required caller inputs were checked above; an absent optional library
        # table is watched so its later creation invalidates this checkpoint.
        inputs = list(inputs)
        if stage in {"place", "route"}:
            for raw in tuple(inputs):
                board = Path(raw).expanduser()
                if not board.is_absolute():
                    board = root / board
                if board.suffix.casefold() == ".kicad_pcb":
                    inputs.extend(_native_footprint_context_inputs(board.resolve().parent))
        with _RunLock(root):
            # Load only this root's manifest. Descendant discovery is intended
            # for user-facing open/selection APIs, never for native hooks.
            state = _load_project_state_file(project_state_path(root))
            if state is None:
                state = ProjectState(name=root.name or "project", kind="native")
                save_project_state(root, state)
            run = _runner(state)
            if not run.get("run_id") or run.get("status") != "running":
                run["run_id"] = str(uuid.uuid4())
            run.update({"status": "running", "current_stage": stage})
            _touch(run)
            _write_state(root, state)
            run_id = run["run_id"]
            input_before = _snapshot(root, inputs)
            record_stage(
                root, stage, inputs=inputs, outputs=outputs, options=normalized_options, status="running", run_id=run_id
            )
            value: Any = None
            try:
                value = action()
                if (
                    isinstance(value, dict)
                    and "status" in value
                    and value.get("status") not in {"ok", "success", "completed"}
                ):
                    raise WorkflowError(
                        "stage_failed", str(value.get("message") or value.get("reason") or f"{stage} failed")
                    )
                missing_outputs = [path["path"] for path in _snapshot(root, outputs) if not path["exists"]]
                if missing_outputs:
                    raise WorkflowError(
                        "stage_output_missing",
                        f"Stage did not produce expected files: {', '.join(missing_outputs)}",
                    )
                if _snapshot(root, inputs) != input_before:
                    raise WorkflowError("input_changed", f"Input files changed while {stage} was running")
                record_stage(
                    root,
                    stage,
                    inputs=inputs,
                    outputs=outputs,
                    options=normalized_options,
                    status="completed",
                    run_id=run_id,
                    result=value,
                )
                state = _load(root)
                run = _runner(state)
                run.update({"status": "idle", "current_stage": None, "last_completed_stage": stage})
                _touch(run)
                _write_state(root, state)
                return _result(True, "completed", stage=stage, result=value, run_id=run_id)
            except Exception as exc:
                record_stage(
                    root,
                    stage,
                    inputs=inputs,
                    outputs=outputs,
                    options=normalized_options,
                    status="failed",
                    error=_error(exc),
                    run_id=run_id,
                    result=value,
                )
                state = _load(root)
                run = _runner(state)
                run.update({"status": "failed", "current_stage": stage, "last_error": _error(exc)})
                _touch(run)
                _write_state(root, state)
                return _result(False, "failed", stage=stage, error=_error(exc), result=value, run_id=run_id)
    except Exception as exc:
        return _result(False, "error", error=_error(exc))
