"""Regression coverage for fail-closed schematic generation invariants."""

from __future__ import annotations

import copy
import re

import pytest

from circuit_weaver.allocator import SheetAllocation, ensure_unique_sheet_names
from circuit_weaver.component_db import BUILTIN_REGISTRY, ComponentDef, PinDef
from circuit_weaver.generator import (
    _avoid_foreign_net_anchors,
    _generate_root_schematic,
    _normalize_polyline,
    _plan_power_flags,
    _render_sheet,
    _render_symbol_name_and_sexpr,
    _validate_rendered_pin_mappings,
    generate_from_components,
)
from circuit_weaver.placer import (
    PlacedComponent,
    PlacedPassive,
    SheetLayout,
    _component_ref,
    reserve_explicit_refs,
    reset_ref_counters,
)
from circuit_weaver.primitives import (
    _extract_sexpr_block,
    _resolve_label_collisions,
    get_pin_positions,
    pin_connection_point,
    sexpr_global_label,
    sexpr_junction,
    sexpr_no_connect,
    sexpr_wire,
    sheet_title_text,
)
from circuit_weaver.validator import _validate_pin_mapping_integrity


def test_sheet_title_banner_is_left_anchored_at_the_safe_margin():
    """Long project titles must not render left of their in-bounds anchor."""
    banner = sheet_title_text("OLED_Display_Module", "Top-level overview", x=20, y=15)

    assert '(text "OLED_Display_Module" (at 38.10 15.24 0)' in banner
    assert '(text "Top-level overview" (at 38.10 20.32 0)' in banner


def _label_anchor(label: str) -> tuple[float, float]:
    match = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)\s+\d+\)", label)
    assert match
    return float(match.group(1)), float(match.group(2))


def _wire_endpoints(wire: str) -> tuple[tuple[float, float], tuple[float, float]]:
    match = re.search(
        r"\(pts\s+\(xy\s+([-\d.]+)\s+([-\d.]+)\)\s+"
        r"\(xy\s+([-\d.]+)\s+([-\d.]+)\)\)",
        wire,
    )
    assert match
    return (
        (float(match.group(1)), float(match.group(2))),
        (float(match.group(3)), float(match.group(4))),
    )


