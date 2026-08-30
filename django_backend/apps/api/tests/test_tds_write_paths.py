"""
Integration tests for the critical write paths: POST /api/tds (create_tds)
and PATCH /api/tds/{id}/packing/recompute.

These exercise the full view + permission + service-layer stack via
APIClient, not just the pure calculation functions.
"""
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from django.test import TestCase

from apps.core.models import TDSInput
from apps.api.tests import factories
from apps.api.tests.factories import make_user, make_tds_lookup_set

TDS_CREATE_URL = '/api/tds'


def auth_client(user):
    client = APIClient()
    token = RefreshToken.for_user(user)
    token['sub'] = str(user.user_id)
    token['role'] = user.role
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    return client


class CreateTdsPermissionTests(TestCase):
    def setUp(self):
        self.lookups = make_tds_lookup_set()

    def test_viewer_role_cannot_create_tds(self):
        viewer = make_user(email='viewer@ravasco.com', role='viewer')
        client = auth_client(viewer)
        response = client.post(TDS_CREATE_URL, self.lookups['payload'], format='json')
        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_request_is_rejected(self):
        client = APIClient()
        response = client.post(TDS_CREATE_URL, self.lookups['payload'], format='json')
        self.assertEqual(response.status_code, 401)


class CreateTdsWritePathTests(TestCase):
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        self.creator = make_user(email='creator@ravasco.com', role='tds_creator')
        self.client = auth_client(self.creator)

    def test_creates_tds_with_server_computed_fields(self):
        response = self.client.post(TDS_CREATE_URL, self.lookups['payload'], format='json')
        self.assertEqual(response.status_code, 201, response.data)

        record = TDSInput.objects.get(pk=response.data['tds_id'])
        self.assertEqual(record.status, 'draft')
        self.assertEqual(record.created_by_id, self.creator.user_id)
        # total_thickness_mm = top(3) + bottom(1.5) + carcass(4.5) = 9.0
        self.assertEqual(float(record.total_thickness_mm), 9.0)
        # belt_weight_per_m_kg is server-computed since payload doesn't send it
        self.assertIsNotNone(record.belt_weight_per_m_kg)
        # fabric_style is always server-computed, never client-supplied
        self.assertIsNone(self.lookups['payload'].get('fabric_style_id'))

    def test_tds_number_increments_across_creates(self):
        first = self.client.post(TDS_CREATE_URL, self.lookups['payload'], format='json')
        second = self.client.post(TDS_CREATE_URL, self.lookups['payload'], format='json')
        self.assertNotEqual(first.data['tds_number'], second.data['tds_number'])

    def test_cover_grade_from_wrong_standard_is_rejected(self):
        other_lookups = make_tds_lookup_set()  # independent Standard/CoverGrade pair
        payload = dict(self.lookups['payload'])
        payload['cover_grade_id'] = other_lookups['cover_grade'].pk  # belongs to a different standard
        response = self.client.post(TDS_CREATE_URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_endless_belt_over_length_cap_is_rejected(self):
        payload = dict(self.lookups['payload'])
        payload['construction_type'] = 'Endless'
        payload['belt_length_m'] = 150
        response = self.client.post(TDS_CREATE_URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_international_purpose_without_shipping_fields_is_rejected(self):
        from apps.core.models import Purpose
        intl_purpose = Purpose.objects.create(
            purpose_id=next(factories._next_legacy_pk), purpose_type='International',
        )
        payload = dict(self.lookups['payload'])
        payload['purpose_id'] = intl_purpose.pk
        response = self.client.post(TDS_CREATE_URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_non_numeric_dimension_returns_clean_400_not_500(self):
        payload = dict(self.lookups['payload'])
        payload['belt_width_mm'] = 'not-a-number'
        response = self.client.post(TDS_CREATE_URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_reel_and_packing_type_auto_computes_packing_fields(self):
        payload = dict(self.lookups['payload'])
        payload['reel_type_id'] = self.lookups['reel_type'].pk
        payload['packing_type_id'] = self.lookups['packing_type'].pk
        response = self.client.post(TDS_CREATE_URL, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        record = TDSInput.objects.get(pk=response.data['tds_id'])
        self.assertIsNotNone(record.num_rolls)
        self.assertIsNotNone(record.net_weight_kg)
        self.assertIsNotNone(record.gross_weight_kg)


class PackingRecomputeWritePathTests(TestCase):
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        self.creator = make_user(email='creator@ravasco.com', role='tds_creator')
        self.viewer = make_user(email='viewer2@ravasco.com', role='viewer')
        self.client = auth_client(self.creator)

        create_response = self.client.post(TDS_CREATE_URL, self.lookups['payload'], format='json')
        assert create_response.status_code == 201, create_response.data
        self.tds_id = create_response.data['tds_id']

    def _recompute_url(self):
        return f'/api/tds/{self.tds_id}/packing/recompute'

    def test_viewer_cannot_recompute_packing(self):
        client = auth_client(self.viewer)
        response = client.patch(self._recompute_url(), {
            'reel_type_id': self.lookups['reel_type'].pk,
            'packing_type_id': self.lookups['packing_type'].pk,
        }, format='json')
        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_cannot_recompute_packing(self):
        client = APIClient()
        response = client.patch(self._recompute_url(), {
            'reel_type_id': self.lookups['reel_type'].pk,
            'packing_type_id': self.lookups['packing_type'].pk,
        }, format='json')
        self.assertEqual(response.status_code, 401)

    def test_creator_can_recompute_packing_and_values_persist(self):
        response = self.client.patch(self._recompute_url(), {
            'reel_type_id': self.lookups['reel_type'].pk,
            'packing_type_id': self.lookups['packing_type'].pk,
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(response.data['num_rolls'])

        record = TDSInput.objects.get(pk=self.tds_id)
        self.assertEqual(record.reel_type_id, self.lookups['reel_type'].pk)
        self.assertEqual(record.num_rolls, response.data['num_rolls'])

    def test_recompute_on_approved_tds_is_rejected(self):
        record = TDSInput.objects.get(pk=self.tds_id)
        record.status = 'approved'
        record.save(update_fields=['status'])

        response = self.client.patch(self._recompute_url(), {
            'reel_type_id': self.lookups['reel_type'].pk,
            'packing_type_id': self.lookups['packing_type'].pk,
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_unavailable_packing_type_is_rejected(self):
        from apps.core.models import PackingType
        unavailable = PackingType.objects.create(packing_name='Palette', is_available=False)
        response = self.client.patch(self._recompute_url(), {
            'reel_type_id': self.lookups['reel_type'].pk,
            'packing_type_id': unavailable.pk,
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_missing_ids_returns_400(self):
        response = self.client.patch(self._recompute_url(), {}, format='json')
        self.assertEqual(response.status_code, 400)
