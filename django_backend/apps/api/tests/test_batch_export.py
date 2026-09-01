"""
apps/api/tests/test_batch_export.py — Integration tests for the async batch
PDF export flow (start job -> poll status -> download).

Introduced alongside apps/api/routers/batch_export_views.py, which moved
batch ZIP/merged-ZIP/print-all PDF generation off the request thread and
onto a background thread + BatchExportJob DB row (see that module's
docstring for why: WeasyPrint rendering for a whole batch can exceed
gunicorn's request timeout). This is also the first real test coverage for
any batch PDF export path — previously untested entirely.

Uses TransactionTestCase rather than TestCase: the export job runs on a real
background thread with its own DB connection, and TestCase's per-test
transaction rollback would make batch/job rows created in the test invisible
to that connection (they're never actually committed).
"""
import io
import time
import zipfile

from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from django.test import TransactionTestCase

from apps.core.models import BatchExportJob, BeltRatingValue, TDSParameter
from apps.api.tests.factories import make_user, make_tds_lookup_set

BATCH_CREATE_URL = '/api/tds/batch/'

PARAM_CARCASS_THICKNESS = 4


def _ensure_carcass_eav(lookups):
    """
    make_tds_lookup_set() only seeds an interply-skim EAV row (param_id=5),
    not a carcass-thickness one (param_id=4) — fine for create_tds (which
    takes carcass_thickness_mm straight from the request), but create_batch's
    per-belt loop always reads carcass_from_rating via _fetch_carcass_eav()
    and tds_inputs.carcass_from_rating is NOT NULL, so a belt rating with no
    param_id=4 row makes every create_batch call fail. Seed it here rather
    than in the shared factory, to avoid changing behavior for other tests.
    """
    carcass_param, _ = TDSParameter.objects.get_or_create(
        parameter_id=PARAM_CARCASS_THICKNESS,
        defaults=dict(parameter_group='Construction', parameter_name='Carcass Thickness',
                      display_order=0),
    )
    BeltRatingValue.objects.create(
        belt_rating=lookups['belt_rating'], parameter=carcass_param, indus_value='4.5',
    )


def auth_client(user):
    client = APIClient()
    token = RefreshToken.for_user(user)
    token['sub'] = str(user.user_id)
    token['role'] = user.role
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    return client


def _poll_job(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f'/api/tds/batch/export/{job_id}/status/')
        assert resp.status_code == 200, resp.content
        if resp.data['status'] in ('done', 'failed'):
            return resp.data
        time.sleep(0.5)
    raise AssertionError(f'export job {job_id} did not finish within {timeout}s')


def _belt_row(payload):
    return {
        'belt_type_id':    payload['belt_type_id'],
        'belt_width_mm':   payload['belt_width_mm'],
        'fabric_type_id':  payload['fabric_type_id'],
        'belt_rating_id':  payload['belt_rating_id'],
        'top_cover_mm':    payload['top_cover_mm'],
        'bottom_cover_mm': payload['bottom_cover_mm'],
        'cover_grade_id':  payload['cover_grade_id'],
        'belt_length_m':   payload['belt_length_m'],
    }


def _create_batch(client, lookups, customer_name):
    resp = client.post(BATCH_CREATE_URL, {
        'shared': {
            'purpose_id':  lookups['purpose'].pk,
            'brand_id':    lookups['brand'].pk,
            'standard_id': lookups['standard'].pk,
        },
        'customer': {'customer_name': customer_name},
        'belts': [_belt_row(lookups['payload'])],
    }, format='json')
    assert resp.status_code == 201, resp.content
    return resp.data['batch']['batch_id']


def _create_batch_multi(client, lookups, customer_name, belt_count):
    """Same as _create_batch(), but with N identical belt rows (none with a
    TDS Document Number) -- for exercising the zip-export filename
    collision guard, which only matters once a batch has more than one belt
    sharing the same fallback (customer-name) filename base."""
    resp = client.post(BATCH_CREATE_URL, {
        'shared': {
            'purpose_id':  lookups['purpose'].pk,
            'brand_id':    lookups['brand'].pk,
            'standard_id': lookups['standard'].pk,
        },
        'customer': {'customer_name': customer_name},
        'belts': [_belt_row(lookups['payload']) for _ in range(belt_count)],
    }, format='json')
    assert resp.status_code == 201, resp.content
    return resp.data['batch']['batch_id']


