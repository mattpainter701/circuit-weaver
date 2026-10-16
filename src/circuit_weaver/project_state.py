"""Durable, portable project state for Circuit Weaver workflows.

``design.log`` remains an append-only diagnostic timeline.  This module keeps
the smaller piece of state needed to reliably reopen a project: what kind of
project it is, which source files belong to it, which analyses completed, and
what a user can do next.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR_NAME = ".circuit-weaver"
STATE_FILE_NAME = "project.json"
STATE_LOCK_FILE_NAME = "project.lock"
STATE_SCHEMA_VERSION = 1

_GERBER_SUFFIXES = frozenset(
    {
        ".gbr",
        ".gtl",
        ".gbl",
        ".gts",
        ".gbs",
        ".gtp",
        ".gbp",
        ".gto",
        ".gbo",
        ".gko",
        ".gm1",
        ".gml",
    }
)
_PROTEL_INTERNAL_OR_MECHANICAL_SUFFIX = re.compile(r"\.g(?:\d+|p\d+|m\d+)\Z", re.IGNORECASE)
_APPEND_ONLY_GENERATION_LOGS = frozenset({"design.log", "circuit-weaver.log"})
_STATE_DESCENDANT_SEARCH_DEPTH = 3
_STATE_SEARCH_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "site-packages",
        "venv",
    }
)


def manufacturing_artwork_kind(path: str | Path) -> str | None:
    """Classify artwork understood by the bundled Gerber analyzer.

    The explicit extensions cover the standard Protel/KiCad copper, mask,
    paste, legend, outline, drill, and job files. Numbered ``.g1``/``.gp1``
    and ``.gm2`` variants cover internal plane and mechanical layers without
    treating every arbitrary ``.g*`` file as manufacturing artwork.
    """

    suffix = Path(path).suffix.casefold()
    if suffix == ".drl":
        return "drill"
    if suffix == ".gbrjob":
        return "gerber_job"
    if suffix in _GERBER_SUFFIXES or _PROTEL_INTERNAL_OR_MECHANICAL_SUFFIX.fullmatch(suffix):
        return "gerber"
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _absolute(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = Path.cwd() / value
    return value.resolve(strict=False)


def project_state_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / STATE_DIR_NAME / STATE_FILE_NAME


class ConcurrentProjectStateUpdate(RuntimeError):
    """A caller attempted to save a state snapshot that is no longer current."""


class AmbiguousProjectStateError(ValueError):
    """More than one generated child project could own a lookup target."""


@contextmanager
def _project_state_lock(project_dir: Path, *, timeout: float = 10.0):
    """Hold a dependency-free cross-process lock for one project manifest."""

    lock_path = project_dir / STATE_DIR_NAME / STATE_LOCK_FILE_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        acquired = False
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for project-state lock: {lock_path}") from exc
                time.sleep(0.05)

        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _file_prefix_sha256(path: Path, size: int, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash exactly the recorded prefix of an append-only artifact."""

    remaining = size
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: str | Path, payload: dict[str, Any]) -> None:
    """Public atomic JSON writer used by durable project artifacts."""

    _atomic_write_json(Path(path), payload)


def _has_project_marker(directory: Path) -> bool:
    if project_state_path(directory).is_file() or (directory / "design.yaml").is_file():
        return True
    try:
        return any(directory.glob("*.kicad_pro"))
    except OSError:
        return False


def resolve_project_root(target: str | Path = ".") -> Path:
    """Resolve a file or directory to its nearest Circuit Weaver project root.

    Existing manifests win, followed by ``design.yaml`` and KiCad project
    markers.  If no marker exists, a directory resolves to itself and a file
    resolves to its parent.  The latter makes the function useful while a new
    import project is being initialized.
    """

    candidate = _absolute(target)
    start = candidate.parent if candidate.is_file() or (not candidate.exists() and candidate.suffix) else candidate

    for directory in (start, *start.parents):
        if project_state_path(directory).is_file():
            return directory
    for directory in (start, *start.parents):
        if _has_project_marker(directory):
            return directory
    return start


