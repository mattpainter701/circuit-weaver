"""Tests for DFM (Design for Manufacture) checker."""

from circuit_weaver.dfm_checker import (
    _DFM_PROFILES,
    DFMViolation,
    _parse_pcb_s_expr,
    check_dfm,
    dfm_report,
)


class TestDFMProfiles:
    """Test DFM profile definitions."""

    def test_jlcpcb_profile_exists(self):
        """JLCPCB profile should be defined."""
        assert "jlcpcb" in _DFM_PROFILES
        profile = _DFM_PROFILES["jlcpcb"]
        assert profile["trace_width_min"] == 0.127
        assert profile["via_diameter_min"] == 0.45

    def test_pcbway_profile_exists(self):
        """PCBWay profile should be defined."""
        assert "pcbway" in _DFM_PROFILES
        profile = _DFM_PROFILES["pcbway"]
        assert profile["trace_width_min"] == 0.1
        assert profile["via_diameter_min"] == 0.3


class TestPCBParsing:
    """Test PCB S-expression parsing."""

    def test_parse_empty_pcb(self):
        """Parse minimal PCB content."""
        content = "(kicad_pcb (version 20231120))"
        pcb = _parse_pcb_s_expr(content)
        assert pcb["nets"] == {}
        assert pcb["tracks"] == []
        assert pcb["vias"] == []

    def test_parse_nets(self):
        """Extract net names and numbers."""
        content = """
        (kicad_pcb (version 20231120)
            (net 0 "")
            (net 1 "GND")
            (net 2 "VDD_3V3")
        )
        """
        pcb = _parse_pcb_s_expr(content)
        assert pcb["nets"]["GND"] == 1
        assert pcb["nets"]["VDD_3V3"] == 2

    def test_parse_board_size(self):
        """Extract board dimensions."""
        content = "(kicad_pcb " + _rectangular_outline(10, 20, 110, 100) + ")"
        pcb = _parse_pcb_s_expr(content)
        assert pcb["board_width_mm"] == 100.0
        assert pcb["board_height_mm"] == 80.0


def _write_width_board(tmp_path, version: int, segments: str):
    pcb_file = tmp_path / f"width-{version}.kicad_pcb"
    pcb_file.write_text(f'(kicad_pcb (version {version}) (net 1 "N") {segments})', encoding="utf-8")
    return pcb_file


def _segment(width_field: str) -> str:
    return f"(segment (start 1 1) (end 2 1) {width_field} (layer F.Cu) (net 1))"


def _rectangular_outline(min_x=0, min_y=0, max_x=20, max_y=20, *, quoted_layer=False):
    layer = '"Edge.Cuts"' if quoted_layer else "Edge.Cuts"
    return " ".join(
        [
            f"(gr_line (start {min_x} {min_y}) (end {max_x} {min_y}) (layer {layer}))",
            f"(gr_line (start {max_x} {min_y}) (end {max_x} {max_y}) (layer {layer}))",
            f"(gr_line (start {max_x} {max_y}) (end {min_x} {max_y}) (layer {layer}))",
            f"(gr_line (start {min_x} {max_y}) (end {min_x} {min_y}) (layer {layer}))",
        ]
    )


def test_legacy_kicad_width_is_millimeters_and_passes_when_above_minimum(tmp_path):
    pcb_file = _write_width_board(tmp_path, 20171130, _segment("(width 0.2032)"))

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    assert not [v for v in violations if v.type.startswith("trace_width")]


def test_modern_kicad_width_is_millimeters_and_passes_when_above_minimum(tmp_path):
    pcb_file = _write_width_board(tmp_path, 20260206, _segment("(width 0.2)"))

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    assert not [v for v in violations if v.type.startswith("trace_width")]


def test_trace_below_minimum_reports_actual_mm_width(tmp_path):
    pcb_file = _write_width_board(tmp_path, 20260206, _segment("(width 0.08)"))

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")
    narrow = [v for v in violations if v.type == "trace_width"]

    assert len(narrow) == 1
    assert narrow[0].actual == 0.08
    assert narrow[0].minimum == 0.09


def test_arc_track_widths_use_millimeters_and_keep_genuine_findings(tmp_path):
    arcs = " ".join(
        f'(arc (start 1 1) (mid 1.5 1.5) (end 2 1) {field} (layer "F.Cu") (net 1))'
        for field in ("(width 0.2)", "(width 0.08)", "")
    )
    pcb_file = _write_width_board(tmp_path, 20260206, arcs)
    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    assert [(v.type, v.location, v.actual) for v in violations] == [
        ("trace_width", "arc 2", 0.08),
        ("trace_width_unknown", "arc 3", None),
    ]


