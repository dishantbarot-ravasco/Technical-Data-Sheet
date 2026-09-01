"""
Regression tests for the batch_views.py compute_packing() silent-failure fix.

Both create_batch() and text_import_batch() used to wrap compute_packing()
in `except Exception: logger.exception(...)` and otherwise fall through with
all packing_* fields left None -- the belt row was still created inside the
enclosing transaction.atomic() block, so a batch with one belt whose packing
calculation failed (e.g. belt_length_m <= 0, which passes the earlier
"is this a number" validation but fails compute_packing's own > 0 guard)
silently produced a TDS record with no Number of Rolls / Net Weight / Gross
Weight and no error surfaced to the caller.

compute_packing only ever raises ValueError for attributable input problems,
so the fix catches ValueError specifically and re-raises it as a
ValidationError tied to the offending row -- this aborts the whole atomic
block (no half-computed record is persisted) and returns a 400 with a
message identifying which belt failed and why.
"""
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from django.test import TestCase

from apps.core.models import TDSInput, TDSBatch, TDSParameter, BeltRatingValue
from apps.api.tests.factories import make_user, make_tds_lookup_set

CREATE_BATCH_URL = '/api/tds/batch/'
TEXT_IMPORT_URL  = '/api/tds/batch/text-import/'

PARAM_CARCASS_THICKNESS = 4


def _seed_carcass_eav(belt_rating, value='4.5'):
    # batch_views.py's _fetch_carcass_eav() requires an EAV row for this
    # parameter id (the tds_inputs.carcass_from_rating column is NOT NULL);
    # make_tds_lookup_set() only seeds the interply-skim parameter, so batch
    # creation tests need this seeded separately.
    param, _ = TDSParameter.objects.get_or_create(
        parameter_id=PARAM_CARCASS_THICKNESS,
        defaults=dict(parameter_group='Construction', parameter_name='Carcass Thickness',
                      display_order=2),
    )
    BeltRatingValue.objects.create(
        belt_rating=belt_rating, parameter=param, indus_value=value,
    )


def auth_client(user):
    client = APIClient()
    token = RefreshToken.for_user(user)
    token['sub'] = str(user.user_id)
    token['role'] = user.role
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    return client


class CreateBatchPackingFailureTests(TestCase):
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        _seed_carcass_eav(self.lookups['belt_rating'])
        self.creator = make_user(email='creator@ravasco.com', role='tds_creator')
        self.client = auth_client(self.creator)

    def _belt_row(self, **overrides):
        p = self.lookups['payload']
        row = {
            'belt_type_id':         p['belt_type_id'],
            'belt_width_mm':        p['belt_width_mm'],
            'fabric_type_id':       p['fabric_type_id'],
            'belt_rating_id':       p['belt_rating_id'],
            'top_cover_mm':         p['top_cover_mm'],
            'bottom_cover_mm':      p['bottom_cover_mm'],
            'cover_grade_id':       p['cover_grade_id'],
            'belt_length_m':        100,
        }
        row.update(overrides)
        return row

    def _body(self, belts):
        return {
            'shared': {
                'purpose_id':      self.lookups['purpose'].pk,
                'brand_id':        self.lookups['brand'].pk,
                'standard_id':     self.lookups['standard'].pk,
                'reel_type_id':    self.lookups['reel_type'].pk,
                'packing_type_id': self.lookups['packing_type'].pk,
            },
            'customer': {'customer_name': 'Test Customer Co'},
            'belts': belts,
        }

    def test_zero_belt_length_returns_400_with_attributable_message(self):
        # belt_length_m=0 passes the earlier "is a number" validation but
        # fails compute_packing's own `belt_length_m > 0` guard.
        body = self._body([self._belt_row(belt_length_m=0)])
        response = self.client.post(CREATE_BATCH_URL, body, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        detail = response.data['detail']
        self.assertIn('belts[0]', detail)
        self.assertIn('Packing calculation failed', str(detail['belts[0]']))

    def test_failed_row_leaves_no_partial_batch_or_tds_records(self):
        # The whole atomic block must roll back -- no TDSBatch/TDSInput from
        # this request should exist after the 400.
        before_batches = TDSBatch.objects.count()
        before_tds     = TDSInput.objects.count()
        body = self._body([self._belt_row(belt_length_m=0)])
        response = self.client.post(CREATE_BATCH_URL, body, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TDSBatch.objects.count(), before_batches)
        self.assertEqual(TDSInput.objects.count(), before_tds)

    def test_valid_belt_still_creates_batch_successfully(self):
        body = self._body([self._belt_row()])
        response = self.client.post(CREATE_BATCH_URL, body, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        batch_id = response.data['batch']['batch_id']
        records = TDSInput.objects.filter(batch_id=batch_id)
        self.assertEqual(records.count(), 1)
        self.assertIsNotNone(records.first().num_rolls)


class TextImportBatchPackingFailureTests(TestCase):
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        _seed_carcass_eav(self.lookups['belt_rating'])
        self.creator = make_user(email='creator2@ravasco.com', role='tds_creator')
        self.client = auth_client(self.creator)

    def _belt_line(self, length=100):
        return {
            'width':     1000,
            'fabric':    self.lookups['fabric_type'].fabric_code,
            'rating':    self.lookups['belt_rating'].rating_name,
            'top':       3,
            'bottom':    1.5,
            'grade':     self.lookups['cover_grade'].grade_code,
            'edge':      'Moulded',
            'end_type':  'Open-End',
            'belt_type': self.lookups['belt_type'].belt_type,
            'length':    length,
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
            'customer':    {'customer_name': 'Test Customer Co'},
            'belt_lines':  belt_lines,
        }

    def test_zero_length_line_returns_400_not_silent_success(self):
        body = self._body([self._belt_line(length=0)])
        response = self.client.post(TEXT_IMPORT_URL, body, format='json')
        # Either the text-line-level validator rejects length<=0 (400 under
        # 'belt_lines'), or it reaches compute_packing and is rejected there
        # (400 under 'belts[0]') -- either way this must NOT be a 201 with a
        # silently-null packing row.
        self.assertEqual(response.status_code, 400, response.data)
