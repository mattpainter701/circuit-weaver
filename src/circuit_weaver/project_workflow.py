"""Shared, resumable project workflow API for CLI, MCP, and Python callers.

The runner stores JSON-safe plans and content-addressed checkpoints in the
existing ``ProjectState.workflow`` mapping. Native work is performed through
the existing CLI dispatcher in a child Python process; child invocations are
marked so integrations can avoid recording duplicate checkpoints.
"""

from __future__ import annotations

import contextlib
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
    _load_project_state_file,
    file_sha256,
    get_project_state_summary,
    load_project_state,
    project_state_path,
    resolve_project_state_root,
    save_project_state,
)

STAGES = ("validate", "generate", "build", "place", "route", "preview")
DEFAULT_STAGES = STAGES
_RUNNER_KEY = "runner"
_LOCK_NAME = "workflow.lock"
_CHILD_ENV = "CIRCUIT_WEAVER_WORKFLOW_CHILD"
_STAGE_TIMEOUTS = {"build": 300, "place": 300, "route": 1800, "preview": 180}
_APPEND_ONLY_OUTPUTS = {"design.log", "circuit-weaver.log"}


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str, choices: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.choices = choices


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


def _spec_path(root: Path) -> Path:
    candidate = root / "design.yaml"
    if candidate.is_file():
        return candidate
    candidates = sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml")) + sorted(root.glob("*.json"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise WorkflowError("spec_missing", f"No design spec found in {root}")
    raise WorkflowError("ambiguous_spec", f"Multiple design specs found in {root}; use design.yaml")


def _has_spec(root: Path) -> bool:
    return (
        (root / "design.yaml").is_file()
        or any(root.glob("*.yaml"))
        or any(root.glob("*.yml"))
        or any(root.glob("*.json"))
    )


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
                    if path.is_file()
                    and path.suffix.casefold() in {".kicad_pro", ".kicad_sch", ".kicad_pcb", ".gbr", ".zip"}
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
            )
        summary = get_project_state_summary(root)
        run = _runner(state)
        return _result(
            True,
            str(run.get("status", summary.get("status", "open"))),
            project_dir=str(root),
            summary=summary,
            workflow=run,
        )
    except Exception as exc:
        return _result(False, "error", error=_error(exc), choices=getattr(exc, "choices", None))


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
    try:
        normalized = json.loads(json.dumps(options, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise WorkflowError("invalid_options", "options must contain JSON-safe values") from exc
    for key in ("clearance_mm", "minimum_drill_mm"):
        if key not in normalized:
            continue
        value = normalized[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise WorkflowError("invalid_options", f"{key} must be a finite positive number")
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
    if proc.returncode:
        raise WorkflowError(
            "stage_failed", proc.stderr[-4000:] or proc.stdout[-4000:] or f"{stage} exited {proc.returncode}"
        )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError("invalid_stage_output", f"{stage} returned no machine-readable result") from exc
    if not isinstance(result, dict):
        raise WorkflowError("invalid_stage_output", f"{stage} returned a non-object result")
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
        return {"status": "ok", "valid": True}, []
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
            has_design = (
                (root / "design.yaml").is_file()
                or any(root.glob("*.yaml"))
                or any(root.glob("*.yml"))
                or any(root.glob("*.json"))
            )
            design = _spec_path(root) if has_design else None
            if design is not None:
                args.extend(["--design", str(design)])
            result = _cli(stage, args, root, _STAGE_TIMEOUTS[stage])
            _require_ok(result, stage)
            outputs = _publish_attempt(attempt, root, out, root / out.name, result)
        elif stage == "route":
            attempt = _attempt_dir(root, stage)
            out = attempt / "routed.ses"
            routed_board = attempt / "routed.kicad_pcb"
            args = [
                "autoroute",
                str(board),
                "--output",
                str(out),
                "--routed-board",
                str(routed_board),
                "--timeout",
                str(min(max(float(options.get("route_timeout", 300)), 1), 1800)),
                *_tool_args(options, "freerouting_path", "kicad_cli_path", "kicad_python_path"),
            ]
            result = _cli(
                stage, args, root, min(max(int(options.get("route_timeout", 300)) + 30, 60), _STAGE_TIMEOUTS[stage])
            )
            _require_ok(result, stage)
            outputs = _publish_attempt(
                attempt, root, routed_board, root / routed_board.name, result, extras=[(out, root / out.name)]
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
) -> list[str]:
    staged = [(primary, destination)]
    staged.extend(
        (primary.with_suffix(suffix), destination.with_suffix(suffix))
        for suffix in (".kicad_pro", ".kicad_dru")
        if primary.with_suffix(suffix).is_file()
    )
    staged.extend(extras or [])
    for source, target in staged:
        if not source.is_file():
            raise WorkflowError("stage_output_missing", f"Expected staged output is missing: {source}")
        if target.exists():
            raise WorkflowError("output_exists", f"Refusing to replace existing workflow output: {target}")
    published: list[Path] = []
    linked_sources: dict[Path, tuple[int, int]] = {}
    try:
        for source, target in staged:
            # Hard-link creation is atomic and fails if a concurrent writer
            # created the destination after our preflight check.
            os.link(source, target)
            published.append(target)
            source_stat = source.stat()
            linked_sources[source] = (source_stat.st_dev, source_stat.st_ino)
    except OSError as exc:
        for target in reversed(published):
            try:
                target_stat = target.stat()
                source = next(source for source, candidate in staged if candidate == target)
                source_stat = source.stat()
                if (target_stat.st_dev, target_stat.st_ino) == (source_stat.st_dev, source_stat.st_ino):
                    target.unlink(missing_ok=True)
            except OSError:
                pass
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
    if stage == "build":
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
            if stage == "place" and _project_has_spec(root):
                paths.append(_spec_path(root))
        except WorkflowError:
            raise
    paths.extend(completed_outputs)
    return [path for path in paths if Path(path).name.casefold() not in _APPEND_ONLY_OUTPUTS]


def _project_has_spec(root: Path) -> bool:
    return (
        (root / "design.yaml").is_file()
        or any(root.glob("*.yaml"))
        or any(root.glob("*.yml"))
        or any(root.glob("*.json"))
    )


def _hierarchical_schematic_inputs(root: Path, selected_root: Path) -> list[Path]:
    search_root = root / "output" if selected_root.is_relative_to(root / "output") else root
    return sorted(
        (path for path in search_root.rglob("*.kicad_sch") if path.is_file()),
        key=lambda path: str(path).casefold(),
    )


def _custom_footprint_inputs(root: Path) -> list[Path]:
    paths = [*root.glob("fp-lib-table"), *(root / "output").glob("fp-lib-table")]
    paths.extend(root.glob("sym-lib-table"))
    paths.extend((root / "output").glob("sym-lib-table"))
    for base in (root, root / "output"):
        if base.is_dir():
            paths.extend(path for path in base.rglob("*.kicad_mod") if path.is_file())
            paths.extend(path for path in base.rglob("*.kicad_sym") if path.is_file())
    return sorted(set(paths), key=lambda path: str(path).casefold())


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
                        result=locals().get("result"),
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
