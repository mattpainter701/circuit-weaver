from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from circuit_weaver.footprint_lib import KiCadFootprintLibrary
from circuit_weaver.schematic_pcb import (
    _annotation_errors_present,
    _copy_schematic_project,
    _parse_netlist,
    _worker_result,
    schematic_to_pcb,
)


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


def test_netlist_maps_only_declared_pin_numbers_and_preserves_explicit_no_connect(tmp_path):
    library = _fixture_library(tmp_path)
    model = _parse_netlist(_write_netlist(tmp_path / "board.xml"), library)
    assert model["nets"] == ["SIG"]
    assert model["components"][0]["nets"] == {"1": "SIG"}
    assert model["explicit_no_connects"] == [{"reference": "U1", "pin": "2"}]


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
    assert not list(tmp_path.glob(".cw-schematic-pcb-*"))


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
