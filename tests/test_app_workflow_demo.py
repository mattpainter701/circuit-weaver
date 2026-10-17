"""The demo must reject plausible routing results for unverified board bytes."""

import csv
import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "workflow_demo", Path(__file__).resolve().parents[1] / "scripts" / "validate_app_workflow.py"
)
_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_demo)


def test_demo_rejects_stale_final_board_evidence(tmp_path):
    board = tmp_path / "routed.kicad_pcb"
    board.write_text("verified board")
    payload = {"routing_complete": True, "output_kind": "routed_kicad_board",
               "drc": {"status": "ok", "passed": True, "blocker_count": 0,
                       "board": str(board), "board_sha256": _demo.digest(board)}}
    _demo.verify_routing(payload, board)
    board.write_text("changed after verification")
    with pytest.raises(RuntimeError, match="final board bytes"):
        _demo.verify_routing(payload, board)


def test_demo_rejects_partial_routing_even_with_clean_drc(tmp_path):
    with pytest.raises(RuntimeError, match="incomplete"):
        _demo.verify_routing({"routing_complete": False, "drc": {"passed": True}}, tmp_path / "board.kicad_pcb")


def test_working_spec_preserves_source_bytes_without_size_overrides(tmp_path):
    source = tmp_path / "design.yaml"
    destination = tmp_path / "out" / "design.yaml"
    destination.parent.mkdir()
    original = b"project: demo\npcb_constraints: [{kind: placement, target: board, width_mm: 36, height_mm: 30}]\n"
    source.write_bytes(original)

    metadata = _demo.prepare_working_spec(source, destination, None, None)

    assert destination.read_bytes() == original
    assert metadata["board_size_original_mm"] == [36.0, 30.0]
    assert metadata["board_size_effective_mm"] == [36.0, 30.0]
    assert metadata["board_size_override_applied"] is False
    assert metadata["source_spec_sha256"] == metadata["working_spec_sha256"]


def test_working_spec_size_override_updates_all_supported_dimension_aliases_and_duplicates(tmp_path):
    source = tmp_path / "design.yaml"
    destination = tmp_path / "out" / "design.yaml"
    destination.parent.mkdir()
    source.write_text(
        "project: demo\n"
        "pcb_constraints:\n"
        "  - kind: placement\n    target: board\n    width_mm: 36\n    height_mm: 30\n"
        "  - type: placement\n    ref: pcb\n    board_width_mm: 38\n    board_height_mm: 31\n"
        "  - kind: placement\n    board: {width_mm: 39, height_mm: 32}\n"
        "  - kind: placement\n    target: J1\n    width_mm: 7\n    height_mm: 5\n",
        encoding="utf-8",
    )
    original_bytes = source.read_bytes()

    metadata = _demo.prepare_working_spec(source, destination, 40.0, None)
    design = _demo.yaml.safe_load(destination.read_text(encoding="utf-8"))
    constraints = design["pcb_constraints"]

    assert source.read_bytes() == original_bytes
    assert metadata["board_size_original_mm"] == [39.0, 32.0]
    assert metadata["board_size_effective_mm"] == [40.0, 32.0]
    assert metadata["board_size_overrides_mm"] == {"width_mm": 40.0, "height_mm": None}
    assert metadata["board_size_override_applied"] is True
    assert metadata["source_spec_sha256"] != metadata["working_spec_sha256"]
    assert constraints[0]["width_mm"] == 40.0
    assert constraints[0]["height_mm"] == 32.0
    assert constraints[1]["board_width_mm"] == 40.0
    assert constraints[1]["board_height_mm"] == 32.0
    assert constraints[2]["board"] == {"width_mm": 40.0, "height_mm": 32.0}
    assert constraints[3]["width_mm"] == 7
    assert constraints[3]["height_mm"] == 5
    assert constraints[-1] == {
        "kind": "placement", "target": "board", "width_mm": 40.0, "height_mm": 32.0,
    }
    assert _demo._design_board_size(design) == (40.0, 32.0)


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_runner_rejects_invalid_size_override_before_creating_output(tmp_path, monkeypatch, value):
    output = tmp_path / "not-created"
    monkeypatch.setattr("sys.argv", [
        "validate_app_workflow.py", "--output", str(output), "--width", str(value),
    ])

    with pytest.raises(SystemExit) as error:
        _demo.main()

    assert error.value.code == 2
    assert not output.exists()