@dataclass
class ProjectState:
    schema_version: int = STATE_SCHEMA_VERSION
    revision: int = 0
    project_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "project"
    kind: str = "circuit_weaver"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    status: str = "new"
    current_phase: str = "initialized"
    next_actions: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    analyses: dict[str, dict[str, Any]] = field(default_factory=dict)
    workflow: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProjectState:
        known = {
            "schema_version",
            "revision",
            "project_id",
            "name",
            "kind",
            "created_at",
            "updated_at",
            "status",
            "current_phase",
            "next_actions",
            "sources",
            "artifacts",
            "analyses",
            "workflow",
            "last_error",
        }
        values = {key: payload[key] for key in known if key in payload}
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_project_state_file(path: Path) -> ProjectState | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read project state at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Project state at {path} must be a JSON object")
    version = int(payload.get("schema_version", 0) or 0)
    if version > STATE_SCHEMA_VERSION:
        raise ValueError(
            f"Project state schema {version} is newer than supported schema {STATE_SCHEMA_VERSION}"
        )
    return ProjectState.from_dict(payload)


def _descendant_project_state_roots(start: Path) -> list[Path]:
    """Find nearby child project manifests without crawling an entire drive.

    Standalone specs normally generate into ``output/`` (or another shallow
    child) because there is no parent ``design.yaml`` marker.  Read operations
    need to reconnect that source to its generated state, while mutation paths
    continue to use :func:`resolve_project_root` and never guess a child.
    """

    if not start.is_dir():
        return []

    found: list[Path] = []
    pending: list[tuple[Path, int]] = [(start, 0)]
    while pending:
        directory, depth = pending.pop()
        if depth and project_state_path(directory).is_file():
            found.append(directory)
            # A nested manifest is its own boundary. Do not select projects
            # buried inside its private state/import directories as peers.
            continue
        if depth >= _STATE_DESCENDANT_SEARCH_DEPTH:
            continue
        try:
            children = sorted(
                (
                    child
                    for child in directory.iterdir()
                    if child.is_dir()
                    and not child.is_symlink()
                    and child.name.casefold() not in _STATE_SEARCH_IGNORED_DIRS
                    and child.name != STATE_DIR_NAME
                ),
                key=lambda child: child.name.casefold(),
                reverse=True,
            )
        except OSError:
            continue
        pending.extend((child, depth + 1) for child in children)
    return sorted(found, key=lambda root: str(root).casefold())


def _state_source_paths(root: Path, state: ProjectState) -> set[Path]:
    """Return normalized generation-source paths referenced by one manifest."""

    values: set[str] = set()
    workflow_source = str((state.workflow.get("generate") or {}).get("source", "")).strip()
    if workflow_source:
        values.add(workflow_source)
    for record in state.sources:
        if record.get("role") == "generation_source":
            value = str(record.get("path", "")).strip()
            if value:
                values.add(value)

    resolved: set[Path] = set()
    for value in values:
        source = Path(value).expanduser()
        if not source.is_absolute():
            source = root / source
        resolved.add(source.resolve(strict=False))
    return resolved


def _state_matches_lookup_target(root: Path, state: ProjectState, target: Path, *, is_file: bool) -> bool:
    sources = _state_source_paths(root, state)
    if is_file:
        return target in sources
    return any(source.parent == target for source in sources)


def _ambiguous_state_error(target: Path, roots: list[Path]) -> AmbiguousProjectStateError:
    manifests = "\n".join(f"  - {project_state_path(root)}" for root in roots)
    return AmbiguousProjectStateError(
        f"Multiple Circuit Weaver project states could match {target}. "
        "Pass the intended generated project directory explicitly:\n"
        f"{manifests}"
    )


