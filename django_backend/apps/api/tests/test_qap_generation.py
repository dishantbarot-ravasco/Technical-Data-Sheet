"""
Integration tests for GET /api/tds/{id}/qap/pdf (apps/api/routers/qap_views.py).

Full PDF rendering needs WeasyPrint plus seeded QAPTemplate/QAPSection/QAPItem
rows (see apps/core/management/commands/seed_qap_templates.py) and isn't
exercised here — these tests cover the endpoint's auth gate and the two
cheap, deterministic error paths (missing TDS, unmapped standard) that don't
require that fixture graph.
"""
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.api.tests.factories import make_user, make_tds_lookup_set

TDS_CREATE_URL = '/api/tds'


def auth_client(user):
    client = APIClient()
    token = RefreshToken.for_user(user)
    token['sub'] = str(user.user_id)
    token['role'] = user.role
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    return client


class QapGenerationAuthTests(TestCase):
    def test_unauthenticated_request_is_rejected(self):
        client = APIClient()
        response = client.get('/api/tds/1/qap/pdf')
        self.assertEqual(response.status_code, 401)

    def test_missing_tds_returns_404(self):
        user = make_user()
        client = auth_client(user)
        response = client.get('/api/tds/999999/qap/pdf')
        self.assertEqual(response.status_code, 404)


class QapGenerationTemplateResolutionTests(TestCase):
    """A standard with no QAP template mapping must fail cleanly (422), never 500."""

    def test_unmapped_standard_returns_422_not_500(self):
        lookups = make_tds_lookup_set()  # 'IS 1891' standard created fresh, no seeded QAP template for it
        creator = make_user(email='creator@ravasco.com', role='tds_creator')
        client = auth_client(creator)

        create_response = client.post(TDS_CREATE_URL, lookups['payload'], format='json')
        self.assertEqual(create_response.status_code, 201, create_response.data)
        tds_id = create_response.data['tds_id']

        response = client.get(f'/api/tds/{tds_id}/qap/pdf')
        self.assertEqual(response.status_code, 422)
