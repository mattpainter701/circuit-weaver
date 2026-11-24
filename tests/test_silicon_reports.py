import json
import xml.etree.ElementTree as ET

import klayout.db as kdb
import klayout.rdb as rdb
import pytest

from circuit_weaver.silicon_reports import inspect_silicon_report


def test_tiny_tapeout_provider_xml(tmp_path):
    path = tmp_path / "results.xml"
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="Tiny Tapeout Prechecks")
    for name in ("Pin check", "Boundary check", "Layer check", "Verilog syntax check"):
        case = ET.SubElement(suite, "testcase", name=name)
    tree = ET.ElementTree(root)
    tree.write(path)
    assert inspect_silicon_report("provider_precheck", path, "chip") == "tiny_tapeout_precheck"
    ET.SubElement(case, "error", message="invalid macro")
    tree.write(path)
    with pytest.raises(ValueError, match="failed or skipped"):
        inspect_silicon_report("provider_precheck", path, "chip")
    suite.remove(case)
    tree.write(path)
    with pytest.raises(ValueError, match="incomplete"):
        inspect_silicon_report("provider_precheck", path, "chip")


def test_physical_markers_override_a_pass_attestation(tmp_path):
    path = tmp_path / "check.lyrdb"
    result = rdb.ReportDatabase("width")
    result.top_cell_name = "chip"
    cell = result.create_cell("chip")
    rule = result.create_category("metal width")
    result.save(str(path))
    assert inspect_silicon_report("drc", path, "chip") == "klayout_drc"
    with pytest.raises(ValueError, match="declared top"):
        inspect_silicon_report("drc", path, "another")
    result.create_item(cell.rdb_id(), rule.rdb_id())
    result.save(str(path))
    with pytest.raises(ValueError, match="1 markers"):
        inspect_silicon_report("drc", path, "chip")


def test_lvs_requires_a_compared_root(tmp_path):
    path = tmp_path / "check.lvsdb"
    layout = kdb.Layout()
    chip = layout.create_cell("chip")
    metal = layout.layer(1, 0)
    chip.shapes(metal).insert(kdb.Box(0, 0, 100, 100))
    lvs = kdb.LayoutVsSchematic(kdb.RecursiveShapeIterator(layout, chip, [metal]))
    lvs.connect(lvs.make_layer(metal, "m1"))
    lvs.extract_netlist()
    lvs.reference = lvs.netlist().dup()
    assert lvs.compare(kdb.NetlistComparer())
    lvs.write(str(path))
    assert inspect_silicon_report("lvs", path, "chip") == "klayout_lvs"
    with pytest.raises(ValueError, match="declared root"):
        inspect_silicon_report("lvs", path, "other_chip")


def test_openroad_timing_requires_both_finite_nonnegative_slacks(tmp_path):
    path = tmp_path / "finish.json"
    stats = {"finish__timing__setup__ws": 0.1, "finish__timing__hold__ws": 0.0}
    path.write_text(json.dumps(stats))
    assert inspect_silicon_report("timing", path, "chip") == "openroad_timing"
    for invalid in (None, -0.001, "NaN", False):
        stats["finish__timing__hold__ws"] = invalid
        path.write_text(json.dumps(stats))
        with pytest.raises(ValueError, match="hold slack"):
            inspect_silicon_report("timing", path, "chip")


def test_librelane_timing_checks_each_reported_corner(tmp_path):
    path = tmp_path / "metrics.json"
    stats = {}
    for corner in ("", "__corner:slow"):
        for kind in ("setup", "hold"):
            stats[f"timing__{kind}__ws{corner}"] = 1.0
            stats[f"timing__{kind}_vio__count{corner}"] = 0
    path.write_text(json.dumps(stats))
    assert inspect_silicon_report("timing", path, "chip") == "librelane_timing"
    stats["timing__hold__ws__corner:slow"] = -0.01
    path.write_text(json.dumps(stats))
    with pytest.raises(ValueError, match="negative LibreLane hold"):
        inspect_silicon_report("timing", path, "chip")


def test_netgen_requires_the_matching_top_and_final_result(tmp_path):
    report = tmp_path / "lvs.netgen.rpt"
    report.write_text("Device classes chip and chip are equivalent.\nFinal result: Circuits match uniquely.\n")
    assert inspect_silicon_report("lvs", report, "chip") == "netgen_lvs"
    with pytest.raises(ValueError, match="declared top"):
        inspect_silicon_report("lvs", report, "wrapper")
    report.write_text("Device classes chip and chip are equivalent.\nFinal result: Netlists do not match.\n")
    with pytest.raises(ValueError, match="unique final match"):
        inspect_silicon_report("lvs", report, "chip")
