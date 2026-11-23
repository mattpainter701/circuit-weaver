"""Generic controlled-impedance math helpers for PCB workflows."""

from __future__ import annotations

import math

GENERIC_6L_FR4_STACKUP = {
    "name": "generic_6layer_fr4",
    "total_mm": 1.6,
    "layers": [
        ("L1_F.Cu", "copper", 0.035, None, None, 1.0),
        ("PP_top", "prepreg", 0.10, 4.10, 0.02, None),
        ("L2_In1.Cu", "copper", 0.018, None, None, 0.5),
        ("Core_1", "core", 0.55, 4.30, 0.02, None),
        ("L3_In2.Cu", "copper", 0.018, None, None, 0.5),
        ("PP_mid", "prepreg", 0.12, 4.20, 0.02, None),
        ("L4_In3.Cu", "copper", 0.018, None, None, 0.5),
        ("Core_2", "core", 0.55, 4.30, 0.02, None),
        ("L5_In4.Cu", "copper", 0.018, None, None, 0.5),
        ("PP_bot", "prepreg", 0.10, 4.10, 0.02, None),
        ("L6_B.Cu", "copper", 0.035, None, None, 1.0),
    ],
}


def microstrip_z0(w_mm: float, h_mm: float, er: float, t_mm: float = 0.035) -> tuple[float, float]:
    """Microstrip impedance using Hammerstad-Jensen.

    The analytical approximation is unchanged. Invalid or numerically
    unsupported geometry is rejected instead of returning NaN/complex output.
    """
    w = _finite_positive("w_mm", w_mm)
    h = _finite_positive("h_mm", h_mm)
    dielectric = _finite_number("er", er)
    if not math.isfinite(dielectric) or dielectric < 1.0:
        raise ValueError("er must be finite and at least 1")
    thickness = _finite_number("t_mm", t_mm)
    if not math.isfinite(thickness) or thickness < 0.0:
        raise ValueError("t_mm must be finite and non-negative")

    try:
        if thickness > 0.0:
            dw = (thickness / math.pi) * (1.0 + math.log(2.0 * h / thickness))
            effective_width = w + dw
        else:
            effective_width = w
        if not math.isfinite(effective_width) or effective_width <= 0.0:
            raise ValueError("effective width is outside the numeric range")
        u = effective_width / h
        if not math.isfinite(u) or u <= 0.0:
            raise ValueError("normalized width is outside the numeric range")
        a = (
            1.0
            + (1.0 / 49.0) * math.log((u**4 + (u / 52.0) ** 2) / (u**4 + 0.432))
            + (1.0 / 18.7) * math.log(1.0 + (u / 18.1) ** 3)
        )
        b = 0.564 * ((dielectric - 0.9) / (dielectric + 3.0)) ** 0.053
        er_eff = (dielectric + 1.0) / 2.0 + (dielectric - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-a * b)
        f_u = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
        z0 = (377.0 / (2.0 * math.pi * math.sqrt(er_eff))) * math.log(
            f_u / u + math.sqrt(1.0 + (2.0 / u) ** 2)
        )
    except (OverflowError, ZeroDivisionError, ValueError) as exc:
        raise ValueError("microstrip inputs are outside the model's numeric range") from exc
    if not math.isfinite(er_eff) or er_eff < 1.0 or not math.isfinite(z0) or z0 <= 0.0:
        raise ValueError("microstrip result is outside the valid numeric range")
    return z0, er_eff


def _arithmetic_geometric_mean(a: float, b: float) -> float:
    """Evaluate AGM(a, b) with the standard convergent iteration."""
    for _ in range(256):
        arithmetic = (a + b) / 2.0
        geometric = math.sqrt(a * b)
        if arithmetic == geometric or abs(arithmetic - geometric) <= 1e-15 * arithmetic:
            return arithmetic
        a, b = arithmetic, geometric
    raise ArithmeticError("arithmetic-geometric mean did not converge")


