"""Safety and fidelity tests for stock SES-to-DSN continuations."""

from __future__ import annotations

from decimal import localcontext

import pytest

from circuit_weaver._specctra_canonical import _direct_blocks, _root
from circuit_weaver._specctra_continuation import continuation_dsn


def _dsn(*, unit: str = "um", name: str = '"design with spaces"') -> str:
    return f'''(pcb {name}
  (parser (host_cad "test"))
  (resolution um 10)
  (unit {unit})
  (structure
    (layer F.Cu (type signal) (property (index 0)))
    (layer B.Cu (type signal) (property (index 1)))
    (boundary (rect pcb 0 0 10000 10000))
    (via "Via 1")
    (rule (width 200) (clearance 150))
  )
  (library
    (image "Part A" (outline (path signal 0 0 0 1 1)))
    (padstack "Via 1" (shape (circle F.Cu 600)) (shape (circle B.Cu 600)))
  )
  (placement)
  (network
    (net "NET A"
      (pins "U 1" R1-1)
    )
    (net NET_B (pins U2-1 R2-1))
  )
  (wiring
  )
)
'''


def _ses(*, resolution: str = "um 10", net: str = '"NET A"') -> str:
    return f'''(session "design with spaces"
  (base_design "design with spaces")
  (placement)
  (was_is)
  (routes
    (resolution {resolution})
    (parser (host_cad "test"))
    (library_out
      (padstack "Via 1" (shape (circle F.Cu 6000)) (shape (circle B.Cu 6000)))
    )
    (network_out
      (net {net}
        (wire
          (path F.Cu 2000
            12500 -25500
            13500 -24500
          )
        )
        (via "Via 1" 13500 -24500)
      )
    )
  )
)
'''


def test_converts_session_resolution_and_marks_generated_copper_normal() -> None:
    dsn = _dsn()
    updated, report = continuation_dsn(dsn, _ses())

    assert updated.startswith(dsn[: dsn.index("  (wiring")])
    assert '(wire (path F.Cu 200 1250 -2550 1350 -2450) (net "NET A") (type normal))' in updated
    assert '(via "Via 1" 1350 -2450 (net "NET A") (type normal))' in updated
    assert "(pins \"U 1\" R1-1)" in updated
    assert report["wire_count"] == 1
    assert report["via_count"] == 1
    assert report["net_count"] == 1
    assert report["ses_resolution_unit"] == "um"
    assert report["ses_resolution_scale"] == "10"
    assert report["dsn_unit"] == "um"
    assert report["record_type"] == "normal"


def test_conversion_uses_decimal_resolution_and_preserves_point_direction() -> None:
    dsn = _dsn()
    session = _ses(resolution="mm 1000000")
    # 12500 at 1,000,000 session units/mm is exactly 12.5 um.
    updated, _report = continuation_dsn(dsn, session)

    # The 2000/1e6 mm width is exactly 2 um; coordinates remain in order.
    assert "(path F.Cu 2 12.5 -25.5 13.5 -24.5) (net \"NET A\") (type normal))" in updated


def test_preserves_fractional_session_grid_and_negative_coordinates_exactly() -> None:
    session = _ses().replace("12500 -25500", "1417753 -255003").replace(
        "13500 -24500", "1417763 -245007"
    )
    updated, _report = continuation_dsn(_dsn(), session)

    assert '(wire (path F.Cu 200 141775.3 -25500.3 141776.3 -24500.7) (net "NET A") (type normal))' in updated
    assert '(via "Via 1" 141776.3 -24500.7 (net "NET A") (type normal))' in updated


def test_exact_conversion_ignores_ambient_decimal_precision() -> None:
    session = _ses().replace("12500 -25500", "1417753 -255003").replace(
        "13500 -24500", "1417763 -245007"
    )
    with localcontext() as context:
        context.prec = 2
        updated, _report = continuation_dsn(_dsn(), session)

    assert "141775.3 -25500.3 141776.3 -24500.7" in updated


def test_preserves_every_source_byte_outside_the_single_wiring_block() -> None:
    dsn = _dsn()
    marker = "(parser (host_cad \"test\"))"
    updated, _report = continuation_dsn(dsn, _ses())

    assert marker in updated
    original_root, original_start, original_end = _root(dsn)
    updated_root, updated_start, updated_end = _root(updated)
    original_span = next((a, b) for a, b, kind, _ in _direct_blocks(original_root) if kind == "wiring")
    updated_span = next((a, b) for a, b, kind, _ in _direct_blocks(updated_root) if kind == "wiring")
    assert dsn[: original_start + original_span[0]] == updated[: updated_start + updated_span[0]]
    assert dsn[original_start + original_span[1] : original_end] == updated[
        updated_start + updated_span[1] : updated_end
    ]
    assert dsn[original_end:] == updated[updated_end:]
    assert updated.count("(wiring") == 1


def test_rejects_locked_or_nonempty_source_wiring() -> None:
    dsn = _dsn().replace("(wiring\n  )", "(wiring (wire (type route)))")
    with pytest.raises(ValueError, match="not empty|pre-existing"):
        continuation_dsn(dsn, _ses())


def test_rejects_nonempty_was_is_and_changed_placement() -> None:
    with pytest.raises(ValueError, match="was_is"):
        continuation_dsn(_dsn(), _ses().replace("(was_is)", "(was_is (net old new))"))

    dsn = _dsn().replace("  (placement)", "  (placement (component Foot (place U1 20 30 front -90)))")
    session = _ses().replace(
        "  (placement)", "  (placement (resolution um 10) (component Foot (place U1 200 300 front -90)))"
    )
    continuation_dsn(dsn, session)
    changed = session.replace("(place U1 200 300", "(place U1 201 300")
    with pytest.raises(ValueError, match="placement differs"):
        continuation_dsn(dsn, changed)
    changed_image = session.replace("(component Foot", "(component OtherFoot")
    with pytest.raises(ValueError, match="placement differs"):
        continuation_dsn(dsn, changed_image)


def test_rejects_nonterminating_or_excess_precision_conversion() -> None:
    with pytest.raises(ValueError, match="exact DSN decimal"):
        continuation_dsn(_dsn(), _ses(resolution="um 3"))


@pytest.mark.parametrize(
    "session, message",
    [
        ("not a session", "malformed|session"),
        (_ses().replace("(resolution um 10)", "(resolution furlong 10)"), "resolution unit"),
        (_ses().replace("(path F.Cu 2000", "(path In1.Cu 2000"), "undeclared DSN layer"),
        (_ses().replace('"Via 1" 13500', '"unknown via" 13500'), "undeclared padstack"),
        (_ses().replace("(wire\n          (path", "(wire\n          (arc"), "unsupported|path"),
        (_ses().replace("(net \"NET A\"", "(net UNKNOWN"), "undeclared DSN net"),
    ],
)
def test_rejects_malformed_or_unsupported_ses(session: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        continuation_dsn(_dsn(), session)


def test_rejects_dsns_outside_the_supported_two_layer_subset() -> None:
    dsn = _dsn().replace(
        '(layer B.Cu (type signal) (property (index 1)))',
        '(layer B.Cu (type signal) (property (index 1)))\n    (layer In1.Cu (type signal) (property (index 2)))',
    )
    with pytest.raises(ValueError, match="two distinct copper|only copper"):
        continuation_dsn(dsn, _ses())