def test_generated_usb_cc_pull_downs_remain_in_disjoint_wire_components(tmp_path):
    from circuit_weaver.generator import _WIRE_PTS_RE, _segments_touch_or_cross

    usb = copy.deepcopy(BUILTIN_REGISTRY.get("USB-C-PWR"))
    assert usb is not None
    usb.source_ref = "J1"
    generate_from_components(
        [usb],
        output_dir=str(tmp_path),
        project_name="UsbCcIsolation",
        stable_uuids=True,
        validate=False,
        readiness_gate=False,
    )
    schematic = (tmp_path / "UsbCcIsolation.kicad_sch").read_text(encoding="utf-8")
    segments = [
        tuple(float(match.group(index)) for index in range(1, 5))
        for match in _WIRE_PTS_RE.finditer(schematic)
    ]
    assert segments

    parent = list(range(len(segments)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    for first_index, first in enumerate(segments):
        for second_index in range(first_index + 1, len(segments)):
            if _segments_touch_or_cross(first, segments[second_index]):
                union(first_index, second_index)

    label_components: dict[str, set[int]] = {}
    for net_name in ("USB_CC1", "USB_CC2"):
        label_match = re.search(
            rf'\(global_label\s+"{net_name}".*?\(at\s+([\d.-]+)\s+([\d.-]+)\s+\d+\)',
            schematic,
            re.DOTALL,
        )
        assert label_match, net_name
        point = (
            float(label_match.group(1)),
            float(label_match.group(2)),
        )
        point_segment = (point[0], point[1], point[0], point[1])
        attached = {
            find(index)
            for index, segment in enumerate(segments)
            if _segments_touch_or_cross(point_segment, segment)
        }
        assert attached, f"{net_name} is detached from every wire"
        label_components[net_name] = attached

    assert label_components["USB_CC1"].isdisjoint(label_components["USB_CC2"])


def test_named_net_component_invariant_rejects_t_junction_short():
    from circuit_weaver.primitives import _validate_named_net_components

    wires = [
        sexpr_wire(0, 0, 10.16, 0),
        sexpr_wire(5.08, 0, 5.08, 5.08),
    ]
    labels = [
        sexpr_global_label(0, 0, "NET_A"),
        sexpr_global_label(5.08, 5.08, "NET_B"),
    ]

    with pytest.raises(ValueError, match=r"NET_A, NET_B"):
        _validate_named_net_components(wires, labels)


def test_named_net_component_invariant_allows_unjunctioned_crossing_but_not_junction():
    from circuit_weaver.primitives import _validate_named_net_components

    wires = [
        sexpr_wire(0, 0, 10.16, 0),
        sexpr_wire(5.08, -5.08, 5.08, 5.08),
    ]
    labels = [
        sexpr_global_label(0, 0, "NET_A"),
        sexpr_global_label(5.08, -5.08, "NET_B"),
    ]

    _validate_named_net_components(wires, labels)
    with pytest.raises(ValueError, match=r"NET_A, NET_B"):
        _validate_named_net_components(wires, labels, [sexpr_junction(5.08, 0)])


def test_named_net_component_invariant_rejects_no_connect_on_labeled_wire():
    from circuit_weaver.primitives import _validate_named_net_components

    wires = [sexpr_wire(0, 0, 10.16, 0)]
    labels = [sexpr_global_label(0, 0, "FB_TEST")]

    with pytest.raises(ValueError, match=r"no-connect marker.*FB_TEST"):
        _validate_named_net_components(
            wires,
            labels,
            no_connects=[sexpr_no_connect(5.08, 0)],
        )


def test_named_net_component_invariant_allows_detached_no_connect():
    from circuit_weaver.primitives import _validate_named_net_components

    _validate_named_net_components(
        [sexpr_wire(0, 0, 10.16, 0)],
        [sexpr_global_label(0, 0, "FB_TEST")],
        no_connects=[sexpr_no_connect(15.24, 0)],
    )


def test_unmapped_nc_pin_is_reserved_from_named_passive_stub():
    blocker = ComponentDef(
        mpn="NC-BLOCKER",
        pins=[PinDef("1", "NC", "passive", "R")],
        explicit_no_connects={"1"},
    )
    placed_blocker = PlacedComponent(blocker, "U1", 50.8, 50.8)
    symbol_name, symbol = _render_symbol_name_and_sexpr(blocker)
    pin_positions = get_pin_positions(symbol, symbol_name)
    px, py, pangle, plen, _pname, _ptype = pin_positions["1"]
    nc_x, nc_y = pin_connection_point(
        placed_blocker.x,
        placed_blocker.y,
        px,
        py,
        pangle,
        plen,
    )
    feedback = PlacedPassive(
        "R1",
        "200k",
        "Resistor_SMD:R_0402_1005Metric",
        nc_x + 8.89,
        nc_y,
        "FB_TEST",
        "GND",
        "R",
        angle=0,
        role="strap",
        presentation="symbolic",
        symbol_variant="review",
        pin_span=5.08,
    )
    layout = SheetLayout(
        "main",
        "Main",
        "A4",
        placed_ics=[placed_blocker],
        placed_passives=[feedback],
    )

    content = _render_sheet(layout, "nc-reservation", "", "root", "sheet", 1)
    label_match = re.search(
        r'\(global_label\s+"FB_TEST".*?\(at\s+([-\d.]+)\s+([-\d.]+)\s+\d+\)',
        content,
        re.DOTALL,
    )
    no_connect_match = re.search(
        r"\(no_connect\s+\(at\s+([-\d.]+)\s+([-\d.]+)\)",
        content,
    )
    assert label_match and no_connect_match
    label_point = tuple(float(value) for value in label_match.groups())
    no_connect_point = tuple(float(value) for value in no_connect_match.groups())
    assert label_point != no_connect_point


def test_colliding_sheet_slugs_get_deterministic_unique_output_names():
    sheets = [
        SheetAllocation("mcu_a_b", "A-B", "A4"),
        SheetAllocation("MCU_A_B", "A B", "A4"),
        SheetAllocation("mcu_a_b", "A/B", "A4"),
    ]

    ensure_unique_sheet_names(sheets)

    assert [sheet.name for sheet in sheets] == ["mcu_a_b", "MCU_A_B_2", "mcu_a_b_3"]
    filenames = [f"{sheet.name}.kicad_sch".casefold() for sheet in sheets]
    assert len(filenames) == len(set(filenames))


def test_normalized_symbol_name_collision_keeps_wire_on_each_emitted_geometry():
    left_pin = ComponentDef(
        mpn="ABC-1",
        pins=[PinDef("1", "P", "input", "L")],
        pin_nets={"1": "LEFT_NET"},
    )
    right_pin = ComponentDef(
        mpn="ABC.1",
        pins=[PinDef("1", "P", "input", "R")],
        pin_nets={"1": "RIGHT_NET"},
    )
    layout = SheetLayout(
        "main",
        "Main",
        "A4",
        placed_ics=[
            PlacedComponent(left_pin, "U1", 50.8, 50.8),
            PlacedComponent(right_pin, "U2", 101.6, 50.8),
        ],
    )

    content = _render_sheet(layout, "collision", "", "root", "sheet", 1)
    instances = re.findall(
        r'\(symbol\s+\(lib_id\s+"([^"]+)"\)\s+'
        r"\(at\s+([-\d.]+)\s+([-\d.]+)\s+\d+\)",
        content,
    )
    assert len(instances) == 2
    assert instances[0][0] != instances[1][0]

    wire_starts = {
        _wire_endpoints(match.group(0))[0]
        for match in re.finditer(
            r"\(wire\s+\(pts\s+\(xy\s+[-\d.]+\s+[-\d.]+\)\s+"
            r"\(xy\s+[-\d.]+\s+[-\d.]+\)\).*?\n\s*\)",
            content,
            re.DOTALL,
        )
    }
    expected_starts = set()
    for lib_id, x, y in instances:
        definition_start = content.find(f'(symbol "{lib_id}"')
        assert definition_start >= 0
        definition = _extract_sexpr_block(content, definition_start)
        pin = get_pin_positions(definition, lib_id)["1"]
        expected = pin_connection_point(float(x), float(y), *pin[:4])
        expected_starts.add((round(expected[0], 2), round(expected[1], 2)))

    assert expected_starts <= wire_starts


def test_multi_label_fixed_point_never_detaches_labels_from_claimed_wires():
    anchors = [(10.16 + index * 1.27, 20.32) for index in range(4)]
    wires = [sexpr_wire(x + 7.62, y, x, y) for x, y in anchors]
    labels = [
        sexpr_global_label(x, y, f"VERY_LONG_COLLIDING_NET_{index}", 0)
        for index, (x, y) in enumerate(anchors)
    ]

    moved_labels, extended_wires = _resolve_label_collisions(labels, wires)

    assert any(_label_anchor(label) != anchor for label, anchor in zip(moved_labels, anchors))
    for label, wire in zip(moved_labels, extended_wires):
        assert _label_anchor(label) == _wire_endpoints(wire)[1]


def test_nonexistent_empty_and_overlapping_pin_maps_fail_closed():
    comp = ComponentDef(
        mpn="BROKEN-MAP",
        pins=[PinDef("1", "VALID", "input", "L")],
        pin_nets={"1": "", "99": "PHANTOM"},
        power_pins={"1": "VDD"},
        explicit_no_connects={"1", "100"},
    )

    issues = _validate_pin_mapping_integrity([comp])
    messages = "\n".join(issue.message for issue in issues)
    assert all(issue.level == "error" for issue in issues)
    assert "empty net names" in messages
    assert "both signal and power" in messages
    assert "both mapped and explicitly no-connect" in messages
    assert "99" in messages and "100" in messages

    symbol_name, symbol = _render_symbol_name_and_sexpr(comp)
    pin_positions = get_pin_positions(symbol, symbol_name)
    with pytest.raises(ValueError, match="Pin mapping integrity failed") as exc_info:
        _validate_rendered_pin_mappings(comp, pin_positions, "U1")
    rendered_error = str(exc_info.value)
    assert "empty signal nets" in rendered_error
    assert "both signal and power" in rendered_error
    assert "absent from emitted symbol" in rendered_error


def test_explicit_refs_are_reserved_before_anonymous_allocation():
    anonymous = ComponentDef(mpn="AUTO", ref_prefix="U")
    explicit = ComponentDef(mpn="FIXED", ref_prefix="U", source_ref="U1")

    reset_ref_counters()
    try:
        reserve_explicit_refs([anonymous, explicit])
        assert _component_ref(anonymous) == "U2"
        assert _component_ref(explicit) == "U1"
    finally:
        reset_ref_counters()


def test_power_flags_are_project_wide_and_skip_real_power_outputs():
    source = ComponentDef(
        mpn="SOURCE",
        pins=[
            PinDef("1", "VBUS", "power_out", "R"),
            PinDef("2", "GND", "power_in", "B"),
        ],
        power_pins={"1": "VBUS_5V", "2": "GND"},
    )
    remote_sink = ComponentDef(
        mpn="SINK",
        pins=[
            PinDef("1", "VIN", "power_in", "L"),
            PinDef("2", "GND", "power_in", "B"),
        ],
        power_pins={"1": "VBUS_5V", "2": "GND"},
    )
    layouts = [
        SheetLayout("source", "Source", "A4", placed_ics=[PlacedComponent(source, "J1", 50.8, 50.8)]),
        SheetLayout("sink", "Sink", "A4", placed_ics=[PlacedComponent(remote_sink, "U1", 50.8, 50.8)]),
    ]

    assert _plan_power_flags(layouts) == [{"GND"}, set()]


def test_marker_stub_never_lands_on_foreign_named_anchor():
    length = _avoid_foreign_net_anchors(
        "GND",
        10.16,
        10.16,
        90,
        6.35,
        [("VDD_3P3", 10.16, 16.51)],
    )

    assert length == pytest.approx(2.54)


def test_marker_stub_avoids_foreign_reserved_pin_without_explicit_anchors():
    route_state = {
        "reserved_segments": [
            ("RES_N_U2", (15.24, 10.16, 15.24, 10.16)),
        ]
    }

    length = _avoid_foreign_net_anchors(
        "GND",
        10.16,
        10.16,
        180,
        6.35,
        [],
        route_state,
    )

    assert length == pytest.approx(3.81)


def test_route_polyline_cancels_immediate_hairpin_backtracks():
    assert _normalize_polyline(
        [(0, 0), (1.27, 0), (0, 0), (0, 2.54)]
    ) == [(0.0, 0.0), (0.0, 2.54)]


def test_bme280_metadata_models_i2c_strap_and_real_kicad_footprint():
    bme = BUILTIN_REGISTRY.get("BME280")
    assert bme is not None
    assert bme.footprint == (
        "Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering"
    )
    assert {pin.number: pin.electrical_type for pin in bme.pins}["5"] == "passive"


def test_generation_emits_portable_namespaced_project_symbol_library(tmp_path):
    component = ComponentDef(
        mpn="LOCAL-LIB",
        source_ref="U1",
        pins=[PinDef("1", "NC", "passive", "L")],
        explicit_no_connects={"1"},
    )

    files = generate_from_components(
        [component],
        output_dir=str(tmp_path),
        project_name="LocalLibrary",
        stable_uuids=True,
        validate=False,
        readiness_gate=False,
    )

    schematic = (tmp_path / "LocalLibrary.kicad_sch").read_text(encoding="utf-8")
    symbol_library = (tmp_path / "CircuitWeaver.kicad_sym").read_text(encoding="utf-8")
    symbol_table = (tmp_path / "sym-lib-table").read_text(encoding="utf-8")
    assert 'lib_id "CircuitWeaver:main__LOCAL_LIB"' in schematic
    assert '(symbol "CircuitWeaver:main__LOCAL_LIB"' in schematic
    assert '(symbol "main__LOCAL_LIB_1_1"' in schematic
    assert 'CircuitWeaver:main__LOCAL_LIB_1_1' not in schematic
    assert '(symbol "main__LOCAL_LIB"' in symbol_library
    assert '(symbol "main__LOCAL_LIB_1_1"' in symbol_library
    assert '${KIPRJMOD}/CircuitWeaver.kicad_sym' in symbol_table
    assert str(tmp_path / "CircuitWeaver.kicad_sym") in files
    assert str(tmp_path / "sym-lib-table") in files
    assert str(tmp_path / "LocalLibrary.kicad_pro") in files


def test_multisheet_library_scopes_same_named_symbols_and_root_project(tmp_path):
    components = [
        ComponentDef(
            mpn="SHARED",
            source_ref=f"U{index}",
            category="power" if index <= 5 else "sensor",
            pins=[PinDef("1", "NC", "passive", "L" if index <= 5 else "R")],
            explicit_no_connects={"1"},
        )
        for index in range(1, 10)
    ]

    files = generate_from_components(
        components,
        output_dir=str(tmp_path),
        project_name="ScopedLibrary",
        stable_uuids=True,
        hierarchical=True,
        validate=False,
        readiness_gate=False,
    )

    power_sheet = (tmp_path / "power.kicad_sch").read_text(encoding="utf-8")
    sensor_sheet = (tmp_path / "sensors.kicad_sch").read_text(encoding="utf-8")
    library = (tmp_path / "CircuitWeaver.kicad_sym").read_text(encoding="utf-8")
    root = (tmp_path / "ScopedLibrary.kicad_sch").read_text(encoding="utf-8")
    assert 'CircuitWeaver:power__SHARED' in power_sheet
    assert 'CircuitWeaver:sensors__SHARED' in sensor_sheet
    assert '(symbol "power__SHARED"' in library
    assert '(symbol "sensors__SHARED"' in library
    assert '(property "Sheetfile" "power.kicad_sch"' in root
    assert '(property "Sheetfile" "sensors.kicad_sch"' in root
    assert str(tmp_path / "ScopedLibrary.kicad_pro") in files


def test_root_sheet_pins_land_exactly_on_grid_aligned_rectangle_edges(tmp_path):
    sheet_infos = [
        {
            "alloc": SheetAllocation(f"section_{index}", f"Section {index}", "A4"),
            "uuid": f"00000000-0000-0000-0000-{index:012d}",
            "filename": f"section_{index}.kicad_sch",
            "page_num": index + 2,
            "labels": set(),
            "hier_labels": {"SHARED_BUS"},
            "hier_label_shapes": {"SHARED_BUS": "bidirectional"},
        }
        for index in range(1, 5)
    ]
    root_path = _generate_root_schematic(
        sheet_infos,
        tmp_path,
        "GridExact",
        "",
        "11111111-1111-1111-1111-111111111111",
        hierarchical=True,
    )

    root = root_path.read_text(encoding="utf-8")
    sheet_blocks = [
        _extract_sexpr_block(root, match.start())
        for match in re.finditer(r"\(sheet\s+\(at\s", root)
    ]
    assert len(sheet_blocks) == len(sheet_infos)

    for sheet_info, block in zip(sheet_infos, sheet_blocks):
        assert (
            f'(instances\n      (project "GridExact"\n'
            f'        (path "/11111111-1111-1111-1111-111111111111" '
            f'(page "{sheet_info["page_num"]}"))\n      )\n    )'
        ) in block
        origin = re.search(r"\(sheet\s+\(at\s+([-\d.]+)\s+([-\d.]+)\)", block)
        size = re.search(r"\(size\s+([-\d.]+)\s+([-\d.]+)\)", block)
        assert origin and size
        sx, sy = float(origin.group(1)), float(origin.group(2))
        width, height = float(size.group(1)), float(size.group(2))
        assert width / 1.27 == pytest.approx(round(width / 1.27))
        assert height / 1.27 == pytest.approx(round(height / 1.27))

        pins = re.findall(
            r'\(pin\s+"[^"]+"\s+\w+\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+(0|180)\)',
            block,
        )
        assert pins
        for pin_x_text, pin_y_text, angle_text in pins:
            pin_x = float(pin_x_text)
            pin_y = float(pin_y_text)
            expected_x = sx + width if angle_text == "0" else sx
            assert pin_x == pytest.approx(expected_x)
            assert sy <= pin_y <= sy + height
