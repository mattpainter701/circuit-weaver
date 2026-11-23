from __future__ import annotations

import math

import pytest

from circuit_weaver.helpers import stripline_z0, symmetric_stripline_z0_ideal


def test_ideal_symmetric_stripline_matches_reference_benchmarks() -> None:
    # Independent constants from the Cohn zero-thickness symmetric solution.
    expected = [
        (0.08, 56.423623051259774),
        (0.10, 50.21622539252667),
        (0.20, 32.6768125728855),
        (0.30, 24.258045443655426),
    ]
    for width_mm, expected_ohm in expected:
        z0, er_eff = symmetric_stripline_z0_ideal(width_mm, 0.2, 4.0)
        assert z0 == pytest.approx(expected_ohm, rel=2e-10)
        assert er_eff == 4.0


def test_ideal_symmetric_stripline_has_independent_self_dual_reference_point() -> None:
    # At q=acosh(sqrt(2)), sech(q)=tanh(q)=1/sqrt(2), so K(k)/K(k')=1.
    # This expected value follows directly from eta0/(4*sqrt(er)), without AGM.
    plane_separation_mm = 1.0
    q = math.acosh(math.sqrt(2.0))
    width_mm = (2.0 / math.pi) * q * plane_separation_mm
    z0, er_eff = symmetric_stripline_z0_ideal(width_mm, plane_separation_mm, 4.0)
    assert z0 == pytest.approx(376.730313668 / 8.0, rel=1e-14)
    assert er_eff == 4.0


def test_ideal_stripline_has_width_scale_and_permittivity_invariance() -> None:
    base = symmetric_stripline_z0_ideal(0.1, 0.2, 4.0)[0]
    scaled_geometry = symmetric_stripline_z0_ideal(0.5, 1.0, 4.0)[0]
    higher_permittivity = symmetric_stripline_z0_ideal(0.1, 0.2, 16.0)[0]
    assert scaled_geometry == pytest.approx(base, rel=1e-14)
    assert higher_permittivity == pytest.approx(base / 2.0, rel=1e-14)


def test_new_formula_is_continuous_at_old_piecewise_boundary() -> None:
    # Old offset formula branched at w/(2*h_eff)=0.5; with h1=h2=.1, that is w=.1.
    values = [symmetric_stripline_z0_ideal(width, 0.2, 4.0)[0] for width in (0.1 - 1e-9, 0.1, 0.1 + 1e-9)]
    assert max(values) - min(values) < 1e-6
    assert values[0] > values[1] > values[2]


def test_normalized_ratio_avoids_overflow_for_large_equal_dimensions() -> None:
    ordinary = symmetric_stripline_z0_ideal(1.0, 1.0, 4.0)
    huge = symmetric_stripline_z0_ideal(1e308, 1e308, 4.0)
    assert huge == pytest.approx(ordinary, rel=1e-14)
    with pytest.raises(ValueError, match="numeric range"):
        symmetric_stripline_z0_ideal(1e308, 1e-308, 4.0)


def test_ideal_symmetric_stripline_impedance_decreases_with_width() -> None:
    widths_mm = [0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20, 0.30, 0.50]
    values = [symmetric_stripline_z0_ideal(w, 0.2, 4.0)[0] for w in widths_mm]
    assert all(left > right for left, right in zip(values, values[1:]))


def test_legacy_offset_signature_accepts_only_explicit_ideal_symmetric_case() -> None:
    expected = symmetric_stripline_z0_ideal(0.1, 0.2, 4.0)
    assert stripline_z0(0.1, 0.1, 0.1, 4.0, t_mm=0.0) == pytest.approx(expected)
    with pytest.raises(ValueError, match="finite copper thickness is unsupported"):
        stripline_z0(0.1, 0.1, 0.1, 4.0)
    with pytest.raises(ValueError, match="offset stripline is unsupported"):
        stripline_z0(0.1, 0.09, 0.11, 4.0, t_mm=0.0)


@pytest.mark.parametrize(
    ("width", "separation", "er"),
    [
        (0.0, 0.2, 4.0),
        (-0.1, 0.2, 4.0),
        (math.nan, 0.2, 4.0),
        (0.1, 0.0, 4.0),
        (0.1, math.inf, 4.0),
        (0.1, 0.2, 0.99),
        (0.1, 0.2, math.nan),
    ],
)
def test_ideal_model_rejects_invalid_inputs(width: float, separation: float, er: float) -> None:
    with pytest.raises(ValueError):
        symmetric_stripline_z0_ideal(width, separation, er)


@pytest.mark.parametrize("thickness", [-0.001, math.nan, math.inf, 0.018])
def test_legacy_wrapper_rejects_unsupported_thickness(thickness: float) -> None:
    with pytest.raises(ValueError):
        stripline_z0(0.1, 0.1, 0.1, 4.0, t_mm=thickness)
