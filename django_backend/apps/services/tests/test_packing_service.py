"""
Unit tests for apps/services/packing_service.py — Packing & Logistics computation.

Covers all three reel formulas (circular/twin/elliptical), the
back-calculation path when the belt is too long for one roll, the
guard clauses, and validate_custom_roll_lengths().
"""
from django.test import TestCase

from apps.core.models import ReelType
from apps.services.packing_service import compute_packing, validate_custom_roll_lengths


def make_reel(formula_key, max_roll_diameter_m='2.50', core_diameter_m='0.3',
              center_to_center_m=None, num_rolls_base=1, name=None):
    return ReelType.objects.create(
        reel_name=name or f'{formula_key}-reel',
        formula_key=formula_key,
        num_rolls_base=num_rolls_base,
        core_diameter_m=core_diameter_m,
        center_to_center_m=center_to_center_m,
        max_roll_diameter_m=max_roll_diameter_m,
    )


class ComputePackingGuardTests(TestCase):

    def test_unknown_reel_type_id_raises(self):
        with self.assertRaises(ValueError):
            compute_packing(999, 1, 1, 10, 100, 1000, 12.0)

    def test_zero_thickness_raises(self):
        reel = make_reel('circular')
        with self.assertRaises(ValueError):
            compute_packing(reel.pk, 1, 1, 0, 100, 1000, 12.0)

    def test_zero_belt_length_raises(self):
        reel = make_reel('circular')
        with self.assertRaises(ValueError):
            compute_packing(reel.pk, 1, 1, 10, 0, 1000, 12.0)

    def test_invalid_num_rolls_base_raises(self):
        reel = make_reel('circular', num_rolls_base=0)
        with self.assertRaises(ValueError):
            compute_packing(reel.pk, 1, 1, 10, 100, 1000, 12.0)


class ComputePackingCircularTests(TestCase):

    def test_fits_on_single_roll_uses_base_rolls(self):
        # small belt -> D stays under max_roll_diameter_m -> no back-calc branch
        reel = make_reel('circular', max_roll_diameter_m='5.00', num_rolls_base=1)
        result = compute_packing(reel.pk, 1, 1, 10, 50, 1000, 12.0)
        self.assertEqual(result.num_rolls, 1)
        self.assertEqual(result.length_per_roll_m, 50)

    def test_oversized_belt_splits_into_multiple_rolls(self):
        # Force D > max_D so the back-calculation branch runs.
        reel = make_reel('circular', max_roll_diameter_m='1.00', core_diameter_m='0.3', num_rolls_base=1)
        result = compute_packing(reel.pk, 1, 1, 10, 5000, 1000, 12.0)
        self.assertGreater(result.num_rolls, 1)
        # Total length across rolls must reconstruct the belt length.
        self.assertAlmostEqual(result.length_per_roll_m * result.num_rolls, 5000, delta=result.num_rolls)

    def test_net_and_gross_weight_scale_with_length(self):
        reel = make_reel('circular', max_roll_diameter_m='5.00')
        r1 = compute_packing(reel.pk, 1, 1, 10, 50, 1000, 12.0)
        r2 = compute_packing(reel.pk, 1, 1, 10, 100, 1000, 12.0)
        self.assertGreater(r2.net_weight_kg, r1.net_weight_kg)
        self.assertGreater(r2.gross_weight_kg, r1.gross_weight_kg)
        # Gross always >= net (extra 0.5mm accounted in gross formula).
        self.assertGreaterEqual(r1.gross_weight_kg, r1.net_weight_kg)

    def test_roll_dimensions_string_format(self):
        reel = make_reel('circular', max_roll_diameter_m='5.00')
        result = compute_packing(reel.pk, 1, 1, 10, 50, 1000, 12.0)
        self.assertIn('H:', result.roll_dimensions)
        self.assertIn('W:', result.roll_dimensions)

    def test_gross_weight_per_roll_is_none_when_zero_rolls_impossible(self):
        # num_rolls is always >= 1 in practice; sanity check it's populated.
        reel = make_reel('circular', max_roll_diameter_m='5.00')
        result = compute_packing(reel.pk, 1, 1, 10, 50, 1000, 12.0)
        self.assertIsNotNone(result.gross_weight_per_roll_kg)