def _wheel_fixture(tmp_path, files):
    wheel = tmp_path / "circuit_weaver-0.35-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("circuit_weaver/__init__.py", files.get(
            "__init__.py", "__version__='0.35'\n"
        ).encode("utf-8"))
        for name, content in files.items():
            if name != "__init__.py":
                archive.writestr(f"circuit_weaver/{name}", content.encode("utf-8"))
        archive.writestr("circuit_weaver-0.35.dist-info/METADATA", "Version: 0.35\n")
        archive.writestr("circuit_weaver-0.35.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    return wheel


def _installed_fixture(tmp_path, files):
    package = tmp_path / "site-packages" / "circuit_weaver"
    package.mkdir(parents=True)
    for name, content in files.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    return package


def test_expected_wheel_verification_matches_installed_code_and_resource_bytes(tmp_path):
    files = {"__init__.py": "__version__='0.35'\n", "core.py": "value = 1\n",
             "_bundled_skills/circuit-weaver/SKILL.md": "workflow help\n"}
    wheel = _wheel_fixture(tmp_path, files)
    package = _installed_fixture(tmp_path, files)

    evidence = _demo.verify_installed_wheel_payload(package, wheel)

    assert evidence["payload_matches"] is True
    assert evidence["payload_file_count"] == 3
    assert evidence["wheel_sha256"] == _demo.digest(wheel)
    assert evidence["expected_payload_sha256"] == evidence["installed_payload_sha256"]


def test_expected_wheel_rejects_stale_same_version_content(tmp_path):
    wheel = _wheel_fixture(tmp_path, {"__init__.py": "__version__='0.35'\n", "core.py": "value = 2\n"})
    package = _installed_fixture(
        tmp_path, {"__init__.py": "__version__='0.35'\n", "core.py": "value = 1\n"}
    )

    with pytest.raises(RuntimeError, match=r"changed=\['core.py'\]"):
        _demo.verify_installed_wheel_payload(package, wheel)


def test_runner_rejects_stale_expected_wheel_before_application_commands(tmp_path, monkeypatch):
    wheel = _wheel_fixture(tmp_path, {"__init__.py": "__version__='0.35'\n", "core.py": "value = 2\n"})
    package = _installed_fixture(
        tmp_path, {"__init__.py": "__version__='0.35'\n", "core.py": "value = 1\n"}
    )
    spec = tmp_path / "design.yaml"
    spec.write_text("project: demo\n", encoding="utf-8")
    output = tmp_path / "output"
    instances = []

    class RecordingDemo:
        def __init__(self, *args, **kwargs):
            self.commands = []
            self.cwd = output
            self.env = {}
            instances.append(self)

        def command(self, *args, **kwargs):
            self.commands.append(args)
            raise AssertionError("Application command started before wheel verification")

        def report(self, summary):
            self.summary = summary

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "rev-parse"]:
            stdout = "test-revision\n"
        elif command[:2] == ["git", "status"]:
            stdout = ""
        else:
            stdout = str(package / "__init__.py") + "\n"
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(_demo, "Demo", RecordingDemo)
    monkeypatch.setattr(_demo.subprocess, "run", fake_run)
    monkeypatch.setattr("sys.argv", [
        "validate_app_workflow.py", "--spec", str(spec), "--output", str(output),
        "--installed", "--expected-wheel", str(wheel),
    ])

    assert _demo.main() == 1
    assert instances[0].commands == []
    assert "does not match expected wheel" in instances[0].summary["error"]


