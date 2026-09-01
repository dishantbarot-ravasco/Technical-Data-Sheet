"""
Tests for stripping the fabric-code prefix off displayed/printed belt
ratings (e.g. "EP 1000/5" -> "1000/5" -- see
apps.services.calculations.strip_fabric_prefix).

Fabric Type is always its own separately-selected field alongside a belt
rating, so repeating the fabric code inside the rating text was redundant
everywhere it was shown: the belt description embedded in the actual TDS
PDF, the belt-rating dropdown, and search results. rating_name itself is
never modified in the DB -- this only affects what gets displayed/printed
and, for GET /api/belt-ratings/resolve, what a bare "<kN>/<plies>" number
can be matched back to.
"""
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from django.core.cache import cache
from django.test import TestCase

from apps.core.models import BeltRating, FabricType, TDSInput, TDSParameter, BeltRatingValue
from apps.api.tests.factories import make_user, make_tds_lookup_set

CREATE_BATCH_URL   = '/api/tds/batch/'
TEXT_IMPORT_URL    = '/api/tds/batch/text-import/'
RESOLVE_RATING_URL = '/api/belt-ratings/resolve'

PARAM_CARCASS_THICKNESS = 4


def auth_client(user):
    client = APIClient()
    token = RefreshToken.for_user(user)
    token['sub'] = str(user.user_id)
    token['role'] = user.role
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    return client


def _seed_carcass_eav(belt_rating, value='4.5'):
    param, _ = TDSParameter.objects.get_or_create(
        parameter_id=PARAM_CARCASS_THICKNESS,
        defaults=dict(parameter_group='Construction', parameter_name='Carcass Thickness',
                      display_order=2),
    )
    BeltRatingValue.objects.create(
        belt_rating=belt_rating, parameter=param, indus_value=value,
    )


class BeltDescriptionFabricAndRatingAreSeparateTokensTests(TestCase):
    """
    create_batch()'s belt_description (the string stored on TDSInput and
    printed verbatim in the TDS PDF -- see pdf_service.py's "Belt Description"
    GI row) must show Fabric Type and Belt Rating as two separate "X"-joined
    tokens ("...X EP X 315/3 X..."), not the old combined "EP 315/3" -- the
    fabric code isn't dropped, just no longer glued onto the rating number.
    """
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        _seed_carcass_eav(self.lookups['belt_rating'])
        self.creator = make_user(email='creator@ravasco.com', role='tds_creator')
        self.client = auth_client(self.creator)

    def test_batch_created_belt_description_shows_fabric_and_rating_separately(self):
        p = self.lookups['payload']
        body = {
            'shared': {
                'purpose_id':      self.lookups['purpose'].pk,
                'brand_id':        self.lookups['brand'].pk,
                'standard_id':     self.lookups['standard'].pk,
                'reel_type_id':    self.lookups['reel_type'].pk,
                'packing_type_id': self.lookups['packing_type'].pk,
            },
            'customer': {'customer_name': 'Test Customer Co'},
            'belts': [{
                'belt_type_id':    p['belt_type_id'],
                'belt_width_mm':   p['belt_width_mm'],
                'fabric_type_id':  p['fabric_type_id'],
                'belt_rating_id':  p['belt_rating_id'],
                'top_cover_mm':    p['top_cover_mm'],
                'bottom_cover_mm': p['bottom_cover_mm'],
                'cover_grade_id':  p['cover_grade_id'],
                'belt_length_m':   100,
            }],
        }
        response = self.client.post(CREATE_BATCH_URL, body, format='json')
        self.assertEqual(response.status_code, 201, response.data)

        # self.lookups['belt_rating'].rating_name == 'EP 315/3' (see factories.py);
        # self.lookups['fabric_type'].fabric_code is a tagged unique string like
        # 'EP-90000005' (see factories.py), NOT necessarily literal 'EP' -- the
        # composed description must use the FABRIC TYPE's own fabric_code (the
        # one actually selected for this belt), not whatever happens to be
        # baked into the belt rating's rating_name prefix.
        record = TDSInput.objects.get(batch_id=response.data['batch']['batch_id'])
        fabric_code = self.lookups['fabric_type'].fabric_code
        # Rating shows bare (no fabric prefix glued on).
        self.assertIn('315/3', record.belt_description)
        self.assertNotIn('EP 315/3', record.belt_description)
        # Fabric code appears as its own " X <code> X " token.
        self.assertIn(f'X {fabric_code} X 315/3', record.belt_description)