def test_missing_malformed_and_zero_width_fail_as_unknown_not_zero(tmp_path):
    segments = " ".join(
        [
            "(segment (start 1 1) (end 2 1) (layer F.Cu) (net 1))",
            _segment("(width not-a-number)"),
            _segment("(width 0)"),
        ]
    )
    pcb_file = _write_width_board(tmp_path, 20260206, segments)

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")
    unknown = [v for v in violations if v.type == "trace_width_unknown"]

    assert len(unknown) == 3
    assert all(v.severity == "critical" and v.actual is None for v in unknown)


def test_missing_width_does_not_borrow_following_segment_width(tmp_path):
    segments = _segment("") + " " + _segment("(width 0.2)")
    pcb_file = _write_width_board(tmp_path, 20260206, segments)

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    assert [(v.type, v.location, v.actual) for v in violations] == [
        ("trace_width_unknown", "segment 1", None)
    ]


def test_incomplete_nested_segment_makes_pcb_root_unknown(tmp_path):
    pcb_file = _write_width_board(tmp_path, 20260206, "(segment (start 1 1) (end 2 1) (width 0.2")

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    assert len(violations) == 1
    assert violations[0].type == "dfm_input_unknown"
    assert violations[0].actual is None


def test_via_dimensions_and_coordinates_are_millimeters(tmp_path):
    pcb_file = _write_width_board(
        tmp_path,
        20171130,
        "(via (at 10 10) (size 0.5) (drill 0.2) (layers F.Cu B.Cu) (net 1)) "
        + _rectangular_outline(),
    )
    pcb = _parse_pcb_s_expr(pcb_file.read_text(encoding="utf-8"))

    assert pcb["vias"] == [{"x": 10.0, "y": 10.0, "diameter": 0.5, "drill": 0.2}]
    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")
    assert not [v for v in violations if v.type.startswith("via_") or v.type == "board_edge_clearance"]


def test_signed_negative_via_coordinates_are_parsed_as_millimeters(tmp_path):
    pcb_file = _write_width_board(
        tmp_path,
        20260206,
        "(via (at -10.25 -5.5) (size 0.5) (drill 0.2) (layers F.Cu B.Cu) (net 1)) "
        + _rectangular_outline(-20, -10, 20, 10),
    )
    pcb = _parse_pcb_s_expr(pcb_file.read_text(encoding="utf-8"))

    assert pcb["vias"][0]["x"] == -10.25
    assert pcb["vias"][0]["y"] == -5.5
    assert not [v for v in check_dfm(pcb_file, profile="jlcpcb_4layer") if v.type == "board_edge_clearance"]


def test_malformed_via_does_not_borrow_following_via_dimensions(tmp_path):
    malformed = "(via (at -1 2) (size nope) (drill 0.2) (layers F.Cu B.Cu) (net 1))"
    valid = "(via (at 10 10) (size 0.5) (drill 0.2) (layers F.Cu B.Cu) (net 1))"
    pcb_file = _write_width_board(tmp_path, 20260206, malformed + " " + valid + " " + _rectangular_outline())

    pcb = _parse_pcb_s_expr(pcb_file.read_text(encoding="utf-8"))
    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    assert len(pcb["vias"]) == 1
    assert len(pcb["via_errors"]) == 1
    unknown = [v for v in violations if v.type == "via_geometry_unknown"]
    assert len(unknown) == 1
    assert unknown[0].location == "Via 1"
    assert unknown[0].actual is None


def test_missing_and_nonpositive_via_fields_are_unknown(tmp_path):
    via_blocks = [
        "(via (at 1 2) (drill 0.2) (layers F.Cu B.Cu) (net 1))",
        "(via (at 3 4) (size -0.5) (drill 0.2) (layers F.Cu B.Cu) (net 1))",
        "(via (at 5 6) (size 0.5) (drill 0) (layers F.Cu B.Cu) (net 1))",
    ]
    pcb_file = _write_width_board(tmp_path, 20260206, " ".join(via_blocks) + " " + _rectangular_outline())

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    unknown = [v for v in violations if v.type == "via_geometry_unknown"]
    assert len(unknown) == 3
    assert all(v.severity == "critical" and v.actual is None for v in unknown)


