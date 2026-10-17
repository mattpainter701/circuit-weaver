"""Contract tests for fail-closed Specctra/Freerouting integration."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from circuit_weaver.autoroute import (
    _apply_specctra_clearance_margin,
    _effective_optimization_threads,
    _find_freerouting_command,
    _find_freerouting_jar,
    _find_kicad_cli,
    _iter_sexpr_block_spans,
    _kicad_cli_supports_specctra,
    _native_placement_variant,
    _parse_freerouting_version,
    _parse_routing_stats,
    _probe_freerouting_capabilities,
    _validate_specctra_artifact,
    autoroute_pcb,
    export_dsn,
    preflight_pcb,
)


@pytest.mark.parametrize(
    ("suffix", "routed", "attempts", "requested", "expected"),
    [
        (".kicad_pcb", True, 1, None, 0),
        (".kicad_pcb", True, 3, None, 0),
        (".kicad_pcb", True, 3, 2, 2),
        (".kicad_pcb", False, 3, None, 1),
        (".dsn", False, 3, None, 1),
        (".dsn", False, 1, None, None),
    ],
)
def test_routed_native_board_defaults_to_optimizer_disabled_but_respects_explicit_threads(
    suffix, routed, attempts, requested, expected,
):
    assert _effective_optimization_threads(
        source_suffix=suffix, routed_board=routed, attempts=attempts, requested=requested,
    ) == expected

ROUTABLE_PCB = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VDD_3P3")
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 10 10)
    (property "Reference" "R1" (at 0 -1.2 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "VDD_3P3"))
  )
  (footprint "Connector_PinHeader_1x02_P2.54mm_Vertical" (layer "F.Cu") (at 20 10)
    (property "Reference" "J1" (at 0 -2 0) (layer "F.SilkS"))
    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask")
      (net 2 "VDD_3P3"))
    (pad "2" thru_hole circle (at 0 2.54) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask")
      (net 1 "GND"))
  )
  (gr_rect (start 5 5) (end 25 20) (stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts"))
)
"""

PREVIEW_PCB = """(kicad_pcb (version 20240108) (generator "schematic_engine placement_preview")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 10 10)
    (property "Reference" "R1" (at 0 -1.2 0) (layer "F.SilkS"))
  )
)
"""

VALID_DSN = """(pcb "sensor-board"
  (parser (string_quote ")"))
  (resolution mil 1000)
  (unit mil)
  (structure
    (layer "F.Cu" (type signal))
    (layer "B.Cu" (type signal))
    (boundary (path pcb 0 0 0 20000 0 20000 15000 0 15000 0 0))
    (rule (clearance 5) (clearance 1.25 (type smd_smd)))
  )
  (placement
    (component "Resistor_SMD:R_0402_1005Metric" (place R1 10000 10000 front 0))
  )
  (library (image "Resistor_SMD:R_0402_1005Metric"))
  (network
    (net GND (pins R1-1 J1-2))
    (net VDD_3P3 (pins R1-2 J1-1))
    (class kicad_default GND VDD_3P3 (rule (clearance 5)))
  )
  (comment "(clearance 99) is quoted data, not a rule")
)
"""

VALID_SES = """(session "sensor-board"
  (base_design "sensor-board.dsn")
  (placement)
  (routes
    (resolution mil 1000)
    (library_out)
    (network_out
      (net GND (wire (path F.Cu 250 10000 10000 20000 12540)))
      (net VDD_3P3 (wire (path F.Cu 250 10500 10000 20000 10000)))
    )
  )
)
"""


def _write_routable_board(tmp_path: Path, name: str = "sensor-board.kicad_pcb") -> Path:
    path = tmp_path / name
    path.write_text(ROUTABLE_PCB, encoding="utf-8")
    return path


def _write_dsn(tmp_path: Path, name: str = "sensor-board.dsn", text: str = VALID_DSN) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_router_only_clearance_margin_updates_general_rules_and_preserves_typed_exceptions(tmp_path):
    dsn = _write_dsn(tmp_path)

    result = _apply_specctra_clearance_margin(dsn, 0.01)
    text = dsn.read_text(encoding="utf-8")

    assert result == {
        "applied": True,
        "margin_mm": 0.01,
        "dsn_unit": "mil",
        "declarations_adjusted": 2,
        "typed_exceptions_unchanged": 1,
        "native_board_rules_changed": False,
    }
    assert text.count("(clearance 5.393700787401574803149606299)") == 2
    assert "(clearance 1.25 (type smd_smd))" in text
    assert '(comment "(clearance 99) is quoted data, not a rule")' in text


def test_native_placement_variants_reorder_only_component_and_nested_place_spans():
    dsn = '''(pcb "ordered-board"
  (structure (layer F.Cu (type signal)) (rule (width 200) (clearance 137)))
  (placement
    (component "Device:R" (place "R 1" 10 10 front 90) (place R2 20 10 front 0))
    (component "Connector:USB" (place J1 30 10 front 0) (place J2 40 10 front 0))
  )
  (library (image "Device:R" (pin 1 0 0)))
  (network (net GND (pins R1-1 J1-1)))
  (comment "(component fake (place FAKE 0 0 front 0))")
)\n'''

    ascending, ascending_refs = _native_placement_variant(dsn, "ascending")
    descending, descending_refs = _native_placement_variant(dsn, "descending")
    original, original_refs = _native_placement_variant(dsn, "original")

    assert ascending_refs == ["J1", "J2", "R 1", "R2"]
    assert descending_refs == ["R2", "R 1", "J2", "J1"]
    assert original_refs == ["R 1", "R2", "J1", "J2"]
    assert ascending != dsn
    assert descending != dsn
    assert original == dsn
    assert ascending.partition("(placement")[0] == dsn.partition("(placement")[0]
    assert ascending.split("(library", 1)[1] == dsn.split("(library", 1)[1]
    assert '(rule (width 200) (clearance 137))' in ascending
    assert '(comment "(component fake (place FAKE 0 0 front 0))")' in ascending
    # The complete place records travel as byte-preserved strings.
    assert '(place "R 1" 10 10 front 90)' in ascending
    assert '(place J1 30 10 front 0)' in ascending


@pytest.mark.parametrize(
    "placement",
    [
        '(placement (component Device:R))',
        '(placement (component Device:R (place R1 0 0 front 0)) '
        '(component Device:C (place R1 1 1 front 0)))',
        '(placement (component Device:R (place 0 0 front 0)))',
    ],
)
def test_native_placement_variant_rejects_ambiguous_component_identity(placement):
    with pytest.raises(ValueError, match="Cannot safely reorder native DSN"):
        _native_placement_variant(f'(pcb "b" {placement})', "ascending")


