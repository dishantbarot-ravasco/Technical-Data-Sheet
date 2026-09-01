"""
Regression test for the gross_weight_per_roll_kg rounding fallback in
_validate_and_compute_tds_fields() (apps/api/routers/tds_views.py).

packing_service.compute_packing() deliberately rounds gross_weight_per_roll_kg
precisely (round(x, 2)) rather than up to the nearest 0.5kg, so it reconciles
exactly with gross_weight_kg / num_rolls (see CLAUDE.md's "Net/gross weight is
a precise decimal" note). But the fallback path used when num_rolls/
gross_weight_kg are supplied directly (skipping compute_packing entirely) had
regressed back to the old math.ceil(x*2)/2 round-up-to-half-kg convention --
reintroducing the exact Belt-Specs-vs-Packing mismatch that convention was
removed to fix. This test locks in the precise-rounding fix.
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


class GrossWeightPerRollRoundingTests(TestCase):
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        self.creator = make_user(role='tds_creator')
        self.client = auth_client(self.creator)

    def test_manually_supplied_num_rolls_rounds_precisely_not_up_to_half_kg(self):
        payload = dict(self.lookups['payload'])
        # No reel_type_id/packing_type_id -> compute_packing's own branch is
        # skipped entirely, forcing the manual-fallback code path.
        payload['num_rolls']      = 3
        payload['gross_weight_kg'] = 1000.32

        response = self.client.post(TDS_CREATE_URL, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)

        gw_per_roll = float(response.data['gross_weight_per_roll_kg'])
        # Precise: round(1000.32 / 3, 2) == 333.44
        # Old (buggy) behavior: math.ceil((1000.32/3)*2)/2 == 333.5
        self.assertEqual(gw_per_roll, 333.44)
        self.assertNotEqual(gw_per_roll, 333.5)
        # The reconciliation property the CLAUDE.md note cares about:
        # per-roll x num_rolls should land back on the total within 1 cent
        # per roll of rounding slack, not systematically over by design.
        self.assertAlmostEqual(gw_per_roll * 3, 1000.32, places=2)
