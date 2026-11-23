"""Pure validation of native board-rule extraction and custom edge-rule gating."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from circuit_weaver._native_board_rules import native_edge_clearance, native_edge_clearance_metadata


class _Board:
    def __init__(self, path, raw=500_000):
        self.path = str(path)
        self.settings = SimpleNamespace(m_CopperEdgeClearance=raw)

    def GetDesignSettings(self):
        return self.settings

    def GetFileName(self):
        return self.path


class _Pcbnew:
    @staticmethod
    def ToMM(value):
        return value / 1_000_000


@pytest.mark.parametrize("value_iu,value_mm", [(0, 0.0), (200_000, 0.2), (500_000, 0.5), (800_000, 0.8)])
def test_native_edge_setting_uses_loaded_board_project_value(tmp_path, value_iu, value_mm):
    board = _Board(tmp_path / "board.kicad_pcb", value_iu)

    assert native_edge_clearance(board, _Pcbnew) == {
        "value_iu": value_iu,
        "value_mm": value_mm,
        "value_um": value_mm * 1000.0,
        "source": "BOARD_DESIGN_SETTINGS.m_CopperEdgeClearance",
    }


@pytest.mark.parametrize("value", [True, -1, 1.5, float("nan"), float("inf"), "500000"])
def test_native_edge_setting_rejects_invalid_values(tmp_path, value):
    with pytest.raises(ValueError, match="native IU integer"):
        native_edge_clearance(_Board(tmp_path / "board.kicad_pcb", value), _Pcbnew)


def test_custom_edge_clearance_constraint_fails_closed(tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    rules = tmp_path / "board.kicad_dru"
    rules.write_text(
        '(version 1) (rule "edge" (constraint edge_clearance (min 0.8mm)))\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="custom KiCad edge_clearance rules"):
        native_edge_clearance(_Board(board_path), _Pcbnew)

    metadata = native_edge_clearance_metadata(_Board(board_path), _Pcbnew)
    assert metadata == {
        "status": "unsupported",
        "message": "custom KiCad edge_clearance rules are unsupported by native routing geometry",
    }


def test_inspection_metadata_is_successful_for_supported_edge_value(tmp_path):
    metadata = native_edge_clearance_metadata(_Board(tmp_path / "board.kicad_pcb", 800_000), _Pcbnew)

    assert metadata["status"] == "ok"
    assert metadata["value_mm"] == 0.8


@pytest.mark.parametrize("rules_text", [
    '(version 1) (rule "track" (constraint track_width (min 0.2mm)))\n',
    '; (constraint edge_clearance (min 1mm))\n(rule "quoted" (constraint clearance (min 0.2mm)))',
    '# (constraint edge_clearance (min 1mm))\n(rule "quoted" (constraint clearance (min 0.2mm)))',
    '(rule "constraint edge_clearance" (constraint via_diameter (min 0.6mm)))',
])
def test_other_custom_rules_and_comments_do_not_trigger_edge_rejection(tmp_path, rules_text):
    board_path = tmp_path / "board.kicad_pcb"
    (tmp_path / "board.kicad_dru").write_text(rules_text, encoding="utf-8")

    result = native_edge_clearance(_Board(board_path), _Pcbnew)

    assert result["value_iu"] == 500_000


def test_real_custom_edge_rule_after_hash_comment_still_fails_closed(tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    (tmp_path / "board.kicad_dru").write_text(
        '# (constraint edge_clearance (min 1mm))\n'
        '(rule "real edge" (constraint edge_clearance (min 0.8mm)))\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="custom KiCad edge_clearance rules"):
        native_edge_clearance(_Board(board_path), _Pcbnew)


def test_malformed_custom_rules_fail_closed(tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    (tmp_path / "board.kicad_dru").write_text('(rule "x" (constraint clearance (min 0.2mm))', encoding="utf-8")

    with pytest.raises(ValueError, match="unclosed expression"):
        native_edge_clearance(_Board(board_path), _Pcbnew)
