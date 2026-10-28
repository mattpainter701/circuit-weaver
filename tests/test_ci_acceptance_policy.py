"""Keep the expensive acceptance ladder behind its prerequisite checks."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name):
    # BaseLoader preserves GitHub's `on` key as a string under YAML 1.1 too.
    return yaml.load((ROOT / ".github" / "workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_native_ladder_requires_source_wheel_and_native_prerequisites_without_cycles():
    jobs = _workflow("ci.yml")["jobs"]

    def ancestors(job, stack=()):
        assert job not in stack, f"CI dependency cycle: {stack + (job,)}"
        needs = jobs[job].get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        result = set(needs)
        for dependency in needs:
            result.update(ancestors(dependency, stack + (job,)))
        return result

    for job in jobs:
        ancestors(job)
    assert {"test", "bundled-skills", "distribution", "test-wheel"} <= ancestors("kicad-validation")
    assert "kicad-validation" in ancestors("unseen-scale-validation")
    for job in ("kicad-validation", "unseen-scale-validation"):
        assert jobs[job]["strategy"]["max-parallel"] == "1"
        assert jobs[job].get("continue-on-error", "false") == "false"


def test_native_ci_requires_complete_shared_four_layer_cad_on_installed_package():
    steps = _workflow("ci.yml")["jobs"]["kicad-validation"]["steps"]
    commands = [step["run"] for step in steps if "four_layer_acceptance/run_acceptance.py" in step.get("run", "")]
    assert len(commands) == 1
    command = commands[0]
    for required in ("--require-cad", "--through preview", "--engine-mode installed", "--case iot_environment_node"):
        assert required in command


def test_samples_require_exact_wheel_and_pinned_bounded_recovery_without_waiving_cases():
    job = _workflow("ci.yml")["jobs"]["kicad-validation"]
    assert job["env"]["CIRCUIT_WEAVER_TEST_PACKAGE"] == "wheel"
    steps = job["steps"]
    downloads = [step for step in steps if step.get("uses", "").startswith("actions/download-artifact@")]
    assert any(step.get("with", {}).get("name") == "ci-distributions" for step in downloads)
    sample = next(
        step for step in steps if step["name"] == "Validate all samples with native assembled previews and MCP"
    )
    command = sample["run"]
    for required in (
        "--suite samples", "--require-clean", "--installed", '--expected-wheel "$wheel"',
        "--route-max-passes 20", "--repair-open-connections", "--recovery-timeout 600",
        "f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7",
        "sha256sum --check",
    ):
        assert required in command
    assert sample["env"]["CIRCUIT_WEAVER_FREEROUTING"] == "/tmp/freerouting-2.5.0.jar"
    assert sample.get("continue-on-error", "false") == "false"
    # Legacy native coverage stays separate from the explicit recovery profile.
    core = next(step for step in steps if step["name"] == "Verify pinned Freerouting and route real basic circuits")
    assert core["env"]["CIRCUIT_WEAVER_FREEROUTING"] == "/tmp/freerouting-2.4.1.jar"


def test_design_validation_runs_once_per_pr_revision_and_cancels_superseded_work():
    workflow = _workflow("validate-design.yml")
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "pull_request" in workflow["on"]
    assert workflow["concurrency"]["cancel-in-progress"] == "true"
    assert "github.head_ref" in workflow["concurrency"]["group"]
