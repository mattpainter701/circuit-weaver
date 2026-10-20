"""The demo must reject plausible routing results for unverified board bytes."""

import base64
import csv
import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "workflow_demo", Path(__file__).resolve().parents[1] / "scripts" / "validate_app_workflow.py"
)
_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_demo)


def test_workflow_cases_keep_private_state_below_an_existing_parent_project(tmp_path):
    from circuit_weaver.project_state import ensure_project_state, resolve_project_root, save_project_state

    parent_state = save_project_state(tmp_path, ensure_project_state(tmp_path))
    original = parent_state.read_bytes()
    ids = []
    for name in ("case-a", "case-b"):
        case = tmp_path / name
        case.mkdir()
        _demo.initialize_workflow_project(case, cwd=tmp_path, env=os.environ.copy())
        local = case / ".circuit-weaver" / "project.json"
        ids.append(json.loads(local.read_text(encoding="utf-8"))["project_id"])
        assert resolve_project_root(case / "generated") == case
    assert len(set(ids)) == 2
    assert parent_state.read_bytes() == original


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


def test_demo_requires_return_refinement_preservation_and_connection_evidence(tmp_path):
    source, board = tmp_path / "routed-base.kicad_pcb", tmp_path / "routed.kicad_pcb"
    source.write_bytes(b"base")
    board.write_bytes(b"refined")
    payload = {
        "status": "ok", "output_kind": "refined_kicad_board",
        "source_sha256": _demo.digest(source),
        "refinement": {
            "original_copper_preserved": True,
            "original_footprints_and_text_preserved": True,
            "connections": [{"status": "routed"}],
        },
        "silkscreen": {"unresolved_count": 0, "remaining_conflict_count": 0},
        "drc": {"status": "ok", "passed": True, "blocker_count": 0,
                "board": str(board), "board_sha256": _demo.digest(board)},
    }
    _demo.verify_refinement(payload, board, source, 1)
    payload["refinement"]["original_copper_preserved"] = False
    with pytest.raises(RuntimeError, match="copper preservation"):
        _demo.verify_refinement(payload, board, source, 1)


def test_return_plan_preflight_uses_demo_source_package_environment(tmp_path):
    plan = tmp_path / "return-paths.json"
    plan.write_text('{"connections":[{"from_ref":"J1","from_pad":"1",'
                    '"to_ref":"C1","to_pad":"2"}]}', encoding="utf-8")
    demo = _demo.Demo(tmp_path, 30)
    stale_package = tmp_path / "stale-site-packages" / "circuit_weaver"
    stale_package.mkdir(parents=True)
    (stale_package / "__init__.py").write_text("", encoding="utf-8")
    env = demo.env.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(_demo.REPO / "src"), str(stale_package.parent)))

    result = _demo.load_return_plan_in_runtime(plan, cwd=demo.cwd, env=env)

    assert result == {"connections": [{"from_ref": "J1", "from_pad": "1", "to_ref": "C1", "to_pad": "2"}]}


def test_cleanup_plan_preflight_uses_demo_source_package_environment(tmp_path):
    plan = tmp_path / "cleanup.json"
    plan.write_text('{"nets":["GND"],"protected_connections":[{"from_ref":"J1",'
                    '"from_pad":"1","to_ref":"C1","to_pad":"2"}]}', encoding="utf-8")
    demo = _demo.Demo(tmp_path, 30)
    stale_package = tmp_path / "stale-site-packages" / "circuit_weaver"
    stale_package.mkdir(parents=True)
    (stale_package / "__init__.py").write_text("", encoding="utf-8")
    env = demo.env.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(_demo.REPO / "src"), str(stale_package.parent)))

    result = _demo.load_cleanup_plan_in_runtime(plan, cwd=demo.cwd, env=env)

    assert result == {"nets": ["GND"], "protected_connections": [
        {"from_ref": "J1", "from_pad": "1", "to_ref": "C1", "to_pad": "2"},
    ]}


