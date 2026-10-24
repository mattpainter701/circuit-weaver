"""KiCad PCB DRC runner producing only the shared T248 finding model."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .evidence import EvidenceLedger, EvidenceSource
from .kicad_project_context import (
    _hash_file_limited,
    _sha256_limited,
    inspect_project_footprint_context,
    stage_installed_footprint_context,
    stage_project_footprint_context,
)
from .pcb_contracts import PcbConstraint, drc_validation_issue
from .validator import ValidationIssue


@dataclass(frozen=True)
class DrcResult:
    """Operational result plus T248 findings; no DRC-specific violation type."""

    status: str
    board: str
    board_sha256: str = ""
    tool_version: str = ""
    evidence_id: str = ""
    findings: tuple[ValidationIssue, ...] = ()
    raw_report: Mapping[str, Any] | None = None
    failure_reason: str = ""
    library_context: Mapping[str, Any] | None = None

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "blocker" and not finding.suppressed)

    @property
    def passed(self) -> bool:
        return self.status == "ok" and self.blocker_count == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "board": self.board,
            "board_sha256": self.board_sha256,
            "tool_version": self.tool_version,
            "evidence_id": self.evidence_id,
            "blocker_count": self.blocker_count,
            "passed": self.passed,
            "failure_reason": self.failure_reason,
            "library_context": _public_library_context(self.library_context),
            "findings": [finding.to_dict() for finding in self.findings],
        }


_KNOWN_RULE_NUMBERS = {
    "clearance": 1,
    "unconnected_items": 2,
    "track_width": 3,
    "via_size": 4,
    "hole_size": 5,
    "board_edge": 6,
    "diff_pair_gap": 7,
    "diff_pair_uncoupled_length": 8,
    "length": 9,
    "skew": 10,
    "courtyard_overlap": 11,
    "lib_footprint_issues": 12,
    "schematic_parity": 13,
}


def _kicad_cli_path() -> Path | None:
    configured = os.environ.get("CIRCUIT_WEAVER_KICAD_CLI")
    if configured:
        selected = shutil.which(configured) or configured
        candidate = Path(selected)
        return candidate if candidate.is_file() else None
    from_path = shutil.which("kicad-cli")
    if from_path:
        return Path(from_path)
    for version in ("10.0", "9.0", "8.0"):
        candidate = Path(f"C:/Program Files/KiCad/{version}/bin/kicad-cli.exe")
        if candidate.is_file():
            return candidate
    return None


def _kicad_cli_major(cli: Path, timeout: int) -> int | None:
    """Ask the selected executable which versioned config directory it reads."""
    try:
        result = subprocess.run(
            [str(cli), "version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=min(timeout, 15),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    version_text = result.stdout.strip() or result.stderr.strip()
    match = re.search(r"(?:^|\s)(8|9|10)\.\d+(?:\.\d+)?(?:\s|$)", version_text)
    return int(match.group(1)) if match else None


def _public_library_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    public = {key: value for key, value in (context or {}).items() if not key.startswith("_")}
    assets = public.get("asset_identity")
    if isinstance(assets, Mapping):
        public["asset_identity"] = {key: value for key, value in assets.items() if not key.startswith("_")}
    return public


def _rule_number(rule_type: str) -> int:
    normalized = str(rule_type or "unknown").strip().lower()
    known = _KNOWN_RULE_NUMBERS.get(normalized)
    if known is not None:
        return known
    return 500 + int(hashlib.sha256(normalized.encode()).hexdigest()[:8], 16) % 500


def _raw_findings(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for collection, fallback_type in (
        ("violations", "unknown"),
        ("unconnected_items", "unconnected_items"),
        ("schematic_parity", "schematic_parity"),
    ):
        values = raw.get(collection, [])
        if not isinstance(values, list):
            raise ValueError(f"KiCad DRC field {collection!r} must be a list")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError(f"KiCad DRC field {collection!r} contains a non-object")
            row = dict(value)
            row.setdefault("type", fallback_type)
            rows.append(row)
    return rows


def _object_refs(row: Mapping[str, Any]) -> tuple[str, str]:
    descriptions = [str(row.get("description") or "")]
    items = row.get("items")
    if isinstance(items, list):
        descriptions.extend(str(item.get("description") or "") for item in items if isinstance(item, Mapping))
    combined = " ".join(descriptions)
    ref_match = re.search(r"\b(?:Footprint|Pad|Reference|Component)\s+([A-Za-z]+\d+)\b", combined)
    net_match = re.search(r"\b[Nn]et\s+['\"]?([^'\"\s,;]+)", combined)
    return (
        ref_match.group(1) if ref_match else "",
        net_match.group(1) if net_match else "",
    )


def _expected_constraint(rule_type: str, constraints: Sequence[PcbConstraint]) -> str:
    normalized = rule_type.lower()
    klasses: set[str]
    if "clearance" in normalized:
        klasses = {"clearance", "keepout"}
    elif "width" in normalized:
        klasses = {"width"}
    elif "via" in normalized or "hole" in normalized:
        klasses = {"via"}
    elif "length" in normalized or "skew" in normalized:
        klasses = {"length", "diff_pair"}
    elif "courtyard" in normalized:
        klasses = {"placement", "keepout"}
    else:
        klasses = set()
    ids = [item.id for item in constraints if item.klass in klasses]
    return ", ".join(sorted(ids)) if ids else "KiCad configured DRC rule"


def _parse_drc_json(
    raw: Mapping[str, Any],
    *,
    evidence_id: str,
    constraints: Sequence[PcbConstraint] = (),
    approved_overrides: Mapping[str, str] | None = None,
) -> tuple[ValidationIssue, ...]:
    """Parse KiCad 8/9/10 JSON into the existing ValidationIssue schema."""

    overrides = approved_overrides or {}
    findings: list[ValidationIssue] = []
    for row in _raw_findings(raw):
        rule_type = str(row.get("type") or "unknown")
        raw_severity = str(row.get("severity") or "error").lower()
        severity = "blocker" if raw_severity in {"error", "fatal"} else "major"
        ref, net = _object_refs(row)
        message = str(row.get("description") or rule_type)
        number = _rule_number(rule_type)
        rule_id = f"CW-DRC-{number:03d}"
        issue = drc_validation_issue(
            rule_number=number,
            message=message,
            severity=severity,
            evidence_ids=(evidence_id,),
            ref=ref,
            net=net,
            observed_value=message,
            expected_constraint=_expected_constraint(rule_type, constraints),
            safest_next_action=f"Resolve the KiCad {rule_type} finding on the staged board and rerun DRC.",
        )
        suppression_id = overrides.get(rule_id)
        if suppression_id:
            issue = replace(issue, suppressed=True, suppression_id=suppression_id)
        findings.append(issue)
    return tuple(findings)


def run_drc(
    board: str | Path,
    *,
    evidence_ledger: EvidenceLedger,
    constraints: Sequence[PcbConstraint] = (),
    approved_overrides: Mapping[str, str] | None = None,
    timeout: int = 120,
    project_context: str | Path | None = None,
) -> DrcResult:
    """Run DRC on exact board bytes and record the tool result as evidence."""

    board_path = Path(board)
    if not board_path.is_file():
        return DrcResult(status="failed", board=str(board_path), failure_reason="board not found")
    original_bytes = board_path.read_bytes()
    board_hash = hashlib.sha256(original_bytes).hexdigest()
    settings_hashes = {
        board_path.with_suffix(suffix): (
            hashlib.sha256(board_path.with_suffix(suffix).read_bytes()).hexdigest()
            if board_path.with_suffix(suffix).is_file()
            else None
        )
        for suffix in (".kicad_pro", ".kicad_dru")
    }
    cli = _kicad_cli_path()
    if cli is None:
        return DrcResult(
            status="skipped",
            board=str(board_path),
            board_sha256=board_hash,
            failure_reason="KiCad CLI not available",
        )
    cli_major = _kicad_cli_major(cli, timeout)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        report_path = Path(handle.name)
    try:
        with tempfile.TemporaryDirectory(prefix="cw-drc-context-") as folder:
            stage = Path(folder)
            staged_board = stage / board_path.name
            shutil.copy2(board_path, staged_board)
            if hashlib.sha256(staged_board.read_bytes()).hexdigest() != board_hash:
                raise ValueError("Board changed while staging DRC")
            for suffix in (".kicad_pro", ".kicad_dru"):
                sidecar = board_path.with_suffix(suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, staged_board.with_suffix(suffix))
                    staged_hash = hashlib.sha256(staged_board.with_suffix(suffix).read_bytes()).hexdigest()
                    if staged_hash != settings_hashes[sidecar]:
                        raise ValueError("Project settings changed while staging DRC")
            context = Path(project_context).resolve() if project_context is not None else board_path.resolve().parent
            if context.is_file():
                context = context.parent
            context_before = inspect_project_footprint_context(context)
            stage_project_footprint_context(context, stage)
            installed_context: dict[str, Any]
            if cli_major is None:
                installed_context = {
                    "status": "unavailable",
                    "reason": "selected KiCad CLI version could not be determined",
                }
            else:
                installed_context = stage_installed_footprint_context(
                    cli, cli_major, stage, board_bytes=original_bytes, project_root=context
                )
            run_env = os.environ.copy()
            private_config = stage / "kicad-config"
            if not private_config.exists():
                private_config.mkdir()
            run_env["KICAD_CONFIG_HOME"] = str(private_config)
            for name in (
                "KICAD_FOOTPRINT_DIR",
                "KICAD8_FOOTPRINT_DIR",
                "KICAD9_FOOTPRINT_DIR",
                "KICAD10_FOOTPRINT_DIR",
            ):
                run_env.pop(name, None)
            if installed_context.get("status") == "staged":
                run_env[str(installed_context["footprint_env"])] = str(installed_context["effective_root"])
            process = subprocess.run(
                [str(cli), "pcb", "drc", "--format", "json", "--output", str(report_path), str(staged_board)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=run_env,
            )
            if staged_board.read_bytes() != original_bytes:
                return DrcResult(
                    status="failed",
                    board=str(board_path),
                    board_sha256=board_hash,
                    failure_reason="kicad-cli mutated the isolated board bytes during DRC",
                )
            for original, digest in settings_hashes.items():
                staged_settings = staged_board.with_suffix(original.suffix)
                if digest is not None and (
                    not staged_settings.is_file() or hashlib.sha256(staged_settings.read_bytes()).hexdigest() != digest
                ):
                    raise ValueError("kicad-cli mutated authored project settings during DRC")
            if installed_context.get("status") == "staged":
                source_table = Path(str(installed_context["source"]))
                if _sha256_limited(source_table, 1024 * 1024) != installed_context["sha256"]:
                    raise ValueError("installed footprint table changed during DRC")
                staged_table = Path(str(installed_context["config_root"])) / f"{cli_major}.0" / "fp-lib-table"
                if _sha256_limited(staged_table, 1024 * 1024) != installed_context["generated_table_sha256"]:
                    raise ValueError("staged installed footprint table changed during DRC")
                checked_asset_bytes = 0
                for asset_path, expected in installed_context.get("asset_identity", {}).get("_paths", []):
                    actual_hash, actual_size = _hash_file_limited(
                        Path(asset_path), 64 * 1024 * 1024 - checked_asset_bytes
                    )
                    checked_asset_bytes += actual_size
                    if actual_hash != expected:
                        raise ValueError("installed stock footprint changed during DRC")
                staged_asset_bytes = 0
                for asset_path, expected in installed_context.get("_staged_paths", []):
                    actual_hash, actual_size = _hash_file_limited(
                        Path(asset_path), 64 * 1024 * 1024 - staged_asset_bytes
                    )
                    staged_asset_bytes += actual_size
                    if actual_hash != expected:
                        raise ValueError("staged stock footprint changed during DRC")
        for original, digest in settings_hashes.items():
            current = hashlib.sha256(original.read_bytes()).hexdigest() if original.is_file() else None
            if current != digest:
                raise ValueError("Source project settings changed during DRC")
        if inspect_project_footprint_context(context) != context_before:
            return DrcResult(
                status="failed",
                board=str(board_path),
                board_sha256=board_hash,
                failure_reason="Project footprint context changed during DRC",
            )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()
            return DrcResult(
                status="failed",
                board=str(board_path),
                board_sha256=board_hash,
                failure_reason=f"kicad-cli exited {process.returncode}: {detail[:500]}",
            )
        if not report_path.is_file() or report_path.stat().st_size == 0:
            return DrcResult(
                status="failed",
                board=str(board_path),
                board_sha256=board_hash,
                failure_reason="kicad-cli produced no DRC JSON",
            )
        raw = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("KiCad DRC JSON root must be an object")
        if board_path.read_bytes() != original_bytes:
            return DrcResult(
                status="failed",
                board=str(board_path),
                board_sha256=board_hash,
                failure_reason="kicad-cli mutated the staged board bytes during DRC",
            )
        tool_version = str(raw.get("kicad_version") or "unknown")
        report_major_match = re.search(r"\b(8|9|10)\.\d+", tool_version)
        if cli_major is not None and report_major_match and int(report_major_match.group(1)) != cli_major:
            return DrcResult(
                status="failed",
                board=str(board_path),
                board_sha256=board_hash,
                tool_version=tool_version,
                failure_reason="kicad-cli version output did not match the selected executable",
                library_context=_public_library_context(installed_context),
            )
        observed_count = len(_raw_findings(raw))
        claim = json.dumps(
            {
                "board_sha256": board_hash,
                "kicad_version": tool_version,
                "observed_findings": observed_count,
                "library_context_status": installed_context.get("status", "unavailable"),
                "stock_table_sha256": installed_context.get("sha256", ""),
                "generated_table_sha256": installed_context.get("generated_table_sha256", ""),
                "stock_asset_identity_status": installed_context.get("asset_identity", {}).get("status", "unverified"),
                "stock_asset_manifest_sha256": installed_context.get("asset_identity", {}).get("sha256", ""),
                "stock_asset_file_count": installed_context.get("asset_identity", {}).get("file_count", 0),
                "project_context_sha256": hashlib.sha256(
                    "".join(
                        f"{path}\0{digest}\n"
                        for path, digest in sorted(context_before.get("source_hashes", {}).items())
                    ).encode("utf-8")
                ).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence_id = evidence_ledger.record(
            subject_ref="tool:drc",
            claim=claim,
            kind="tool_result",
            source=EvidenceSource(
                doc_id=f"kicad-cli-{tool_version}",
                content_hash=board_hash,
                extraction_method="kicad-cli-pcb-drc-json",
            ),
            confidence="verified",
            freshness="current",
        )
        findings = _parse_drc_json(
            raw,
            evidence_id=evidence_id,
            constraints=constraints,
            approved_overrides=approved_overrides,
        )
        return DrcResult(
            status="ok",
            board=str(board_path),
            board_sha256=board_hash,
            tool_version=tool_version,
            evidence_id=evidence_id,
            findings=findings,
            raw_report=raw,
            library_context=_public_library_context(installed_context),
        )
    except subprocess.TimeoutExpired:
        return DrcResult(
            status="failed",
            board=str(board_path),
            board_sha256=board_hash,
            failure_reason=f"kicad-cli DRC timed out after {timeout}s",
        )
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return DrcResult(
            status="failed",
            board=str(board_path),
            board_sha256=board_hash,
            failure_reason=f"DRC output parse error: {exc}",
        )
    finally:
        report_path.unlink(missing_ok=True)
