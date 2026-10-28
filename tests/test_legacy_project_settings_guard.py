from unittest import mock

import pytest

from circuit_weaver.autoroute import autoroute_pcb, export_dsn
from circuit_weaver.pcb_layout import _prepare_settings, place_pcb


def test_settings_staging_refuses_legacy_rules_without_modern_project(tmp_path):
    source = tmp_path / "legacy-board.kicad_pcb"
    source.write_bytes(b"(kicad_pcb)")
    source.with_suffix(".pro").write_bytes(b"legacy rules")
    staged = tmp_path / "staging" / source.name
    staged.parent.mkdir()

    with pytest.raises(ValueError, match=r"(?i)legacy.*\.pro.*\.kicad_pro.*convert"):
        _prepare_settings(source, staged)

    assert not staged.with_suffix(".kicad_pro").exists()


def test_settings_staging_preserves_modern_or_missing_project_behavior(tmp_path):
    source = tmp_path / "modern-board.kicad_pcb"
    source.write_bytes(b"(kicad_pcb)")
    modern = source.with_suffix(".kicad_pro")
    modern.write_bytes(b"modern rules")
    source.with_suffix(".pro").write_bytes(b"stale legacy rules")
    staged = tmp_path / "staging" / source.name
    staged.parent.mkdir()

    snapshots = _prepare_settings(source, staged)
    assert staged.with_suffix(".kicad_pro").read_bytes() == b"modern rules"
    assert snapshots[modern]

    modern.unlink()
    staged.with_suffix(".kicad_pro").unlink()
    source.with_suffix(".pro").unlink()
    snapshots = _prepare_settings(source, staged)
    assert snapshots[source.with_suffix(".kicad_pro")] is None


def test_board_routing_refuses_legacy_rules_before_creating_outputs(tmp_path):
    source = tmp_path / "legacy-board.kicad_pcb"
    source.write_bytes(b"board bytes")
    source.with_suffix(".pro").write_bytes(b"legacy rules")

    with mock.patch("circuit_weaver.autoroute._find_freerouting_command") as find_router:
        result = autoroute_pcb(source)

    assert result["status"] == "error"
    assert "Legacy KiCad project settings" in result["message"]
    assert "convert the project rules in KiCad" in result["message"]
    find_router.assert_not_called()
    assert not source.with_name("legacy-board_autoroute.dsn").exists()
    assert not source.with_suffix(".ses").exists()


def test_placement_refuses_legacy_rules_before_searching(tmp_path):
    source = tmp_path / "legacy-board.kicad_pcb"
    source.write_bytes(b"board bytes")
    source.with_suffix(".pro").write_bytes(b"legacy rules")
    output = tmp_path / "placed.kicad_pcb"

    with mock.patch("circuit_weaver.pcb_layout.optimize_placement") as optimize:
        result = place_pcb(source, output)

    assert result["status"] == "error"
    assert "Legacy KiCad project settings" in result["message"]
    optimize.assert_not_called()
    assert not output.exists()


def test_direct_dsn_export_refuses_legacy_rules_before_native_preflight(tmp_path):
    source = tmp_path / "legacy-board.kicad_pcb"
    source.write_bytes(b"board bytes")
    source.with_suffix(".pro").write_bytes(b"legacy rules")
    output = tmp_path / "export.dsn"

    with mock.patch("circuit_weaver.autoroute.preflight_pcb") as preflight:
        result = export_dsn(source, output)

    assert result["status"] == "error"
    assert "Legacy KiCad project settings" in result["message"]
    preflight.assert_not_called()
    assert not output.exists()


@pytest.mark.parametrize("source", [None, 123, object()])
def test_direct_dsn_export_invalid_paths_remain_structured_errors(tmp_path, source):
    output = tmp_path / "export.dsn"
    result = export_dsn(source, output)
    assert result["status"] == "error"
    assert not output.exists()