def test_ground_plane_plan_preflight_uses_demo_source_package_environment(tmp_path):
    plan = tmp_path / "ground-plane.json"
    plan.write_text(json.dumps({
        "net": "GND", "layers": ["F.Cu", "B.Cu"], "edge_clearance_mm": 0.5,
        "clearance_mm": 0.25, "min_thickness_mm": 0.2,
        "thermal_gap_mm": 0.25, "thermal_spoke_width_mm": 0.3,
        "protected_connections": [],
    }), encoding="utf-8")
    demo = _demo.Demo(tmp_path, 30)
    stale_package = tmp_path / "stale-site-packages" / "circuit_weaver"
    stale_package.mkdir(parents=True)
    (stale_package / "__init__.py").write_text("", encoding="utf-8")
    env = demo.env.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(_demo.REPO / "src"), str(stale_package.parent)))

    result = _demo.load_ground_plane_plan_in_runtime(plan, cwd=demo.cwd, env=env)

    assert result == json.loads(plan.read_text(encoding="utf-8"))


def _ground_plane_evidence(tmp_path):
    source, board = tmp_path / "cleaned.kicad_pcb", tmp_path / "grounded.kicad_pcb"
    source.write_bytes(b"cleaned")
    board.write_bytes(b"grounded")
    connection = {"from_ref": "C1", "from_pad": "1", "to_ref": "U1", "to_pad": "1"}
    plan = {"net": "GND", "layers": ["F.Cu", "B.Cu"], "protected_connections": [connection]}
    payload = {
        "status": "ok", "output_kind": "ground_plane_kicad_board", "source_sha256": _demo.digest(source),
        "ground_plane": {
            "net": "GND", "layers": ["F.Cu", "B.Cu"], "tracks_preserved": True,
            "original_footprints_and_text_preserved": True, "saved_fill_geometry_verified": True,
            "original_board_geometry_preserved": True,
            "saved_connectivity_verified": True, "filled_area_mm2": 200.0,
            "added_zones": 2, "filled_zones": 2,
            "zone_stats": [{"polygon_count": 1, "island_count": 0, "polygon_sha256": "a" * 64},
                           {"polygon_count": 1, "island_count": 0, "polygon_sha256": "b" * 64}],
            "zone_connected_copper_counts": [1, 2], "island_count": 0,
            "protected_connections": [{**connection, "status": "preserved"}],
        },
        "silkscreen": {"unresolved_count": 0, "remaining_conflict_count": 0},
        "drc": {"status": "ok", "passed": True, "blocker_count": 0,
                "board": str(board), "board_sha256": _demo.digest(board)},
    }
    return source, board, plan, payload


def test_plane_cleanup_preflight_uses_demo_source_environment(tmp_path):
    plan = tmp_path / "plane-cleanup.json"
    content = {"net": "GND", "protected_connections": [
        {"from_ref": "C1", "from_pad": "1", "to_ref": "U1", "to_pad": "1"},
    ]}
    plan.write_text(json.dumps(content), encoding="utf-8")
    demo = _demo.Demo(tmp_path, 30)
    stale_package = tmp_path / "stale-site-packages" / "circuit_weaver"
    stale_package.mkdir(parents=True)
    (stale_package / "__init__.py").write_text("", encoding="utf-8")
    env = demo.env.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(_demo.REPO / "src"), str(stale_package.parent)))
    assert _demo.load_plane_cleanup_plan_in_runtime(plan, cwd=demo.cwd, env=env) == content


def test_plane_cleanup_workflow_requires_plane_stage_before_creating_output(tmp_path):
    output = tmp_path / "should-not-exist"
    result = subprocess.run([
        os.sys.executable, str(_demo.REPO / "scripts/validate_app_workflow.py"),
        "--plane-cleanup", "plan.json", "--output", str(output),
    ], capture_output=True, text=True)
    assert result.returncode == 2
    assert "--plane-cleanup requires --ground-plane" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["source", "output", "coverage", "vias", "inventory", "endpoint"])