def _finite_number(name: str, value: float) -> float:
    """Convert a numeric input while rejecting bool's int-like coercion."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not bool")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _finite_positive(name: str, value: float) -> float:
    number = _finite_number(name, value)
    if number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def symmetric_stripline_z0_ideal(
    w_mm: float,
    plane_separation_mm: float,
    er: float,
) -> tuple[float, float]:
    """Exact ideal-conductor, zero-thickness symmetric stripline impedance.

    Uses Cohn's closed-form elliptic-integral result for one centered strip
    between parallel ground planes in a homogeneous dielectric. This ideal
    model does not include finite copper thickness, roughness, loss,
    dispersion, soldermask, vias, launches, or inhomogeneous stackups.

    Reference: S. B. Cohn (1954), DOI 10.1109/TMTT.1954.1124875;
    Polar Instruments Si6000b User Guide, equations (4), (5a), (5b).
    """
    width = _finite_positive("w_mm", w_mm)
    separation = _finite_positive("plane_separation_mm", plane_separation_mm)
    dielectric = _finite_number("er", er)
    if not math.isfinite(dielectric) or dielectric < 1.0:
        raise ValueError("er must be finite and at least 1")

    normalized_width = width / separation
    q = (math.pi / 2.0) * normalized_width
    if not math.isfinite(q) or q <= 0.0:
        raise ValueError("w_mm/plane_separation_mm is outside the numeric range")
    exp_minus_q = math.exp(-q)
    k = (2.0 * exp_minus_q) / (1.0 + exp_minus_q * exp_minus_q)
    k_prime = math.tanh(q)
    if k <= 0.0 or k_prime <= 0.0:
        raise ValueError("w_mm/plane_separation_mm is outside the numeric range")

    # K(k)/K(k') = AGM(1, k) / AGM(1, k') for complementary moduli.
    elliptic_ratio = _arithmetic_geometric_mean(1.0, k) / _arithmetic_geometric_mean(1.0, k_prime)
    eta0 = 376.730313668
    z0 = (eta0 / (4.0 * math.sqrt(dielectric))) * elliptic_ratio
    if not math.isfinite(z0) or z0 <= 0.0:
        raise ValueError("stripline impedance is outside the numeric range")
    return z0, dielectric


def stripline_z0(
    w_mm: float,
    h1_mm: float,
    h2_mm: float,
    er: float,
    t_mm: float = 0.018,
) -> tuple[float, float]:
    """Calculate only the supported symmetric, zero-thickness stripline case.

    ``h1_mm`` and ``h2_mm`` are conductor-to-plane distances. Asymmetric
    offset geometry and finite copper thickness are not modeled; they fail
    explicitly. Set ``t_mm=0`` to request the ideal Cohn model.
    """
    h1 = _finite_positive("h1_mm", h1_mm)
    h2 = _finite_positive("h2_mm", h2_mm)
    thickness = _finite_number("t_mm", t_mm)
    if not math.isfinite(thickness) or thickness < 0.0:
        raise ValueError("t_mm must be finite and non-negative")
    if h1 != h2:
        raise ValueError("offset stripline is unsupported; use symmetric h1_mm == h2_mm")
    if thickness != 0.0:
        raise ValueError("finite copper thickness is unsupported; set t_mm=0 for the ideal thin-strip model")
    return symmetric_stripline_z0_ideal(w_mm, h1 + h2, er)


def differential_microstrip_z0(
    w_mm: float,
    s_mm: float,
    h_mm: float,
    er: float,
    t_mm: float = 0.035,
) -> tuple[float, float, float, float]:
    """Edge-coupled differential microstrip approximation.

    Returns only finite positive results. Strong-coupling regimes where this
    simplified approximation becomes nonphysical are rejected; no alternate
    field solution is substituted.
    """
    spacing = _finite_positive("s_mm", s_mm)
    height = _finite_positive("h_mm", h_mm)
    z0_single, er_eff = microstrip_z0(w_mm, height, er, t_mm)
    gap_ratio = spacing / height
    if not math.isfinite(gap_ratio) or gap_ratio <= 0.0:
        raise ValueError("s_mm/h_mm is outside the numeric range")
    k_coup = math.exp(-2.0 * gap_ratio) if gap_ratio < 5.0 else 0.0
    z_odd = z0_single * (1.0 - 0.48 * k_coup * math.sqrt(er_eff))
    z_even = z0_single * (1.0 + 0.48 * k_coup * math.sqrt(er_eff))
    z_diff = 2.0 * z_odd
    results = (z_diff, z_odd, z_even, er_eff)
    if any(not math.isfinite(value) or value <= 0.0 for value in results):
        raise ValueError("differential microstrip approximation is nonphysical for these inputs")
    return results


def find_width_for_z0(
    target_z0: float,
    h_mm: float,
    er: float,
    t_mm: float = 0.035,
    w_min: float = 0.05,
    w_max: float = 2.0,
    tol: float = 0.01,
) -> float:
    """Find a width within the requested bracket and verify its residual."""
    target = _finite_positive("target_z0", target_z0)
    height = _finite_positive("h_mm", h_mm)
    dielectric = _finite_number("er", er)
    if not math.isfinite(dielectric) or dielectric < 1.0:
        raise ValueError("er must be finite and at least 1")
    thickness = _finite_number("t_mm", t_mm)
    if not math.isfinite(thickness) or thickness < 0.0:
        raise ValueError("t_mm must be finite and non-negative")
    lo = _finite_positive("w_min", w_min)
    hi = _finite_positive("w_max", w_max)
    if lo >= hi:
        raise ValueError("w_min must be less than w_max")
    tolerance = _finite_positive("tol", tol)

    z_lo, _ = microstrip_z0(lo, height, dielectric, thickness)
    z_hi, _ = microstrip_z0(hi, height, dielectric, thickness)
    if not (z_lo >= target >= z_hi):
        raise ValueError(
            f"target impedance {target:g} ohm is not bracketed by widths "
            f"[{lo:g}, {hi:g}] mm (endpoint impedances {z_lo:g}, {z_hi:g} ohm)"
        )
    if abs(z_lo - target) <= tolerance:
        return lo
    if abs(z_hi - target) <= tolerance:
        return hi

    for _ in range(100):
        mid = lo + (hi - lo) / 2.0
        if mid == lo or mid == hi:
            break
        z_mid, _ = microstrip_z0(mid, height, dielectric, thickness)
        residual = z_mid - target
        if abs(residual) <= tolerance:
            return mid
        if residual > 0.0:
            lo = mid
        else:
            hi = mid

    width = lo + (hi - lo) / 2.0
    z0, _ = microstrip_z0(width, height, dielectric, thickness)
    if abs(z0 - target) > tolerance:
        raise ArithmeticError(
            f"microstrip width search did not converge: residual {abs(z0 - target):g} ohm "
            f"exceeds tolerance {tolerance:g} ohm"
        )
    return width
