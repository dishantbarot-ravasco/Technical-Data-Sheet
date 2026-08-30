"""
Unit tests for apps/services/calculations.py — pure business-logic math.

Split into:
  - PureMathTests        : no DB access, deterministic formulas only.
  - DbBackedLookupTests   : functions that fall back to a hardcoded table
                            when the corresponding DB lookup table is empty
                            (get_splice_buffer, get_sampling_count) or that
                            read real FK rows (get_container_constraints,
                            auto_select_fabric_style).
"""
from django.test import TestCase

from apps.core.models import (
    ContainerType, RegionContainerWeightLimit, FabricType, FabricStyle,
    SpliceMethodConfig, SamplingPlanLookup,
)
from apps.services.calculations import (
    round_half_up, validate_endless_belt_length,
    validate_international_shipping_fields, get_container_constraints,
    belt_weight_per_metre, belt_gross_weight_per_metre, total_belt_weight,
    reel_diameter_circular, reel_diameter_twin, reel_diameter_elliptical,
    reel_diameter, parse_belt_rating, auto_select_fabric_style,
    step_length_mm, get_splice_buffer, splice_length_mm,
    total_extra_length_m, is1891_sampling_count, get_sampling_count,
    ENDLESS_MAX_BELT_LENGTH_M,
)