def test_demo_rejects_stale_or_incomplete_plane_cleanup_evidence(tmp_path, mutation):
    source, board = tmp_path / "grounded.kicad_pcb", tmp_path / "cleaned.kicad_pcb"
    source.write_bytes(b"grounded")
    board.write_bytes(b"cleaned")
    pair = {"from_ref": "C1", "from_pad": "1", "to_ref": "U1", "to_pad": "1"}
    plan = {"net": "GND", "protected_connections": [pair]}
    report = dict.fromkeys((
        "existing_filled_planes_verified", "only_covered_unlocked_traces_removed", "no_copper_added",
        "vias_preserved", "nonselected_copper_preserved", "locked_copper_preserved", "original_geometry_preserved",
        "original_board_geometry_preserved",
        "saved_fill_geometry_verified", "saved_coverage_verified", "saved_connectivity_verified",
        "protected_paths_preserved",
    ), True)
    report.update(net="GND", removed_count=1, track_count_before=3, track_count_after=2,
                  removed_tracks=[{"uuid": "removed"}],
                  protected_connections=[{**pair, "status": "preserved", "protected_path_preserved": True}])
    payload = {"status": "ok", "output_kind": "plane_cleaned_kicad_board", "source_sha256": _demo.digest(source),
               "plane_cleanup": report, "silkscreen": {"unresolved_count": 0, "remaining_conflict_count": 0},
               "drc": {"status": "ok", "passed": True, "blocker_count": 0,
                       "board": str(board), "board_sha256": _demo.digest(board)}}
    _demo.verify_plane_cleanup(payload, board, source, plan)
    if mutation == "source":
        source.write_bytes(b"changed")
    elif mutation == "output":
        board.write_bytes(b"changed")
    elif mutation == "coverage":
        report["saved_coverage_verified"] = False
    elif mutation == "vias":
        report.pop("vias_preserved")
    elif mutation == "inventory":
        report["removed_count"] = 2
    else:
        report["protected_connections"][0]["to_pad"] = "2"
    with pytest.raises(RuntimeError):
        _demo.verify_plane_cleanup(payload, board, source, plan)


@pytest.mark.parametrize("flag", [
    "tracks_preserved", "original_footprints_and_text_preserved",
    "original_board_geometry_preserved",
    "saved_fill_geometry_verified", "saved_connectivity_verified",
])
def test_demo_ground_plane_requires_native_preservation_and_saved_fill(tmp_path, flag):
    source, board, plan, payload = _ground_plane_evidence(tmp_path)
    _demo.verify_ground_plane(payload, board, source, plan)
    payload["ground_plane"].pop(flag)
    with pytest.raises(RuntimeError, match="did not confirm"):
        _demo.verify_ground_plane(payload, board, source, plan)


@pytest.mark.parametrize("change, message", [
    ("source", "different source board bytes"),
    ("board", "final board bytes"),
    ("net", "net or layers"),
    ("unfilled", "missing or unfilled"),
    ("endpoint", "unverified protected connection"),
    ("nan_area", "no native filled copper"),
])
def test_demo_ground_plane_rejects_stale_or_incomplete_evidence(tmp_path, change, message):
    source, board, plan, payload = _ground_plane_evidence(tmp_path)
    if change == "source":
        source.write_bytes(b"changed input")
    elif change == "board":
        board.write_bytes(b"changed output")
    elif change == "net":
        payload["ground_plane"]["net"] = "VDD"
    elif change == "unfilled":
        payload["ground_plane"]["filled_zones"] = 1
    elif change == "endpoint":
        payload["ground_plane"]["protected_connections"][0]["to_pad"] = "2"
    else:
        payload["ground_plane"]["filled_area_mm2"] = float("nan")
    with pytest.raises(RuntimeError, match=message):
        _demo.verify_ground_plane(payload, board, source, plan)


