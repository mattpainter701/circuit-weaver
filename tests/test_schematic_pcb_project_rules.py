from __future__ import annotations

import json

import pytest

from circuit_weaver.schematic_pcb import _check_snapshot, _restore_authored_project_settings


def test_restore_exact_authored_rules_and_severities_after_native_overwrite(tmp_path):
    board = tmp_path / "review.kicad_pcb"
    pro = board.with_suffix(".kicad_pro")
    dru = board.with_suffix(".kicad_dru")
    original = b'{"net_settings":{"classes":[{"name":"Default","clearance":0.1016}]},' \
        b'"board":{"design_settings":{"rule_severities":{"silk_over_copper":"error"}}}}\n'
    rules = b'(version 1)\n(rule "authored" (constraint clearance (min 0.1)))\n'
    pro.write_text('{"net_settings":{"classes":[{"name":"Default","clearance":0.2}]}}')
    dru.write_text("native replacement")
    snapshot = _restore_authored_project_settings(board, {".kicad_pro": original, ".kicad_dru": rules})
    assert pro.read_bytes() == original and dru.read_bytes() == rules
    _check_snapshot(snapshot)
    pro.write_text("changed after restore")
    with pytest.raises(ValueError, match="changed"):
        _check_snapshot(snapshot)


def test_declared_prototype_assumptions_preserve_other_source_settings(tmp_path):
    board = tmp_path / "review.kicad_pcb"
    pro = board.with_suffix(".kicad_pro")
    source = {"schematic": {"legacy": "retained"},
              "board": {"design_settings": {"rule_severities": {"silk_over_copper": "error"}}}}
    native_nets = {"meta": {"version": 5}, "classes": [{"name": "Default", "clearance": 0.127}]}
    pro.write_text(json.dumps({"net_settings": native_nets, "schematic": {"native": "discarded"}}))
    _restore_authored_project_settings(board, {".kicad_pro": json.dumps(source).encode()},
                                      clearance_mm=0.127, minimum_drill_mm=0.2)
    restored = json.loads(pro.read_text())
    assert restored["schematic"] == source["schematic"]
    assert restored["board"]["design_settings"]["rule_severities"]["silk_over_copper"] == "error"
    assert restored["net_settings"] == native_nets
    assert restored["board"]["design_settings"]["rules"]["min_through_hole_diameter"] == 0.2


@pytest.mark.parametrize("native", [{}, {"net_settings": None}, {"net_settings": {"classes": None}},
                                    {"net_settings": {"classes": [{"name": "Default", "clearance": 0.2}]}}])
def test_unverified_native_prototype_clearance_is_rejected(tmp_path, native):
    board = tmp_path / "review.kicad_pcb"
    board.with_suffix(".kicad_pro").write_text(json.dumps(native))
    with pytest.raises(ValueError):
        _restore_authored_project_settings(board, {".kicad_pro": b"{}"}, clearance_mm=0.127)


def test_source_owned_netclasses_cannot_be_replaced_by_native_defaults(tmp_path):
    board = tmp_path / "review.kicad_pcb"
    with pytest.raises(ValueError, match="authored"):
        _restore_authored_project_settings(board, {".kicad_pro": b'{"net_settings":{}}'}, clearance_mm=0.127)


def test_missing_source_sidecars_leave_new_native_settings_intact(tmp_path):
    board = tmp_path / "review.kicad_pcb"
    pro = board.with_suffix(".kicad_pro")
    pro.write_bytes(b"native settings")
    assert _restore_authored_project_settings(board, {".kicad_pro": None, ".kicad_dru": None}) == {}
    assert pro.read_bytes() == b"native settings"
