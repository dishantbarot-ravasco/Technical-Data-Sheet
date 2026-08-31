"""
Unit tests for apps/services/splicing_service.py — DB-backed splice calculator.

compute_splicing() tries DB lookup tables (splice_step_lookup,
splice_method_config) first and falls back to the hardcoded tables in
calculations.py when they're empty — both paths are covered here.
"""
from django.test import TestCase

from apps.core.models import SpliceStepLookup, SpliceMethodConfig
from apps.services.calculations import round_half_up, step_length_mm
from apps.services.splicing_service import compute_splicing


class ComputeSplicingFallbackTests(TestCase):
    """No DB lookup rows exist -> falls back to calculations.py's hardcoded tables."""

    def setUp(self):
        # migrations/0025_seed_reference_catalog.py seeds real splice_step_lookup
        # / splice_method_config rows into every database, including the test
        # DB — unlike per-test factory rows, migration-seeded rows aren't
        # rolled back between tests. This class specifically exercises the
        # empty-table fallback path, so it must clear them first; the delete
        # itself rolls back at the end of the test along with everything else
        # (TestCase wraps each test in a transaction).
        SpliceStepLookup.objects.all().delete()
        SpliceMethodConfig.objects.all().delete()

    def test_falls_back_to_hardcoded_step_and_buffer(self):
        result = compute_splicing(
            belt_rating_kn_m=315, num_plies=3, belt_width_mm=1000,
            num_joints=2, vulcanization_method='hot',
        )
        expected_step = step_length_mm(315, 3)
        expected_splice = round_half_up(0.3 * 1000 + expected_step * 2 + 50)
        self.assertEqual(result.step_length_mm, expected_step)
        self.assertEqual(result.splice_length_mm, expected_splice)
        self.assertEqual(result.total_extra_length_m, round(2 * expected_splice / 1000, 3))

    def test_cold_method_uses_75mm_fallback_buffer(self):
        hot = compute_splicing(315, 3, 1000, 1, 'hot')
        cold = compute_splicing(315, 3, 1000, 1, 'cold')
        self.assertEqual(cold.splice_length_mm - hot.splice_length_mm, 25)

    def test_method_is_case_insensitive(self):
        lower = compute_splicing(315, 3, 1000, 1, 'hot')
        upper = compute_splicing(315, 3, 1000, 1, 'HOT')
        self.assertEqual(lower, upper)

    def test_zero_plies_raises(self):
        with self.assertRaises(ValueError):
            compute_splicing(315, 0, 1000, 1, 'hot')

    def test_none_vulcanization_method_defaults_to_hot(self):
        result = compute_splicing(315, 3, 1000, 1, None)
        hot = compute_splicing(315, 3, 1000, 1, 'hot')
        self.assertEqual(result, hot)


class ComputeSplicingDbLookupTests(TestCase):
    """DB lookup rows present -> DB values win over the hardcoded fallback tables."""

    def setUp(self):
        # Start from a clean slate rather than the real seeded catalog (see
        # ComputeSplicingFallbackTests.setUp) so the rows each test creates
        # here are the only ones compute_splicing() sees — both to avoid
        # colliding with a real value (e.g. max_fabric_rating_kn_m=100 is a
        # real seeded row) and so "overrides the hardcoded table" is actually
        # testing this test's own row, not incidentally agreeing with reality.
        SpliceStepLookup.objects.all().delete()
        SpliceMethodConfig.objects.all().delete()

    def test_db_step_lookup_overrides_hardcoded_table(self):
        # fabric_rating = 315/3 = 105 -> would hit the (125,200) row hardcoded,
        # but a DB row for max_fabric_rating_kn_m=110 with a distinct step wins.
        SpliceStepLookup.objects.create(max_fabric_rating_kn_m=110, step_length_mm=777)
        result = compute_splicing(315, 3, 1000, 1, 'hot')
        self.assertEqual(result.step_length_mm, 777)

    def test_db_buffer_overrides_hardcoded_buffer(self):
        SpliceMethodConfig.objects.create(vulcanization_method='hot', buffer_mm=123)
        result = compute_splicing(315, 3, 1000, 1, 'hot')
        expected_step = step_length_mm(315, 3)  # no step row seeded -> hardcoded step
        expected_splice = round_half_up(0.3 * 1000 + expected_step * 2 + 123)
        self.assertEqual(result.splice_length_mm, expected_splice)

    def test_fabric_rating_above_every_db_row_uses_highest_step(self):
        SpliceStepLookup.objects.create(max_fabric_rating_kn_m=50, step_length_mm=111)
        SpliceStepLookup.objects.create(max_fabric_rating_kn_m=100, step_length_mm=222)
        # fabric_rating = 900/3 = 300, above every row -> highest row's step (222)
        result = compute_splicing(900, 3, 1000, 1, 'hot')
        self.assertEqual(result.step_length_mm, 222)
