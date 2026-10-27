#!/usr/bin/env python3
"""Run the diverse, high-part-count Circuit Weaver workflow suite.

Each run delegates to the repository's full app-workflow validator. The suite
adds repeatable input provenance and requires the native assembly preview to
account for at least 100 populated physical components with 3D models.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - package dependency
    raise SystemExit("PyYAML is required to load general-scale benchmark specs") from exc


REPO = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = Path(__file__).resolve().parent / "cases.yaml"
SAMPLE_SUITE = Path(__file__).resolve().parent / "samples.yaml"
WORKFLOW = REPO / "scripts" / "validate_app_workflow.py"
PACKAGE_SOURCE_SUFFIXES = {".py", ".json", ".step", ".md", ".yaml"}
REQUIRED_STEPS = {
    "doctor", "validate", "generate", "confidence", "build", "place", "route",
    "assembly-gate", "manufacturing-gate", "status", "resume", "prototype-positions",
    "board-preview-front", "board-preview-back", "board-preview-front-layers", "mcp",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _package_source_sha256(package_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        p for p in package_root.rglob("*") if p.is_file() and p.suffix.lower() in PACKAGE_SOURCE_SUFFIXES
    ):
        digest.update(f"{path.relative_to(package_root).as_posix()}:{sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest()


def _benchmark_tree_sha256() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".yaml", ".md"}
    ):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def git_value(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return result.stdout.strip()


def _python_executable(path: Path) -> Path:
    """Make the executable path absolute without dereferencing venv symlinks."""
    return path.expanduser().absolute()


def source_state() -> dict[str, str]:
    status = git_value("status", "--porcelain")
    revision = git_value("rev-parse", "HEAD")
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--binary"], cwd=REPO, capture_output=True, check=True,
    ).stdout
    package_hash = hashlib.sha256()
    package_root = REPO / "src" / "circuit_weaver"
    for path in sorted(
        p for p in package_root.rglob("*") if p.is_file() and p.suffix.lower() in PACKAGE_SOURCE_SUFFIXES
    ):
        package_hash.update(path.relative_to(package_root).as_posix().encode("utf-8"))
        package_hash.update(b"\0")
        package_hash.update(bytes.fromhex(sha256(path)))
    workflow_hash = sha256(WORKFLOW)
    return {
        "revision": revision,
        "status": status,
        "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
        "diff_head_sha256": hashlib.sha256(diff).hexdigest(),
        "package_tree_sha256": package_hash.hexdigest(),
        "workflow_sha256": workflow_hash,
        "benchmark_tree_sha256": _benchmark_tree_sha256(),
    }


def _materialize_suite(suite_path: Path, output: Path) -> Path:
    """Expand frozen fixtures beside run evidence instead of in the checkout."""
    builders = {
        "unseen_scale": ("build_stress_fixtures.py", "materialize"),
        "diverse_systems": ("build_diverse_trials.py", "build_corpus"),
    }
    suite_kind = next((name for name in builders
                       if suite_path.resolve() == (REPO / "benchmarks" / name / "cases.yaml").resolve()), None)
    if suite_kind is None:
        return suite_path
    builder_file, function = builders[suite_kind]
    builder = REPO / "benchmarks" / suite_kind / builder_file
    module_spec = importlib.util.spec_from_file_location(f"cw_{suite_kind}_fixture_builder", builder)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("Cannot load the retained diverse fixture generator")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    target = output / "generated-fixtures"
    if function == "materialize":
        module.materialize(target)
    else:
        module.build_corpus(REPO, target)
    return target / "cases.yaml"


def _load_suite(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    suite = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(suite, dict) or suite.get("schema_version") != 1:
        raise ValueError("suite must be a schema_version 1 object")
    suite_kind = suite.get("suite_kind", "general")
    if suite_kind not in {"general", "samples"}:
        raise ValueError("suite_kind must be 'general' or 'samples'")
    cases = suite.get("cases")
    if not isinstance(cases, list) or len(cases) < 3:
        raise ValueError("suite must define at least three benchmark cases")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    families = [case.get("family") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or len(set(ids)) != len(ids) or any(not x for x in ids):
        raise ValueError("case ids must be present and unique")
    if len(set(families)) < 3 or any(not x for x in families):
        raise ValueError("suite must cover at least three distinct architecture families")
    minimum = suite.get("minimum_physical_parts")
    floor = 100 if suite_kind == "general" else 1
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < floor:
        raise ValueError(f"{suite_kind} suite minimum_physical_parts must be an integer of at least {floor}")
    for case in cases:
        case_minimum = case.get("minimum_physical_parts", minimum)
        if isinstance(case_minimum, bool) or not isinstance(case_minimum, int) or case_minimum < minimum:
            raise ValueError(f"case {case['id']} minimum_physical_parts must be an integer of at least {minimum}")
        spec_path = (path.parent / case["spec"]).resolve()
        if not spec_path.is_file() or not spec_path.is_relative_to(REPO.resolve()):
            raise ValueError(f"case {case['id']} has a missing or out-of-repository spec")
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError(f"case {case['id']} spec must be a YAML mapping")
        minimum_drill = case.get("minimum_drill_mm")
        if minimum_drill is not None and (
            isinstance(minimum_drill, bool) or not isinstance(minimum_drill, (int, float))
            or not math.isfinite(minimum_drill) or minimum_drill <= 0
        ):
            raise ValueError(f"case {case['id']} minimum_drill_mm must be a positive finite millimeter value")
        for section in case.get("required_sections", []):
            if not isinstance(spec.get(section), list) or not spec[section]:
                raise ValueError(f"case {case['id']} must have a non-empty {section} section")
        case["_spec_path"] = spec_path
        case["_spec_sha256"] = sha256(spec_path)
    return suite, cases


def _stage_inputs(suite_path: Path, suite: dict[str, Any], cases: list[dict[str, Any]], output: Path) -> dict[str, Any]:
    """Snapshot the exact suite and YAML inputs beside the run evidence."""
    inputs = output / "inputs"
    inputs.mkdir()
    suite_copy = inputs / "cases.yaml"
    suite_copy.write_bytes(suite_path.read_bytes())
    staged = []
    for case in cases:
        case["_source_spec_path"] = case["_spec_path"]
        destination = inputs / f"{case['id']}.yaml"
        destination.write_bytes(case["_spec_path"].read_bytes())
        case["_spec_path"] = destination
        row = {"case_id": case["id"], "path": str(destination), "sha256": sha256(destination)}
        channels = case.get("required_channels", [])
        paths = case.get("required_paths", [])
        if channels or paths:
            contract_path = inputs / f"{case['id']}.native-contracts.json"
            contract_path.write_text(
                json.dumps({"required_channels": channels, "required_paths": paths}, indent=2) + "\n",
                encoding="utf-8",
            )
            case["_contracts_path"] = contract_path
            case["_contracts_sha256"] = sha256(contract_path)
            row["native_contracts_path"] = str(contract_path)
            row["native_contracts_sha256"] = case["_contracts_sha256"]
        staged.append(row)
    return {"suite_path": str(suite_copy), "suite_sha256": sha256(suite_copy), "case_specs": staged}


def _source_probe(python: Path, installed: bool, wheel: Path | None, timeout: int) -> dict[str, Any]:
    python = _python_executable(python)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    if not installed:
        env["PYTHONPATH"] = str(REPO / "src")
    else:
        env.pop("PYTHONPATH", None)
    probe = subprocess.run(
        [str(python), "-c", (
            "from mcp import ClientSession, StdioServerParameters; "
            "from mcp.client.stdio import stdio_client; "
            "import circuit_weaver, circuit_weaver.generator; "
            "print(circuit_weaver.__file__); print(circuit_weaver.generator.__file__)"
        )],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    if probe.returncode:
        raise RuntimeError(f"Python import preflight failed: {probe.stderr.strip()}")
    import_paths = [Path(line).resolve() for line in probe.stdout.strip().splitlines()]
    if len(import_paths) != 2:
        raise RuntimeError("Python import preflight did not report both package and generator paths")
    package_init, generator_path = import_paths
    source_package = (REPO / "src" / "circuit_weaver").resolve()
    if installed and package_init.is_relative_to(source_package):
        raise RuntimeError("installed mode imported the checkout source package")
    installed_package = package_init.parent
    if installed and not generator_path.is_relative_to(installed_package):
        raise RuntimeError(f"installed mode imported generator from outside its package: {generator_path}")
    if not installed and package_init != (source_package / "__init__.py"):
        raise RuntimeError(f"source mode imported {package_init}, expected {source_package}")
    if not installed and not generator_path.is_relative_to(source_package):
        raise RuntimeError(f"source mode generator imported from outside checkout: {generator_path}")
    payload = {"mode": "installed" if installed else "source", "package_init": str(package_init),
               "generator_module": str(generator_path),
               "package_source_sha256": _package_source_sha256(package_init.parent)}
    if wheel is not None:
        if not installed:
            raise ValueError("--expected-wheel requires --installed")
        if not wheel.is_file():
            raise ValueError(f"expected wheel does not exist: {wheel}")
        payload["expected_wheel"] = str(wheel.resolve())
        payload["expected_wheel_sha256"] = sha256(wheel)
    return payload


def _validate_evidence(run_dir: Path, minimum_parts: int, *, expected_spec_hash: str,
                       expected_board_mm: list[float] | None = None,
                       expected_wheel_hash: str | None = None,
                       expected_package_hash: str | None = None,
                       expected_runner_hash: str | None = None) -> dict[str, Any]:
    evidence_path = run_dir / "evidence.json"
    if not evidence_path.is_file():
        raise RuntimeError("workflow validator did not write evidence.json")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not str(evidence.get("status", "")).startswith("PASS"):
        raise RuntimeError(f"workflow evidence status is {evidence.get('status')!r}")
    steps = evidence.get("steps")
    if not isinstance(steps, list) or len(steps) != len(REQUIRED_STEPS):
        raise RuntimeError("workflow evidence must contain the exact 16-stage inventory")
    if any(not isinstance(item, dict) or item.get("passed") is not True for item in steps):
        raise RuntimeError("workflow evidence contains a failed or malformed stage")
    names = [item.get("name") for item in steps]
    if len(set(names)) != len(names):
        raise RuntimeError("workflow evidence contains duplicate stage names")
    passed = set(names)
    missing = sorted(REQUIRED_STEPS - passed)
    if missing:
        raise RuntimeError(f"workflow evidence omits required passing stages: {missing}")
    preview = evidence.get("previews", {}).get("front", {})
    coverage = preview.get("model_coverage", {})
    components = preview.get("component_coverage", {})
    count = components.get("expected")
    represented = components.get("represented")
    missing_models = components.get("missing")
    if isinstance(count, bool) or not isinstance(count, int) or count < minimum_parts:
        raise RuntimeError(f"native assembly preview represents {count!r} physical parts; needs {minimum_parts}")
    if represented != count or missing_models != 0:
        raise RuntimeError("native assembly preview does not represent every populated physical part")
    if coverage.get("expected", 0) < minimum_parts or coverage.get("resolved") != coverage.get("expected") \
            or coverage.get("missing") != 0:
        raise RuntimeError("native 3D model inventory does not cover the required assembly scale")
    records = components.get("components")
    if not isinstance(records, list) or len(records) != count:
        raise RuntimeError("native assembly component inventory disagrees with its physical part count")
    refs = [item.get("reference") for item in records if isinstance(item, dict)]
    if len(refs) != count or any(not isinstance(ref, str) or not ref for ref in refs) or len(set(refs)) != count:
        raise RuntimeError("native assembly component references are missing or duplicated")
    if any(item.get("required") is not True or not isinstance(item.get("visible_model_count"), int)
           or item["visible_model_count"] < 1 for item in records if isinstance(item, dict)):
        raise RuntimeError("assembly inventory contains a populated component without a visible native model")
    if not evidence.get("source_unchanged") or evidence.get("source_spec_sha256") != expected_spec_hash \
            or evidence.get("working_spec_sha256") != expected_spec_hash:
        raise RuntimeError("workflow evidence does not bind unchanged source and working spec hashes")
    if expected_board_mm is not None:
        actual_board_mm = evidence.get("board_size_effective_mm")
        if not isinstance(actual_board_mm, list) or len(actual_board_mm) != 2 \
                or any(not isinstance(value, (int, float)) or abs(value - expected) > 1e-6
                       for value, expected in zip(actual_board_mm, expected_board_mm)):
            raise RuntimeError(
                f"effective board size {actual_board_mm!r} disagrees with suite metadata {expected_board_mm}"
            )
    if not evidence.get("board_sha256") or not evidence.get("package_source_sha256") \
            or evidence.get("runner_sha256") != (expected_runner_hash or sha256(WORKFLOW)):
        raise RuntimeError("workflow evidence omits board or package source hashes")
    if expected_package_hash and evidence.get("package_source_sha256") != expected_package_hash:
        raise RuntimeError("workflow package source hash does not match the selected Python import")
    if expected_wheel_hash:
        wheel = evidence.get("wheel_payload_verification", {})
        if wheel.get("wheel_sha256") != expected_wheel_hash or wheel.get("payload_matches") is not True:
            raise RuntimeError("installed package payload does not match the requested wheel")
    drc = evidence.get("drc", {})
    board_path = Path(drc.get("board", ""))
    if not drc.get("passed") or drc.get("status") != "ok" or drc.get("blocker_count") != 0:
        raise RuntimeError("native DRC did not pass with zero blockers")
    if not board_path.is_file() or sha256(board_path) != evidence["board_sha256"] \
            or drc.get("board_sha256") != evidence["board_sha256"]:
        raise RuntimeError("final routed board bytes do not match the workflow and DRC hashes")
    silkscreen = evidence.get("silkscreen", {})
    if silkscreen.get("unresolved_count") != 0 or silkscreen.get("remaining_conflict_count") != 0:
        raise RuntimeError("native reference placement contains unresolved silkscreen conflicts")
    transcript = run_dir / "mcp-transcript.json"
    if not transcript.is_file():
        raise RuntimeError("MCP workflow transcript is missing")
    return {
        "workflow_status": evidence["status"],
        "passed_stages": sorted(passed),
        "physical_part_count": count,
        "native_3d_model_count": coverage["resolved"],
        "board_sha256": evidence["board_sha256"],
        "package_source_sha256": evidence["package_source_sha256"],
        "source_spec_sha256": evidence.get("source_spec_sha256"),
        "drc_blocker_count": drc["blocker_count"],
    }


def _inspect_and_verify_connectivity(
    case: dict[str, Any], args: argparse.Namespace, run_dir: Path, log_dir: Path, board_path: Path,
) -> dict[str, Any]:
    """Read actual routed PCB pad nets with the selected Circuit Weaver runtime."""
    before = sha256(board_path)
    contracts_path = Path(case.get("_contracts_path") or (log_dir / f"{case['id']}.connectivity-contracts.json"))
    channels = case.get("required_channels", [])
    paths = case.get("required_paths", [])
    contracts = {"required_channels": channels, "required_paths": paths} if paths else channels
    if "_contracts_path" not in case:
        contracts_path.write_text(json.dumps(contracts, indent=2) + "\n", encoding="utf-8")
    expected_contract_hash = case.get("_contracts_sha256", sha256(contracts_path))
    if sha256(contracts_path) != expected_contract_hash:
        raise RuntimeError("snapshotted native contract changed before final-board verification")
    helper_dir = Path(__file__).resolve().parent
    script = (
        "import json,sys; sys.path.insert(0,sys.argv[3]); "
        "from circuit_weaver.kicad_bridge import run_kicad; "
        "from connectivity import verify_native_connectivity; "
        "from signal_connectivity import verify_native_signal_paths; "
        "inventory=run_kicad('inspect',board=sys.argv[1],timeout=float(sys.argv[2])); "
        "contracts=json.load(open(sys.argv[4],encoding='utf-8')); "
        "channels=contracts.get('required_channels',[]) if isinstance(contracts,dict) else contracts; "
        "paths=contracts.get('required_paths',[]) if isinstance(contracts,dict) else []; "
        "proof={'inventory':inventory}; "
        "proof.update({'connectivity':verify_native_connectivity(inventory,channels)} if channels else {}); "
        "proof.update({'signal_connectivity':verify_native_signal_paths(inventory,paths)} if paths else {}); "
        "print(json.dumps(proof))"
    )
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    if args.installed:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = str(REPO / "src")
    timeout = min(max(args.timeout, 120), 600)
    python = _python_executable(args.python)
    result = subprocess.run(
        [str(python), "-c", script, str(board_path.resolve()), str(timeout),
         str(helper_dir), str(contracts_path.resolve())],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 30,
    )
    (log_dir / f"{case['id']}.native-inspect.stdout.log").write_text(result.stdout, encoding="utf-8")
    (log_dir / f"{case['id']}.native-inspect.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"native PCB connectivity inspection failed for {case['id']}; inspect native logs")
    try:
        proof = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"native PCB connectivity inspection returned invalid JSON for {case['id']}") from exc
    inventory = proof.get("inventory")
    connectivity = proof.get("connectivity")
    after = sha256(board_path)
    if before != after:
        raise RuntimeError("native PCB inspection changed routed board bytes")
    if sha256(contracts_path) != expected_contract_hash:
        raise RuntimeError("native contract changed during final-board verification")
    if not isinstance(inventory, dict) or inventory.get("status") != "ok":
        raise RuntimeError(f"native PCB connectivity verification did not pass for {case['id']}")
    details = {}
    if channels or not paths:
        if not isinstance(connectivity, dict) or connectivity.get("status") != "pass":
            raise RuntimeError(f"native PCB channel verification did not pass for {case['id']}")
        (run_dir / "native-connectivity-verification.json").write_text(
            json.dumps(connectivity, indent=2) + "\n", encoding="utf-8"
        )
        details.update(verified_native_channels=connectivity["verified_channels"],
                       native_connectivity_status=connectivity["status"])
    if paths:
        signal_proof = proof.get("signal_connectivity")
        if not isinstance(signal_proof, dict) or signal_proof.get("status") != "pass" \
                or signal_proof.get("verified_paths") != len(paths):
            raise RuntimeError(f"native PCB signal path verification did not pass for {case['id']}")
        (run_dir / "native-signal-path-verification.json").write_text(
            json.dumps(signal_proof, indent=2) + "\n", encoding="utf-8"
        )
        details.update(verified_native_signal_paths=signal_proof["verified_paths"],
                       native_signal_path_status=signal_proof["status"])
    (run_dir / "native-pcb-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return {**details, "native_inventory_sha256":
            hashlib.sha256(json.dumps(inventory, sort_keys=True).encode("utf-8")).hexdigest()}


def _run_case(case: dict[str, Any], args: argparse.Namespace, output: Path) -> dict[str, Any]:
    run_dir = output / "runs" / case["id"]
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite prior evidence directory: {run_dir}")
    (output / "runs").mkdir(parents=True, exist_ok=True)
    log_dir = output / "logs"
    log_dir.mkdir(exist_ok=True)
    log_stem = log_dir / case["id"]
    spec_path: Path = case["_spec_path"]
    python = _python_executable(args.python)
    command = [
        str(python), str(WORKFLOW), "--spec", str(spec_path), "--output", str(run_dir),
        "--iterations", str(args.iterations), "--attempts", str(args.attempts),
        "--route-timeout", str(args.route_timeout),
        "--mcp-smoke", "--render-3d",
    ]
    contracts_path = case.get("_contracts_path")
    contracts_hash = case.get("_contracts_sha256")
    if contracts_path is not None:
        contracts_path = Path(contracts_path)
        if not contracts_path.is_file() or sha256(contracts_path) != contracts_hash:
            raise RuntimeError("snapshotted native contract is missing or changed before workflow launch")
        command.extend([
            "--native-contracts",
            str(contracts_path.resolve()),
            "--native-contracts-sha256",
            str(contracts_hash),
        ])
    if args.installed:
        command.append("--installed")
        if args.expected_wheel:
            command.extend(["--expected-wheel", str(args.expected_wheel.resolve())])
    if case.get("minimum_drill_mm") is not None:
        command.extend(["--minimum-drill", str(case["minimum_drill_mm"])])
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    if args.installed:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = str(REPO / "src")
    (log_dir / f"{case['id']}.invocation.json").write_text(json.dumps({
        "case_id": case["id"], "family": case["family"], "spec": str(spec_path),
        "spec_sha256": case["_spec_sha256"], "command": command,
        "minimum_drill_mm": case.get("minimum_drill_mm"),
        "native_contracts_sha256": contracts_hash,
        "minimum_physical_parts": max(args.minimum_physical_parts, case.get("minimum_physical_parts", 0)),
        "timeout_seconds": args.timeout, "python": str(python),
    }, indent=2) + "\n", encoding="utf-8")
    started = time.monotonic()
    status = "FAILED"
    try:
        result = subprocess.run(
            command, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=args.timeout,
        )
        Path(f"{log_stem}.stdout.log").write_text(result.stdout, encoding="utf-8")
        Path(f"{log_stem}.stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"workflow validator exited {result.returncode}; inspect runner logs and evidence")
        details = _validate_evidence(
            run_dir, max(args.minimum_physical_parts, case.get("minimum_physical_parts", 0)),
            expected_spec_hash=case["_spec_sha256"],
            expected_board_mm=case["board_mm"],
            expected_wheel_hash=sha256(args.expected_wheel) if args.expected_wheel else None,
            expected_package_hash=getattr(args, "package_source_sha256", None),
            expected_runner_hash=getattr(args, "workflow_sha256", None),
        )
        if case.get("required_channels") or case.get("required_paths"):
            board_path = Path(json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))["drc"]["board"])
            details.update(_inspect_and_verify_connectivity(case, args, run_dir, log_dir, board_path))
        if sha256(spec_path) != case["_spec_sha256"] or sha256(case["_source_spec_path"]) != case["_spec_sha256"]:
            raise RuntimeError("benchmark input spec or its source changed during workflow execution")
        status = "PASS"
        return {"case_id": case["id"], "family": case["family"], "status": status,
                "seconds": round(time.monotonic() - started, 2), "evidence_dir": str(run_dir),
                "logs_dir": str(log_dir), **details}
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        Path(f"{log_stem}.stdout.log").write_text(out, encoding="utf-8")
        Path(f"{log_stem}.stderr.log").write_text(err, encoding="utf-8")
        failure = f"workflow timed out after {args.timeout} seconds"
        (log_dir / f"{case['id']}.failure.json").write_text(
            json.dumps({"status": status, "error": failure}, indent=2) + "\n"
        )
        return {"case_id": case["id"], "family": case["family"], "status": status,
                "seconds": round(time.monotonic() - started, 2), "evidence_dir": str(run_dir),
                "logs_dir": str(log_dir), "error": failure}
    except Exception as exc:
        (log_dir / f"{case['id']}.failure.json").write_text(json.dumps({
            "status": status, "error": str(exc),
        }, indent=2) + "\n", encoding="utf-8")
        return {"case_id": case["id"], "family": case["family"], "status": status,
                "seconds": round(time.monotonic() - started, 2), "evidence_dir": str(run_dir),
                "logs_dir": str(log_dir), "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE,
                        help="suite YAML path, or 'samples' to run all 13 compact repository samples")
    parser.add_argument("--case", action="append", dest="case_ids",
                        help="run one selected case ID (repeatable; useful for focused smoke runs)")
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory; existing output is refused")
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="Python executable for the workflow")
    parser.add_argument("--installed", action="store_true", help="use installed package mode")
    parser.add_argument("--expected-wheel", type=Path, help="expected installed wheel; implies --installed")
    parser.add_argument("--minimum-physical-parts", type=int,
                        help="assembly floor (general suite defaults to 100; samples suite uses its own floor)")
    parser.add_argument("--workers", type=int, default=1, help="bounded concurrent workflow runs (1–2)")
    parser.add_argument("--timeout", type=int, default=7200, help="per-case hard timeout in seconds")
    parser.add_argument("--iterations", type=int, default=400, help="native placement search iterations")
    parser.add_argument("--attempts", type=int, default=2, help="autoroute attempts")
    parser.add_argument("--route-timeout", type=int, default=300,
                        help="total routing budget shared across attempts")
    parser.add_argument("--require-clean", action="store_true",
                        help="refuse dirty source and fail if revision/tree changes during the workflow")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 2:
        parser.error("--workers must be between 1 and 2")
    if not 60 <= args.timeout <= 43_200 or not 1 <= args.route_timeout <= 3_600 \
            or not 1 <= args.iterations <= 10_000 or not 1 <= args.attempts <= 5:
        parser.error("timeout, route-timeout, iterations, and attempts exceed supported bounds")
    if args.expected_wheel and not args.installed:
        parser.error("--expected-wheel requires --installed")
    initial_source = source_state()
    if args.require_clean and initial_source["status"]:
        parser.error("--require-clean requires a clean source worktree before starting")
    output = args.output.resolve()
    if output.exists():
        parser.error(f"refusing to overwrite evidence directory: {output}")
    output.mkdir(parents=True)
    suite_path = SAMPLE_SUITE if str(args.suite).casefold() == "samples" else args.suite.resolve()
    suite_path = _materialize_suite(suite_path, output)
    suite, cases = _load_suite(suite_path)
    suite_floor = suite["minimum_physical_parts"]
    if args.minimum_physical_parts is None:
        args.minimum_physical_parts = suite_floor
    elif args.minimum_physical_parts < suite_floor:
        parser.error(f"--minimum-physical-parts cannot be lower than suite floor {suite_floor}")
    staged_inputs = _stage_inputs(suite_path, suite, cases, output)
    args.minimum_physical_parts = max(args.minimum_physical_parts, suite_floor)
    if args.case_ids:
        available = {case["id"] for case in cases}
        unknown = sorted(set(args.case_ids) - available)
        if unknown:
            parser.error(f"unknown case id(s): {unknown}")
        cases = [case for case in cases if case["id"] in set(args.case_ids)]
    initial_status = initial_source["status"]
    initial_revision = initial_source["revision"]
    source = _source_probe(_python_executable(args.python), args.installed,
                           args.expected_wheel, min(args.timeout, 120))
    args.package_source_sha256 = source["package_source_sha256"]
    args.workflow_sha256 = initial_source["workflow_sha256"]
    run_manifest: dict[str, Any] = {
        "schema_version": 1,
        "suite": str(suite_path),
        "suite_sha256": sha256(suite_path),
        "inputs": staged_inputs,
        "source_revision": initial_revision,
        "source_status_sha256": hashlib.sha256(initial_status.encode()).hexdigest(),
        "source_status_clean": not initial_status,
        "source_state": {key: value for key, value in initial_source.items() if key != "status"},
        "workflow_script_sha256": sha256(WORKFLOW),
        "python_import": source,
        "minimum_physical_parts": args.minimum_physical_parts,
        "workers": args.workers,
        "runs": [],
    }
    (output / "run-manifest.json").write_text(json.dumps(run_manifest, indent=2) + "\n", encoding="utf-8")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_run_case, case, args, output) for case in cases]
        results = [future.result() for future in futures]
    final_source = source_state()
    final_status = final_source["status"]
    run_manifest["source_status_unchanged"] = final_status == initial_status
    run_manifest["source_state_unchanged"] = final_source == initial_source
    run_manifest["final_source_state"] = {key: value for key, value in final_source.items() if key != "status"}
    run_manifest["results"] = results
    stable_source = run_manifest["source_state_unchanged"]
    clean_source = not final_status
    source_ok = stable_source and (clean_source if args.require_clean else True)
    run_manifest["status"] = "PASS" if all(row["status"] == "PASS" for row in results) and source_ok else "FAILED"
    (output / "run-manifest.json").write_text(json.dumps(run_manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": run_manifest["status"], "output": str(output), "results": results}, indent=2))
    return 0 if run_manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
