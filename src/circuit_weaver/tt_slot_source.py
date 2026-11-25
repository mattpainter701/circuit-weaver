"""Read-only access to official Tiny Tapeout shuttle artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import yaml

from .tt_slot_index import lookup_tt_slot

_REPO = "TinyTapeout/tinytapeout-sky-26d"
_TITLE = "Tiny Tapeout SKY 26d"
_SKU_RE = re.compile(r"^(?:tt)?sky26d$", re.IGNORECASE)
_MACRO_RE = re.compile(r"^tt_um_[A-Za-z0-9_]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_ZIP_BYTES = 100_000_000
_MAX_MEMBER_BYTES = 200_000_000


class _MissingArtifactError(ValueError):
    """A workflow run has no artifact with the requested name."""


def _gh_api(endpoint: str, *, raw: bool = False, output_path: Path | None = None) -> bytes:
    command = ["gh", "api", endpoint]
    if raw:
        command.extend(["-H", "Accept: application/vnd.github.raw+json"])
    try:
        if output_path is None:
            result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        else:
            with Path(output_path).open("wb") as output:
                result = subprocess.run(command, check=False, stdout=output, stderr=subprocess.PIPE, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"GitHub CLI request failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"GitHub API request failed for {endpoint}: {detail or 'unknown error'}")
    if output_path is not None:
        return b""
    return result.stdout


def _json_api(endpoint: str) -> Any:
    try:
        return json.loads(_gh_api(endpoint))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"GitHub returned invalid JSON for {endpoint}") from exc


def _normalize_shuttle(shuttle: str) -> tuple[str, str]:
    if not isinstance(shuttle, str) or not _SKU_RE.fullmatch(shuttle.strip()):
        raise ValueError("unsupported shuttle SKU; currently supported SKU: TTSKY26d")
    return _TITLE, _REPO


def _safe_extract(archive: bytes, expected_names: set[str], expected_digest: str, directory: Path) -> Path:
    digest = hashlib.sha256(archive).hexdigest()
    if expected_digest != f"sha256:{digest}":
        raise ValueError("downloaded artifact SHA-256 does not match GitHub artifact digest")
    if len(archive) > _MAX_ZIP_BYTES:
        raise ValueError("downloaded artifact exceeds the allowed compressed size")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        from io import BytesIO

        with zipfile.ZipFile(BytesIO(archive)) as zipped:
            matches = []
            total = 0
            for info in zipped.infolist():
                member = PurePosixPath(info.filename)
                if member.is_absolute() or ".." in member.parts or "\\" in info.filename:
                    raise ValueError("artifact contains an unsafe archive path")
                if info.file_size > _MAX_MEMBER_BYTES:
                    raise ValueError("artifact member exceeds the allowed uncompressed size")
                total += info.file_size
                if total > _MAX_MEMBER_BYTES:
                    raise ValueError("artifact exceeds the allowed total uncompressed size")
                if not info.is_dir() and (
                    member.name in expected_names
                    or ("*.oas" in expected_names and member.suffix.lower() == ".oas")
                    or ("*.oasis" in expected_names and member.suffix.lower() == ".oasis")
                ):
                    matches.append(info)
            if len(matches) != 1:
                raise ValueError("artifact must contain exactly one supported payload file")
            info = matches[0]
            payload = zipped.read(info)
    except zipfile.BadZipFile as exc:
        raise ValueError("downloaded artifact is not a valid ZIP archive") from exc
    member_name = PurePosixPath(info.filename).name
    destination_name = member_name
    if member_name not in expected_names:
        destination_name = "layout" + PurePosixPath(member_name).suffix.lower()
    destination = directory / destination_name
    destination.write_bytes(payload)
    return destination


def _artifact(repo: str, run_id: int, artifact_name: str) -> tuple[bytes, dict[str, Any]]:
    listing = _json_api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
    artifacts = listing.get("artifacts") if isinstance(listing, dict) else None
    if not isinstance(artifacts, list):
        raise ValueError("GitHub returned an invalid artifact listing")
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("name") == artifact_name]
    if not matches:
        raise _MissingArtifactError(f"run {run_id} has no {artifact_name} artifact")
    if len(matches) != 1:
        raise ValueError(f"run {run_id} has multiple {artifact_name} artifacts")
    artifact = matches[0]
    if artifact.get("expired") is True:
        raise ValueError(f"official {artifact_name} artifact has expired; no older run was substituted")
    digest = artifact.get("digest")
    artifact_id = artifact.get("id")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("GitHub artifact metadata has no supported SHA-256 digest")
    if isinstance(artifact_id, bool) or not isinstance(artifact_id, int):
        raise ValueError("GitHub artifact metadata has an invalid artifact ID")
    with tempfile.NamedTemporaryFile(prefix="cw-tt-artifact-", suffix=".zip", delete=False) as download:
        download_path = Path(download.name)
    try:
        _gh_api(f"repos/{repo}/actions/artifacts/{artifact_id}/zip", output_path=download_path)
        return download_path.read_bytes(), artifact
    finally:
        download_path.unlink(missing_ok=True)


def _runs(repo: str, workflow: str, *, event: str, status: str, head_sha: str | None = None) -> list[dict[str, Any]]:
    sha_query = f"&head_sha={quote(head_sha, safe='')}" if head_sha is not None else ""
    payload = _json_api(
        f"repos/{repo}/actions/workflows/{workflow}/runs?branch=main&event={event}&status={status}"
        f"{sha_query}&per_page=100"
    )
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        raise ValueError(f"GitHub returned an invalid {workflow} workflow run listing")
    return runs


def _successful_push(run: dict[str, Any]) -> bool:
    return (
        run.get("event") == "push"
        and run.get("conclusion") == "success"
        and run.get("head_branch") == "main"
        and isinstance(run.get("head_sha"), str)
        and _SHA_RE.fullmatch(run["head_sha"]) is not None
        and isinstance(run.get("id"), int)
        and not isinstance(run.get("id"), bool)
    )


def fetch_tt_index(shuttle: str, directory: Path) -> tuple[Path, dict[str, Any]]:
    """Fetch the latest successful main push index and verify its provenance."""
    title, repo = _normalize_shuttle(shuttle)
    runs = [run for run in _runs(repo, "gds.yaml", event="push", status="success") if _successful_push(run)]
    if not runs:
        raise ValueError("no successful main push GDS run is available for this shuttle")
    runs.sort(key=lambda run: (run.get("run_number", 0), run.get("run_attempt", 0)), reverse=True)
    run = runs[0]
    archive, artifact = _artifact(repo, run["id"], "shuttle_index")
    workflow_run = artifact.get("workflow_run", {})
    if workflow_run.get("id") != run["id"] or workflow_run.get("head_sha") != run["head_sha"]:
        raise ValueError("shuttle index artifact is not attached to the selected GDS run")
    path = _safe_extract(archive, {"shuttle_index.json"}, artifact["digest"], Path(directory))
    try:
        raw_index = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("shuttle_index artifact does not contain valid JSON") from exc
    if not isinstance(raw_index, dict) or raw_index.get("commit") != run["head_sha"]:
        raise ValueError("shuttle index provider commit does not match its successful main GDS run")
    # Reuse the strict schema and provider validation before returning fetched content.
    lookup_tt_slot(path, shuttle=title, macro="tt_um_cw_validation_probe")
    source = {
        "shuttle": title,
        "sku": shuttle,
        "repo": repo,
        "commit": run["head_sha"],
        "gds_run_id": run["id"],
        "url": f"https://github.com/{repo}/actions/runs/{run['id']}",
        "index_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    return path, source


def fetch_tt_layout(source: dict[str, Any], directory: Path) -> tuple[Path, dict[str, Any]]:
    """Fetch final OAS from the unique successful precheck run for the index SHA."""
    repo = source.get("repo") if isinstance(source, dict) else None
    commit = source.get("commit") if isinstance(source, dict) else None
    if repo != _REPO or not isinstance(commit, str) or _SHA_RE.fullmatch(commit) is None:
        raise ValueError("source metadata is not pinned to the supported Tiny Tapeout shuttle commit")
    runs = [
        run for run in _runs(repo, "precheck.yaml", event="workflow_run", status="success", head_sha=commit)
        if run.get("conclusion") == "success" and run.get("head_sha") == commit and run.get("event") == "workflow_run"
    ]
    candidates: list[tuple[dict[str, Any], dict[str, Any], bytes]] = []
    for run in runs:
        if not isinstance(run.get("id"), int) or isinstance(run.get("id"), bool):
            continue
        try:
            archive, artifact = _artifact(repo, run["id"], "final_oas")
        except _MissingArtifactError:
            continue
        workflow_run = artifact.get("workflow_run", {})
        if workflow_run.get("id") != run["id"] or workflow_run.get("head_sha") != commit:
            raise ValueError("final_oas artifact is not attached to the selected same-commit precheck run")
        candidates.append((run, artifact, archive))
    if len(candidates) != 1:
        raise ValueError(
            "expected exactly one successful same-commit precheck run with final_oas; "
            "refusing ambiguous or stale layouts"
        )
    run, artifact, archive = candidates[0]
    path = _safe_extract(archive, {"*.oas", "*.oasis"}, artifact["digest"], Path(directory))
    return path, {
        "precheck_run_id": run["id"],
        "url": f"https://github.com/{repo}/actions/runs/{run['id']}",
        "artifact": "final_oas",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_commit": commit,
    }


def fetch_tt_info(source: dict[str, Any], macro: str, directory: Path) -> Path:
    """Fetch published info.yaml for a macro from the source's pinned repo SHA."""
    if not isinstance(macro, str) or not _MACRO_RE.fullmatch(macro):
        raise ValueError("macro must be a validated Tiny Tapeout macro name")
    repo = source.get("repo") if isinstance(source, dict) else None
    commit = source.get("commit") if isinstance(source, dict) else None
    if repo != _REPO or not isinstance(commit, str) or _SHA_RE.fullmatch(commit) is None:
        raise ValueError("source metadata is not pinned to the supported Tiny Tapeout shuttle commit")
    data = _gh_api(f"repos/{repo}/contents/projects/{macro}/info.yaml?ref={commit}", raw=True)
    if not data or len(data) > 1_000_000:
        raise ValueError("published info.yaml is empty or exceeds the allowed size")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("published info.yaml is not UTF-8 text") from exc
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError("published info.yaml is not valid YAML") from exc
    project = parsed.get("project") if isinstance(parsed, dict) else None
    if not isinstance(project, dict) or project.get("top_module") != macro:
        raise ValueError("published info.yaml is missing expected Tiny Tapeout project fields")
    destination_dir = Path(directory)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{macro}-info.yaml"
    destination.write_text(content, encoding="utf-8")
    return destination
