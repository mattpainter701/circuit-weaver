from __future__ import annotations

import pytest

from circuit_weaver.pcb_assembly_inventory import assembly_component_coverage


def _board(*footprints: str, extra: str = "") -> str:
    return f'(kicad_pcb (version 20250114) {extra} {" ".join(footprints)})'


def _footprint(reference: str | None, *children: str) -> str:
    ref = f'(property "Reference" "{reference}")' if reference is not None else ""
    return f'(footprint "Demo" {ref} (pad "1" smd rect) {" ".join(children)})'


def test_required_component_without_model_is_missing():
    coverage = assembly_component_coverage(_board(_footprint("R1")))

    assert coverage["expected"] == 1
    assert coverage["represented"] == 0
    assert coverage["missing"] == 1
    assert coverage["missing_references"] == ["R1"]
    assert coverage["components"][0]["reason"] == "no_model_declared"


@pytest.mark.parametrize("hide", ["hide", "(hide)", "(hide yes)"])
def test_hidden_only_model_does_not_represent_component(hide: str):
    coverage = assembly_component_coverage(_board(_footprint("C1", f'(model "body.step" {hide})')))

    assert coverage["missing_references"] == ["C1"]
    assert coverage["components"][0]["hidden_model_count"] == 1
    assert coverage["components"][0]["reason"] == "only_hidden_models"


def test_hidden_model_and_multiple_visible_models_are_counted_separately():
    coverage = assembly_component_coverage(_board(_footprint(
        "U1", '(model "body.step")', '(model "pins.step")', '(model "hidden.step" (hide yes))'
    )))

    assert coverage["expected"] == coverage["represented"] == 1
    assert coverage["missing"] == 0
    assert coverage["model_declaration_total"] == 3
    assert coverage["components"][0]["visible_model_count"] == 2
    assert coverage["components"][0]["hidden_model_count"] == 1


def test_quoted_lookalikes_escaped_strings_and_comments_are_not_structure():
    footprint = (
        '(footprint "Demo" (property "Reference" "J\\\"1") '
        '(property "Description" "(model \\\"fake.step\\\") (footprint fake)") '
        '(pad "1" smd rect) (model "real.step"))'
    )
    coverage = assembly_component_coverage(_board(
        footprint,
        extra='; (footprint "fake" (property "Reference" "FAKE") (model "bad.step"))\n'
              '(property "Note" "(footprint hidden) (model \\\"also-fake\\\")")',
    ))

    assert coverage["expected"] == coverage["represented"] == 1
    assert coverage["components"][0]["reference"] == 'J"1'
    assert coverage["model_declaration_total"] == 1


def test_legacy_reference_text_is_supported():
    footprint = '(footprint "Demo" (fp_text reference "R7" (at 0 0)) (pad "1" thru_hole circle))'

    coverage = assembly_component_coverage(_board(footprint))

    assert coverage["components"][0]["reference"] == "R7"
    assert coverage["missing_references"] == ["R7"]


@pytest.mark.parametrize(
    "footprint",
    [
        '(footprint "Mechanical" (property "Reference" "H1") (pad "1" np_thru_hole circle))',
        '(footprint "NoPads" (property "Reference" "TP1") (model "body.step"))',
        '(footprint "DNP" (property "Reference" "R2") (attr dnp) (pad "1" smd rect))',
        '(footprint "BoardOnly" (property "Reference" "B1") (attr board_only) (pad "1" thru_hole circle))',
        '(footprint "DNPProperty" (property "Reference" "R3") (property "dnp" "yes") (pad "1" smd rect))',
    ],
)
def test_mechanical_unpopulated_and_board_only_footprints_are_excluded(footprint: str):
    coverage = assembly_component_coverage(_board(footprint))

    assert coverage["expected"] == 0
    assert coverage["represented"] == 0
    assert coverage["missing"] == 0
    assert coverage["components"][0]["required"] is False


def test_missing_reference_on_required_component_fails():
    with pytest.raises(ValueError, match="missing an annotated reference"):
        assembly_component_coverage(_board(_footprint(None)))


def test_duplicate_required_references_fail():
    with pytest.raises(ValueError, match="duplicate footprint reference: R1"):
        assembly_component_coverage(_board(_footprint("R1"), _footprint("R1")))


@pytest.mark.parametrize(
    "source",
    [
        '(kicad_pcb (version 1) (footprint "Demo"',
        '(kicad_pcb (version 1)))',
        '(not_a_pcb)',
        '(kicad_pcb) (kicad_pcb)',
        '(kicad_pcb (property "x" "unterminated))',
    ],
)
def test_malformed_or_non_pcb_expressions_fail(source: str):
    with pytest.raises(ValueError):
        assembly_component_coverage(source)