class PureMathTests(TestCase):

    # ── round_half_up ────────────────────────────────────────────────────────

    def test_round_half_up_rounds_half_away_from_bankers_rounding(self):
        self.assertEqual(round_half_up(0.5), 1)
        self.assertEqual(round_half_up(1.5), 2)
        self.assertEqual(round_half_up(2.5), 3)

    def test_round_half_up_with_decimals(self):
        self.assertEqual(round_half_up(1.005, 2), 1.01)
        self.assertEqual(round_half_up(1.234, 2), 1.23)

    def test_round_half_up_returns_int_for_zero_decimals(self):
        self.assertIsInstance(round_half_up(4.0), int)

    # ── validate_endless_belt_length ────────────────────────────────────────

    def test_endless_belt_within_cap_is_ok(self):
        validate_endless_belt_length('Endless', ENDLESS_MAX_BELT_LENGTH_M)

    def test_endless_belt_over_cap_raises(self):
        with self.assertRaises(ValueError):
            validate_endless_belt_length('Endless', ENDLESS_MAX_BELT_LENGTH_M + 0.01)

    def test_non_endless_construction_type_is_never_checked(self):
        validate_endless_belt_length('Open-End', 10_000)

    def test_endless_belt_missing_length_is_noop(self):
        validate_endless_belt_length('Endless', None)

    def test_construction_type_matching_is_case_insensitive(self):
        with self.assertRaises(ValueError):
            validate_endless_belt_length('  ENDLESS  ', 101)

    # ── validate_international_shipping_fields ──────────────────────────────

    def test_international_requires_both_fields(self):
        with self.assertRaises(ValueError):
            validate_international_shipping_fields('International', None, None)

    def test_international_missing_only_container_type(self):
        with self.assertRaises(ValueError) as ctx:
            validate_international_shipping_fields('International', 'EU', None)
        self.assertIn('container_type_id', str(ctx.exception))

    def test_international_with_both_fields_is_ok(self):
        validate_international_shipping_fields('International', 'EU', 1)

    def test_domestic_purpose_never_requires_shipping_fields(self):
        validate_international_shipping_fields('Domestic', None, None)

    # ── belt weight ──────────────────────────────────────────────────────────

    def test_belt_weight_per_metre_formula(self):
        # SG=1.2, T=10mm, W=1000mm -> 1.2 * 10 * 1.0 = 12.0
        self.assertEqual(belt_weight_per_metre(1.2, 1000, 10), 12.0)

    def test_belt_weight_per_metre_rounds_to_4dp(self):
        result = belt_weight_per_metre(1.23456, 1000, 10.5)
        self.assertEqual(result, round(1.23456 * 10.5 * 1.0, 4))

    def test_belt_gross_weight_adds_half_mm_thickness(self):
        # Gross uses (T + 0.5), net does not.
        net = belt_weight_per_metre(1.2, 1000, 10)
        gross = belt_gross_weight_per_metre(1.2, 1000, 10)
        self.assertGreater(gross, net)
        self.assertEqual(gross, round(1.2 * 10.5 * 1.0, 4))

    def test_total_belt_weight(self):
        self.assertEqual(total_belt_weight(12.0, 100), 1200.0)

    # ── reel diameter formulas ───────────────────────────────────────────────

    def test_reel_diameter_circular_matches_formula(self):
        import math
        d_m, L, k = 0.01, 100, 0.3
        expected = round(math.sqrt((4 / math.pi) * d_m * L + k ** 2), 3)
        self.assertEqual(reel_diameter_circular(d_m, L, k), expected)

    def test_reel_diameter_twin_uses_half_length(self):
        d_m, L, k = 0.01, 100, 0.3
        twin = reel_diameter_twin(d_m, L, k)
        circular_half = reel_diameter_circular(d_m, L / 2, k)
        self.assertEqual(twin, circular_half)

    def test_reel_diameter_elliptical_smaller_than_circular_for_same_length(self):
        d_m, L = 0.01, 100
        self.assertLess(reel_diameter_elliptical(d_m, L), reel_diameter_circular(d_m, L))

    def test_reel_diameter_dispatch_by_formula_key(self):
        args = dict(total_thickness_mm=10, belt_length_m=100, k_m=0.3, center_to_center_m=1.32)
        d_m = 10 / 1000
        self.assertEqual(reel_diameter('circular', **args), reel_diameter_circular(d_m, 100, 0.3))
        self.assertEqual(reel_diameter('twin', **args), reel_diameter_twin(d_m, 100, 0.3))
        self.assertEqual(reel_diameter('elliptical', **args), reel_diameter_elliptical(d_m, 100, 0.3, 1.32))

    def test_reel_diameter_unknown_key_defaults_to_circular(self):
        args = dict(total_thickness_mm=10, belt_length_m=100)
        self.assertEqual(reel_diameter('bogus', **args), reel_diameter('circular', **args))

    # ── parse_belt_rating ────────────────────────────────────────────────────

    def test_parse_belt_rating_standard_format(self):
        self.assertEqual(parse_belt_rating('EP 315/3'), (315.0, 3))

    def test_parse_belt_rating_decimal_kn(self):
        self.assertEqual(parse_belt_rating('NN 630.5/4'), (630.5, 4))

    def test_parse_belt_rating_unparseable_raises(self):
        with self.assertRaises(ValueError):
            parse_belt_rating('garbage')

    def test_parse_belt_rating_none_raises(self):
        with self.assertRaises(ValueError):
            parse_belt_rating(None)

    # ── step length lookup table ─────────────────────────────────────────────

    def test_step_length_uses_lowest_matching_threshold(self):
        # rating_per_ply = 300/3 = 100 -> table row (100, 150)
        self.assertEqual(step_length_mm(300, 3), 150)

    def test_step_length_exact_threshold_boundary(self):
        # rating_per_ply = 400/1 = 400 -> exact match on last table row
        self.assertEqual(step_length_mm(400, 1), 400)

    def test_step_length_above_max_uses_fallback_max(self):
        self.assertEqual(step_length_mm(2000, 1), 400)

    def test_step_length_zero_plies_raises(self):
        with self.assertRaises(ValueError):
            step_length_mm(315, 0)

    # ── splice_length_mm (calculations.py's own DB-free variant) ────────────

    def test_splice_length_mm_hot_default_buffer(self):
        length, step = splice_length_mm(1000, 315, 3, splice_type='hot')
        expected_step = step_length_mm(315, 3)
        expected = round_half_up(0.3 * 1000 + expected_step * (3 - 1) + 50)
        self.assertEqual((length, step), (expected, expected_step))

    def test_splice_length_mm_cold_default_buffer_differs_from_hot(self):
        hot_len, _ = splice_length_mm(1000, 315, 3, splice_type='hot')
        cold_len, _ = splice_length_mm(1000, 315, 3, splice_type='cold')
        self.assertEqual(cold_len - hot_len, 25)  # cold buffer(75) - hot buffer(50)

    def test_splice_length_mm_explicit_buffer_overrides_default(self):
        length, _ = splice_length_mm(1000, 315, 3, splice_type='hot', buffer=999)
        length_default, _ = splice_length_mm(1000, 315, 3, splice_type='hot')
        self.assertNotEqual(length, length_default)

    # ── total_extra_length_m ────────────────────────────────────────────────

    def test_total_extra_length(self):
        self.assertEqual(total_extra_length_m(2, 500), 1.0)

    # ── IS 1891 hardcoded sampling table ────────────────────────────────────

    def test_is1891_sampling_count_boundaries(self):
        self.assertEqual(is1891_sampling_count(500), 1)
        self.assertEqual(is1891_sampling_count(501), 2)
        self.assertEqual(is1891_sampling_count(999_999), 7)