def test_demo_binds_cleanup_input_evidence_and_all_preservation_flags(tmp_path):
    source, board = tmp_path / "routed.kicad_pcb", tmp_path / "cleaned.kicad_pcb"
    source.write_bytes(b"routed")
    board.write_bytes(b"cleaned")
    cleanup = {
        "all_pads_connected": True, "protected_paths_preserved": True,
        "unselected_copper_preserved": True, "original_footprints_and_text_preserved": True,
        "copper_changes_limited_to_selected_nets": True, "locked_copper_preserved": True,
        "protected_connections": [{"status": "connected", "protected_path_preserved": True}],
    }
    payload = {
        "status": "ok", "output_kind": "cleaned_kicad_board", "source_sha256": _demo.digest(source),
        "cleanup": cleanup,
        "silkscreen": {"unresolved_count": 0, "remaining_conflict_count": 0},
        "drc": {"status": "ok", "passed": True, "blocker_count": 0,
                "board": str(board), "board_sha256": _demo.digest(board)},
    }

    _demo.verify_cleanup(payload, board, source, 1)
    cleanup["copper_changes_limited_to_selected_nets"] = False
    with pytest.raises(RuntimeError, match="copper changes limited to selected nets"):
        _demo.verify_cleanup(payload, board, source, 1)


def test_demo_rejects_cleanup_bound_to_changed_refinement_input(tmp_path):
    source, board = tmp_path / "routed-refined.kicad_pcb", tmp_path / "cleaned.kicad_pcb"
    source.write_bytes(b"refined")
    board.write_bytes(b"cleaned")
    payload = {
        "status": "ok", "output_kind": "cleaned_kicad_board", "source_sha256": "stale",
        "cleanup": {}, "drc": {},
    }

    with pytest.raises(RuntimeError, match="different source board bytes"):
        _demo.verify_cleanup(payload, board, source, 0)


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


def _assembly_preview_fixture(tmp_path):
    board = tmp_path / "routed.kicad_pcb"
    board.write_text("board with native models")
    model_file = tmp_path / "Part.step"
    model_file.write_text("native model")
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 3, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00\x00\x00\xff" * 2
                                          + b"\x00" + b"\x00\x00\x00\xff" * 2
                                          + b"\x00" + b"\x00\x00\x00\xff" * 2))
           + chunk(b"IEND", b""))
    encoded = base64.b64encode(png).decode("ascii")
    svg = tmp_path / "assembly.svg"
    png_hash = hashlib.sha256(png).hexdigest()
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="2px" height="3px" viewBox="0 0 2 3" '
        'data-appearance="assembly" data-side="front">'
        f'<metadata source_sha256="{_demo.digest(board)}" png_sha256="{png_hash}" '
        'png_width="2" png_height="3" side="front" appearance="assembly" '
        'model_expected="1" model_resolved="1" model_missing="0" '
        'component_expected="1" component_represented="1" component_missing="0"/>'
        f'<image id="assembled-board-models" x="0" y="0" width="2" height="3" '
        f'preserveAspectRatio="xMidYMid meet" href="data:image/png;base64,{encoded}"/>'
        '</svg>', encoding="utf-8",
    )
    payload = {
        "status": "ok", "appearance": "assembly", "source_sha256": _demo.digest(board),
        "svg_sha256": _demo.digest(svg), "output_path": str(svg), "side": "front", "prototype": True,
        "embedded_png_sha256": png_hash, "png_width": 2, "png_height": 3,
        "component_coverage": {"expected": 1, "represented": 1, "missing": 0,
                               "missing_references": [],
                               "components": [{"reference": "U1", "required": True, "visible_model_count": 1}]},
        "model_preview": {
            "status": "ok", "source_sha256": _demo.digest(board), "png_sha256": png_hash, "side": "top",
            "png_width": 2, "png_height": 3,
            "model_coverage": {"expected": 1, "resolved": 1, "missing": 0,
                               "models": [{"source_path": "${KICAD9_3DMODEL_DIR}/Part.step",
                                           "resolved_path": str(model_file), "sha256": _demo.digest(model_file)}]},
        },
    }
    return board, svg, payload


def test_demo_verifies_self_contained_assembly_preview(tmp_path):
    board, svg, payload = _assembly_preview_fixture(tmp_path)

    _demo.verify_preview(payload, board, svg, "front", "assembly")