class BatchExportPermissionTests(TransactionTestCase):
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        _ensure_carcass_eav(self.lookups)
        self.creator = make_user(email='creator@ravasco.com', role='tds_creator')
        self.client = auth_client(self.creator)
        self.batch_id = _create_batch(self.client, self.lookups, 'Export Perm Co')

    def test_unauthenticated_cannot_start_export(self):
        client = APIClient()
        resp = client.post(f'/api/tds/batch/{self.batch_id}/export/', {'export_type': 'zip'}, format='json')
        self.assertEqual(resp.status_code, 401)

    def test_invalid_export_type_rejected(self):
        resp = self.client.post(f'/api/tds/batch/{self.batch_id}/export/', {'export_type': 'bogus'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_nonexistent_batch_404(self):
        resp = self.client.post('/api/tds/batch/999999/export/', {'export_type': 'zip'}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_status_for_unknown_job_404(self):
        resp = self.client.get('/api/tds/batch/export/999999/status/')
        self.assertEqual(resp.status_code, 404)

    def test_download_before_done_returns_409(self):
        # Create the job row directly (status='running') to avoid racing the
        # background thread that a real POST /export/ would kick off.
        job = BatchExportJob.objects.create(batch_id=self.batch_id, export_type='zip', status='running')
        resp = self.client.get(f'/api/tds/batch/export/{job.job_id}/download/')
        self.assertEqual(resp.status_code, 409)

    def test_download_unknown_job_404(self):
        resp = self.client.get('/api/tds/batch/export/999999/download/')
        self.assertEqual(resp.status_code, 404)


class BatchExportJobRunTests(TransactionTestCase):
    """Exercises the real background-thread render pipeline end-to-end for each export type."""

    def setUp(self):
        self.lookups = make_tds_lookup_set()
        _ensure_carcass_eav(self.lookups)
        self.creator = make_user(email='creator@ravasco.com', role='tds_creator')
        self.client = auth_client(self.creator)
        self.batch_id = _create_batch(self.client, self.lookups, 'Export Run Co')

    def _run_export(self, export_type, extra=None):
        body = {'export_type': export_type}
        body.update(extra or {})
        start = self.client.post(f'/api/tds/batch/{self.batch_id}/export/', body, format='json')
        self.assertEqual(start.status_code, 202, start.content)
        job_id = start.data['job_id']
        self.assertEqual(start.data['status'], 'pending')
        result = _poll_job(self.client, job_id)
        return job_id, result

    def test_zip_export_completes_and_downloads(self):
        job_id, result = self._run_export('zip', {'copy': 'internal'})
        self.assertEqual(result['status'], 'done', result)
        # The batch has exactly 1 belt (see _create_batch) — progress should
        # reflect that it processed that one record, not stay at 0/0.
        self.assertEqual(result['progress_current'], 1)
        self.assertEqual(result['progress_total'], 1)
        dl = self.client.get(f'/api/tds/batch/export/{job_id}/download/')
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl['Content-Type'], 'application/zip')
        self.assertGreater(len(dl.content), 0)

    def test_zip_export_names_tds_file_by_doc_number_or_customer_name(self):
        # Same base-name convention as the single-belt download
        # (pdf_service.tds_filename_base): the belt's TDS Document Number if
        # it has one, else the batch's customer name -- this batch's belt has
        # no doc number, so it falls back to the customer name given to
        # _create_batch ('Export Run Co', spaces kept -- only filesystem-
        # unsafe characters like "/" get replaced). No revision suffix
        # (unlike the single/revision downloads) — batch exports aren't
        # reached via Search TDS's edit-then-download flow.
        job_id, result = self._run_export('zip', {'copy': 'internal'})
        self.assertEqual(result['status'], 'done', result)
        dl = self.client.get(f'/api/tds/batch/export/{job_id}/download/')
        names = zipfile.ZipFile(io.BytesIO(dl.content)).namelist()
        tds_names = [n for n in names if n.endswith('.pdf') and not n.endswith('_QAP.pdf') and 'Merged' not in n]
        self.assertEqual(len(tds_names), 1, names)
        self.assertEqual(tds_names[0], 'Export Run Co.pdf')
        self.assertNotIn('_rev_', tds_names[0])

    def test_zip_export_disambiguates_belts_that_would_share_a_filename(self):
        # Two belts, same customer, neither has a TDS Document Number --
        # tds_filename_base() would return the identical customer name for
        # both. Without a uniqueness guard, the second belt's zip entry would
        # silently overwrite the first's (same name twice in one zip), and
        # most unzip tools would only ever show one of the two PDFs. The
        # tds_number-suffixed fallback (see _unique_zip_name in
        # batch_export_views.py) must kick in for the second one.
        batch_id = _create_batch_multi(self.client, self.lookups, 'Shared Customer Co', belt_count=2)
        start = self.client.post(f'/api/tds/batch/{batch_id}/export/', {'export_type': 'zip', 'copy': 'internal'}, format='json')
        self.assertEqual(start.status_code, 202, start.content)
        result = _poll_job(self.client, start.data['job_id'])
        self.assertEqual(result['status'], 'done', result)

        dl = self.client.get(f"/api/tds/batch/export/{start.data['job_id']}/download/")
        names = zipfile.ZipFile(io.BytesIO(dl.content)).namelist()
        tds_names = [n for n in names if n.endswith('.pdf') and not n.endswith('_QAP.pdf') and 'Merged' not in n]
        self.assertEqual(len(tds_names), 2, names)
        self.assertEqual(len(set(tds_names)), 2, "two belts collapsed onto the same zip entry name")
        self.assertIn('Shared Customer Co.pdf', tds_names)
        self.assertTrue(
            any(n.startswith('Shared Customer Co_') for n in tds_names if n != 'Shared Customer Co.pdf'),
            tds_names,
        )

    def test_merged_zip_export_completes_and_downloads(self):
        job_id, result = self._run_export('merged_zip', {'copy': 'internal'})
        self.assertEqual(result['status'], 'done', result)
        dl = self.client.get(f'/api/tds/batch/export/{job_id}/download/')
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl['Content-Type'], 'application/zip')

    def test_print_all_export_completes_and_downloads(self):
        job_id, result = self._run_export('print_all', {'copy': 'internal'})
        self.assertEqual(result['status'], 'done', result)
        dl = self.client.get(f'/api/tds/batch/export/{job_id}/download/')
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl['Content-Type'], 'application/pdf')

    def test_export_for_batch_with_no_records_fails_cleanly(self):
        # A batch id that exists in the DB but whose TDSInput rows were all
        # deleted — the job must reach 'failed' with a message, never hang
        # or crash the background thread silently.
        from apps.core.models import TDSInput
        TDSInput.objects.filter(batch_id=self.batch_id).delete()
        job_id, result = self._run_export('zip', {'copy': 'internal'})
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(result['error_message'])