def test_via_below_minimum_reports_actual_mm_dimensions(tmp_path):
    pcb_file = _write_width_board(
        tmp_path,
        20260206,
        "(via (at 10 10) (size 0.2) (drill 0.1) (layers F.Cu B.Cu) (net 1)) "
        + _rectangular_outline(quoted_layer=True),
    )

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")
    by_type = {v.type: v for v in violations}

    assert by_type["via_diameter"].actual == 0.2
    assert by_type["via_diameter"].minimum == 0.3
    assert by_type["via_drill"].actual == 0.1
    assert by_type["via_drill"].minimum == 0.15


def test_via_edge_check_uses_real_mm_coordinates(tmp_path):
    pcb_file = _write_width_board(
        tmp_path,
        20260206,
        "(via (at 0.1 10) (size 0.5) (drill 0.2) (layers F.Cu B.Cu) (net 1)) "
        + _rectangular_outline(),
    )

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")
    edge = [v for v in violations if v.type == "board_edge_clearance"]

    assert len(edge) == 1
    assert "0.1" in edge[0].location


def test_via_edge_clearance_subtracts_copper_radius(tmp_path):
    pcb_file = _write_width_board(
        tmp_path,
        20260206,
        "(via (at 0.35 10) (size 0.4) (drill 0.2) (layers F.Cu B.Cu) (net 1)) "
        + _rectangular_outline(),
    )

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")
    edge = [v for v in violations if v.type == "board_edge_clearance"]

    assert len(edge) == 1
    assert abs(edge[0].actual - 0.15) < 1e-9
    assert edge[0].minimum == 0.3


def test_vias_with_unsupported_outline_return_unknown_not_false_edge_pass(tmp_path):
    pcb_file = _write_width_board(
        tmp_path,
        20260206,
        "(via (at 10 10) (size 0.5) (drill 0.2) (layers F.Cu B.Cu) (net 1))",
    )

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")
    unknown = [v for v in violations if v.type == "board_edge_clearance_unknown"]

    assert len(unknown) == 1
    assert unknown[0].severity == "critical"
    assert unknown[0].actual is None


def test_rectangular_gr_rect_outline_with_modern_quoted_layer_is_supported(tmp_path):
    content = (
        '(kicad_pcb (version 20260206) '
        '(via (at 10 10) (size 0.5) (drill 0.2) (layers F.Cu B.Cu) (net 1)) '
        '(gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts")))'
    )
    pcb_file = tmp_path / "modern-rect.kicad_pcb"
    pcb_file.write_text(content, encoding="utf-8")

    pcb = _parse_pcb_s_expr(content)
    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    assert pcb["board_bounds_mm"] == (0.0, 0.0, 20.0, 20.0)
    assert not [v for v in violations if v.type in {"board_edge_clearance", "board_edge_clearance_unknown"}]


def test_curved_edge_outline_is_unknown_instead_of_rectangular_bbox_pass(tmp_path):
    content = (
        '(kicad_pcb (version 20260206) '
        '(via (at 10 10) (size 0.5) (drill 0.2) (layers F.Cu B.Cu) (net 1)) '
        + _rectangular_outline()
        + ' (gr_arc (start 2 2) (mid 3 2) (end 4 2) (layer "Edge.Cuts")))'
    )
    pcb_file = tmp_path / "curved-outline.kicad_pcb"
    pcb_file.write_text(content, encoding="utf-8")

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    assert len([v for v in violations if v.type == "board_edge_clearance_unknown"]) == 1
    assert not [v for v in violations if v.type == "board_edge_clearance"]


def test_malformed_outline_coordinate_is_unknown_without_crashing(tmp_path):
    outline = _rectangular_outline().replace("(start 0 0)", "(start . 0)", 1)
    content = (
        '(kicad_pcb (version 20260206) '
        '(via (at 10 10) (size 0.5) (drill 0.2) (layers F.Cu B.Cu) (net 1)) '
        + outline
        + ')'
    )
    pcb_file = tmp_path / "malformed-outline.kicad_pcb"
    pcb_file.write_text(content, encoding="utf-8")

    violations = check_dfm(pcb_file, profile="jlcpcb_4layer")

    assert len([v for v in violations if v.type == "board_edge_clearance_unknown"]) == 1
    assert not [v for v in violations if v.type == "board_edge_clearance"]


class TestDFMViolation:
    """Test DFMViolation data class."""

    def test_violation_creation(self):
        """Create a DFM violation."""
        v = DFMViolation(
            severity="critical",
            type="trace_width",
            location="net VDD_3V3",
            actual=0.1,
            minimum=0.127,
            message="Trace too narrow",
            suggestion="Increase trace width",
        )
        assert v.severity == "critical"
        assert v.type == "trace_width"

    def test_violation_to_dict(self):
        """Convert violation to dict."""
        v = DFMViolation(
            severity="warning",
            type="via_drill",
            location="Via 5",
            actual=0.15,
            minimum=0.2,
            message="Via drill too small",
        )
        d = v.to_dict()
        assert d["severity"] == "warning"
        assert d["actual"] == 0.15