@pytest.mark.parametrize(
    ("unit", "clearance", "expected"),
    [
        ("um", "127", "137"),
        ("mm", "0.127", "0.137"),
        ("inch", "0.005", "0.005393700787401574803149606299"),
        ("cm", "0.0127", "0.0137"),
    ],
)
def test_router_clearance_margin_converts_declared_dsn_units(tmp_path, unit, clearance, expected):
    dsn = _write_dsn(
        tmp_path,
        text=f'(pcb "b" (unit {unit}) (structure (rule (clearance {clearance}))))',
    )

    result = _apply_specctra_clearance_margin(dsn, 0.01)

    assert result["dsn_unit"] == unit
    assert f"(clearance {expected})" in dsn.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "dsn_text",
    [
        '(pcb "b" (structure (rule (clearance 127))))',
        '(pcb "b" (unit um) (structure (rule (clearance unknown))))',
        '(pcb "b" (unit um) (structure (rule (clearance 127junk))))',
        '(pcb "b" (unit furlong) (structure (rule (clearance 127))))',
    ],
)
def test_router_clearance_margin_fails_closed_without_changing_ambiguous_dsn(tmp_path, dsn_text):
    from circuit_weaver.autoroute import _apply_specctra_clearance_margin

    dsn = _write_dsn(tmp_path, text=dsn_text)
    original = dsn.read_bytes()

    with pytest.raises(ValueError, match="Cannot safely apply routing margin"):
        _apply_specctra_clearance_margin(dsn, 0.01)

    assert dsn.read_bytes() == original


def _routing_output(
    *,
    incomplete: int = 0,
    clearance_violations: int = 0,
    traces: int = 4,
    vias: int = 1,
    progress: str = "",
    version: str | None = "2.2.4",
) -> str:
    final_statistics = {
        "host": "KiCad's Pcbnew,9.0.6",
        "connections": {"maximum_count": 5, "incomplete_count": incomplete},
        "traces": {"total_count": traces},
        "vias": {"total_count": vias},
        "clearance_violations": {"total_count": clearance_violations},
    }
    return "\n".join(
        part
        for part in (
            f"2026-07-09 12:00:00 INFO Freerouting v{version} (build-date: 2026-05-13)"
            if version
            else "",
            progress,
            json.dumps(final_statistics, indent=2),
        )
        if part
    )


def _fake_freerouting_success(output: str | None = None, ses_text: str = VALID_SES):
    def run(command, **_kwargs):
        if "-help" in command:
            return mock.Mock(
                returncode=0,
                stdout="Freerouting v2.2.4\n-mp passes\n--gui.enabled=false",
                stderr="",
            )
        destination = Path(command[command.index("-do") + 1])
        destination.write_text(ses_text, encoding="utf-8")
        return mock.Mock(returncode=0, stdout=output or _routing_output(), stderr="")

    return run


def _portfolio_runner(outputs, seen, *, after_attempt=None):
    def run(command, **kwargs):
        if "-help" in command:
            return mock.Mock(returncode=0, stdout="Freerouting v2.2.4\n-random_seed seed", stderr="")
        assert Path(kwargs["cwd"]).is_dir()
        assert Path(command[command.index("-de") + 1]).is_absolute()
        assert Path(command[command.index("-do") + 1]).is_absolute()
        seen.append((command, kwargs))
        output = outputs[len(seen) - 1]
        if isinstance(output, Exception):
            raise output
        path = Path(command[command.index("-do") + 1])
        path.write_text(VALID_SES.replace("  (routes", " " * len(seen) + "(routes"), encoding="utf-8")
        if after_attempt:
            after_attempt()
        return mock.Mock(returncode=0, stdout=output, stderr="")
    return run


def test_portfolio_publishes_best_complete_candidate_and_removes_losers(tmp_path):
    source = _write_dsn(tmp_path)
    original_dsn = source.read_bytes()
    seen = []
    outputs = [_routing_output(incomplete=2, vias=0), _routing_output(vias=5), _routing_output(vias=1)]
    with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]), \
            mock.patch("subprocess.run", side_effect=_portfolio_runner(outputs, seen)):
        result = autoroute_pcb(source, attempts=3, seed=10, timeout_seconds=90)
    assert result["status"] == "ok"
    assert result["search"]["selected_attempt"] == 3
    assert result["stats"]["vias"] == 1
    assert "\n   (routes" in source.with_suffix(".ses").read_text(encoding="utf-8")
    assert [int(cmd[cmd.index("-random_seed") + 1]) for cmd, _ in seen] == [10, 11, 12]
    assert all(cmd[cmd.index("-mt") + 1] == "1" for cmd, _ in seen)
    assert seen[0][1]["timeout"] <= 30
    assert not list(tmp_path.glob(".cw-routing-*"))
    assert result["artifact"]["path"] == str(source.with_suffix(".ses"))
    assert source.read_bytes() == original_dsn


def test_portfolio_retains_success_after_timeout_and_preserves_partial_status(tmp_path):
    source = _write_dsn(tmp_path)
    seen = []
    outputs = [_routing_output(incomplete=1), subprocess.TimeoutExpired("freerouting", 1)]
    with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]), \
            mock.patch("subprocess.run", side_effect=_portfolio_runner(outputs, seen)):
        result = autoroute_pcb(source, attempts=2)
    assert result["status"] == "partial"
    assert result["search"]["selected_attempt"] == 1
    assert result["search"]["attempts"][1]["status"] == "error"
    assert result["fabrication_ready"] is False


def test_portfolio_prioritizes_reported_clearance_over_via_count(tmp_path):
    source = _write_dsn(tmp_path)
    seen = []
    outputs = ["incomplete: 0 vias: 0 traces: 1", _routing_output(vias=20)]
    with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]), \
            mock.patch("subprocess.run", side_effect=_portfolio_runner(outputs, seen)):
        result = autoroute_pcb(source, attempts=2)
    assert result["search"]["selected_attempt"] == 2
    assert result["status"] == "ok"


@pytest.mark.parametrize("change_input", [False, True])
def test_failed_or_changed_input_portfolio_preserves_existing_output(tmp_path, change_input):
    source = _write_dsn(tmp_path)
    destination = source.with_suffix(".ses")
    destination.write_bytes(b"previous reviewed session")
    seen = []
    def change():
        source.write_text(VALID_DSN + "\n; changed", encoding="utf-8")
    outputs = [_routing_output(clearance_violations=0 if change_input else 2)] * 2
    with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]), \
            mock.patch("subprocess.run", side_effect=_portfolio_runner(
                outputs, seen, after_attempt=change if change_input else None,
            )):
        result = autoroute_pcb(source, attempts=2, overwrite=True)
    assert result["status"] == "error"
    assert destination.read_bytes() == b"previous reviewed session"
    assert not list(tmp_path.glob(".cw-routing-*"))