@pytest.mark.parametrize("mutation, message", [
    ("missing_models", "no native 3D models"),
    ("unresolved_model", "missing or unresolved models"),
    ("missing_image", "exactly one embedded model image"),
    ("external_image", "not embedded as a PNG data URL"),
    ("invalid_png_base64", "invalid base64"),
    ("wrong_png_hash", "hash does not match"),
    ("stale_model", "source changed after rendering"),
    ("missing_component", "omits one or more populated electrical components"),
])
def test_demo_rejects_invalid_assembly_preview_evidence(tmp_path, mutation, message):
    board, svg, payload = _assembly_preview_fixture(tmp_path)
    if mutation == "missing_models":
        payload["model_preview"]["model_coverage"].update(expected=0, resolved=0, missing=0, models=[])
    elif mutation == "unresolved_model":
        payload["model_preview"]["model_coverage"].update(expected=2, resolved=1, missing=1)
        payload["model_preview"]["model_coverage"]["models"].append({"resolved_path": None})
    elif mutation == "missing_image":
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" data-appearance="assembly" data-side="front"/>',
            encoding="utf-8",
        )
        payload["svg_sha256"] = _demo.digest(svg)
    elif mutation == "external_image":
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" data-appearance="assembly" data-side="front">'
                       '<image id="assembled-board-models" href="https://example.com/model.png"/></svg>',
                       encoding="utf-8")
        payload["svg_sha256"] = _demo.digest(svg)
    elif mutation == "invalid_png_base64":
        svg.write_text(svg.read_text(encoding="utf-8").replace(
            "data:image/png;base64,", "data:image/png;base64,not-base64!"
        ), encoding="utf-8")
        payload["svg_sha256"] = _demo.digest(svg)
    elif mutation == "wrong_png_hash":
        payload["embedded_png_sha256"] = "0" * 64
    elif mutation == "stale_model":
        Path(payload["model_preview"]["model_coverage"]["models"][0]["resolved_path"]).write_text("changed")
    elif mutation == "missing_component":
        payload["component_coverage"].update(expected=2, represented=1, missing=1)

    with pytest.raises(RuntimeError, match=message):
        _demo.verify_preview(payload, board, svg, "front", "assembly")


def test_demo_rejects_assembly_svg_external_resource_references(tmp_path):
    board, svg, payload = _assembly_preview_fixture(tmp_path)
    svg.write_text(svg.read_text(encoding="utf-8").replace(
        "data-appearance=\"assembly\"", "data-appearance=\"assembly\" href=\"https://example.com/a.svg\""
    ), encoding="utf-8")
    payload["svg_sha256"] = _demo.digest(svg)

    with pytest.raises(RuntimeError, match="external resource reference"):
        _demo.verify_preview(payload, board, svg, "front", "assembly")


def test_demo_rejects_stale_assembly_board_binding(tmp_path):
    board, svg, payload = _assembly_preview_fixture(tmp_path)
    board.write_text("changed after assembly render")

    with pytest.raises(RuntimeError, match="different board bytes"):
        _demo.verify_preview(payload, board, svg, "front", "assembly")


@pytest.mark.parametrize("change, message", [
    ("shift_image", "does not preserve the PNG aspect ratio"),
    ("conflicting_fallback", "conflicting SVG2 and xlink image references"),
])
def test_demo_rejects_assembly_svg_frame_or_fallback_mismatch(tmp_path, change, message):
    board, svg, payload = _assembly_preview_fixture(tmp_path)
    content = svg.read_text(encoding="utf-8")
    if change == "shift_image":
        content = content.replace('x="0" y="0"', 'x="1" y="0"')
    else:
        content = content.replace(
            'xmlns="http://www.w3.org/2000/svg"',
            'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"',
        ).replace(
            'href="data:image/png;base64,',
            'xlink:href="data:image/png;base64,AAAA" href="data:image/png;base64,',
        )
    svg.write_text(content, encoding="utf-8")
    payload["svg_sha256"] = _demo.digest(svg)

    with pytest.raises(RuntimeError, match=message):
        _demo.verify_preview(payload, board, svg, "front", "assembly")


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