class ComputePackingTwinTests(TestCase):

    def test_twin_reel_always_produces_even_roll_count(self):
        reel = make_reel('twin', max_roll_diameter_m='1.00', core_diameter_m='0.3', num_rolls_base=2)
        result = compute_packing(reel.pk, 1, 1, 10, 5000, 1000, 12.0)
        self.assertEqual(result.num_rolls % 2, 0)

    def test_twin_reel_fits_on_base_rolls_when_small(self):
        reel = make_reel('twin', max_roll_diameter_m='5.00', num_rolls_base=2)
        result = compute_packing(reel.pk, 1, 1, 10, 50, 1000, 12.0)
        self.assertEqual(result.num_rolls, 2)
        self.assertEqual(result.length_per_roll_m, 25)


class ComputePackingEllipticalTests(TestCase):

    def test_elliptical_reel_within_max_uses_base_rolls(self):
        reel = make_reel('elliptical', max_roll_diameter_m='5.00', center_to_center_m='1.32', num_rolls_base=1)
        result = compute_packing(reel.pk, 1, 1, 10, 50, 1000, 12.0)
        self.assertEqual(result.num_rolls, 1)

    def test_elliptical_reel_oversized_splits(self):
        reel = make_reel('elliptical', max_roll_diameter_m='1.00', center_to_center_m='1.32', num_rolls_base=1)
        result = compute_packing(reel.pk, 1, 1, 10, 5000, 1000, 12.0)
        self.assertGreater(result.num_rolls, 1)

    def test_unknown_formula_key_raises_during_backcalc(self):
        reel = make_reel('mystery', max_roll_diameter_m='1.00', core_diameter_m='0.3')
        with self.assertRaises(ValueError):
            compute_packing(reel.pk, 1, 1, 10, 5000, 1000, 12.0)


class ValidateCustomRollLengthsTests(TestCase):

    def test_lengths_must_sum_to_belt_length(self):
        reel = make_reel('circular', max_roll_diameter_m='5.00')
        with self.assertRaises(ValueError):
            validate_custom_roll_lengths(reel.pk, 10, 100, 1000, [40, 40])

    def test_valid_unequal_split_succeeds(self):
        reel = make_reel('circular', max_roll_diameter_m='5.00')
        result = validate_custom_roll_lengths(reel.pk, 10, 100, 1000, [70, 30])
        self.assertIn('H:', result.roll_dimensions)

    def test_roll_exceeding_max_diameter_raises(self):
        reel = make_reel('circular', max_roll_diameter_m='0.5', core_diameter_m='0.3')
        with self.assertRaises(ValueError):
            validate_custom_roll_lengths(reel.pk, 10, 500, 1000, [500])

    def test_twin_requires_even_roll_count(self):
        reel = make_reel('twin', max_roll_diameter_m='5.00')
        with self.assertRaises(ValueError):
            validate_custom_roll_lengths(reel.pk, 10, 100, 1000, [40, 30, 30])

    def test_empty_roll_lengths_raises(self):
        reel = make_reel('circular', max_roll_diameter_m='5.00')
        with self.assertRaises(ValueError):
            validate_custom_roll_lengths(reel.pk, 10, 100, 1000, [])

    def test_non_numeric_roll_length_raises(self):
        reel = make_reel('circular', max_roll_diameter_m='5.00')
        with self.assertRaises(ValueError):
            validate_custom_roll_lengths(reel.pk, 10, 100, 1000, ['abc'])

    def test_negative_or_zero_length_raises(self):
        reel = make_reel('circular', max_roll_diameter_m='5.00')
        with self.assertRaises(ValueError):
            validate_custom_roll_lengths(reel.pk, 10, 100, 1000, [100, 0])

    def test_unknown_reel_type_raises(self):
        with self.assertRaises(ValueError):
            validate_custom_roll_lengths(999, 10, 100, 1000, [100])