def test_portfolio_varies_optimizer_when_seed_is_not_supported(tmp_path):
    source = _write_dsn(tmp_path)
    with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]), \
            mock.patch("subprocess.run", side_effect=_fake_freerouting_success()) as run:
        result = autoroute_pcb(source, attempts=2)
    assert result["status"] == "ok"
    assert [row["optimizer_strategy"] for row in result["search"]["attempts"]] == ["greedy", "global"]
    assert run.call_count == 3
    assert all("-random_seed" not in call.args[0] for call in run.call_args_list)
    assert source.with_suffix(".ses").exists()


@pytest.mark.parametrize("attempts", [0, 33, True, 1.5])
def test_invalid_portfolio_budget_is_rejected(tmp_path, attempts):
    source = _write_dsn(tmp_path)
    result = autoroute_pcb(source, attempts=attempts)
    assert result["status"] == "error"
    assert "attempts" in result["message"]


def test_portfolio_rejects_seed_overflow(tmp_path):
    result = autoroute_pcb(_write_dsn(tmp_path), attempts=2, seed=9_223_372_036_854_775_807)
    assert result["status"] == "error"
    assert "seed" in result["message"]


def test_portfolio_shares_elapsed_budget_and_keeps_stable_tie(tmp_path):
    source = _write_dsn(tmp_path)
    seen = []
    clock = [0.0]
    def advance():
        clock[0] += 8.0
    outputs = [_routing_output()] * 3
    with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]), \
            mock.patch("circuit_weaver.autoroute.time.perf_counter", side_effect=lambda: clock[0]), \
            mock.patch("subprocess.run", side_effect=_portfolio_runner(outputs, seen, after_attempt=advance)):
        result = autoroute_pcb(source, attempts=3, timeout_seconds=30, optimization_threads=2)
    assert result["search"]["selected_attempt"] == 1
    assert [kwargs["timeout"] for _, kwargs in seen] == [10, 11, 14]
    assert all(cmd[cmd.index("-mt") + 1] == "2" for cmd, _ in seen)
    assert result["stats"]["routing_time_seconds"] == 24


def test_portfolio_does_not_replace_destination_created_during_search(tmp_path):
    source = _write_dsn(tmp_path)
    destination = source.with_suffix(".ses")
    seen = []
    def create_destination():
        destination.write_bytes(b"concurrent user session")
    with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]), \
            mock.patch("subprocess.run", side_effect=_portfolio_runner(
                [_routing_output()] * 2, seen, after_attempt=create_destination,
            )):
        result = autoroute_pcb(source, attempts=2)
    assert result["status"] == "error"
    assert destination.read_bytes() == b"concurrent user session"
    assert not list(tmp_path.glob(".cw-routing-*"))


class TestPreflight:
    def test_real_pad_and_net_board_passes(self, tmp_path):
        result = preflight_pcb(_write_routable_board(tmp_path))

        assert result["routable"] is True
        assert result["stats"] == {
            "footprints": 2,
            "pads": 4,
            "nets": 2,
            "connected_pad_nets": 2,
            "placement_preview": False,
        }

    def test_placement_preview_marker_and_filename_fail(self, tmp_path):
        marked = tmp_path / "layout-hint.kicad_pcb"
        marked.write_text(PREVIEW_PCB, encoding="utf-8")
        named = tmp_path / "sensor_placement_preview.kicad_pcb"
        named.write_text(ROUTABLE_PCB, encoding="utf-8")

        for path in (marked, named):
            result = preflight_pcb(path)
            assert result["routable"] is False
            assert "placement preview" in result["reason"].lower()

    def test_padless_and_unassigned_boards_fail(self, tmp_path):
        padless = tmp_path / "padless.kicad_pcb"
        padless.write_text(PREVIEW_PCB.replace("schematic_engine placement_preview", "pcbnew"), encoding="utf-8")
        unassigned = tmp_path / "unassigned.kicad_pcb"
        unassigned.write_text(
            ROUTABLE_PCB.replace('(net 1 "GND")', "").replace('(net 2 "VDD_3P3")', ""),
            encoding="utf-8",
        )

        assert "no pads" in preflight_pcb(padless)["reason"].lower()
        assert "no named nets" in preflight_pcb(unassigned)["reason"].lower()

    def test_pad_net_number_must_be_declared_and_name_must_match(self, tmp_path):
        undeclared = tmp_path / "undeclared.kicad_pcb"
        undeclared.write_text(ROUTABLE_PCB.replace('(net 1 "GND"))', '(net 99 "GND"))', 1), encoding="utf-8")
        mismatched = tmp_path / "mismatched.kicad_pcb"
        mismatched.write_text(
            ROUTABLE_PCB.replace('(net 1 "GND"))', '(net 1 "NOT_GND"))', 1),
            encoding="utf-8",
        )

        assert "undeclared or mismatched net 99" in preflight_pcb(undeclared)["reason"]
        assert "undeclared or mismatched net 1" in preflight_pcb(mismatched)["reason"]

    def test_wrong_input_extension_fails(self, tmp_path):
        path = tmp_path / "board.txt"
        path.write_text(ROUTABLE_PCB, encoding="utf-8")
        assert preflight_pcb(path)["routable"] is False


