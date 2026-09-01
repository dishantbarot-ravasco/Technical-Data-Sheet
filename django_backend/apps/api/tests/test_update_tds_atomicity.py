"""
Regression test for the _update_tds() atomicity fix (apps/api/routers/tds_views.py).

_update_tds() creates a TDSRevision snapshot and then calls record.save() as
two separate statements. If record.save() raised (e.g. a DB constraint
violation), the TDSRevision row was already permanently committed even
though the edit it describes never actually applied -- a revision-history
entry for a change that never happened, and current_revision left out of
sync with the max revision number actually in tds_revisions. Wrapping both
in one transaction.atomic() block means either both persist or neither does.
"""
from unittest.mock import patch

from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from django.test import TestCase
from django.utils import timezone

from apps.core.models import TDSInput, TDSRevision
from apps.api.tests.factories import make_user, make_tds_lookup_set

TDS_CREATE_URL = '/api/tds'


def auth_client(user):
    client = APIClient()
    token = RefreshToken.for_user(user)
    token['sub'] = str(user.user_id)
    token['role'] = user.role
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    return client


class UpdateTdsAtomicityTests(TestCase):
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        self.creator = make_user(role='tds_creator')
        self.client = auth_client(self.creator)

        # Create the record via the real (unpatched) create_tds path first.
        response = self.client.post(TDS_CREATE_URL, self.lookups['payload'], format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.tds_id = response.data['tds_id']

        # first_downloaded_at must be non-NULL for _update_tds to take the
        # TDSRevision-snapshot branch at all (pre-issue edits are skipped).
        record = TDSInput.objects.get(pk=self.tds_id)
        record.first_downloaded_at = timezone.now()
        record.save()

    def test_save_failure_rolls_back_the_revision_snapshot_too(self):
        revisions_before = TDSRevision.objects.filter(tds_id=self.tds_id).count()
        revision_number_before = TDSInput.objects.get(pk=self.tds_id).current_revision

        changed_payload = dict(self.lookups['payload'])
        changed_payload['top_cover_mm'] = float(changed_payload['top_cover_mm']) + 1

        with patch(
            'apps.api.routers.tds_views.TDSInput.save',
            side_effect=RuntimeError('simulated DB failure on record.save()'),
        ):
            response = self.client.patch(f'{TDS_CREATE_URL}/{self.tds_id}', changed_payload, format='json')

        self.assertEqual(response.status_code, 500)

        # The TDSRevision.create() that ran just before the simulated save()
        # failure must have been rolled back along with it -- not left as an
        # orphaned snapshot of a change that was never actually applied.
        self.assertEqual(
            TDSRevision.objects.filter(tds_id=self.tds_id).count(), revisions_before
        )
        self.assertEqual(
            TDSInput.objects.get(pk=self.tds_id).current_revision, revision_number_before
        )

    def test_successful_edit_still_creates_exactly_one_revision(self):
        changed_payload = dict(self.lookups['payload'])
        changed_payload['top_cover_mm'] = float(changed_payload['top_cover_mm']) + 1

        response = self.client.patch(f'{TDS_CREATE_URL}/{self.tds_id}', changed_payload, format='json')
        self.assertEqual(response.status_code, 200, response.data)

        self.assertEqual(TDSRevision.objects.filter(tds_id=self.tds_id).count(), 1)
        self.assertEqual(TDSInput.objects.get(pk=self.tds_id).current_revision, 1)