class DbBackedLookupTests(TestCase):
    """Functions that read a DB table but fall back to a hardcoded value
    when that table is missing/empty rows for the given input."""

    def test_get_splice_buffer_falls_back_when_table_empty(self):
        from apps.services.calculations import get_splice_buffer
        self.assertEqual(get_splice_buffer('hot'), 50)
        self.assertEqual(get_splice_buffer('cold'), 75)

    def test_get_splice_buffer_reads_db_row_when_present(self):
        SpliceMethodConfig.objects.create(vulcanization_method='hot', buffer_mm=999)
        self.assertEqual(get_splice_buffer('hot'), 999)
        # untouched method still falls back
        self.assertEqual(get_splice_buffer('cold'), 75)

    def test_get_sampling_count_falls_back_when_table_empty(self):
        self.assertEqual(get_sampling_count(500), is1891_sampling_count(500))

    def test_get_sampling_count_reads_db_row_when_present(self):
        SamplingPlanLookup.objects.create(max_belt_length_m=500, sample_count=99)
        self.assertEqual(get_sampling_count(400), 99)

    def test_get_container_constraints_missing_container_raises(self):
        with self.assertRaises(ValueError):
            get_container_constraints(999, 'EU')

    def test_get_container_constraints_missing_region_raises(self):
        ct = ContainerType.objects.create(name='20ft', max_height_m=2.5, max_width_m=2.3)
        with self.assertRaises(ValueError):
            get_container_constraints(ct.pk, 'nonexistent-region')

    def test_get_container_constraints_returns_resolved_limits(self):
        ct = ContainerType.objects.create(name='40ft', max_height_m=2.5, max_width_m=2.3)
        RegionContainerWeightLimit.objects.create(
            region='EU', container_type=ct, max_gross_weight_kg=25000,
        )
        result = get_container_constraints(ct.pk, 'EU')
        self.assertEqual(result.max_height_m, 2.5)
        self.assertEqual(result.max_width_m, 2.3)
        self.assertEqual(result.max_gross_weight_kg, 25000.0)

    def test_auto_select_fabric_style_picks_tightest_fit(self):
        ft = FabricType.objects.create(fabric_code='EP')
        FabricStyle.objects.create(fabric_type=ft, style_name='EP 150')
        style_201 = FabricStyle.objects.create(fabric_type=ft, style_name='EP 201')
        FabricStyle.objects.create(fabric_type=ft, style_name='EP 250')
        # rating EP 1000/5 -> per_ply = 200 -> tightest style >= 200 is EP 201
        result = auto_select_fabric_style(ft.pk, kn=1000, plies=5)
        self.assertEqual(result, style_201.pk)

    def test_auto_select_fabric_style_no_qualifying_style_returns_none(self):
        ft = FabricType.objects.create(fabric_code='NN')
        FabricStyle.objects.create(fabric_type=ft, style_name='NN 100')
        result = auto_select_fabric_style(ft.pk, kn=1000, plies=2)  # per_ply=500
        self.assertIsNone(result)

    def test_auto_select_fabric_style_zero_plies_returns_none(self):
        ft = FabricType.objects.create(fabric_code='EP')
        self.assertIsNone(auto_select_fabric_style(ft.pk, kn=100, plies=0))
