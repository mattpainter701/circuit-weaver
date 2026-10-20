from __future__ import annotations

import logging
from pathlib import Path

import pytest

from circuit_weaver import dispatcher as dispatcher_module
from circuit_weaver.component_db import ComponentDef, PinDef
from circuit_weaver.design_ir import DesignIR
from circuit_weaver.dispatcher import ValidationReport, generate_artifacts
from circuit_weaver.generator import generate_from_components
from circuit_weaver.sexpr_builder import validate_sexpr_balance
from circuit_weaver.validator import ValidationCheckResult, ValidationIssue


def test_validate_sexpr_balance_warns_on_early_extra_close(caplog):
    with caplog.at_level(logging.WARNING, logger="circuit_weaver.sexpr_builder"):
        valid = validate_sexpr_balance(")(", "broken.kicad_sch")

    assert not valid
    assert "broken.kicad_sch" in caplog.text
    assert "min_depth=-1" in caplog.text


def test_virtual_container_is_excluded_from_symbol_bom_and_board():
    from circuit_weaver.generator import _render_symbol_name_and_sexpr

    virtual = ComponentDef(
        mpn="PULLUPS_ONLY",
        ref_prefix="RP",
        source_ref="RP1",
        pins=[PinDef("1", "SDA", "bidirectional", "L")],
        is_virtual_container=True,
    )
    ordinary = ComponentDef(
        mpn="TEST",
        ref_prefix="U",
        pins=[PinDef("1", "SIG", "bidirectional", "L")],
    )

    _name, virtual_symbol = _render_symbol_name_and_sexpr(virtual)
    _name, ordinary_symbol = _render_symbol_name_and_sexpr(ordinary)

    assert "(in_bom no) (on_board no)" in virtual_symbol
    assert "(in_bom yes) (on_board yes)" in ordinary_symbol


def test_generate_artifacts_writes_log_on_validation_failure(tmp_path: Path, monkeypatch):
    report = ValidationReport(
        profile="standard",
        valid=False,
        summary={"structural": 1, "electrical": 0, "implementation": 0, "presentation": 0},
        metadata={"project": "Broken"},
    )
    monkeypatch.setattr(dispatcher_module, "validate_design", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        dispatcher_module,
        "compile_design_ir",
        lambda *args, **kwargs: pytest.fail("compile_design_ir should not run after validation failure"),
    )

    output_dir = tmp_path / "out"
    with pytest.raises(ValueError, match="Design failed standard validation"):
        generate_artifacts({"project": "Broken"}, output_dir=output_dir)

    log_path = output_dir / "circuit-weaver.log"
    assert log_path.exists(), "circuit-weaver.log not created on validation failure"
    content = log_path.read_text(encoding="utf-8")
    assert "Design failed standard validation" in content


def _test_comp(
    *,
    ref: str = "U1",
    pin_nets: dict[str, str] | None = None,
    power_pins: dict[str, str] | None = None,
    pins: list[PinDef] | None = None,
    unmapped_required_pins: dict[str, str] | None = None,
) -> ComponentDef:
    return ComponentDef(
        mpn=f"TEST_{ref}",
        ref_prefix="U",
        source_ref=ref,
        value=f"TEST_{ref}",
        footprint="Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm",
        category="digital",
        pins=pins
        or [
            PinDef("1", "SIG", "bidirectional", "L"),
            PinDef("2", "VDD", "power_in", "T"),
            PinDef("3", "GND", "power_in", "B"),
        ],
        pin_nets=pin_nets or {},
        power_pins=power_pins or {"2": "VDD_3P3", "3": "GND"},
        unmapped_required_pins=unmapped_required_pins or {},
    )


@pytest.mark.parametrize(
    "project_name",
    [
        "../escape",
        "..\\escape",
        "nested/escape",
        "nested\\escape",
        "/tmp/absolute-escape",
        r"C:\absolute-escape",
        r"C:drive-relative-escape",
    ],
)
def test_generate_rejects_project_path_forms_before_creating_output(
    tmp_path: Path,
    project_name: str,
):
    output_dir = tmp_path / "out"

    with pytest.raises(ValueError, match="Project name"):
        generate_from_components(
            [_test_comp(pin_nets={"1": "SIG"})],
            output_dir=output_dir,
            project_name=project_name,
            validate=False,
            readiness_gate=False,
        )

    assert not output_dir.exists()


def test_generate_accepts_portable_project_name_and_writes_only_inside_output(tmp_path: Path):
    output_dir = tmp_path / "out"
    project_name = "Board-v1.2_alpha 01"

    files = generate_from_components(
        [_test_comp(pin_nets={"1": "SIG"})],
        output_dir=output_dir,
        project_name=project_name,
        validate=False,
        readiness_gate=False,
    )

    schematic = output_dir / f"{project_name}.kicad_sch"
    assert schematic.exists()
    assert schematic.resolve().is_relative_to(output_dir.resolve())
    assert schematic in {Path(path) for path in files}