class ResolveBeltRatingsEndpointTests(TestCase):
    """GET /api/belt-ratings/resolve?rating=<bare number> -- cross-fabric lookup
    the frontend's live belt-description parser uses to figure out which
    Fabric Type a bare rating (no fabric letters) belongs to."""

    def setUp(self):
        # This endpoint is @cache_page'd, and that cache isn't part of the
        # per-test DB transaction rollback -- a query string reused across
        # two test methods would otherwise silently return the FIRST test's
        # (now stale) response to the second. Belt and suspenders: clear the
        # cache AND give every test its own distinct query number below.
        cache.clear()
        self.lookups = make_tds_lookup_set()
        self.client = APIClient()  # AllowAny, matches every other master-data GET in this file
        # NOTE: the reference catalog seeded by core.migrations.0025 already
        # contains real belt ratings for common numbers like "315/3" (across
        # multiple real fabric types) -- every test here uses a fictional,
        # collision-proof number instead of relying on the shared
        # make_tds_lookup_set() fixture's "EP 315/3" for that reason.

    def test_unique_match_returns_one_result(self):
        rating = BeltRating.objects.create(
            fabric_type=self.lookups['fabric_type'], rating_name='EP 854321/1',
        )
        response = self.client.get(RESOLVE_RATING_URL, {'rating': '854321/1'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], rating.pk)
        self.assertEqual(response.data[0]['fabric_type_id'], self.lookups['fabric_type'].pk)

    def test_ambiguous_number_returns_all_matching_fabric_types(self):
        BeltRating.objects.create(fabric_type=self.lookups['fabric_type'], rating_name='EP 854321/2')
        other_fabric = FabricType.objects.create(fabric_code='NN-test')
        BeltRating.objects.create(fabric_type=other_fabric, rating_name='NN-test 854321/2')

        response = self.client.get(RESOLVE_RATING_URL, {'rating': '854321/2'})
        self.assertEqual(response.status_code, 200, response.data)
        fabric_type_ids = {r['fabric_type_id'] for r in response.data}
        self.assertEqual(len(response.data), 2)
        self.assertEqual(fabric_type_ids, {self.lookups['fabric_type'].pk, other_fabric.pk})

    def test_no_match_returns_empty_list(self):
        response = self.client.get(RESOLVE_RATING_URL, {'rating': '854321/3'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_does_not_false_positive_on_a_longer_number(self):
        # Querying "854321/4" must not match a stored "854321/40" -- the
        # digit after the query differs, so an iendswith(" 854321/4") match
        # against " 854321/40" fails (correctly).
        BeltRating.objects.create(fabric_type=self.lookups['fabric_type'], rating_name='EP 854321/4')
        BeltRating.objects.create(fabric_type=self.lookups['fabric_type'], rating_name='EP 854321/40')

        response = self.client.get(RESOLVE_RATING_URL, {'rating': '854321/4'})
        ratings = [r['rating_name'] for r in response.data]
        self.assertIn('EP 854321/4', ratings)
        self.assertNotIn('EP 854321/40', ratings)

    def test_missing_query_param_returns_400(self):
        response = self.client.get(RESOLVE_RATING_URL)
        self.assertEqual(response.status_code, 400)


class ResolveBeltLineAcceptsBareRatingTests(TestCase):
    """
    text_import_batch()'s per-line rating matching (_resolve_belt_line) must
    accept either the full "EP 315/3" (legacy) or the bare "315/3" (current
    display convention) now that fabric is already resolved separately from
    its own `fabric` field on each line.
    """
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        _seed_carcass_eav(self.lookups['belt_rating'])
        self.creator = make_user(email='creator2@ravasco.com', role='tds_creator')
        self.client = auth_client(self.creator)

    def _line(self, rating):
        p = self.lookups['payload']
        return {
            'width':     p['belt_width_mm'],
            'fabric':    self.lookups['fabric_type'].fabric_code,
            'rating':    rating,
            'top':       p['top_cover_mm'],
            'bottom':    p['bottom_cover_mm'],
            'grade':     self.lookups['cover_grade'].grade_code,
            'edge':      'Moulded',
            'end_type':  'Open-End',
            'belt_type': self.lookups['belt_type'].belt_type,
            'length':    100,
        }

    def _body(self, belt_lines):
        return {
            'shared': {
                'purpose_id':      self.lookups['purpose'].pk,
                'brand_id':        self.lookups['brand'].pk,
                'standard_id':     self.lookups['standard'].pk,
                'reel_type_id':    self.lookups['reel_type'].pk,
                'packing_type_id': self.lookups['packing_type'].pk,
            },
            'customer':   {'customer_name': 'Test Customer Co'},
            'belt_lines': belt_lines,
        }

    def test_bare_rating_text_resolves_successfully(self):
        response = self.client.post(TEXT_IMPORT_URL, self._body([self._line('315/3')]), format='json')
        self.assertEqual(response.status_code, 201, response.data)

    def test_full_rating_text_still_resolves_successfully(self):
        response = self.client.post(TEXT_IMPORT_URL, self._body([self._line('EP 315/3')]), format='json')
        self.assertEqual(response.status_code, 201, response.data)

    def test_belt_description_shows_fabric_and_rating_as_separate_tokens(self):
        response = self.client.post(TEXT_IMPORT_URL, self._body([self._line('315/3')]), format='json')
        self.assertEqual(response.status_code, 201, response.data)

        record = TDSInput.objects.get(pk=response.data['tds_records'][0]['tds_id'])
        fabric_code = self.lookups['fabric_type'].fabric_code
        self.assertIn(f'X {fabric_code} X 315/3', record.belt_description)