def resolve_project_state_root(target: str | Path = ".") -> Path:
    """Resolve read-only status/resume lookups to authoritative saved state.

    Exact state still wins.  When a standalone source spec wrote state beneath
    a child output directory, a unique manifest is selected; multiple child
    manifests are selected only when exactly one records the requested source.
    Ambiguous parents fail explicitly instead of being presented as a newly
    synthesized ``kicad_native`` project.
    """

    candidate = _absolute(target)
    is_file = candidate.is_file() or (not candidate.exists() and bool(candidate.suffix))
    start = candidate.parent if is_file else candidate

    if project_state_path(start).is_file():
        return start

    descendants = _descendant_project_state_roots(start)
    if descendants:
        matches: list[Path] = []
        for root in descendants:
            try:
                state = _load_project_state_file(project_state_path(root))
            except ValueError:
                # A sole corrupt state is returned below so its parse error is
                # surfaced. With peers present it cannot safely disambiguate.
                continue
            if state is not None and _state_matches_lookup_target(
                root,
                state,
                candidate if is_file else start,
                is_file=is_file,
            ):
                matches.append(root)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise _ambiguous_state_error(candidate, matches)
        if len(descendants) > 1:
            raise _ambiguous_state_error(candidate, descendants)
        # A sole nearby manifest is not sufficient evidence that it owns this
        # lookup target.  In particular, status on an arbitrary KiCad file must
        # never jump into an unrelated generated sibling merely because it is
        # the only saved project below the same parent.

    for directory in start.parents:
        if project_state_path(directory).is_file():
            return directory
    return resolve_project_root(candidate)


def load_project_state(target: str | Path) -> ProjectState | None:
    candidate = _absolute(target)
    exact_root = candidate.parent if candidate.is_file() else candidate
    exact = _load_project_state_file(project_state_path(exact_root))
    if exact is not None:
        return exact
    root = resolve_project_state_root(candidate)
    return _load_project_state_file(project_state_path(root))


