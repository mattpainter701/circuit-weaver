from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from circuit_weaver.helpers import impedance
from circuit_weaver.helpers.impedance import differential_microstrip_z0, find_width_for_z0, microstrip_z0


class ImpedanceInputGuardTests(unittest.TestCase):
    def test_nominal_50_ohm_width_and_model_value_are_unchanged(self):
        width = find_width_for_z0(50.0, h_mm=0.10, er=4.1, t_mm=0.035)
        impedance, _ = microstrip_z0(width, h_mm=0.10, er=4.1, t_mm=0.035)
        self.assertAlmostEqual(width, 0.17139892578125, places=14)
        self.assertAlmostEqual(impedance, 49.992085463414035, places=12)

    def test_unreachable_targets_outside_width_bracket_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "not bracketed"):
            find_width_for_z0(1000.0, h_mm=0.10, er=4.1)
        with self.assertRaisesRegex(ValueError, "not bracketed"):
            find_width_for_z0(1.0, h_mm=0.10, er=4.1)

    def test_search_rejects_reversed_bounds_and_invalid_target_tolerance(self):
        with self.assertRaises(ValueError):
            find_width_for_z0(50.0, h_mm=0.1, er=4.1, w_min=2.0, w_max=0.05)
        for target in (0.0, -1.0, math.nan, math.inf):
            with self.subTest(target=target), self.assertRaises(ValueError):
                find_width_for_z0(target, h_mm=0.1, er=4.1)
        for tolerance in (0.0, -0.1, math.nan, math.inf):
            with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
                find_width_for_z0(50.0, h_mm=0.1, er=4.1, tol=tolerance)
        for bounds in ((0.0, 2.0), (-0.1, 2.0), (0.05, 0.0), (0.05, math.nan),
                       (math.inf, 2.0), (2.0, 2.0)):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                find_width_for_z0(50.0, h_mm=0.1, er=4.1, w_min=bounds[0], w_max=bounds[1])
        for args in ((50.0, 0.0, 4.1, 0.035), (50.0, 0.1, 0.9, 0.035),
                     (50.0, 0.1, 4.1, -0.01)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                find_width_for_z0(*args)

    def test_bracket_endpoint_targets_round_trip(self):
        low_z = microstrip_z0(0.05, 0.1, 4.1, 0.035)[0]
        high_z = microstrip_z0(2.0, 0.1, 4.1, 0.035)[0]
        low_width = find_width_for_z0(low_z, 0.1, 4.1, 0.035, 0.05, 2.0, 1e-12)
        high_width = find_width_for_z0(high_z, 0.1, 4.1, 0.035, 0.05, 2.0, 1e-12)
        self.assertEqual(low_width, 0.05)
        self.assertEqual(high_width, 2.0)
        self.assertLessEqual(abs(microstrip_z0(low_width, 0.1, 4.1, 0.035)[0] - low_z), 1e-12)
        self.assertLessEqual(abs(microstrip_z0(high_width, 0.1, 4.1, 0.035)[0] - high_z), 1e-12)

    def test_unachievable_numerical_tolerance_fails_instead_of_returning_guess(self):
        def discontinuous_monotonic(width, _height, _er, _thickness):
            return (2.0 if width < 1.025 else 1.0), 2.0

        with patch.object(impedance, "microstrip_z0", side_effect=discontinuous_monotonic):
            with self.assertRaisesRegex(ArithmeticError, "did not converge"):
                find_width_for_z0(1.5, h_mm=0.1, er=4.1, tol=1e-30)

    def test_microstrip_rejects_nonfinite_or_invalid_geometry(self):
        invalid = [
            (0.0, 0.1, 4.1, 0.035), (-0.1, 0.1, 4.1, 0.035),
            (0.1, 0.0, 4.1, 0.035), (0.1, -0.1, 4.1, 0.035),
            (0.1, 0.1, 0.0, 0.035), (0.1, 0.1, 0.9, 0.035),
            (0.1, 0.1, math.nan, 0.035), (0.1, 0.1, math.inf, 0.035),
            (0.1, 0.1, 4.1, -0.001), (0.1, 0.1, 4.1, math.nan), (0.1, 0.1, 4.1, math.inf),
            (math.nan, 0.1, 4.1, 0.035), (0.1, math.inf, 4.1, 0.035),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                microstrip_z0(*args)

    def test_zero_thickness_microstrip_remains_supported(self):
        impedance, effective_er = microstrip_z0(0.2, 0.1, 4.1, 0.0)
        self.assertTrue(math.isfinite(impedance) and impedance > 0.0)
        self.assertTrue(math.isfinite(effective_er) and effective_er >= 1.0)

    def test_boolean_values_are_not_accepted_as_numeric_geometry(self):
        for args in ((True, 0.1, 4.1, 0.035), (0.1, True, 4.1, 0.035),
                     (0.1, 0.1, True, 0.035), (0.1, 0.1, 4.1, True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                microstrip_z0(*args)
        self.assertTrue(math.isfinite(microstrip_z0(0.2, 0.1, 4.1, 0.0)[0]))
        for args in ((True, 0.1, 4.1), (50.0, True, 4.1), (50.0, 0.1, True),
                     (50.0, 0.1, 4.1, True), (50.0, 0.1, 4.1, 0.035, True, 2.0),
                     (50.0, 0.1, 4.1, 0.035, 0.05, True),
                     (50.0, 0.1, 4.1, 0.035, 0.05, 2.0, True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                find_width_for_z0(*args)
        for args in ((True, 0.2, 0.1, 4.1, 0.035), (0.1, True, 0.1, 4.1, 0.035),
                     (0.1, 0.2, True, 4.1, 0.035), (0.1, 0.2, 0.1, True, 0.035),
                     (0.1, 0.2, 0.1, 4.1, True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                differential_microstrip_z0(*args)

    def test_differential_spacing_and_nonphysical_strong_coupling_fail_closed(self):
        for spacing in (0.0, -0.01, math.nan, math.inf):
            with self.subTest(spacing=spacing), self.assertRaises(ValueError):
                differential_microstrip_z0(0.1, spacing, 0.1, 4.1)
        # The approximation yields negative odd-mode impedance for this
        # extreme coupling case; the guard must refuse that result.
        with self.assertRaisesRegex(ValueError, "nonphysical"):
            differential_microstrip_z0(0.1, 0.001, 0.1, 20.0, 0.035)

    def test_valid_differential_nominal_is_unchanged(self):
        actual = differential_microstrip_z0(0.1, 0.2, 0.1, 4.1, 0.035)
        expected = (126.19386658448902, 63.09693329224451, 65.05945295250676, 3.034037242597771)
        for got, want in zip(actual, expected, strict=True):
            self.assertLessEqual(abs(got - want), 1e-11)


if __name__ == "__main__":
    unittest.main()