class TestDFMReport:
    """Test DFM report generation."""

    def test_empty_report(self):
        """Report with no violations."""
        report = dfm_report([])
        assert "No DFM violations detected" in report

    def test_critical_violations_report(self):
        """Report with critical violations."""
        violations = [
            DFMViolation(
                severity="critical",
                type="trace_width",
                location="net signal",
                actual=0.1,
                minimum=0.127,
                message="Trace width violation",
                suggestion="Increase width",
            )
        ]
        report = dfm_report(violations)
        assert "CRITICAL" in report
        assert "TRACE_WIDTH" in report.upper()

    def test_warning_violations_report(self):
        """Report with warning violations."""
        violations = [
            DFMViolation(
                severity="warning",
                type="board_edge_clearance",
                location="Via 1",
                actual=None,
                minimum=0.3,
                message="Via near edge",
                suggestion="Move via away from edge",
            )
        ]
        report = dfm_report(violations)
        assert "WARNINGS" in report or "⚠️" in report


class TestCheckDFM:
    """Test full DFM check."""

    def test_check_dfm_missing_file(self, tmp_path):
        """Check nonexistent file."""
        violations = check_dfm(tmp_path / "missing.kicad_pcb")
        assert len(violations) == 1
        assert violations[0].type == "dfm_input_unknown"
        assert violations[0].severity == "critical"
        assert violations[0].actual is None

    def test_check_dfm_unreadable_file_is_unknown_not_clean(self, tmp_path, monkeypatch):
        pcb_file = tmp_path / "unreadable.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)", encoding="utf-8")
        original_read_text = type(pcb_file).read_text

        def fail_read(path, *args, **kwargs):
            if path == pcb_file:
                raise OSError("simulated read failure")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(type(pcb_file), "read_text", fail_read)
        violations = check_dfm(pcb_file)

        assert len(violations) == 1
        assert violations[0].type == "dfm_input_unknown"
        assert violations[0].severity == "critical"
        assert violations[0].actual is None

    def test_check_dfm_well_formed_empty_board(self, tmp_path):
        """An empty but complete board expression is a valid empty input."""
        pcb_file = tmp_path / "empty-board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20231120))")
        violations = check_dfm(str(pcb_file), profile="jlcpcb")
        assert violations == []

    def test_empty_text_is_unknown_not_clean(self, tmp_path):
        pcb_file = tmp_path / "empty-text.kicad_pcb"
        pcb_file.write_text("", encoding="utf-8")

        violations = check_dfm(pcb_file)

        assert len(violations) == 1
        assert violations[0].type == "dfm_input_unknown"
        assert violations[0].actual is None

    def test_readable_junk_is_unknown_not_clean(self, tmp_path):
        pcb_file = tmp_path / "junk.kicad_pcb"
        pcb_file.write_text("not a KiCad PCB (segment (width 0.2))", encoding="utf-8")

        violations = check_dfm(pcb_file)

        assert len(violations) == 1
        assert violations[0].type == "dfm_input_unknown"
        assert violations[0].actual is None

    def test_truncated_outer_pcb_expression_is_unknown(self, tmp_path):
        pcb_file = tmp_path / "truncated-root.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (segment (start 0 0) (end 1 0) (width 0.2))", encoding="utf-8")

        violations = check_dfm(pcb_file)

        assert len(violations) == 1
        assert violations[0].type == "dfm_input_unknown"
        assert violations[0].actual is None

    def test_duplicate_top_level_pcb_roots_are_unknown(self, tmp_path):
        pcb_file = tmp_path / "duplicate-roots.kicad_pcb"
        pcb_file.write_text("(kicad_pcb) (kicad_pcb)", encoding="utf-8")

        violations = check_dfm(pcb_file)

        assert len(violations) == 1
        assert violations[0].type == "dfm_input_unknown"
        assert violations[0].actual is None

    def test_check_dfm_with_custom_rules(self, tmp_path):
        """Check with custom DFM rules."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20231120))")
        custom = {"trace_width_min": 0.5}
        violations = check_dfm(str(pcb_file), custom_rules=custom)
        assert isinstance(violations, list)

    def test_check_dfm_profile_fallback(self, tmp_path):
        """Unknown profile should fall back to default."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20231120))")
        violations = check_dfm(str(pcb_file), profile="unknown_fab")
        # Should not raise, should return list
        assert isinstance(violations, list)
