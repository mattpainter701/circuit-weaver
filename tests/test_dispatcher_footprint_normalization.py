from pathlib import Path

import pytest

from circuit_weaver.component_db import ComponentDef, PinDef
from circuit_weaver.design_ir import DesignBlock, DesignIR
from circuit_weaver.design_loader import CompiledDesign
from circuit_weaver.dispatcher import _validate_component_resolution


@pytest.fixture
def footprint_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "footprints"
    pretty = root / "Package_TO_SOT_SMD.pretty"
    pretty.mkdir(parents=True)
    for name in ("SOT-23-5", "SOT-23-6"):
        (pretty / f"{name}.kicad_mod").write_text('(footprint "x")', encoding="utf-8")
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(root))
    monkeypatch.delenv("KICAD8_FOOTPRINT_DIR", raising=False)
    return root


def _implementation_messages(footprint: str):
    compiled = CompiledDesign(
        ir=DesignIR(blocks=[DesignBlock(id="u1", section="power", kind="component", ref="U1")]),
        components=[
            ComponentDef(
                mpn="TEST-REGULATOR",
                source_ref="U1",
                footprint=footprint,
                pins=[
                    PinDef("1", "IN", "power_in", "L"),
                    PinDef("2", "GND", "power_in", "L"),
                    PinDef("3", "OUT", "power_out", "R"),
                ],
            )
        ],
        metadata={},
        engine_spec={},
    )
    _, implementation = _validate_component_resolution(compiled)
    return implementation


@pytest.mark.parametrize("bare", ["SOT-23-5", "SOT-23-6"])
def test_validation_qualifies_known_bare_kicad_footprints(footprint_root: Path, bare: str) -> None:
    assert not any(
        issue.code == "footprint-library-missing"
        for issue in _implementation_messages(bare)
    )


@pytest.mark.parametrize(
    "footprint",
    [
        "Package_TO_SOT_SMD:SOT-23-5",
        "Package_TO_SOT_SMD:SOT-23-6",
    ],
)
def test_validation_preserves_exact_qualified_library_refs(footprint_root: Path, footprint: str) -> None:
    assert not any(
        issue.code == "footprint-library-missing"
        for issue in _implementation_messages(footprint)
    )


@pytest.mark.parametrize(
    "footprint",
    [
        "SOT-23-6_Custom",  # known family prefix must not fuzzy-match the installed base footprint
        "Acme_Custom_QFN-32",  # unknown unprefixed package
        "CustomLib:SOT-23-6",  # exact library remains authoritative; no cross-library fallback
        "Package_TO_SOT_SMD:SOT-23-6_Custom",  # qualified custom name must resolve exactly
    ],
)
def test_validation_keeps_unknown_custom_and_unprefixed_packages_missing(
    footprint_root: Path, footprint: str
) -> None:
    assert any(
        issue.code == "footprint-library-missing"
        for issue in _implementation_messages(footprint)
    )
