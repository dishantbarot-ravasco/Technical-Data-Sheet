"""
Tests for the user-signature-image feature:
  - apps/services/signature_service.py (validation/normalization)
  - apps/api/routers/users_views.py's user_signature() (GET/PUT/DELETE)
  - apps/services/pdf_service.py embedding it into TDSDocData.prepared_by_signature

A user's signature is optional and admin-managed only (uploaded via
admin.html's user-edit modal), shown in the "Prepared By" box of every TDS
PDF that user creates.
"""
from io import BytesIO

from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from django.test import TestCase

from apps.core.models import TDSInput, TDSUser
from apps.api.tests.factories import make_user, make_tds_lookup_set
from apps.services.pdf_service import build_tds_doc_data
from apps.services.signature_service import process_signature_image, InvalidSignatureImage

TDS_CREATE_URL = '/api/tds'


def auth_client(user):
    client = APIClient()
    token = RefreshToken.for_user(user)
    token['sub'] = str(user.user_id)
    token['role'] = user.role
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    return client


def _signature_url(user_id):
    return f'/api/users/{user_id}/signature'


def _make_test_png_bytes(size=(300, 100), mode='RGBA'):
    img = Image.new(mode, size, (0, 0, 0, 0) if mode == 'RGBA' else (255, 255, 255))
    out = BytesIO()
    img.save(out, format='PNG')
    return out.getvalue()


class SignatureServiceTests(TestCase):
    def test_valid_png_is_normalized(self):
        png_bytes, content_type = process_signature_image(_make_test_png_bytes())
        self.assertEqual(content_type, 'image/png')
        img = Image.open(BytesIO(png_bytes))
        self.assertEqual(img.format, 'PNG')
        # Fit-within-canvas, never distorted/upscaled beyond the target box.
        self.assertLessEqual(img.width, 480)
        self.assertLessEqual(img.height, 160)

    def test_non_image_bytes_raise_invalid_signature_image(self):
        with self.assertRaises(InvalidSignatureImage):
            process_signature_image(b'this is not an image')

    def test_empty_bytes_raise_invalid_signature_image(self):
        with self.assertRaises(InvalidSignatureImage):
            process_signature_image(b'')

    def test_oversized_upload_rejected_before_decoding(self):
        oversized = b'\x00' * (5 * 1024 * 1024 + 1)
        with self.assertRaises(InvalidSignatureImage):
            process_signature_image(oversized)


class UserSignatureEndpointTests(TestCase):
    def setUp(self):
        self.admin   = make_user(email='admin@ravasco.com', role='admin')
        self.creator = make_user(email='creator3@ravasco.com', role='tds_creator')
        self.admin_client   = auth_client(self.admin)
        self.creator_client = auth_client(self.creator)

    def test_get_signature_404_when_none_uploaded(self):
        response = self.admin_client.get(_signature_url(self.creator.user_id))
        self.assertEqual(response.status_code, 404)

    def test_admin_can_upload_and_fetch_signature(self):
        png = _make_test_png_bytes()
        upload = self.admin_client.put(
            _signature_url(self.creator.user_id),
            {'signature': BytesIO(png)},
            format='multipart',
        )
        self.assertEqual(upload.status_code, 200, upload.data)
        self.assertTrue(upload.data['has_signature'])

        fetched = self.admin_client.get(_signature_url(self.creator.user_id))
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched['Content-Type'], 'image/png')
        # What comes back is the server's normalized PNG, not a byte-for-byte
        # echo of the upload -- just confirm it's a valid, non-empty image.
        img = Image.open(BytesIO(fetched.content))
        self.assertEqual(img.format, 'PNG')

    def test_non_admin_cannot_upload_signature(self):
        png = _make_test_png_bytes()
        response = self.creator_client.put(
            _signature_url(self.creator.user_id),
            {'signature': BytesIO(png)},
            format='multipart',
        )
        self.assertEqual(response.status_code, 403)

    def test_invalid_file_returns_400_not_500(self):
        response = self.admin_client.put(
            _signature_url(self.creator.user_id),
            {'signature': BytesIO(b'garbage-not-an-image')},
            format='multipart',
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_delete_removes_signature(self):
        png = _make_test_png_bytes()
        self.admin_client.put(
            _signature_url(self.creator.user_id), {'signature': BytesIO(png)}, format='multipart',
        )
        delete_response = self.admin_client.delete(_signature_url(self.creator.user_id))
        self.assertEqual(delete_response.status_code, 204)

        get_response = self.admin_client.get(_signature_url(self.creator.user_id))
        self.assertEqual(get_response.status_code, 404)

    def test_list_users_reports_has_signature_flag(self):
        png = _make_test_png_bytes()
        self.admin_client.put(
            _signature_url(self.creator.user_id), {'signature': BytesIO(png)}, format='multipart',
        )
        response = self.admin_client.get('/api/users')
        self.assertEqual(response.status_code, 200)
        entry = next(u for u in response.data if u['user_id'] == self.creator.user_id)
        self.assertTrue(entry['has_signature'])


class PdfSignatureEmbedTests(TestCase):
    """
    Confirms pdf_service.build_tds_doc_data() embeds the TDS creator's
    signature as a data: URI when one is on file, and leaves it None
    (falling back to the plain signature line in tds.html) otherwise.
    """
    def setUp(self):
        self.lookups = make_tds_lookup_set()
        self.creator = make_user(email='creator4@ravasco.com', role='tds_creator')
        self.client  = auth_client(self.creator)

        response = self.client.post(TDS_CREATE_URL, self.lookups['payload'], format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.tds_id = response.data['tds_id']

    def test_no_signature_on_file_leaves_field_none(self):
        doc = build_tds_doc_data(self.tds_id)
        self.assertIsNone(doc.prepared_by_signature)

    def test_signature_on_file_is_embedded_as_data_uri(self):
        png_bytes, content_type = process_signature_image(_make_test_png_bytes())
        self.creator.signature_image        = png_bytes
        self.creator.signature_content_type = content_type
        self.creator.save()

        doc = build_tds_doc_data(self.tds_id)
        self.assertIsNotNone(doc.prepared_by_signature)
        self.assertTrue(doc.prepared_by_signature.startswith('data:image/png;base64,'))
