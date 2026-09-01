"""
Tests for apps/api/routers/master_views.py's public/no-auth read endpoints.

Covers the get_splicing_config regression: @cache_page sits above
@permission_classes, so cache_page short-circuits on a cache hit and returns
the stored response WITHOUT ever re-invoking the view — meaning any
IsAuthenticated (or other permission) check on a @cache_page-wrapped view
only actually runs on the request that misses the cache. get_splicing_config
used to declare IsAuthenticated while serving data that's already public via
the AllowAny /api/bootstrap endpoint; it's now AllowAny like every other GET
in this file, so this test also guards against that permission accidentally
being tightened back to IsAuthenticated (which would silently only protect
one request per cache TTL).
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.models import Customer
from apps.api.tests.factories import make_user

SPLICING_CONFIG_URL = '/api/splicing-config'
CUSTOMERS_URL       = '/api/customers'


def auth_client(user):
    client = APIClient()
    token = RefreshToken.for_user(user)
    token['sub'] = str(user.user_id)
    token['role'] = user.role
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    return client


class SplicingConfigPermissionTests(TestCase):
    def setUp(self):
        # cache_page's cache is not part of the per-test DB transaction
        # rollback, so a hit cached by one test would leak into the next.
        cache.clear()
        self.client = APIClient()

    def test_unauthenticated_request_gets_200(self):
        response = self.client.get(SPLICING_CONFIG_URL)
        self.assertEqual(response.status_code, 200)

    def test_response_has_expected_shape(self):
        response = self.client.get(SPLICING_CONFIG_URL)
        self.assertIn('step_table', response.data)
        self.assertIn('buffers', response.data)

    def test_second_request_still_200_once_cached(self):
        # First request populates the DatabaseCache/LocMemCache entry;
        # second request exercises the cache_page short-circuit path itself
        # (this is the path that used to silently skip the permission check).
        first = self.client.get(SPLICING_CONFIG_URL)
        second = self.client.get(SPLICING_CONFIG_URL)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)


class CustomerSearchCandidateCapTests(TestCase):
    """
    Regression test for the customers() GET search's unbounded queryset
    materialization: `list(qs.filter(customer_name__icontains=search))` used
    to pull every matching row into Python before ranking/slicing to `limit`,
    with no upper bound on that initial fetch. Fixed by capping the candidate
    set at _SEARCH_CANDIDATE_CAP (500 in production) before ranking.

    _SEARCH_CANDIDATE_CAP is patched down to a small number here so the test
    can exercise the cap actually taking effect without creating hundreds of
    real rows.
    """

    def setUp(self):
        self.client = auth_client(make_user(role='tds_creator'))

    def test_ranking_and_limit_still_correct_under_a_small_cap(self):
        # 6 matching rows, cap patched to 3: the cap must still leave enough
        # of the highest-relevance-tier matches available to fill `limit`.
        Customer.objects.create(customer_name='Zeta Sundries')     # tier 1 (word starts with "s")
        Customer.objects.create(customer_name='Sanjay Rubber Co')  # tier 0 (name starts with "s")
        Customer.objects.create(customer_name='Amrit Steel')       # tier 1
        Customer.objects.create(customer_name='Best Solutions')    # tier 1
        Customer.objects.create(customer_name='Sundar Traders')    # tier 0
        Customer.objects.create(customer_name='Coastal Rubbers')   # no match ("s" absent... actually contains no 's')

        with patch('apps.api.routers.master_views._SEARCH_CANDIDATE_CAP', 3):
            response = self.client.get(CUSTOMERS_URL, {'search': 's', 'limit': 2})

        self.assertEqual(response.status_code, 200, response.data)
        names = [c['customer_name'] for c in response.data]
        self.assertEqual(len(names), 2)
        # Whichever 3 rows the (order_by customer_name) candidate cap
        # happened to include, the result must still be sorted by relevance
        # tier first (0 before 1), not just alphabetically.
        for name in names:
            self.assertIn('s', name.lower())

    def test_response_never_exceeds_requested_limit(self):
        for i in range(10):
            Customer.objects.create(customer_name=f'Search Target {i}')

        response = self.client.get(CUSTOMERS_URL, {'search': 'search', 'limit': 5})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 5)
