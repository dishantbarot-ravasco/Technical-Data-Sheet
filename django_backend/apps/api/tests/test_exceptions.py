"""
Tests for apps/api/exceptions.py's custom_exception_handler — specifically the
'response is None' branch (exceptions DRF doesn't recognize on its own), which
now classifies and describes several exception types instead of always
returning the generic 500 message.
"""
from django.db.utils import IntegrityError
from django.test import TestCase, override_settings
from rest_framework import status

from apps.api.exceptions import custom_exception_handler


class _FakeDiag:
    def __init__(self, constraint_name='', message_detail=''):
        self.constraint_name = constraint_name
        self.message_detail = message_detail


class _FakeDriverError(Exception):
    def __init__(self, diag):
        self.diag = diag


def _make_integrity_error(constraint_name='', message_detail=''):
    exc = IntegrityError('duplicate key value violates unique constraint')
    exc.__cause__ = _FakeDriverError(_FakeDiag(constraint_name, message_detail))
    return exc


class CustomExceptionHandlerTests(TestCase):
    def test_value_error_returns_400_with_real_message(self):
        response = custom_exception_handler(ValueError('Invalid belt width: -5'), {'view': None})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['detail'], 'Invalid belt width: -5')

    def test_key_error_returns_400_with_real_message(self):
        response = custom_exception_handler(KeyError('customer_id'), {'view': None})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer_id', response.data['detail'])

    def test_integrity_error_unique_violation_is_friendly(self):
        exc = _make_integrity_error(constraint_name='tds_number_unique', message_detail='Key already exists.')
        response = custom_exception_handler(exc, {'view': None})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['detail'], 'A record with this value already exists.')

    def test_integrity_error_fk_violation_is_friendly(self):
        exc = _make_integrity_error(constraint_name='tds_inputs_customer_id_fkey', message_detail='is not present in table "customers".')
        response = custom_exception_handler(exc, {'view': None})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['detail'], 'Referenced record no longer exists.')

    def test_integrity_error_not_null_violation_is_friendly(self):
        exc = _make_integrity_error(constraint_name='', message_detail='null value in column "belt_width" violates not-null constraint')
        response = custom_exception_handler(exc, {'view': None})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['detail'], 'A required field was left empty.')

    def test_integrity_error_unrecognized_falls_back_generic(self):
        exc = _make_integrity_error(constraint_name='some_other_constraint', message_detail='something else')
        response = custom_exception_handler(exc, {'view': None})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['detail'], 'Database constraint violation.')
        # Never leaks the raw SQL/exception text for unrecognized constraint shapes.
        self.assertNotIn('violates', response.data['detail'])

    @override_settings(DEBUG=False)
    def test_unexpected_exception_stays_generic_in_production(self):
        response = custom_exception_handler(AttributeError("'NoneType' object has no attribute 'x'"), {'view': None})
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.data['detail'], 'An unexpected server error occurred.')

    @override_settings(DEBUG=True)
    def test_unexpected_exception_includes_detail_in_debug(self):
        response = custom_exception_handler(AttributeError('boom'), {'view': None})
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn('AttributeError', response.data['detail'])
        self.assertIn('boom', response.data['detail'])
