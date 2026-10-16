"""Native KiCad integration contract for useful front/back board previews."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from circuit_weaver.kicad_bridge import find_kicad_python
from circuit_weaver.pcb_preview import render_board_preview

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "make_preview_pcb.py"
SVG_NS = "{http://www.w3.org/2000/svg}"


def _native_environment():
    python = find_kicad_python()
    footprint_roots = [
        Path(os.getenv("KICAD_FOOTPRINT_DIR", "/usr/share/kicad/footprints")),
        *(Path(f"C:/Program Files/KiCad/{version}/share/kicad/footprints")
          for version in ("10.0", "9.0", "8.0")),
    ]
    footprints = next((path for path in footprint_roots
                       if (path / "Resistor_SMD.pretty").is_dir()
                       and (path / "Connector_PinHeader_2.54mm.pretty").is_dir()), None)
    cli = shutil.which("kicad-cli")
    if not cli:
        cli = next((str(Path(f"C:/Program Files/KiCad/{version}/bin/kicad-cli.exe"))
                    for version in ("10.0", "9.0", "8.0")
                    if Path(f"C:/Program Files/KiCad/{version}/bin/kicad-cli.exe").is_file()), None)
    if not python or not footprints or not cli:
        reason = "Native board preview needs KiCad pcbnew, kicad-cli, and the standard test footprints"
        if os.getenv("CW_REQUIRE_NATIVE_PCB_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return python, footprints, cli


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _has_x_reflection(transform: str) -> bool:
    for kind, operands in re.findall(r"(scale|matrix)\(([^)]*)\)", transform):
        try:
            values = [float(value) for value in re.split(r"[\s,]+", operands.strip())]
        except ValueError:
            continue
        if kind == "scale" and len(values) >= 2 and values[0] < 0 < values[1]:
            return True
        if kind == "matrix" and len(values) == 6 and values[0] < 0 and values[1] == 0 \
                and values[2] == 0 and values[3] > 0:
            return True
    return False


def _translation_x(transform: str) -> float | None:
    match = re.search(r"translate\(\s*(-?\d+(?:\.\d+)?)", transform)
    return float(match.group(1)) if match else None


def _has_stroked_path(element: ET.Element, color: str) -> bool:
    def visit(node: ET.Element, inherited: dict[str, str]) -> bool:
        style = dict(inherited)
        style.update(
            (key.strip().lower(), value.strip().lower())
            for declaration in node.get("style", "").split(";")
            if ":" in declaration
            for key, value in [declaration.split(":", 1)]
        )
        tag = node.tag.rsplit("}", 1)[-1]
        if (tag in {"path", "line", "polyline"}
                and style.get("stroke") == color.lower()
                and style.get("fill") == "none"):
            return True
        return any(visit(child, style) for child in node)

    return visit(element, {})


def _evidence_copy(tmp_path: Path, metadata: dict) -> None:
    target = os.getenv("CW_NATIVE_PCB_EVIDENCE")
    if not target:
        return
    destination = Path(target) / "native-preview"
    shutil.copytree(tmp_path, destination, dirs_exist_ok=True)
    (destination / "result.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


@pytest.mark.skip_category("optional-tool")
def test_native_preview_preserves_board_geometry_and_physical_back_view(tmp_path, request):
    evidence_metadata = {}
    request.addfinalizer(lambda: _evidence_copy(tmp_path, evidence_metadata))
    python, footprints, cli = _native_environment()
    source = tmp_path / "preview-fixture.kicad_pcb"
    fixture_command = [python, str(FIXTURE), str(footprints), str(source)]
    fixture = subprocess.run(fixture_command, capture_output=True, text=True, timeout=60)
    if fixture.returncode:
        evidence_metadata.update({"fixture_command": fixture_command, "returncode": fixture.returncode,
                                  "stdout": fixture.stdout, "stderr": fixture.stderr})
        pytest.fail(f"Native preview fixture failed.\nstdout:\n{fixture.stdout}\nstderr:\n{fixture.stderr}")

    fixture_info = json.loads(fixture.stdout)
    original_hash = _sha256(source)
    source_project = source.with_suffix(".kicad_pro")
    project_hash = _sha256(source_project) if source_project.is_file() else None
    evidence_metadata.update({"fixture": fixture_info, "fixture_sha256": original_hash,
                              "project_sha256": project_hash})
    cli_version = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=20)
    assert cli_version.returncode == 0, cli_version.stdout + cli_version.stderr
    evidence_metadata["kicad_cli_version"] = cli_version.stdout.strip()

    outputs = {side: tmp_path / f"{side}-preview.svg" for side in ("front", "back")}
    results = {}
    evidence_metadata["results"] = results
    for side, output in outputs.items():
        results[side] = render_board_preview(
            source, output, side=side, timeout_seconds=120, overwrite=False, kicad_cli=cli,
        )
        assert results[side].get("status") == "ok", results[side]
        assert Path(results[side]["path"]).resolve() == output.resolve()
        assert results[side].get("prototype") is True, results[side]
        assert results[side].get("source_sha256") == original_hash, results[side]
        input_hashes = results[side].get("input_sha256")
        assert input_hashes and input_hashes.get(".kicad_pcb") == original_hash
        assert input_hashes.get(".kicad_pro") == project_hash
        assert results[side].get("kicad_version"), results[side]
        assert results[side].get("layers"), results[side]
        assert results[side].get("drill_geometry_count", 0) > 0
        assert results[side].get("svg_sha256") == _sha256(output), results[side]
        assert output.is_file() and output.stat().st_size > 1000

    assert _sha256(source) == original_hash, "Preview rendering changed the input PCB"
    project_unchanged = (
        source_project.is_file() and _sha256(source_project) == project_hash
        if project_hash else not source_project.exists()
    )
    assert project_unchanged, "Preview rendering changed or created the input project's settings sidecar"

    trees = {side: ET.parse(output).getroot() for side, output in outputs.items()}
    for side, root in trees.items():
        assert root.tag == f"{SVG_NS}svg"
        assert root.get("data-side-view") == ("native" if side == "front" else "mirrored-physical")
        assert root.find(".//*[@id='board-outline']") is not None
        silhouette = root.find(".//*[@id='board-silhouette']")
        assert silhouette is not None
        view_box = [float(value) for value in root.attrib["viewBox"].split()]
        assert len(view_box) == 4
        assert view_box[2:] == pytest.approx([50, 40], abs=0.1)
        offset = results[side].get("native_svg_offset_mm")
        assert offset
        assert view_box[:2] == pytest.approx([offset["x"], offset["y"]], abs=0.002)
        assert float(root.get("data-native-svg-offset-x-mm")) == pytest.approx(offset["x"], abs=0.002)
        assert float(root.get("data-native-svg-offset-y-mm")) == pytest.approx(offset["y"], abs=0.002)
        silhouette_coords = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", silhouette.get("d", ""))]
        assert len(silhouette_coords) >= 8
        assert min(silhouette_coords[::2]) == pytest.approx(view_box[0], abs=0.002)
        assert max(silhouette_coords[::2]) == pytest.approx(view_box[0] + 50, abs=0.002)
        assert min(silhouette_coords[1::2]) == pytest.approx(view_box[1], abs=0.002)
        assert max(silhouette_coords[1::2]) == pytest.approx(view_box[1] + 40, abs=0.002)
        title = root.find(f"{SVG_NS}title")
        assert title is not None and side in (title.text or "").lower()
        assert "mirrored physical view" in (title.text or "").lower() if side == "back" else \
            "mirrored" not in (title.text or "").lower()
        bounds = results[side].get("board_bounds_mm")
        assert bounds and bounds.get("x") == pytest.approx(20, abs=0.1)
        assert bounds and bounds.get("y") == pytest.approx(30, abs=0.1)
        assert bounds and bounds.get("width") == pytest.approx(50, abs=0.1)
        assert bounds.get("height") == pytest.approx(40, abs=0.1)
        if side == "back":
            back_view = root.find(".//*[@id='mirrored-physical-back-view']")
            assert back_view is not None and _has_x_reflection(back_view.get("transform", "")), \
                "Back view is labeled mirrored without reflecting physical board coordinates"
            assert _translation_x(back_view.get("transform", "")) == pytest.approx(
                2 * view_box[0] + view_box[2], abs=0.002
            ), "Back reflection is not anchored to the shared board coordinate frame"

    # Native outline geometry must remain a perimeter with a real hole. Do not
    # accept a synthetic filled rectangle that covers the Edge.Cuts cutout.
    front = trees["front"]
    front_paths = [element for element in front.iter(f"{SVG_NS}path") if element.get("d")]
    assert front_paths, "No native path geometry was rendered"
    silhouette = front.find(".//*[@id='board-silhouette']")
    assert silhouette is not None
    assert silhouette.get("fill-rule") == "evenodd" or "evenodd" in silhouette.get("style", ""), \
        "Board polygon does not declare even-odd hole filling"
    assert silhouette.get("data-hole-count") == "1"
    assert results["front"].get("cutout_count") == 1
    assert len({element.get("d") for element in front_paths}) > 5, "Preview has no component/board layer detail"

    # Front and back must render distinct native copper/footprint geometry.
    assert _sha256(outputs["front"]) != _sha256(outputs["back"]), "Front and back views are identical"
    assert trees["front"].get("viewBox") == trees["back"].get("viewBox"), \
        "Front and back layer geometry do not share one aligned coordinate frame"
    layers = {side: set(result["layers"]) for side, result in results.items()}
    assert any("F.Cu" in layer or "front" in layer.lower() for layer in layers["front"])
    assert any("B.Cu" in layer or "back" in layer.lower() for layer in layers["back"])
    for side in ("front", "back"):
        root = trees[side]
        fab_name = "F.Fab" if side == "front" else "B.Fab"
        fab_group = root.find(f".//*[@data-layer='{fab_name}']")
        assert fab_group is not None and list(fab_group), f"{fab_name} footprint geometry is missing"

    front_copper = front.find(".//*[@data-layer='F.Cu']")
    assert front_copper is not None
    assert _has_stroked_path(front_copper, "#e4bd72"), \
        "Front copper track strokes were lost or recolored as invisible"
    copper_drill_nodes = []
    for side, copper_layer in (("front", "F.Cu"), ("back", "B.Cu")):
        copper = trees[side].find(f".//*[@data-layer='{copper_layer}']")
        assert copper is not None
        copper_drill_nodes.extend(element for element in copper.iter()
                                  if element.get("data-drill-hole") == "true")
    assert copper_drill_nodes, "Native through-hole and NPTH drill geometry is missing"
    drill_geometry = {node for element in copper_drill_nodes for node in element.iter()}
    drill_tags = {element.tag.rsplit("}", 1)[-1] for element in drill_geometry}
    assert {"circle", "path"}.issubset(drill_tags), "Round holes or the oblong NPTH slot were lost"
    assert any("#10271b" in element.get("style", "").replace(" ", "").lower()
               for element in drill_geometry), "Drill holes no longer show the board void color"

    evidence_metadata["outputs"] = {
        side: {"path": output.name, "sha256": _sha256(output)}
        for side, output in outputs.items()
    }