def test_generate_artifacts_rejects_project_traversal_before_logging(tmp_path: Path):
    output_dir = tmp_path / "out"

    with pytest.raises(ValueError, match="Project name"):
        generate_artifacts(
            {"project": "../outside", "blocks": []},
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_generate_from_components_blocks_placement_readiness_for_direct_call(tmp_path: Path, monkeypatch):
    import circuit_weaver.generator as generator_module

    comp = _test_comp(pin_nets={"1": "FLOATING"})
    fake_results = [
        ValidationCheckResult(
            code="net-connectivity",
            label="Net connectivity",
            status="fail",
            issues=(
                ValidationIssue(
                    code="single-pin-net",
                    level="warning",
                    ref="U1",
                    mpn="TEST_U1",
                    message="Net 'FLOATING' has only one connection",
                ),
            ),
        )
    ]

    monkeypatch.setattr(generator_module, "run_validation_checks", lambda _components: fake_results)
    monkeypatch.setattr(
        generator_module,
        "allocate_sheets",
        lambda _components: pytest.fail("allocate_sheets should not run when readiness gate blocks"),
    )

    with pytest.raises(ValueError, match="placement_readiness error"):
        generate_from_components([comp], output_dir=tmp_path, validate=False, compiled_ir=DesignIR())


def test_generate_from_components_readiness_gate_override_allows_debug_emit(tmp_path: Path, monkeypatch):
    import circuit_weaver.generator as generator_module

    comp = _test_comp(pin_nets={"1": "FLOATING"})
    fake_results = [
        ValidationCheckResult(
            code="net-connectivity",
            label="Net connectivity",
            status="fail",
            issues=(
                ValidationIssue(
                    code="single-pin-net",
                    level="warning",
                    ref="U1",
                    mpn="TEST_U1",
                    message="Net 'FLOATING' has only one connection",
                ),
            ),
        )
    ]

    monkeypatch.setattr(generator_module, "run_validation_checks", lambda _components: fake_results)
    monkeypatch.setattr(
        generator_module,
        "allocate_sheets",
        lambda _components: (_ for _ in ()).throw(RuntimeError("reached allocation")),
    )

    with pytest.raises(RuntimeError, match="reached allocation"):
        generate_from_components([comp], output_dir=tmp_path, validate=False, readiness_gate=False)


def test_generate_from_components_hard_fails_unmapped_required_pin(tmp_path: Path):
    comp = _test_comp(
        pins=[
            PinDef("1", "PROG", "input", "L"),
            PinDef("2", "VDD", "power_in", "T"),
            PinDef("3", "GND", "power_in", "B"),
        ],
        unmapped_required_pins={"1": "PROG"},
    )

    with pytest.raises(ValueError, match="UNMAPPED signal pin 'PROG'"):
        generate_from_components([comp], output_dir=tmp_path, validate=False, readiness_gate=False)


def test_generate_from_components_hard_fails_floating_power_pin(tmp_path: Path):
    comp = _test_comp(
        pins=[PinDef("1", "VDD", "power_in", "L")],
        power_pins={},
    )

    with pytest.raises(ValueError, match="FLOATING power_in pin 'VDD'"):
        generate_from_components([comp], output_dir=tmp_path, validate=False, readiness_gate=False)


# ================================================================
# T238 (F15) — A0 overflow splits the sheet or fails clearly
# ================================================================


def test_split_sheet_allocation_balances_by_area():
    from circuit_weaver.allocator import SheetAllocation
    from circuit_weaver.placer import split_sheet_allocation

    comps = [_test_comp(ref=f"U{i + 1}") for i in range(4)]
    alloc = SheetAllocation(name="main", title="Main", paper="A3", components=comps)
    halves = split_sheet_allocation(alloc)
    assert halves is not None and len(halves) == 2
    assert halves[0].name == "main_1" and halves[1].name == "main_2"
    assert [c.source_ref for c in halves[0].components + halves[1].components] == [
        "U1",
        "U2",
        "U3",
        "U4",
    ]
    assert halves[0].components and halves[1].components


def test_split_sheet_allocation_single_component_returns_none():
    from circuit_weaver.allocator import SheetAllocation
    from circuit_weaver.placer import split_sheet_allocation

    alloc = SheetAllocation(name="main", title="Main", paper="A3", components=[_test_comp()])
    assert split_sheet_allocation(alloc) is None


def test_generate_splits_overflowing_sheet(tmp_path: Path, monkeypatch):
    """A sheet that overflows A0 is split into two half-sheets that render."""
    import circuit_weaver.generator as generator_module

    real_layout_sheet = generator_module.layout_sheet

    def overflow_multi_component(sheet_alloc, **kwargs):
        layout = real_layout_sheet(sheet_alloc, **kwargs)
        if len(sheet_alloc.components) > 1:
            layout.overflow = True
        return layout

    monkeypatch.setattr(generator_module, "layout_sheet", overflow_multi_component)

    comps = [_test_comp(ref="U1"), _test_comp(ref="U2")]
    comps[1].mpn = "TEST_U2"
    files = generate_from_components(
        comps, output_dir=tmp_path, validate=False, readiness_gate=False
    )
    names = {Path(f).name for f in files}
    sch_names = {n for n in names if n.endswith(".kicad_sch")}
    assert any("_1" in n for n in sch_names), f"expected split sheet files, got {sch_names}"
    assert any("_2" in n for n in sch_names), f"expected split sheet files, got {sch_names}"


def test_generate_fails_clearly_when_sheet_cannot_split(tmp_path: Path, monkeypatch):
    """A single-component sheet that still overflows A0 is a hard failure."""
    import circuit_weaver.generator as generator_module

    real_layout_sheet = generator_module.layout_sheet

    def always_overflow(sheet_alloc, **kwargs):
        layout = real_layout_sheet(sheet_alloc, **kwargs)
        layout.overflow = True
        return layout

    monkeypatch.setattr(generator_module, "layout_sheet", always_overflow)

    with pytest.raises(ValueError, match="Design too large"):
        generate_from_components(
            [_test_comp()], output_dir=tmp_path, validate=False, readiness_gate=False
        )
