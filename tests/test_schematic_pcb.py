from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from circuit_weaver.footprint_lib import KiCadFootprintLibrary
from circuit_weaver.pcb_stackup import (
    expected_copper_layer_names,
    validate_copper_layers,
)
from circuit_weaver.schematic_pcb import (
    _annotation_errors_present,
    _copy_schematic_project,
    _native_build_timeout_seconds,
    _parse_netlist,
    _worker_result,
    schematic_to_pcb,
)


@pytest.mark.parametrize("count,names", [
    (2, ("F.Cu", "B.Cu")),
    (4, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")),
])
def test_supported_copper_layer_arrangements(count, names):
    assert validate_copper_layers(count) == count
    assert expected_copper_layer_names(count) == names


@pytest.mark.parametrize("count", [True, False, 3, 6, 2.0, "4", None])
def test_copper_layer_count_rejects_bool_noninteger_and_unsupported(count):
    with pytest.raises(ValueError, match="integer 2 or 4"):
        validate_copper_layers(count)


def _fixture_library(tmp_path: Path, pads: tuple[str, ...] = ("1", "2")) -> KiCadFootprintLibrary:
    pretty = tmp_path / "Test.pretty"
    pretty.mkdir(exist_ok=True)
    pad_rows = "\n".join(f'  (pad "{number}" smd rect (at {index * 2} 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))'
                          for index, number in enumerate(pads))
    (pretty / "TEST_2PAD.kicad_mod").write_text(
        f'(footprint "TEST_2PAD" (version 20240108) (generator "pcbnew")\n{pad_rows}\n)',
        encoding="utf-8",
    )
    return KiCadFootprintLibrary(tmp_path)


def _write_netlist(path: Path, *, pin_nums=("1", "2"), pad_no_connect=True, doctype="") -> Path:
    pins = "".join(f'<pin num="{number}"/>' for number in pin_nums)
    nc = (
        '<net code="2" name="unconnected-(U1-2-Pad2)">'
        '<node ref="U1" pin="2" pintype="passive+no_connect"/></net>'
        if pad_no_connect and "2" in pin_nums else ""
    )
    path.write_text(
        f'''{doctype}<export version="E"><components><comp ref="U1"><value>TEST</value>
        <footprint>Test:TEST_2PAD</footprint><libsource lib="Test" part="TEST"/>
        <units><unit name="A"><pins>{pins}</pins></unit></units>
        </comp></components><nets><net code="1" name="SIG"><node ref="U1" pin="1" pintype="passive"/></net>
        {nc}</nets></export>''',
        encoding="utf-8",
    )
    return path


def test_native_build_timeout_scales_with_design_size_and_has_hard_cap():
    assert _native_build_timeout_seconds(0, 1) == 90
    assert _native_build_timeout_seconds(768, 66) == 477
    assert _native_build_timeout_seconds(1202, 66) == 586
    assert _native_build_timeout_seconds(5000, 200) == 600


@pytest.mark.parametrize("components,sheets", [(True, 1), (-1, 1), (0, 0), (1.5, 1), (0, False)])
def test_native_build_timeout_rejects_invalid_design_counts(components, sheets):
    with pytest.raises(ValueError, match="component and sheet counts"):
        _native_build_timeout_seconds(components, sheets)


def test_netlist_maps_only_declared_pin_numbers_and_preserves_explicit_no_connect(tmp_path):
    library = _fixture_library(tmp_path)
    model = _parse_netlist(_write_netlist(tmp_path / "board.xml"), library)
    assert model["nets"] == ["SIG"]
    assert model["components"][0]["nets"] == {"1": "SIG"}
    assert model["explicit_no_connects"] == [{"reference": "U1", "pin": "2"}]


def test_legacy_project_rules_cannot_be_ignored_during_forward_generation(tmp_path, monkeypatch):
    from circuit_weaver import schematic_pcb

    source = tmp_path / "source.kicad_sch"
    source.write_text("(kicad_sch)")
    source.with_suffix(".pro").write_text("authored legacy rules")
    monkeypatch.setattr(schematic_pcb, "_kicad_cli_path", lambda: pytest.fail("probe preceded source-rule validation"))
    result = schematic_to_pcb(source, tmp_path / "review.kicad_pcb")
    assert result["status"] == "error" and "conversion" in result["message"]
    assert not (tmp_path / "review.kicad_pcb").exists()


def test_overwrite_publishes_the_same_derived_project_settings_that_drc_validated(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from circuit_weaver import schematic_pcb

    source = tmp_path / "source.kicad_sch"
    source.write_text("(kicad_sch)")
    original_project = b'{"schematic":{"authored":"retained"}}'
    source.with_suffix(".kicad_pro").write_bytes(original_project)
    target = tmp_path / "review.kicad_pcb"
    target.write_text("previous board")
    target.with_suffix(".kicad_pro").write_bytes(original_project)
    library = _fixture_library(tmp_path)
    monkeypatch.setattr(schematic_pcb, "KiCadFootprintLibrary", lambda **kwargs: library)
    monkeypatch.setattr(schematic_pcb, "_kicad_cli_path", lambda: Path("kicad-cli"))
    monkeypatch.setattr(schematic_pcb, "find_kicad_python", lambda *args: "native-python")
    validated = []

    def run(command, **kwargs):
        if "--format" in command:
            _write_netlist(Path(command[command.index("--output") + 1]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        request = json.loads(kwargs["input"])
        board = Path(request["board"])
        board.write_text("(kicad_pcb)")
        board.with_suffix(".kicad_pro").write_text(json.dumps({
            "net_settings": {"classes": [{"name": "Default", "clearance": 0.127}]},
            "schematic": {"native": "discarded"},
        }))
        return SimpleNamespace(returncode=0, stderr="", stdout="CW_SCHEMATIC_PCB_RESULT=" + json.dumps({
            "status": "ok", "footprints": 1, "pads": 2, "nets": 1,
            "copper_layer_count": request["copper_layers"],
            "copper_layer_names": list(expected_copper_layer_names(request["copper_layers"])),
        }))

    def drc(board, **kwargs):
        validated.append(board.with_suffix(".kicad_pro").read_bytes())
        assert json.loads(validated[-1])["net_settings"]["classes"][0]["clearance"] == 0.127
        return SimpleNamespace(status="ok", findings=[])

    monkeypatch.setattr(schematic_pcb.subprocess, "run", run)
    monkeypatch.setattr(schematic_pcb, "run_drc", drc)
    result = schematic_to_pcb(source, target, overwrite=True, clearance_mm=0.127)
    assert result["status"] == "ok", result
    assert target.with_suffix(".kicad_pro").read_bytes() == validated[0]
    assert json.loads(validated[0])["schematic"] == {"authored": "retained"}
    assert source.with_suffix(".kicad_pro").read_bytes() == original_project


@pytest.mark.parametrize("exclusion", [
    '<property name="exclude_from_board"/>',
    '<property name="exclude_from_bom"/><property name="exclude_from_board"/>',
])
def test_native_xml_board_exclusion_preserves_real_components_and_nets(tmp_path, exclusion):
    library = _fixture_library(tmp_path)
    path = _write_netlist(tmp_path / "board.xml")
    path.write_text(path.read_text(encoding="utf-8").replace(
        "</components>",
        f'<comp ref="RP1"><value>PULLUPS_ONLY</value>{exclusion}</comp></components>',
    ).replace(
        '<node ref="U1" pin="1" pintype="passive"/>',
        '<node ref="U1" pin="1" pintype="passive"/><node ref="RP1" pin="3" pintype="passive"/>',
    ), encoding="utf-8")
    model = _parse_netlist(path, library)
    assert [component["reference"] for component in model["components"]] == ["U1"]
    assert model["components"][0]["nets"] == {"1": "SIG"}
    assert model["nets"] == ["SIG"]


@pytest.mark.parametrize("properties", ["", '<property name="exclude_from_bom"/>'])
def test_missing_footprint_is_not_a_board_exclusion(tmp_path, properties):
    library = _fixture_library(tmp_path)
    path = _write_netlist(tmp_path / "board.xml")
    path.write_text(path.read_text(encoding="utf-8").replace(
        "</components>", f'<comp ref="U2"><value>Physical</value>{properties}</comp></components>',
    ), encoding="utf-8")
    with pytest.raises(ValueError, match="U2: schematic component has no footprint"):
        _parse_netlist(path, library)


def test_netlist_rejects_symbol_pin_missing_from_footprint(tmp_path):
    library = _fixture_library(tmp_path, pads=("1",))
    with pytest.raises(ValueError, match=r"missing pads: \['2'\]"):
        _parse_netlist(_write_netlist(tmp_path / "board.xml"), library)


def test_netlist_rejects_extra_footprint_pad(tmp_path):
    library = _fixture_library(tmp_path, pads=("1", "2", "SH"))
    with pytest.raises(ValueError, match=r"extra pads: \['SH'\]"):
        _parse_netlist(_write_netlist(tmp_path / "board.xml"), library)


def test_netlist_rejects_conflicting_pin_nets(tmp_path):
    library = _fixture_library(tmp_path)
    path = _write_netlist(tmp_path / "board.xml", pad_no_connect=False)
    xml = path.read_text(encoding="utf-8").replace(
        "</nets>", '<net code="2" name="OTHER"><node ref="U1" pin="1" pintype="passive"/></net></nets>'
    )
    path.write_text(xml, encoding="utf-8")
    with pytest.raises(ValueError, match="assigns the pin to multiple nets"):
        _parse_netlist(path, library)


def test_netlist_rejects_document_type_declarations(tmp_path):
    library = _fixture_library(tmp_path)
    path = _write_netlist(tmp_path / "board.xml", doctype='<!DOCTYPE export [<!ENTITY x "x">]>')
    with pytest.raises(ValueError, match="must not declare a document type or entity"):
        _parse_netlist(path, library)


def test_netlist_uses_libparts_pin_list_when_component_units_are_absent(tmp_path):
    library = _fixture_library(tmp_path)
    path = _write_netlist(tmp_path / "board.xml", pad_no_connect=False)
    xml = re.sub(r"<units>.*?</units>", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    xml = xml.replace(
        "</components>",
        "</components><libparts><libpart lib=\"Test\" part=\"TEST\"><pins>"
        "<pin num=\"1\"/><pin num=\"2\"/></pins></libpart></libparts>",
    )
    path.write_text(xml, encoding="utf-8")
    model = _parse_netlist(path, library)
    assert model["components"][0]["symbol_pins"] == ["1", "2"]


def test_existing_output_is_preserved_without_overwrite(tmp_path):
    schematic = tmp_path / "input.kicad_sch"
    schematic.write_text("not parsed because output conflicts", encoding="utf-8")
    output = tmp_path / "output.kicad_pcb"
    output.write_bytes(b"previous board")
    result = schematic_to_pcb(schematic, output)
    assert result["status"] == "error"
    assert "already exists" in result["message"]
    assert output.read_bytes() == b"previous board"


def test_failed_netlist_export_preserves_overwrite_target_and_settings(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import circuit_weaver.schematic_pcb as module

    schematic = tmp_path / "input.kicad_sch"
    schematic.write_text("schematic", encoding="utf-8")
    output = tmp_path / "output.kicad_pcb"
    project = output.with_suffix(".kicad_pro")
    rules = output.with_suffix(".kicad_dru")
    output.write_bytes(b"previous board")
    project.write_bytes(b"previous project")
    rules.write_bytes(b"previous rules")
    schematic.with_suffix(".kicad_pro").write_bytes(b"previous project")
    schematic.with_suffix(".kicad_dru").write_bytes(b"previous rules")
    monkeypatch.setattr(module, "_kicad_cli_path", lambda: Path("kicad-cli"))
    monkeypatch.setattr(module, "find_kicad_python", lambda _path=None: "kicad-python")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="export failed", stdout=""),
    )

    result = schematic_to_pcb(schematic, output, overwrite=True)
    assert result["status"] == "error"
    assert "netlist export failed" in result["message"]
    assert output.read_bytes() == b"previous board"
    assert project.read_bytes() == b"previous project"
    assert rules.read_bytes() == b"previous rules"
    assert not list(tmp_path.glob(".cw-c-*"))


def test_unrelated_output_project_settings_are_rejected_before_kicad_probe(tmp_path, monkeypatch):
    import circuit_weaver.schematic_pcb as module

    monkeypatch.setattr(module, "_kicad_cli_path", lambda: None)
    monkeypatch.setattr(module, "find_kicad_python", lambda _path=None: None)
    schematic = tmp_path / "input.kicad_sch"
    schematic.write_text("schematic", encoding="utf-8")
    output = tmp_path / "output.kicad_pcb"
    output.with_suffix(".kicad_pro").write_text("unrelated project", encoding="utf-8")
    result = schematic_to_pcb(schematic, output, overwrite=True)
    assert result["status"] == "error"
    assert "unrelated project settings" in result["message"]
    assert output.with_suffix(".kicad_pro").read_text(encoding="utf-8") == "unrelated project"


@pytest.mark.parametrize("clearance", [0, -0.1, float("nan"), float("inf"), True])
def test_clearance_must_be_finite_positive_mm(tmp_path, clearance):
    schematic = tmp_path / "input.kicad_sch"
    schematic.write_text("schematic", encoding="utf-8")
    result = schematic_to_pcb(schematic, tmp_path / "output.kicad_pcb", clearance_mm=clearance)
    assert result["status"] == "error"
    assert "finite positive" in result["message"]


@pytest.mark.parametrize("minimum", [0, -0.1, float("nan"), float("inf"), True])
def test_minimum_drill_must_be_finite_positive_mm(tmp_path, minimum):
    schematic = tmp_path / "input.kicad_sch"
    schematic.write_text("schematic", encoding="utf-8")
    result = schematic_to_pcb(schematic, tmp_path / "output.kicad_pcb", minimum_drill_mm=minimum)
    assert result["status"] == "error"
    assert "finite positive" in result["message"]


@pytest.mark.parametrize("settings", [".kicad_pro", ".kicad_dru"])
def test_minimum_drill_cannot_override_existing_project_rules(tmp_path, settings):
    schematic = tmp_path / "input.kicad_sch"
    schematic.write_text("schematic", encoding="utf-8")
    prior = ('{"board":{"design_settings":{"rules":{"min_hole_to_hole":0.3}}}}'
             if settings == ".kicad_pro" else "(version 1)")
    rules = schematic.with_suffix(settings)
    rules.write_text(prior, encoding="utf-8")
    result = schematic_to_pcb(schematic, tmp_path / "output.kicad_pcb", minimum_drill_mm=0.2)
    assert result["status"] == "error"
    assert "Cannot override minimum drill" in result["message"]
    assert rules.read_text(encoding="utf-8") == prior


def test_packing_uses_space_beside_large_module_without_losing_courtyard_clearance():
    from types import SimpleNamespace

    from circuit_weaver.schematic_pcb import _pack_components

    class Library:
        def geometry(self, name):
            width, height = (48.5, 41.54) if name == "module" else (1.86, 0.94)
            return SimpleNamespace(width_mm=width, height_mm=height, source="courtyard", confidence="verified",
                                   center_x_mm=2.1, center_y_mm=-3.7)

    items = [{"reference": "U1", "footprint": "module"}] + [
        {"reference": f"R{i}", "footprint": "resistor"} for i in range(1, 31)
    ]
    _pack_components(items, 80, 60, Library())
    boxes = []
    for item in items:
        geometry = Library().geometry(item["footprint"])
        x, y = item["x_mm"] + 2.1, item["y_mm"] - 3.7
        w, h = geometry.width_mm, geometry.height_mm
        assert 5 <= x - w / 2 and x + w / 2 <= 75
        assert 5 <= y - h / 2 and y + h / 2 <= 55
        boxes.append((x - w / 2, y - h / 2, x + w / 2, y + h / 2))
    for index, a in enumerate(boxes):
        for b in boxes[index + 1:]:
            assert a[2] + 3 <= b[0] + 1e-9 or b[2] + 3 <= a[0] + 1e-9 or (
                a[3] + 3 <= b[1] + 1e-9 or b[3] + 3 <= a[1] + 1e-9
            )
    first = [(item["x_mm"], item["y_mm"]) for item in items]
    _pack_components(items, 80, 60, Library())
    assert [(item["x_mm"], item["y_mm"]) for item in items] == first
    with pytest.raises(ValueError, match="do not fit"):
        _pack_components(items, 60, 52, Library())


def test_seed_packing_retries_bounded_gap_with_verified_envelope_clearance():
    from types import SimpleNamespace

    from circuit_weaver.schematic_pcb import _pack_components

    class Library:
        def geometry(self, _name):
            return SimpleNamespace(
                width_mm=5.0,
                height_mm=5.0,
                center_x_mm=2.0,
                center_y_mm=-1.0,
                source="courtyard",
                confidence="verified",
            )

    components = [{"reference": f"U{i}", "footprint": "verified-square"} for i in range(1, 5)]
    diagnostic = _pack_components(components, 21.5, 21.5, Library())
    assert diagnostic["attempted_gaps"] == [
        {"gap_mm": 3.0, "status": "no_fit", "methods": ["shelf", "max_rectangles"]},
        {"gap_mm": 1.5, "status": "placed", "method": "shelf"},
    ]
    assert diagnostic["used_gap_mm"] == 1.5

    geometry = Library().geometry("verified-square")
    boxes = []
    for component in components:
        center_x = component["x_mm"] + geometry.center_x_mm
        center_y = component["y_mm"] + geometry.center_y_mm
        box = (
            center_x - geometry.width_mm / 2,
            center_y - geometry.height_mm / 2,
            center_x + geometry.width_mm / 2,
            center_y + geometry.height_mm / 2,
        )
        assert 5.0 <= box[0] and box[2] <= 16.5
        assert 5.0 <= box[1] and box[3] <= 16.5
        boxes.append(box)
    for index, a in enumerate(boxes):
        for b in boxes[index + 1:]:
            assert (
                a[2] + 1.5 <= b[0] + 1e-9
                or b[2] + 1.5 <= a[0] + 1e-9
                or a[3] + 1.5 <= b[1] + 1e-9
                or b[3] + 1.5 <= a[1] + 1e-9
            )

    reordered = [{"reference": f"U{i}", "footprint": "verified-square"} for i in range(4, 0, -1)]
    reordered_diagnostic = _pack_components(reordered, 21.5, 21.5, Library())
    assert reordered_diagnostic["attempted_gaps"] == diagnostic["attempted_gaps"]
    assert {
        item["reference"]: (item["x_mm"], item["y_mm"]) for item in reordered
    } == {item["reference"]: (item["x_mm"], item["y_mm"]) for item in components}


def test_failed_seed_packing_preserves_positions_and_reports_lower_bounds():
    from types import SimpleNamespace

    from circuit_weaver.schematic_pcb import _pack_components, _SeedPackingFailure

    class Library:
        def geometry(self, _name):
            return SimpleNamespace(
                width_mm=8.0,
                height_mm=8.0,
                center_x_mm=0.0,
                center_y_mm=0.0,
                source="courtyard",
                confidence="verified",
            )

    components = [
        {"reference": f"U{i}", "footprint": "verified-large", "x_mm": float(i), "y_mm": -float(i)}
        for i in range(1, 5)
    ]
    original_positions = {item["reference"]: (item["x_mm"], item["y_mm"]) for item in components}
    with pytest.raises(_SeedPackingFailure, match="bounded courtyard-gap retries") as error:
        _pack_components(components, 21.5, 21.5, Library())

    assert {item["reference"]: (item["x_mm"], item["y_mm"]) for item in components} == original_positions
    diagnostic = error.value.diagnostic
    assert [attempt["gap_mm"] for attempt in diagnostic["attempted_gaps"]] == [3.0, 1.5, 1.0, 0.5]
    assert all(attempt["status"] == "no_fit" for attempt in diagnostic["attempted_gaps"])
    assert diagnostic["measured_courtyard_area_mm2"] == 256.0
    assert diagnostic["board_interior_area_mm2"] == 132.25
    assert diagnostic["minimum_dimensions_lower_bound_mm"]["width_at_least_mm"] == 18.0
    assert diagnostic["minimum_dimensions_lower_bound_mm"]["height_at_least_mm"] == 18.0
    assert diagnostic["minimum_dimensions_lower_bound_mm"]["width_from_area_at_requested_height_mm"] > 21.5
    assert diagnostic["minimum_dimensions_lower_bound_mm"]["height_from_area_at_requested_width_mm"] > 21.5
    assert "does not prove physical infeasibility" in diagnostic["diagnostic_scope"]


def test_explicit_clearance_rejects_existing_source_board_rules_before_kicad_probe(tmp_path, monkeypatch):
    import circuit_weaver.schematic_pcb as module

    monkeypatch.setattr(module, "_kicad_cli_path", lambda: None)
    monkeypatch.setattr(module, "find_kicad_python", lambda _path=None: None)
    schematic = tmp_path / "input.kicad_sch"
    schematic.write_text("schematic", encoding="utf-8")
    schematic.with_suffix(".kicad_pro").write_text(
        '{"net_settings":{"classes":[{"name":"Default","clearance":0.2}]}}',
        encoding="utf-8",
    )
    result = schematic_to_pcb(schematic, tmp_path / "output.kicad_pcb", clearance_mm=0.127)
    assert result["status"] == "error"
    assert "already defines board rules" in result["message"]


def test_explicit_clearance_rejects_source_design_rules_file_before_kicad_probe(tmp_path, monkeypatch):
    import circuit_weaver.schematic_pcb as module

    monkeypatch.setattr(module, "_kicad_cli_path", lambda: None)
    monkeypatch.setattr(module, "find_kicad_python", lambda _path=None: None)
    schematic = tmp_path / "input.kicad_sch"
    schematic.write_text("schematic", encoding="utf-8")
    schematic.with_suffix(".kicad_dru").write_text("(version 1)", encoding="utf-8")
    result = schematic_to_pcb(schematic, tmp_path / "output.kicad_pcb", clearance_mm=0.127)
    assert result["status"] == "error"
    assert "already defines board rules" in result["message"]


@pytest.mark.parametrize("sheetfile", ["missing.kicad_sch", "nested/child.kicad_sch"])
def test_schematic_copy_rejects_missing_or_nested_sheet_references(tmp_path, sheetfile):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "root.kicad_sch"
    source.write_text(f'(kicad_sch (property "Sheetfile" "{sheetfile}"))', encoding="utf-8")
    if sheetfile.startswith("nested/"):
        nested = source_dir / "nested"
        nested.mkdir()
        (nested / "child.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    destination = tmp_path / "staged"
    destination.mkdir()
    with pytest.raises(ValueError, match="hierarchical sheet"):
        _copy_schematic_project(source, destination)


def test_worker_success_payload_cannot_override_nonzero_exit_code():
    from subprocess import CompletedProcess

    process = CompletedProcess(
        args=["kicad-python"],
        returncode=1,
        stdout='CW_SCHEMATIC_PCB_RESULT={"status":"ok"}',
        stderr="worker failed",
    )
    result = _worker_result(process)
    assert result["status"] == "error"
    assert "exit code" in result["message"]


@pytest.mark.parametrize(
    ("output", "has_errors"),
    [
        ("Warning: Found 2 annotation errors", True),
        ("No annotation errors", False),
        ("Annotation errors: 0", False),
    ],
)
def test_annotation_warning_check_ignores_kicad_clean_status(output, has_errors):
    assert _annotation_errors_present(output) is has_errors


@pytest.mark.skip_category("optional-tool")
def test_real_kicad_schematic_produces_pad_bearing_review_board(tmp_path):
    schematic_value = os.environ.get("CW_REAL_KICAD_SCHEMATIC")
    if not schematic_value:
        pytest.skip("set CW_REAL_KICAD_SCHEMATIC to a real KiCad schematic")
    schematic = Path(schematic_value)
    if not schematic.is_file():
        pytest.skip(f"real KiCad schematic is unavailable: {schematic}")
    output = tmp_path / "prototype.kicad_pcb"
    result = schematic_to_pcb(schematic, output, clearance_mm=0.127)
    assert result["status"] == "ok", result
    assert result["output_kind"] == "prototype_pad_bearing_board"
    assert result["fabrication_ready"] is False
    assert result["counts"]["pads"] > 0
    assert result["validation"]["pcbnew_round_trip"] == "passed"
    assert result["validation"]["physical_drc"]["status"] == "ok"
    assert result["validation"]["prototype_assumptions"]["default_netclass_clearance_mm"] == 0.127
    assert output.is_file()
    assert output.with_suffix(".kicad_pro").is_file()


def test_deep_schematic_build_stages_hierarchy_under_windows_path_limit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import circuit_weaver.schematic_pcb as module

    case_name = "multichannel_analog_setpoint_card"
    root_name = "UnseenScale_Multichannel_Analog_Setpoint_Card.kicad_sch"
    target_name = "unplaced.kicad_pcb"
    desired_target_length = 188
    padding_length = desired_target_length - len(str(tmp_path)) - len(case_name) - len(target_name) - 3
    assert 0 < padding_length <= 255
    case_dir = tmp_path / ("p" * padding_length) / case_name
    generated = case_dir / "generated"
    generated.mkdir(parents=True)
    source = generated / root_name
    source.write_text(
        '(kicad_sch (property "Sheetfile" "misc_1.kicad_sch"))', encoding="utf-8"
    )
    (generated / "misc_1.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    target = case_dir / target_name
    assert len(str(source)) == 235
    assert len(str(target)) == desired_target_length

    monkeypatch.setattr(module, "_kicad_cli_path", lambda: Path("kicad-cli"))
    monkeypatch.setattr(module, "find_kicad_python", lambda _path=None: "kicad-python")
    observed = {}

    def export(command, **kwargs):
        staged = Path(command[-1])
        copied = sorted(staged.parent.iterdir())
        observed["copied_names"] = {item.name for item in copied}
        observed["copied_path_lengths"] = [len(str(item)) for item in copied]
        observed["staged_schematic"] = str(staged)
        return SimpleNamespace(returncode=1, stderr="intentional export stop", stdout="")

    monkeypatch.setattr(module.subprocess, "run", export)

    result = schematic_to_pcb(source, target)

    assert result["status"] == "error"
    assert "netlist export failed" in result["message"]
    assert observed["copied_names"] == {root_name, "misc_1.kicad_sch"}
    assert max(observed["copied_path_lengths"]) < 260
    assert len(observed["staged_schematic"]) < 260
    assert not list(case_dir.glob(".cw-c-*"))