def save_project_state(project_dir: str | Path, state: ProjectState) -> Path:
    root = _absolute(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = project_state_path(root)
    with _project_state_lock(root):
        current = _load_project_state_file(path)
        if current is not None:
            if current.project_id != state.project_id:
                raise ConcurrentProjectStateUpdate(
                    f"Project state at {path} belongs to a different project; reload before saving"
                )
            if current.revision != state.revision:
                raise ConcurrentProjectStateUpdate(
                    f"Project state at {path} changed from revision {state.revision} "
                    f"to {current.revision}; reload and retry"
                )

        prior_schema = state.schema_version
        prior_revision = state.revision
        prior_updated_at = state.updated_at
        state.schema_version = STATE_SCHEMA_VERSION
        state.revision = prior_revision + 1
        state.updated_at = _now_iso()
        try:
            _atomic_write_json(path, state.to_dict())
        except BaseException:
            state.schema_version = prior_schema
            state.revision = prior_revision
            state.updated_at = prior_updated_at
            raise
    return path


def ensure_project_state(
    project_dir: str | Path,
    *,
    name: str | None = None,
    kind: str | None = None,
) -> ProjectState:
    root = _absolute(project_dir)
    existing = _load_project_state_file(project_state_path(root))
    if existing is not None:
        if name:
            existing.name = name
        if kind:
            existing.kind = kind
        return existing
    return ProjectState(name=name or root.name or "project", kind=kind or "circuit_weaver")


def _portable_file_record(root: Path, path: str | Path, *, kind: str) -> dict[str, Any] | None:
    candidate = _absolute(path)
    if not candidate.is_file():
        return None
    external = not candidate.is_relative_to(root)
    stored_path = str(candidate) if external else candidate.relative_to(root).as_posix()
    try:
        size = candidate.stat().st_size
        digest = file_sha256(candidate)
    except OSError:
        return None
    return {
        "path": stored_path,
        "external": external,
        "kind": kind,
        "size": size,
        "sha256": digest,
    }


def record_generation_state(
    project_dir: str | Path,
    *,
    project_name: str,
    spec_path: str | Path | None,
    output_dir: str | Path,
    phase: str,
    artifacts: list[str | Path] | None = None,
    validation_report: str | Path | None = None,
    placement_review: str | Path | None = None,
    error: str = "",
    project_root: str | Path | None = None,
) -> Path:
    """Atomically record a restartable generate workflow transition.

    ``phase`` is one of ``running``, ``generated``, or ``failed``. Generated
    artifacts are fingerprinted and stored relative to the project whenever
    possible so moving a project does not discard its restart context.
    """
    if phase not in {"running", "generated", "failed"}:
        raise ValueError(f"Unsupported generation phase: {phase}")

    root = _absolute(project_root) if project_root is not None else resolve_project_root(project_dir)
    state = ensure_project_state(root, name=project_name)
    output = _absolute(output_dir)
    spec = _absolute(spec_path) if spec_path is not None else None

    if spec is not None:
        source_record = _portable_file_record(root, spec, kind="design_spec")
        if source_record is not None:
            source_record["role"] = "generation_source"
            state.sources = [item for item in state.sources if item.get("role") != "generation_source"]
            state.sources.append(source_record)

    prior = dict(state.workflow.get("generate") or {})
    workflow = {
        "status": phase,
        "source": str(spec) if spec is not None else "",
        "output_dir": str(output),
        "started_at": prior.get("started_at") or _now_iso(),
        "updated_at": _now_iso(),
    }
    if phase in {"generated", "failed"}:
        workflow["completed_at"] = _now_iso()

    validation_record = (
        _portable_file_record(root, validation_report, kind="validation_report")
        if validation_report is not None
        else None
    )
    if validation_record is not None:
        workflow["validation_report"] = validation_record["path"]
        try:
            validation_payload = json.loads(_absolute(validation_report).read_text(encoding="utf-8"))
            workflow["validation_valid"] = bool(validation_payload.get("valid"))
        except (OSError, json.JSONDecodeError):
            pass

    placement_record = (
        _portable_file_record(root, placement_review, kind="placement_review")
        if placement_review is not None
        else None
    )
    if placement_record is not None:
        workflow["placement_review"] = placement_record["path"]

    state.workflow["generate"] = workflow
    if artifacts is not None:
        generated_records = []
        for artifact in artifacts:
            artifact_path = _absolute(artifact)
            is_append_only_log = artifact_path.name.casefold() in _APPEND_ONLY_GENERATION_LOGS
            record = _portable_file_record(
                root,
                artifact_path,
                kind="generation_log" if is_append_only_log else "generated_artifact",
            )
            if record is not None:
                record["workflow"] = "generate"
                if is_append_only_log:
                    record.update(
                        {
                            "blocking": False,
                            "mutable": True,
                            "reconciliation_policy": "append_only",
                        }
                    )
                generated_records.append(record)
        state.artifacts = [item for item in state.artifacts if item.get("workflow") != "generate"]
        state.artifacts.extend(sorted(generated_records, key=lambda item: str(item["path"]).casefold()))

    quoted_root = f'"{root}"'
    quoted_spec = f'"{spec}"' if spec is not None else ""
    quoted_output = f'"{output}"'
    if phase == "running":
        state.status = "in_progress"
        state.current_phase = "generating"
        state.last_error = ""
        retry = f"circuit-weaver generate {quoted_spec} -o {quoted_output}" if quoted_spec else ""
        state.next_actions = [
            action for action in (f"circuit-weaver status {quoted_root}", retry) if action
        ]
    elif phase == "failed":
        state.status = "generation_failed"
        state.current_phase = "generation_failed"
        state.last_error = error[:2000]
        retry = f"circuit-weaver generate {quoted_spec} -o {quoted_output}" if quoted_spec else ""
        state.next_actions = [action for action in (retry, f"circuit-weaver status {quoted_root}") if action]
    else:
        state.status = "generated"
        state.current_phase = "placement_review" if placement_record is not None else "generated"
        state.last_error = ""
        actions = [f"circuit-weaver status {quoted_root}"]
        if placement_record is not None:
            actions.insert(0, f"Review {placement_record['path']}")
        state.next_actions = actions

    return save_project_state(root, state)


def _iter_project_files(root: Path) -> list[Path]:
    result: list[Path] = []
    ignored_dirs = {STATE_DIR_NAME, ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
    try:
        paths = root.rglob("*")
        for path in paths:
            if not path.is_file() or any(part in ignored_dirs for part in path.relative_to(root).parts):
                continue
            result.append(path)
    except (OSError, ValueError):
        return result
    return result


def _read_validation_state(root: Path) -> dict[str, Any] | None:
    log_path = root / "design.log"
    if not log_path.is_file():
        return None
    latest: dict[str, Any] | None = None
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "validation":
                    continue
                scope = str(entry.get("scope", "legacy"))
                if scope == "final_report" or latest is None:
                    latest = entry
    except OSError:
        return None
    if latest is None:
        return None
    return {
        "passed": bool(latest.get("passed")),
        "error_count": int(latest.get("error_count", len(latest.get("errors", [])) or 0)),
        "warning_count": int(latest.get("warning_count", len(latest.get("warnings", [])) or 0)),
        "timestamp": latest.get("timestamp", ""),
    }


def _inventory(root: Path) -> dict[str, Any]:
    files = _iter_project_files(root)
    suffixes = [path.suffix.lower() for path in files]
    return {
        "design_specs": sum(path.name.lower() in {"design.yaml", "design.yml"} for path in files),
        "kicad_projects": suffixes.count(".kicad_pro"),
        "schematics": suffixes.count(".kicad_sch") + suffixes.count(".sch"),
        "pcbs": suffixes.count(".kicad_pcb"),
        "gerber_files": sum(manufacturing_artwork_kind(path) is not None for path in files),
        "reports": suffixes.count(".html") + suffixes.count(".md"),
    }


def _merge_recorded_source_inventory(
    inventory: dict[str, Any], state: ProjectState | None
) -> dict[str, Any]:
    """Include non-copied external import sources in user-facing file counts."""
    if state is None:
        return inventory
    recorded = {
        "design_specs": 0,
        "kicad_projects": 0,
        "schematics": 0,
        "pcbs": 0,
        "gerber_files": 0,
        "reports": 0,
    }
    for source in state.sources:
        kind = str(source.get("kind", ""))
        if kind == "design_spec":
            recorded["design_specs"] += 1
        elif kind == "kicad_project":
            recorded["kicad_projects"] += 1
        elif kind == "schematic":
            recorded["schematics"] += 1
        elif kind == "pcb":
            recorded["pcbs"] += 1
        elif kind in {"gerber", "drill", "gerber_job"}:
            recorded["gerber_files"] += 1
    return {key: max(int(inventory.get(key, 0)), value) for key, value in recorded.items()}


def _record_path(root: Path, record: dict[str, Any]) -> Path:
    value = Path(str(record.get("path", "")))
    if record.get("external") or value.is_absolute():
        return value
    return root / value


def _reconcile_records(
    root: Path,
    records: list[dict[str, Any]],
    *,
    record_type: str,
) -> list[dict[str, Any]]:
    reconciled: list[dict[str, Any]] = []
    for record in records:
        path = _record_path(root, record)
        expected_size = record.get("size")
        expected_hash = str(record.get("sha256", ""))
        policy = str(record.get("reconciliation_policy", "immutable"))
        status = "clean"
        actual_size: int | None = None
        actual_hash = ""
        error = ""
        try:
            if not path.is_file():
                status = "missing"
            else:
                actual_size = path.stat().st_size
                if policy == "append_only" and expected_size is not None and expected_hash:
                    recorded_size = int(expected_size)
                    if actual_size < recorded_size:
                        status = "modified"
                    else:
                        actual_hash = _file_prefix_sha256(path, recorded_size)
                        if actual_hash != expected_hash:
                            status = "modified"
                        elif actual_size > recorded_size:
                            status = "appended"
                elif expected_size is not None and actual_size != int(expected_size):
                    status = "modified"
                elif expected_hash:
                    actual_hash = file_sha256(path)
                    if actual_hash != expected_hash:
                        status = "modified"
                else:
                    status = "unverified"
        except OSError as exc:
            status = "unreadable"
            error = str(exc)
        reconciled.append(
            {
                "path": str(record.get("path", "")),
                "kind": str(record.get("kind", record_type)),
                "status": status,
                "blocking": bool(
                    record.get(
                        "blocking",
                        str(record.get("kind", "")) != "source_archive" and not record.get("mutable"),
                    )
                ),
                "mutable": bool(record.get("mutable")),
                "reconciliation_policy": policy,
                "expected_size": expected_size,
                "actual_size": actual_size,
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "error": error,
            }
        )
    return reconciled


def _reconciliation(root: Path, state: ProjectState | None) -> dict[str, Any]:
    if state is None:
        return {
            "dirty": False,
            "sources": [],
            "artifacts": [],
            "summary": {
                "clean": 0,
                "appended": 0,
                "modified": 0,
                "missing": 0,
                "unreadable": 0,
                "unverified": 0,
            },
        }
    sources = _reconcile_records(root, state.sources, record_type="source")
    artifacts = _reconcile_records(root, state.artifacts, record_type="artifact")
    counts = {
        "clean": 0,
        "appended": 0,
        "modified": 0,
        "missing": 0,
        "unreadable": 0,
        "unverified": 0,
    }
    for record in [*sources, *artifacts]:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    dirty = any(
        record["blocking"] and record["status"] in {"modified", "missing", "unreadable"}
        for record in [*sources, *artifacts]
    )
    return {"dirty": dirty, "sources": sources, "artifacts": artifacts, "summary": counts}


def _reconciled_status(
    base_status: str,
    state: ProjectState | None,
    reconciliation: dict[str, Any],
) -> str:
    if state is None:
        return base_status
    bad_sources = [
        item
        for item in reconciliation["sources"]
        if item["blocking"] and item["status"] in {"modified", "missing", "unreadable"}
    ]
    if any(item["status"] in {"missing", "unreadable"} for item in bad_sources):
        return "source_missing"
    if bad_sources:
        return "source_changed"
    bad_artifacts = [
        item
        for item in reconciliation["artifacts"]
        if item["blocking"] and item["status"] in {"modified", "missing", "unreadable"}
    ]
    if any(item["status"] in {"missing", "unreadable"} for item in bad_artifacts):
        return "artifacts_missing"
    if bad_artifacts:
        return "artifacts_changed"
    return base_status


def _recovery_actions(root: Path, state: ProjectState, status: str) -> list[str]:
    quoted_root = f'"{root}"'
    source = str((state.workflow.get("generate") or {}).get("source", ""))
    output = str((state.workflow.get("generate") or {}).get("output_dir", ""))
    if state.kind.startswith("imported"):
        if status == "source_missing":
            import_source = str((state.workflow.get("import") or {}).get("source", ""))
            if import_source:
                return [
                    f'circuit-weaver import-design "{import_source}" --project-dir {quoted_root} --force'
                ]
            return [f"circuit-weaver status {quoted_root}"]
        return [f"circuit-weaver analyze-design {quoted_root} --force"]
    if source and output:
        return [
            f'circuit-weaver generate "{source}" -o "{output}"',
            f"circuit-weaver status {quoted_root}",
        ]
    return [f"circuit-weaver status {quoted_root}"]


def _derived_status(state: ProjectState | None, inventory: dict[str, Any], validation: dict[str, Any] | None) -> str:
    if state is not None and state.status in {
        "analysis_failed",
        "analyzing",
        "generation_failed",
        "import_failed",
        "in_progress",
    }:
        return state.status
    if state is not None and state.kind.startswith("imported"):
        analysis_entries = list(state.analyses.values())
        if analysis_entries and all(entry.get("status") == "ok" for entry in analysis_entries):
            return "analyzed"
        return state.status if state.status not in {"new", "initialized"} else "imported"
    if state is not None and state.status == "generated":
        # Validation happens before generation and its append-only log entry
        # remains useful evidence, but it must not downgrade a later durable
        # generation/placement-review transition back to "validated".
        return "generated"
    if validation and validation.get("passed"):
        return "validated"
    if inventory["gerber_files"]:
        return "fabrication_files"
    if inventory["pcbs"]:
        return "pcb"
    if inventory["schematics"]:
        return "generated"
    if inventory["design_specs"]:
        return "in_progress"
    return state.status if state is not None else "new"


def _next_actions(root: Path, kind: str, status: str) -> list[str]:
    quoted = f'"{root}"'
    if kind.startswith("imported"):
        if status == "analyzed":
            return [
                f"circuit-weaver status {quoted}",
                f"Review {root / STATE_DIR_NAME / 'analysis' / 'index.json'}",
            ]
        return [f"circuit-weaver analyze-design {quoted}", f"circuit-weaver status {quoted}"]
    spec = root / "design.yaml"
    if not spec.exists():
        return [f"circuit-weaver import-design {quoted}"]
    if status == "in_progress":
        return [f'circuit-weaver validate "{spec}"', f'circuit-weaver generate "{spec}" -o "{root / "output"}"']
    if status == "validated":
        return [f'circuit-weaver generate "{spec}" -o "{root / "output"}"']
    return [f"circuit-weaver status {quoted}", f'circuit-weaver validate "{spec}"']


def get_project_state_summary(target: str | Path) -> dict[str, Any]:
    """Return reconciled state without requiring a manifest to already exist."""

    root = resolve_project_state_root(target)
    if not root.exists():
        raise FileNotFoundError(f"Project path does not exist: {root}")
    # ``root`` has already been resolved against the original lookup target.
    # Loading it through ``load_project_state`` would perform a second child
    # search with less-specific directory evidence and could jump from an
    # unrelated file to a neighboring generated project.
    state = _load_project_state_file(project_state_path(root))
    inventory = _merge_recorded_source_inventory(_inventory(root), state)
    validation = _read_validation_state(root)
    kind = state.kind if state is not None else (
        "circuit_weaver" if inventory["design_specs"] else "kicad_native"
    )
    reconciliation = _reconciliation(root, state)
    status = _reconciled_status(_derived_status(state, inventory, validation), state, reconciliation)
    if state is not None and reconciliation["dirty"]:
        actions = _recovery_actions(root, state, status)
    else:
        actions = (
            list(state.next_actions)
            if state is not None and state.next_actions
            else _next_actions(root, kind, status)
        )
    return {
        "project_root": str(root),
        "state_file": str(project_state_path(root)),
        "has_manifest": state is not None,
        "project_id": state.project_id if state is not None else "",
        "name": state.name if state is not None else root.name,
        "kind": kind,
        "status": status,
        "current_phase": state.current_phase if state is not None else status,
        "updated_at": state.updated_at if state is not None else "",
        "inventory": inventory,
        "validation": validation,
        "sources": list(state.sources) if state is not None else [],
        "artifacts": list(state.artifacts) if state is not None else [],
        "analyses": dict(state.analyses) if state is not None else {},
        "reconciliation": reconciliation,
        "last_error": state.last_error if state is not None else "",
        "next_actions": actions,
    }


def resume_project(target: str | Path) -> dict[str, Any]:
    """Return an actionable, deterministic restart plan for a project."""

    summary = get_project_state_summary(target)
    inventory = summary["inventory"]
    resumable = bool(
        summary["has_manifest"]
        or inventory["design_specs"]
        or inventory["kicad_projects"]
        or inventory["schematics"]
        or inventory["pcbs"]
        or inventory["gerber_files"]
    )
    return {
        **summary,
        "resumable": resumable,
        "message": (
            f"Resume {summary['name']} from phase '{summary['current_phase']}'."
            if resumable
            else "No resumable Circuit Weaver or KiCad project state was found."
        ),
    }


def format_project_state(summary: dict[str, Any]) -> str:
    inventory = summary.get("inventory", {})
    lines = [
        "Design Project State",
        "=" * 72,
        f"Project:      {summary.get('name', 'project')}",
        f"Root:         {summary.get('project_root', '')}",
        f"Type:         {summary.get('kind', 'unknown')}",
        f"Status:       {str(summary.get('status', 'unknown')).upper()}",
        f"Current phase:{summary.get('current_phase', 'unknown')}",
        (
            "Files:        "
            f"{inventory.get('schematics', 0)} schematic(s), "
            f"{inventory.get('pcbs', 0)} PCB(s), {inventory.get('gerber_files', 0)} Gerber/drill file(s)"
        ),
    ]
    validation = summary.get("validation")
    if validation:
        verdict = "PASSED" if validation.get("passed") else "FAILED"
        lines.append(
            f"Validation:   {verdict} ({validation.get('error_count', 0)} errors, "
            f"{validation.get('warning_count', 0)} warnings)"
        )
    if summary.get("last_error"):
        lines.append(f"Last error:   {summary['last_error']}")
    reconciliation = summary.get("reconciliation", {})
    if reconciliation.get("dirty"):
        counts = reconciliation.get("summary", {})
        lines.append(
            "Reconciliation: DIRTY "
            f"({counts.get('modified', 0)} modified, {counts.get('missing', 0)} missing, "
            f"{counts.get('unreadable', 0)} unreadable)"
        )
    actions = summary.get("next_actions", [])
    if actions:
        lines.extend(["", "Next actions:"])
        lines.extend(f"  {index}. {action}" for index, action in enumerate(actions, 1))
    return "\n".join(lines)