def test_expected_wheel_rejects_missing_resource_and_unexpected_source(tmp_path):
    wheel = _wheel_fixture(tmp_path, {"core.py": "value = 1\n", "resources/table.json": "{}\n"})
    package = _installed_fixture(tmp_path, {"__init__.py": "__version__='0.35'\n", "core.py": "value = 1\n",
                                            "extra.py": "stale = True\n"})

    with pytest.raises(RuntimeError, match=r"missing=\['resources/table.json'\].*unexpected=\['extra.py'\]"):
        _demo.verify_installed_wheel_payload(package, wheel)


@pytest.mark.parametrize("payload", [b"not a zip archive", None])
def test_expected_wheel_rejects_invalid_archive_and_path_traversal(tmp_path, payload):
    package = _installed_fixture(tmp_path, {"__init__.py": "__version__='0.35'\n"})
    wheel = tmp_path / "bad.whl"
    if payload is None:
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("circuit_weaver/../../outside.py", "bad = True\n")
    else:
        wheel.write_bytes(payload)

    with pytest.raises(RuntimeError, match="Cannot inspect expected wheel|unsafe archive path"):
        _demo.verify_installed_wheel_payload(package, wheel)


def test_demo_rejects_preview_for_stale_board_or_changed_svg(tmp_path):
    board = tmp_path / "routed.kicad_pcb"
    board.write_text("verified board")
    svg = tmp_path / "board-preview.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    payload = {"status": "ok", "source_sha256": _demo.digest(board), "svg_sha256": _demo.digest(svg),
               "side": "front", "prototype": True, "outline_count": 1, "layers": ["F.Cu", "F.Fab"]}
    _demo.verify_preview(payload, board, svg, "front")
    board.write_text("changed board")
    with pytest.raises(RuntimeError, match="different board bytes"):
        _demo.verify_preview(payload, board, svg, "front")
    payload["source_sha256"] = _demo.digest(board)
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><path/></svg>')
    with pytest.raises(RuntimeError, match="SVG bytes"):
        _demo.verify_preview(payload, board, svg, "front")


def test_demo_retains_captured_subprocess_error_detail():
    error = subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"],
                                         stderr="fatal: detected dubious ownership in repository")

    assert "fatal: detected dubious ownership" in _demo.exception_message(error)


def _assembly_delivery(tmp_path, designators="U1,R1"):
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    items = [
        {"reference": "U1", "dnp": False, "exclude_from_bom": False},
        {"reference": "R1", "dnp": False, "exclude_from_bom": False},
        {"reference": "C1", "dnp": True, "exclude_from_bom": False},
        {"reference": "TP1", "dnp": False, "exclude_from_bom": True},
    ]
    (delivery / "assembly_manifest.json").write_text(
        json.dumps({"item_count": len(items), "items": items}), encoding="utf-8"
    )
    (delivery / "delivery_manifest.json").write_text(
        json.dumps({
            "status": "blocked", "fabrication_ready": False, "assembly_item_count": len(items),
            "artifacts": [
                {"kind": "bom", "path": "bom_jlcpcb.csv", "status": "ready"},
                {"kind": "cpl", "path": "cpl_jlcpcb.csv", "status": "blocked"},
            ],
        }), encoding="utf-8"
    )
    with (delivery / "bom_jlcpcb.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part#"])
        writer.writerow(["device", designators, "SOT-23", ""])
    return delivery, {"status": "blocked", "fabrication_ready": False,
                      "assembly_item_count": len(items), "bom_rows": 1}


def test_demo_verifies_blocked_bom_matches_active_assembly_inventory(tmp_path):
    delivery_dir, payload = _assembly_delivery(tmp_path)

    assert _demo.verify_blocked_assembly_delivery(payload, delivery_dir) == {
        "assembly_items": 4, "bom_rows": 1, "bom_designators": 2,
    }


@pytest.mark.parametrize("designators", ["U1", "U1,R1,R1", "U1,R1,EXTRA"])
def test_demo_rejects_bom_missing_duplicate_or_unexpected_designators(tmp_path, designators):
    delivery_dir, payload = _assembly_delivery(tmp_path, designators)

    with pytest.raises(RuntimeError, match="designator"):
        _demo.verify_blocked_assembly_delivery(payload, delivery_dir)