class TestArtifactValidation:
    def test_realistic_dsn_and_correlated_ses_pass(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        ses = tmp_path / "sensor-board.ses"
        ses.write_text(VALID_SES, encoding="utf-8")

        dsn_result = _validate_specctra_artifact(dsn, "dsn")
        ses_result = _validate_specctra_artifact(ses, "ses", source_dsn=dsn)
        assert dsn_result["valid"] is True
        assert dsn_result["net_count"] == 2
        assert ses_result["valid"] is True
        assert ses_result["base_design"] == "sensor-board.dsn"

    def test_canonical_literal_string_quote_directive_passes(self, tmp_path):
        canonical = VALID_DSN.replace('(string_quote ")")', '(string_quote ")')
        assert canonical != VALID_DSN
        dsn = _write_dsn(tmp_path, text=canonical)

        result = _validate_specctra_artifact(dsn, "dsn")

        assert result["valid"] is True
        assert result["net_count"] == 2

    def test_ses_may_omit_source_net_with_only_one_pin(self, tmp_path):
        dsn = _write_dsn(
            tmp_path,
            text=VALID_DSN.replace(
                "    (net VDD_3P3 (pins R1-2 J1-1))",
                "    (net VDD_3P3 (pins R1-2 J1-1))\n"
                "    (net TEST_POINT_ONLY (pins TP1-1))",
            ),
        )
        ses = tmp_path / "sensor-board.ses"
        ses.write_text(VALID_SES, encoding="utf-8")

        dsn_result = _validate_specctra_artifact(dsn, "dsn")
        ses_result = _validate_specctra_artifact(ses, "ses", source_dsn=dsn)

        assert dsn_result["pin_counts"]["TEST_POINT_ONLY"] == 1
        assert "TEST_POINT_ONLY" not in dsn_result["routable_nets"]
        assert ses_result["valid"] is True

    def test_missing_empty_and_malformed_artifacts_fail(self, tmp_path):
        missing = tmp_path / "missing.dsn"
        empty = tmp_path / "empty.dsn"
        empty.write_text("", encoding="utf-8")
        malformed = tmp_path / "bad.ses"
        malformed.write_text("(session bad (routes", encoding="utf-8")

        assert _validate_specctra_artifact(missing, "dsn")["valid"] is False
        assert "small" in _validate_specctra_artifact(empty, "dsn")["reason"]
        assert _validate_specctra_artifact(malformed, "ses")["valid"] is False

    def test_semantically_empty_required_sections_fail(self, tmp_path):
        dsn = _write_dsn(
            tmp_path,
            text=VALID_DSN.replace(
                "(network\n    (net GND (pins R1-1 J1-2))\n    (net VDD_3P3 (pins R1-2 J1-1))\n"
                "    (class kicad_default GND VDD_3P3 (rule (clearance 5)))\n  )",
                "(network)",
            ),
        )
        ses = tmp_path / "empty-routes.ses"
        ses.write_text(
            VALID_SES.replace(
                "(network_out\n      (net GND (wire (path F.Cu 250 10000 10000 20000 12540)))\n"
                "      (net VDD_3P3 (wire (path F.Cu 250 10500 10000 20000 10000)))\n    )",
                "(network_out)",
            ),
            encoding="utf-8",
        )

        assert "contains no nets" in _validate_specctra_artifact(dsn, "dsn")["reason"]
        assert "contains no routed nets" in _validate_specctra_artifact(ses, "ses")["reason"]

    def test_keywords_inside_quoted_text_cannot_fake_required_content(self, tmp_path):
        decoy = _write_dsn(
            tmp_path,
            text=VALID_DSN.replace(
                "(component \"Resistor_SMD:R_0402_1005Metric\" (place R1 10000 10000 front 0))",
                '(comment "(component fake (place R1 0 0 front 0))")',
            ),
        )

        result = _validate_specctra_artifact(decoy, "dsn")
        assert result["valid"] is False
        assert "no components" in result["reason"]

    def test_ses_must_match_source_design_and_nets(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        wrong_base = tmp_path / "wrong-base.ses"
        wrong_base.write_text(VALID_SES.replace("sensor-board.dsn", "another-board.dsn"), encoding="utf-8")
        wrong_net = tmp_path / "wrong-net.ses"
        wrong_net.write_text(VALID_SES.replace("(net GND", "(net SECRET", 1), encoding="utf-8")

        assert "does not reference" in _validate_specctra_artifact(
            wrong_base, "ses", source_dsn=dsn
        )["reason"]
        assert "absent from source DSN" in _validate_specctra_artifact(
            wrong_net, "ses", source_dsn=dsn
        )["reason"]


class TestCommandDiscovery:
    def test_home_jar_uses_discovered_java(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CIRCUIT_WEAVER_FREEROUTING", raising=False)
        monkeypatch.delenv("FREEROUTING_PATH", raising=False)
        jar = tmp_path / ".freerouting" / "freerouting.jar"
        jar.parent.mkdir()
        jar.write_bytes(b"PK\x03\x04fake-jar-content")

        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            with mock.patch("circuit_weaver.autoroute._find_java", return_value="/opt/java/bin/java"):
                assert _find_freerouting_command() == ["/opt/java/bin/java", "-jar", str(jar.resolve())]

    def test_explicit_launcher_takes_precedence_over_environment_jar(self, tmp_path, monkeypatch):
        launcher = tmp_path / "custom-freerouting"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        env_jar = tmp_path / "environment.jar"
        env_jar.write_bytes(b"PK\x03\x04environment-jar")
        monkeypatch.setenv("CIRCUIT_WEAVER_FREEROUTING", str(env_jar))

        assert _find_freerouting_command(launcher) == [str(launcher.resolve())]

    def test_environment_jar_and_kicad_cli_are_discovered(self, tmp_path, monkeypatch):
        jar = tmp_path / "freerouting.jar"
        jar.write_bytes(b"PK\x03\x04fake-jar-content")
        monkeypatch.setenv("CIRCUIT_WEAVER_FREEROUTING", str(jar))
        kicad = tmp_path / "kicad-cli"
        kicad.write_text("executable", encoding="utf-8")
        monkeypatch.setenv("CIRCUIT_WEAVER_KICAD_CLI", str(kicad))

        assert _find_freerouting_jar() == jar.resolve()
        assert _find_kicad_cli() == str(kicad.resolve())

    def test_kicad_capability_probe_requires_advertised_subcommand(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="dxf gerber specctra svg", stderr="")
            assert _kicad_cli_supports_specctra("kicad-cli") is True
            run.return_value = mock.Mock(returncode=0, stdout="dxf gerber svg", stderr="")
            assert _kicad_cli_supports_specctra("kicad-cli") is False

    def test_freerouting_capability_probe_reports_version_and_seed(self):
        help_text = "Freerouting v2.2.4\n-mp passes\n-random_seed seed\n--gui.enabled=false"
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=help_text, stderr="")):
            result = _probe_freerouting_capabilities(["freerouting"], timeout_seconds=5)

        assert result == {"probe_ok": True, "version": "2.2.4", "seed": True, "reason": ""}


class TestExportDsn:
    def test_preview_is_rejected_before_cli_discovery(self, tmp_path):
        preview = tmp_path / "board_placement_preview.kicad_pcb"
        preview.write_text(PREVIEW_PCB, encoding="utf-8")
        with mock.patch("circuit_weaver.autoroute._find_kicad_cli") as find_cli:
            result = export_dsn(preview, tmp_path / "board.dsn")

        assert result["status"] == "error"
        assert "placement preview" in result["message"].lower()
        find_cli.assert_not_called()

    @mock.patch("circuit_weaver.kicad_bridge.run_kicad", return_value={"status": "error", "message": "unavailable"})
    def test_missing_or_incapable_cli_has_manual_export_remediation(self, native, tmp_path):
        board = _write_routable_board(tmp_path)
        with mock.patch("circuit_weaver.autoroute._find_kicad_cli", return_value=None):
            missing = export_dsn(board, tmp_path / "missing-cli.dsn")
        with mock.patch("circuit_weaver.autoroute._find_kicad_cli", return_value="kicad-cli"):
            with mock.patch("circuit_weaver.autoroute._kicad_cli_supports_specctra", return_value=False):
                incapable = export_dsn(board, tmp_path / "incapable-cli.dsn")

        assert "PCB Editor" in missing["message"]
        assert "pcbnew" in incapable["message"]
        assert native.call_count == 2

    def test_export_is_staged_and_existing_output_requires_explicit_overwrite(self, tmp_path):
        board = _write_routable_board(tmp_path)
        destination = tmp_path / "board.dsn"
        destination.write_text("prior validated output", encoding="utf-8")

        with mock.patch("subprocess.run") as run:
            blocked = export_dsn(board, destination)
        assert blocked["status"] == "error"
        assert "--overwrite" in blocked["message"]
        run.assert_not_called()

        def fake_export(command, **_kwargs):
            assert destination.read_text(encoding="utf-8") == "prior validated output"
            staging = Path(command[command.index("-o") + 1])
            assert staging != destination
            staging.write_text(VALID_DSN, encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("circuit_weaver.autoroute._find_kicad_cli", return_value="kicad-cli"):
            with mock.patch("circuit_weaver.autoroute._kicad_cli_supports_specctra", return_value=True):
                with mock.patch("subprocess.run", side_effect=fake_export):
                    result = export_dsn(board, destination, overwrite=True)

        assert result["status"] == "ok"
        assert destination.read_text(encoding="utf-8") == VALID_DSN
        assert result["artifact"]["path"] == str(destination)
        assert not list(tmp_path.glob(".*.cw-stage-*.dsn"))

    def test_failed_export_preserves_existing_destination(self, tmp_path):
        board = _write_routable_board(tmp_path)
        destination = tmp_path / "board.dsn"
        destination.write_text("prior validated output", encoding="utf-8")

        with mock.patch("circuit_weaver.autoroute._find_kicad_cli", return_value="kicad-cli"):
            with mock.patch("circuit_weaver.autoroute._kicad_cli_supports_specctra", return_value=True):
                with mock.patch(
                    "subprocess.run",
                    return_value=mock.Mock(returncode=1, stdout="", stderr="export failed"),
                ):
                    result = export_dsn(board, destination, overwrite=True)

        assert result["status"] == "error"
        assert destination.read_text(encoding="utf-8") == "prior validated output"

    def test_zero_exit_without_valid_staged_output_is_error(self, tmp_path):
        board = _write_routable_board(tmp_path)

        def invalid_export(command, **_kwargs):
            Path(command[command.index("-o") + 1]).write_text("not a DSN", encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("circuit_weaver.autoroute._find_kicad_cli", return_value="kicad-cli"):
            with mock.patch("circuit_weaver.autoroute._kicad_cli_supports_specctra", return_value=True):
                with mock.patch("subprocess.run", side_effect=invalid_export):
                    result = export_dsn(board, tmp_path / "bad.dsn")

        assert result["status"] == "error"
        assert "invalid DSN" in result["message"]
        assert not (tmp_path / "bad.dsn").exists()


class TestAutoroute:
    def test_missing_input_and_invalid_controls_fail_before_execution(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        assert autoroute_pcb(tmp_path / "missing.dsn")["status"] == "error"
        assert autoroute_pcb(dsn, effort="ludicrous")["status"] == "error"
        assert autoroute_pcb(dsn, timeout_seconds=0)["status"] == "error"
        assert autoroute_pcb(dsn, timeout_seconds=float("nan"))["status"] == "error"
        assert autoroute_pcb(dsn, max_passes=-1)["status"] == "error"
        assert autoroute_pcb(dsn, optimization_threads=-1)["status"] == "error"
        assert autoroute_pcb(dsn, optimizer_strategy="hybrid")["status"] == "error"
        assert autoroute_pcb(dsn, optimizer_strategy="global", optimizer_hybrid_ratio="1:1")["status"] == "error"
        assert autoroute_pcb(dsn, optimizer_improvement_threshold=101)["status"] == "error"
        assert autoroute_pcb(dsn, seed=-1)["status"] == "error"

    def test_user_dsn_routes_to_truthful_staged_ses_with_supported_controls(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        original_dsn = dsn.read_bytes()
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run", side_effect=_fake_freerouting_success()) as run:
                result = autoroute_pcb(
                    dsn,
                    max_passes=0,
                    optimization_threads=4,
                    optimizer_strategy="hybrid",
                    optimizer_hybrid_ratio="1:2",
                    optimizer_item_selection="prioritized",
                    optimizer_improvement_threshold=0.1,
                )

        assert result["status"] == "ok"
        assert result["output_kind"] == "specctra_session"
        assert result["output_path"] == str(tmp_path / "sensor-board.ses")
        assert result["artifact"]["path"] == result["output_path"]
        assert result["router"]["version"] == "2.2.4"
        assert "routing_clearance_guardband" not in result
        assert dsn.read_bytes() == original_dsn
        command = run.call_args.args[0]
        assert command[command.index("-mp") + 1] == "0"
        assert command[command.index("-mt") + 1] == "4"
        assert command[command.index("-us") + 1] == "hybrid"
        assert command[command.index("-hr") + 1] == "1:2"
        assert command[command.index("-is") + 1] == "prioritized"
        assert command[command.index("-oit") + 1] == "0.1"
        assert "--gui.enabled=false" in command
        assert "-dr" not in command

    def test_effort_presets_are_large_enough_for_nontrivial_boards(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run", side_effect=_fake_freerouting_success()) as run:
                result = autoroute_pcb(dsn, effort="high")

        assert result["status"] == "ok"
        assert run.call_args.args[0][run.call_args.args[0].index("-mp") + 1] == "1000"

    def test_best_effort_help_probe_supplies_version_when_route_log_omits_it(self, tmp_path):
        def run(command, **_kwargs):
            if "-help" in command:
                return mock.Mock(returncode=0, stdout="Freerouting v2.2.3\n-mp passes", stderr="")
            Path(command[command.index("-do") + 1]).write_text(VALID_SES, encoding="utf-8")
            return mock.Mock(returncode=0, stdout=_routing_output(version=None), stderr="")

        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run", side_effect=run):
                result = autoroute_pcb(_write_dsn(tmp_path))

        assert result["status"] == "ok"
        assert result["router"]["version"] == "2.2.3"

    def test_invalid_dsn_and_output_extension_fail_before_router(self, tmp_path):
        invalid = _write_dsn(tmp_path, text="(pcb invalid)")
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command") as find_router:
            invalid_result = autoroute_pcb(invalid)
        assert "Invalid Specctra DSN" in invalid_result["message"]
        find_router.assert_not_called()

        dsn = _write_dsn(tmp_path)
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            extension_result = autoroute_pcb(dsn, output_path=str(tmp_path / "misnamed.kicad_pcb"))
        assert "must end in .ses" in extension_result["message"]

    def test_real_pcb_requires_supported_dsn_export_and_never_uses_direct_mode(self, tmp_path):
        board = _write_routable_board(tmp_path)
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("circuit_weaver.autoroute._find_kicad_cli", return_value="kicad-cli"):
                with mock.patch("circuit_weaver.autoroute._kicad_cli_supports_specctra", return_value=False):
                    with mock.patch("subprocess.run") as run:
                        result = autoroute_pcb(board)

        assert result["status"] == "error"
        assert "pcbnew" in result["message"]
        assert not any("-de" in call.args[0] for call in run.call_args_list)

    def test_capable_kicad_cli_exports_staged_dsn_then_routes_ses(self, tmp_path):
        board = _write_routable_board(tmp_path)
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if "-help" in command:
                return mock.Mock(returncode=0, stdout="Freerouting v2.2.4", stderr="")
            if command[0] == "kicad-cli":
                Path(command[command.index("-o") + 1]).write_text(VALID_DSN, encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")
            Path(command[command.index("-do") + 1]).write_text(VALID_SES, encoding="utf-8")
            return mock.Mock(returncode=0, stdout=_routing_output(), stderr="")

        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("circuit_weaver.autoroute._find_kicad_cli", return_value="kicad-cli"):
                with mock.patch("circuit_weaver.autoroute._kicad_cli_supports_specctra", return_value=True):
                    with mock.patch("subprocess.run", side_effect=fake_run):
                        result = autoroute_pcb(board)

        assert result["status"] == "ok"
        assert result["routing_clearance_guardband"]["margin_mm"] == 0.01
        assert result["routing_clearance_guardband"]["native_board_rules_changed"] is False
        dsn_text = (tmp_path / "sensor-board_autoroute.dsn").read_text(encoding="utf-8")
        assert dsn_text.count("(clearance 5.393700787401574803149606299)") == 2
        assert "(clearance 1.25 (type smd_smd))" in dsn_text
        assert (tmp_path / "sensor-board_autoroute.dsn").exists()
        assert result["output_path"] == str(tmp_path / "sensor-board.ses")
        assert result["search"]["attempts"][0]["placement_order_strategy"] == "ascending"
        assert result["search"]["attempts"][0]["placement_order"] == ["R1"]
        assert any("specctra" in command for command in calls)
        assert any("-de" in command and "-do" in command for command in calls)

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    @pytest.mark.parametrize(
        ("optimization_threads", "optimizer_strategies"),
        [(0, [None, None, None]), (2, ["greedy", "global", "hybrid"])],
    )
    def test_native_route_attempts_use_isolated_ordered_dsn_variants(
        self, tmp_path, optimization_threads, optimizer_strategies, newline,
    ):
        board = _write_routable_board(tmp_path)
        route_inputs: list[tuple[Path, bytes, list[str]]] = []
        route_commands: list[list[str]] = []
        baseline_snapshots: list[bytes] = []
        original_board = board.read_bytes()
        exported_dsn = VALID_DSN.replace(
            '(component "Resistor_SMD:R_0402_1005Metric" (place R1 10000 10000 front 0))',
            '(component "Resistor_SMD:R_0402_1005Metric" (place R1 10000 10000 front 0))\n'
            '(component "Connector:USB" (place J1 30000 10000 front 0))',
        )

        def fake_run(command, **_kwargs):
            if "-help" in command:
                return mock.Mock(returncode=0, stdout="Freerouting v2.4.1", stderr="")
            if command[0] == "kicad-cli":
                Path(command[command.index("-o") + 1]).write_bytes(
                    exported_dsn.replace("\n", newline).encode("utf-8")
                )
                return mock.Mock(returncode=0, stdout="", stderr="")
            dsn_path = Path(command[command.index("-de") + 1])
            dsn_bytes = dsn_path.read_bytes()
            _, reference_order = _native_placement_variant(dsn_bytes.decode("utf-8"), "original")
            route_inputs.append((dsn_path, dsn_bytes, reference_order))
            route_commands.append(command)
            baseline_snapshots.append((tmp_path / "sensor-board_autoroute.dsn").read_bytes())
            Path(command[command.index("-do") + 1]).write_text(VALID_SES, encoding="utf-8")
            return mock.Mock(returncode=0, stdout=_routing_output(), stderr="")

        def preserve_export_bytes(_path, _margin_mm):
            return {"applied": True, "margin_mm": 0.01, "native_board_rules_changed": False}

        with mock.patch("circuit_weaver.autoroute._apply_specctra_clearance_margin", side_effect=preserve_export_bytes):
            with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
                with mock.patch("circuit_weaver.autoroute._find_kicad_cli", return_value="kicad-cli"):
                    with mock.patch("circuit_weaver.autoroute._kicad_cli_supports_specctra", return_value=True):
                        with mock.patch("subprocess.run", side_effect=fake_run):
                            result = autoroute_pcb(
                                board, attempts=3, optimization_threads=optimization_threads,
                            )

        assert result["status"] == "ok"
        assert len(route_inputs) == 3
        assert [entry[2] for entry in route_inputs] == [["J1", "R1"], ["R1", "J1"], ["R1", "J1"]]
        assert len({entry[0].parent for entry in route_inputs}) == 3
        assert all(entry[0].name == "sensor-board_autoroute.dsn" for entry in route_inputs)
        assert len({entry[1] for entry in route_inputs}) == 2
        assert [row["placement_order_strategy"] for row in result["search"]["attempts"]] == [
            "ascending", "descending", "original",
        ]
        assert [row["placement_order"] for row in result["search"]["attempts"]] == [
            ["J1", "R1"], ["R1", "J1"], ["R1", "J1"],
        ]
        assert [row["optimizer_strategy"] for row in result["search"]["attempts"]] == optimizer_strategies
        assert all(command[command.index("-mt") + 1] == str(optimization_threads) for command in route_commands)
        assert [
            command[command.index("-us") + 1] if "-us" in command else None
            for command in route_commands
        ] == optimizer_strategies
        assert all(row["variant_sha256"] for row in result["search"]["attempts"])
        assert len(set(baseline_snapshots)) == 1
        assert baseline_snapshots[0] == (tmp_path / "sensor-board_autoroute.dsn").read_bytes()
        assert (b"\r\n" in baseline_snapshots[0]) == (newline == "\r\n")
        assert all((b"\r\n" in variant) == (newline == "\r\n") for _, variant, _ in route_inputs)
        def without_placement(content):
            text = content.decode("utf-8")
            spans = list(_iter_sexpr_block_spans(text, "placement"))
            assert len(spans) == 1
            start, end = spans[0]
            return (text[:start] + text[end:]).encode("utf-8")

        assert all(without_placement(variant) == without_placement(baseline_snapshots[0])
                   for _, variant, _ in route_inputs)
        assert board.read_bytes() == original_board
        assert (tmp_path / "sensor-board_autoroute.dsn").exists()
        assert not list(tmp_path.glob(".cw-routing-*"))

    def test_native_variant_drift_cannot_replace_existing_session(self, tmp_path):
        board = _write_routable_board(tmp_path)
        destination = tmp_path / "reviewed.ses"
        destination.write_bytes(b"previous reviewed session")
        baseline_snapshot: list[bytes] = []

        def fake_run(command, **_kwargs):
            if "-help" in command:
                return mock.Mock(returncode=0, stdout="Freerouting v2.4.1", stderr="")
            if command[0] == "kicad-cli":
                Path(command[command.index("-o") + 1]).write_text(VALID_DSN, encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")
            dsn_path = Path(command[command.index("-de") + 1])
            baseline_snapshot.append((tmp_path / "sensor-board_autoroute.dsn").read_bytes())
            Path(command[command.index("-do") + 1]).write_text(VALID_SES, encoding="utf-8")
            dsn_path.write_bytes(dsn_path.read_bytes() + b"\n; changed during routing")
            return mock.Mock(returncode=0, stdout=_routing_output(), stderr="")

        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("circuit_weaver.autoroute._find_kicad_cli", return_value="kicad-cli"):
                with mock.patch("circuit_weaver.autoroute._kicad_cli_supports_specctra", return_value=True):
                    with mock.patch("subprocess.run", side_effect=fake_run):
                        result = autoroute_pcb(board, output_path=str(destination), overwrite=True)

        assert result["status"] == "error"
        assert "variant changed" in result["message"]
        assert destination.read_bytes() == b"previous reviewed session"
        assert baseline_snapshot[0] == (tmp_path / "sensor-board_autoroute.dsn").read_bytes()
        assert not list(tmp_path.glob(".cw-routing-*"))

    def test_incomplete_routing_is_partial_but_published(self, tmp_path):
        output = _routing_output(incomplete=2)
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run", side_effect=_fake_freerouting_success(output)):
                result = autoroute_pcb(_write_dsn(tmp_path))

        assert result["status"] == "partial"
        assert result["stats"]["incomplete"] == 2
        assert (tmp_path / "sensor-board.ses").exists()

    def test_unknown_completeness_fails_without_publishing(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        destination = tmp_path / "unknown-completeness.ses"
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch(
                "subprocess.run",
                side_effect=_fake_freerouting_success("Routed: 4 traces, 1 via"),
            ):
                result = autoroute_pcb(dsn, output_path=str(destination))
        assert result["status"] == "error"
        assert "connection completeness" in result["message"]
        assert not destination.exists()

    def test_unreported_clearance_publishes_only_for_kicad_drc_review(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        destination = tmp_path / "clearance-unreported.ses"
        output = "Freerouting v2.2.4\nRouted: 4 traces, 1 via, 0 incomplete"
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run", side_effect=_fake_freerouting_success(output)):
                result = autoroute_pcb(dsn, output_path=str(destination))

        assert result["status"] == "review_required"
        assert result["routing_complete"] is True
        assert result["fabrication_ready"] is False
        assert result["requires_kicad_drc"] is True
        assert result["verification"]["clearance"] == "unreported_by_router_cli"
        assert "run KiCad DRC" in result["message"]
        assert destination.exists()

    def test_clearance_violations_block_publication(self, tmp_path):
        destination = tmp_path / "violating.ses"
        output = _routing_output(clearance_violations=3)
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run", side_effect=_fake_freerouting_success(output)):
                result = autoroute_pcb(_write_dsn(tmp_path), output_path=str(destination))

        assert result["status"] == "error"
        assert "3 clearance violation" in result["message"]
        assert not destination.exists()

    def test_existing_destination_requires_overwrite_and_is_preserved_on_failure(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        destination = tmp_path / "sensor-board.ses"
        destination.write_text("prior validated SES", encoding="utf-8")

        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run") as run:
                blocked = autoroute_pcb(dsn)
        assert blocked["status"] == "error"
        assert "--overwrite" in blocked["message"]
        run.assert_not_called()

        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch(
                "subprocess.run",
                return_value=mock.Mock(returncode=1, stdout="", stderr="router failed"),
            ):
                failed = autoroute_pcb(dsn, overwrite=True)
        assert failed["status"] == "error"
        assert destination.read_text(encoding="utf-8") == "prior validated SES"

    def test_invalid_or_uncorrelated_ses_is_not_published(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        cases = (
            ("not a session artifact", "invalid SES"),
            (VALID_SES.replace("(net GND", "(net SECRET", 1), "absent from source DSN"),
            (
                VALID_SES.replace(
                    '      (net VDD_3P3 (wire (path F.Cu 250 10500 10000 20000 10000)))\n',
                    "",
                ),
                "missing net(s) from source DSN",
            ),
        )
        for index, (ses_text, expected) in enumerate(cases):
            destination = tmp_path / f"bad-{index}.ses"
            with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
                with mock.patch("subprocess.run", side_effect=_fake_freerouting_success(ses_text=ses_text)):
                    result = autoroute_pcb(dsn, output_path=str(destination))
            assert result["status"] == "error"
            assert expected in result["message"]
            assert not destination.exists()

    def test_no_overwrite_publication_cannot_clobber_file_created_during_route(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        destination = tmp_path / "raced.ses"
        real_link = os.link

        def concurrent_writer_then_link(source, target):
            destination.write_text("concurrent writer\n", encoding="utf-8")
            return real_link(source, target)

        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run", side_effect=_fake_freerouting_success()):
                with mock.patch(
                    "circuit_weaver.autoroute.os.link",
                    side_effect=concurrent_writer_then_link,
                ):
                    result = autoroute_pcb(dsn, output_path=str(destination))

        assert result["status"] == "error"
        assert "refusing to replace" in result["message"]
        assert destination.read_text(encoding="utf-8") == "concurrent writer\n"

    def test_router_failure_and_timeout_are_errors(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch(
                "subprocess.run",
                return_value=mock.Mock(returncode=1, stdout="", stderr="netlist rejected"),
            ):
                failure = autoroute_pcb(dsn)
            with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("freerouting", 3)):
                timeout = autoroute_pcb(dsn, timeout_seconds=3)

        assert "netlist rejected" in failure["message"]
        assert "timed out" in timeout["message"]
        assert not list(tmp_path.glob(".*.cw-stage-*.ses"))

    def test_seed_is_capability_gated_and_passed_when_supported(self, tmp_path):
        dsn = _write_dsn(tmp_path)
        unsupported_help = mock.Mock(returncode=0, stdout="Freerouting v2.1.0\n-mp", stderr="")
        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run", return_value=unsupported_help) as run:
                unsupported = autoroute_pcb(dsn, seed=42)
        assert unsupported["status"] == "error"
        assert "does not advertise -random_seed" in unsupported["message"]
        assert run.call_count == 1

        def supported_run(command, **_kwargs):
            if "-help" in command:
                return mock.Mock(
                    returncode=0,
                    stdout="Freerouting v2.2.4\n-random_seed seed",
                    stderr="",
                )
            assert command[command.index("-random_seed") + 1] == "42"
            Path(command[command.index("-do") + 1]).write_text(VALID_SES, encoding="utf-8")
            return mock.Mock(returncode=0, stdout=_routing_output(), stderr="")

        with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]):
            with mock.patch("subprocess.run", side_effect=supported_run):
                supported = autoroute_pcb(dsn, output_path=str(tmp_path / "seeded.ses"), seed=42)
        assert supported["status"] == "ok"
        assert supported["router"]["seed"] == 42


def test_generated_preview_name_marker_and_preflight_contract(tmp_path):
    from circuit_weaver.component_db import ComponentDef, PinDef
    from circuit_weaver.pcb_export import generate_pcb_placement

    component = ComponentDef(
        mpn="TEST-U1",
        ref_prefix="U",
        source_ref="U1",
        value="TEST-U1",
        footprint="Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm",
        category="digital",
        pins=[
            PinDef("1", "SIG", "bidirectional", "L"),
            PinDef("2", "VDD", "power_in", "T"),
            PinDef("3", "GND", "power_in", "B"),
        ],
        power_pins={"2": "VDD_3P3", "3": "GND"},
    )

    pcb_file, _placements = generate_pcb_placement([component], tmp_path, "Sensor")
    preview = Path(pcb_file)
    result = preflight_pcb(preview)

    assert preview.name == "Sensor_placement_preview.kicad_pcb"
    assert "placement_preview" in preview.read_text(encoding="utf-8")
    assert result["routable"] is False


def test_stat_parser_prefers_final_structured_statistics_over_progress_lines():
    real_style_log = _routing_output(
        incomplete=0,
        clearance_violations=0,
        traces=79,
        vias=7,
        progress=(
            "Auto-router pass #1 was completed with 7 unrouted\n"
            "Auto-router pass #2 was completed with 1 unrouted"
        ),
    )

    assert _parse_routing_stats(real_style_log) == {
        "traces": 79,
        "vias": 7,
        "incomplete": 0,
        "max_connections": 5,
        "clearance_violations": 0,
        "statistics_source": "structured_json",
    }
    assert _parse_freerouting_version(real_style_log) == "2.2.4"


def test_stat_parser_does_not_default_unknown_counts_to_zero():
    assert _parse_routing_stats("Routed: 9 traces, 2 vias") == {
        "traces": 9,
        "vias": 2,
        "incomplete": None,
        "max_connections": None,
        "clearance_violations": None,
        "statistics_source": "text_summary",
    }
    assert _parse_routing_stats(
        "pass 1: 9 unrouted\nRouting finished: 0 incomplete; clearance violations: 0"
    )["incomplete"] == 0


def test_stat_parser_understands_current_freerouting_cli_summary():
    output = (
        "Freerouting v2.2.4 (build-date: 2026-05-13)\n"
        "Auto-router pass #2 was completed with the score of 934.53 (1 unrouted).\n"
        "Stopping the auto-router after 18 passes (1 item still unconnected).\n"
        "Auto-router session completed: final score: 934.53 (1 unrouted)."
    )

    stats = _parse_routing_stats(output)

    assert stats["incomplete"] == 1
    assert stats["clearance_violations"] is None
    assert stats["statistics_source"] == "text_summary"


def test_kicad_10_named_pad_nets_without_legacy_net_table(tmp_path):
    import re

    board = ROUTABLE_PCB.replace("20240108", "20260206")
    board = re.sub(r'^  \(net .*\)\n', '', board, flags=re.MULTILINE)
    board = re.sub(r'\(net \d+ ("[^"]*")\)', r'(net \1)', board)
    path = tmp_path / "kicad10.kicad_pcb"
    path.write_text(board, encoding="utf-8")
    result = preflight_pcb(path)
    assert result["routable"] is True
    assert result["stats"]["nets"] == 2
    path.write_text(board.replace("20260206", "20240108"), encoding="utf-8")
    assert preflight_pcb(path)["routable"] is False


def test_current_router_final_scores_and_geometry_counts(tmp_path):
    def run(command, **kwargs):
        if "-help" in command:
            return mock.Mock(returncode=0, stdout="Freerouting v2.4.1", stderr="")
        assert "--router.fanout.enabled=false" in command
        assert "--router.automatic_neckdown=false" in command
        assert "--router.optimizer.improvement_threshold=0.1" in command
        assert "-da" in command
        Path(command[command.index("-do") + 1]).write_text(VALID_SES)
        return mock.Mock(returncode=0, stdout=(
            "Fanout stage completed: final score: 100 (4 unrouted and 0 violations)\n"
            "Optimizer stage completed: final score: 50 (0 unrouted and 0 violations)\n"
        ), stderr="")
    with mock.patch("circuit_weaver.autoroute._find_freerouting_command", return_value=["freerouting"]), \
            mock.patch("subprocess.run", side_effect=run):
        result = autoroute_pcb(_write_dsn(tmp_path), optimizer_improvement_threshold=0.1)
    assert result["status"] == "ok"
    assert result["stats"]["incomplete"] == 0
    assert result["stats"]["vias"] == 0
    assert result["stats"]["traces"] == 2
    assert not list(tmp_path.glob(".cw-router-*"))
