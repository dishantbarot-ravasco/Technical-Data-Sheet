"""
Regression test for the stale gross_weight_per_roll_kg bug in
_validate_and_compute_tds_fields() (apps/api/routers/tds_views.py).

When reel_type_id + packing_type_id are supplied with num_rolls omitted,
compute_packing() auto-computes packing_num_rolls (and, from it,
gross_weight_per_roll_kg averaged over that count). If the caller then also
supplies roll_lengths_m (a manual unequal-roll-length override), the correct
packing_num_rolls is recomputed as len(roll_lengths_m) -- but
gross_weight_per_roll_kg used to keep the STALE value averaged over the
auto-computed count, producing an internally inconsistent PDF (e.g. "Number
of Rolls: 2" next to a per-roll weight that was actually computed for 1
roll). The fix resets gross_weight_per_roll_kg to None inside the
roll_lengths_m override branch so it gets recomputed against the correct,
just-updated num_rolls.
"""
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from django.test import TestCase

from apps.api.tests.factories import make_user, make_tds_lookup_set

TDS_CREATE_URL = '/api/tds'


def auth_client(user):
    client = APIClient()
    token = RefreshToken.for_user(user)
    token['sub'] = str(user.user_id)
    token['role'] = user.role
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    return client


class RollLengthsOverrideRecomputeTests(TestCase):
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        self.creator = make_user(role='tds_creator')
        self.client = auth_client(self.creator)

    def test_unequal_roll_override_recomputes_gross_weight_per_roll(self):
        payload = dict(self.lookups['payload'])
        payload['reel_type_id']    = self.lookups['reel_type'].pk
        payload['packing_type_id'] = self.lookups['packing_type'].pk
        payload['belt_length_m']   = 100

        # Baseline: with no override, this reel's num_rolls_base=1, so
        # compute_packing() auto-computes 1 roll -- gross_weight_per_roll_kg
        # trivially equals the full gross_weight_kg.
        baseline = self.client.post(TDS_CREATE_URL, payload, format='json')
        self.assertEqual(baseline.status_code, 201, baseline.data)
        self.assertEqual(baseline.data['num_rolls'], 1)
        baseline_gw_per_roll = float(baseline.data['gross_weight_per_roll_kg'])
        baseline_gross       = float(baseline.data['gross_weight_kg'])
        self.assertAlmostEqual(baseline_gw_per_roll, baseline_gross, places=2)

        # Now override with 2 unequal rolls summing to the same belt length.
        override_payload = dict(payload)
        override_payload['roll_lengths_m'] = [60, 40]

        response = self.client.post(TDS_CREATE_URL, override_payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['num_rolls'], 2)

        gross       = float(response.data['gross_weight_kg'])
        gw_per_roll = float(response.data['gross_weight_per_roll_kg'])

        # Must be recomputed for 2 rolls, not left over from the 1-roll
        # baseline (which would equal the full gross weight).
        self.assertAlmostEqual(gw_per_roll, round(gross / 2, 2), places=2)
        self.assertNotAlmostEqual(gw_per_roll, gross, places=2)
