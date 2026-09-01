"""
Integration tests for the login -> device-verify -> cookie -> protected
endpoint flow (apps/api/auth_views.py, device_views.py, auth_backend.py).

Covers the device-aware 2FA gate end to end: a brand-new device must pass
through email-OTP verification before it gets a JWT / httpOnly cookie, and a
device that has already verified once skips straight to a trusted login.
"""
import re

from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import TrustedDevice, OTPCode
from apps.api.tests.factories import make_user

LOGIN_URL = '/api/auth/login'
DEVICE_VERIFY_URL = '/api/auth/device-verify'
LOGOUT_URL = '/api/auth/logout'
TDS_LIST_URL = '/api/tds/'
TOKEN_REFRESH_URL = '/api/auth/token/refresh'


def _extract_otp_from_outbox():
    body = mail.outbox[-1].body
    match = re.search(r'\b(\d{6})\b', body)
    assert match, f"No 6-digit OTP found in email body: {body!r}"
    return match.group(1)


class LoginTests(TestCase):
    def setUp(self):
        # DRF's login throttle (5/min) is backed by Django's cache, which is
        # NOT part of the per-test DB transaction rollback — without this,
        # tests in this class trip each other's throttle counter.
        cache.clear()
        self.client = APIClient()
        self.password = 'Str0ngPassw0rd!'
        self.user = make_user(password=self.password)

    def test_wrong_password_returns_error(self):
        response = self.client.post(LOGIN_URL, {
            'email': self.user.email, 'password': 'wrong-password',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_unknown_email_does_not_leak_which_field_was_wrong(self):
        response = self.client.post(LOGIN_URL, {
            'email': 'nobody@ravasco.com', 'password': 'whatever',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.post(LOGIN_URL, {
            'email': self.user.email, 'password': self.password,
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_new_device_triggers_otp_challenge_not_a_jwt(self):
        response = self.client.post(LOGIN_URL, {
            'email': self.user.email, 'password': self.password,
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'device_verify')
        self.assertNotIn('access_token', response.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(OTPCode.objects.filter(email=self.user.email).exists())


class DeviceVerifyAndTrustedLoginTests(TestCase):
    def setUp(self):
        cache.clear()  # see LoginTests.setUp — throttle counters live in the cache
        self.client = APIClient()
        self.password = 'Str0ngPassw0rd!'
        self.user = make_user(password=self.password)

    def _login_new_device(self):
        return self.client.post(LOGIN_URL, {
            'email': self.user.email, 'password': self.password,
        }, format='json')

    def test_full_new_device_flow_sets_cookies_and_grants_access(self):
        self._login_new_device()
        otp = _extract_otp_from_outbox()

        verify_response = self.client.post(DEVICE_VERIFY_URL, {'code': otp}, format='json')
        self.assertEqual(verify_response.status_code, 200)
        self.assertEqual(verify_response.data['status'], 'ok')
        self.assertIn('access_token', verify_response.data)

        # httpOnly JWT + device-trust cookies must both be set.
        self.assertIn('tds_access', verify_response.cookies)
        self.assertIn('tds_device', verify_response.cookies)
        self.assertTrue(TrustedDevice.objects.filter(user_id=self.user.user_id).exists())

        # Cookie alone (no Authorization header) must now authenticate.
        protected = self.client.get(TDS_LIST_URL)
        self.assertEqual(protected.status_code, 200)

    def test_wrong_otp_code_is_rejected(self):
        self._login_new_device()
        response = self.client.post(DEVICE_VERIFY_URL, {'code': '000000'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TrustedDevice.objects.filter(user_id=self.user.user_id).exists())

    def test_device_verify_without_prior_login_session_is_rejected(self):
        response = self.client.post(DEVICE_VERIFY_URL, {'code': '123456'}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_otp_is_single_use(self):
        self._login_new_device()
        otp = _extract_otp_from_outbox()
        first = self.client.post(DEVICE_VERIFY_URL, {'code': otp}, format='json')
        self.assertEqual(first.status_code, 200)

        # A second, still-unverified device (no tds_device cookie carried
        # over — a fresh client) logging in as the same user gets its own
        # pending session; replaying the FIRST device's already-used OTP
        # against it must fail, since the OTP row was deleted on first use.
        other_device_client = APIClient()
        other_device_client.post(LOGIN_URL, {
            'email': self.user.email, 'password': self.password,
        }, format='json')
        second_attempt_reuses_old_code = other_device_client.post(
            DEVICE_VERIFY_URL, {'code': otp}, format='json'
        )
        self.assertEqual(second_attempt_reuses_old_code.status_code, 400)

    def test_second_login_from_now_trusted_device_skips_otp(self):
        self._login_new_device()
        otp = _extract_otp_from_outbox()
        self.client.post(DEVICE_VERIFY_URL, {'code': otp}, format='json')

        # Same client (carries the tds_device cookie set above) logs in again.
        second_login = self._login_new_device()
        self.assertEqual(second_login.status_code, 200)
        self.assertEqual(second_login.data['status'], 'ok')
        self.assertIn('access_token', second_login.data)


class ProtectedEndpointAuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_user()

    def test_no_credentials_returns_401(self):
        response = self.client.get(TDS_LIST_URL)
        self.assertEqual(response.status_code, 401)

    def test_bearer_token_still_works_without_cookie(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = RefreshToken.for_user(self.user)
        token['sub'] = str(self.user.user_id)
        token['role'] = self.user.role

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
        response = self.client.get(TDS_LIST_URL)
        self.assertEqual(response.status_code, 200)

    def test_invalid_cookie_falls_back_to_bearer_header(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = RefreshToken.for_user(self.user)
        token['sub'] = str(self.user.user_id)
        token['role'] = self.user.role

        self.client.cookies['tds_access'] = 'garbage-not-a-jwt'
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
        response = self.client.get(TDS_LIST_URL)
        self.assertEqual(response.status_code, 200)


class TokenRefreshTests(TestCase):
    """
    Regression test for a production incident: POST /api/auth/token/refresh
    crashed every request with
        FieldError: Cannot resolve keyword 'user_id' into field. Choices are:
        date_joined, email, first_name, groups, id, is_active, ... (auth.User's
        fields, NOT TDSUser's)
    Stock simplejwt's TokenRefreshSerializer re-checks the user is active via
    get_user_model().objects.get(user_id=...) -- get_user_model() resolves
    Django's AUTH_USER_MODEL, which this app leaves at its default (auth.User)
    since it uses TDSUser (a plain, unrelated model) for everything real.
    TDSTokenRefreshSerializer (auth_serializers.py) fixes this by resolving
    TDSUser directly, the same way every other authentication path in this
    app already does.
    """

    def setUp(self):
        self.client = APIClient()

    def test_valid_refresh_token_returns_new_access_token(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        user = make_user()
        refresh = RefreshToken.for_user(user)

        response = self.client.post(TOKEN_REFRESH_URL, {'refresh': str(refresh)}, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('access', response.data)

    def test_refresh_token_for_inactive_user_is_rejected_not_500(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        user = make_user()
        refresh = RefreshToken.for_user(user)
        user.is_active = False
        user.save()

        response = self.client.post(TOKEN_REFRESH_URL, {'refresh': str(refresh)}, format='json')

        # Must be a clean 401 (simplejwt's AuthenticationFailed), never a 500.
        self.assertEqual(response.status_code, 401, response.data)

    def test_refresh_pulls_token_from_cookie_when_body_omits_it(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        user = make_user()
        refresh = RefreshToken.for_user(user)

        self.client.cookies['tds_refresh'] = str(refresh)
        response = self.client.post(TOKEN_REFRESH_URL, {}, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('access', response.data)


class LogoutTests(TestCase):
    def setUp(self):
        cache.clear()  # see LoginTests.setUp — throttle counters live in the cache
        self.client = APIClient()
        self.password = 'Str0ngPassw0rd!'
        self.user = make_user(password=self.password)

    def test_logout_clears_access_cookie(self):
        self.client.post(LOGIN_URL, {
            'email': self.user.email, 'password': self.password,
        }, format='json')
        otp = _extract_otp_from_outbox()
        self.client.post(DEVICE_VERIFY_URL, {'code': otp}, format='json')

        response = self.client.post(LOGOUT_URL, {}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.cookies['tds_access'].value, '')

        # Access cookie now blank -> protected endpoint requires a fresh login.
        protected = self.client.get(TDS_LIST_URL)
        self.assertEqual(protected.status_code, 401)
