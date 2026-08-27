"""
apps/api/auth_views.py — Authentication endpoints.

Endpoints
---------
POST /api/auth/login          — credentials → device-trust / email-OTP intermediate state
POST /api/auth/token/refresh  — refresh an expiring access token
POST /api/auth/token/verify   — verify a token is still valid

Logout (POST /api/auth/logout) lives in apps/api/routers/device_views.py, not
here — it needs to clear both the tds_access AND tds_refresh cookies, and it
sits next to device_verify() which shares its "pending device" session state.

Live login flow (device-trust + email OTP — see auth_serializers.py and
apps/api/routers/device_views.py):
  The login view returns ONE OF:
    { "status": "ok",             "access_token": "...", ... }   — trusted device, fully logged in
    { "status": "device_verify" }                                — new device: OTP emailed,
                                                                      caller must POST the code to
                                                                      /api/auth/device-verify

  (An earlier TOTP/authenticator-app based 2FA design was scaffolded under
  totp_service.py / totp_views.py but was never wired into apps/api/urls.py and
  has since been removed — the device-trust + email-OTP flow above is the only
  2FA this app implements.)
"""

import logging
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.views import (
    TokenRefreshView,
    TokenVerifyView,
)
from .auth_serializers import TDSTokenObtainPairSerializer

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Throttles
# ─────────────────────────────────────────────────────────────────────────────

class LoginRateThrottle(AnonRateThrottle):
    """5 login attempts per minute per IP — brute-force protection."""
    scope = 'login'


# ─────────────────────────────────────────────────────────────────────────────
# Login — returns device-trust / email-OTP intermediate state
# ─────────────────────────────────────────────────────────────────────────────

from rest_framework_simplejwt.views import TokenObtainPairView

class TDSLoginView(TokenObtainPairView):
    """
    POST /api/auth/login
    Body: { "email": "...", "password": "..." }

    Returns:
      { "status": "ok", "access_token": "...", ... }   — trusted device
      { "status": "device_verify" }                    — new device, OTP emailed

    On a trusted-device ("ok") login, this also sets the httpOnly tds_access
    cookie (see apps/services/device_service.py#set_access_cookie) so the
    frontend no longer has to keep the token in sessionStorage to stay
    authenticated — TDSCookieJWTAuthentication already reads this cookie
    first on every request, it just never had anything writing it before.
    access_token is still returned in the body too, for any API client that
    isn't a browser (Postman, scripts, etc.) and can't rely on cookies.
    """
    permission_classes  = [AllowAny]
    serializer_class    = TDSTokenObtainPairSerializer
    throttle_classes    = [LoginRateThrottle]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200 and response.data.get('status') == 'ok':
            from apps.services.device_service import set_access_cookie, set_refresh_cookie
            set_access_cookie(response, response.data['access_token'])
            if response.data.get('refresh'):
                set_refresh_cookie(response, response.data['refresh'])

            # Audit: trusted-device login (skipped OTP). New-device logins are
            # logged separately in device_views.py#device_verify, once the OTP
            # step actually completes — this branch only fires for a device
            # that was already trusted, so it's the whole login right here.
            from apps.core.audit_log import log_tds_action, TDSAuditLog
            from apps.core.models import TDSUser
            user = TDSUser.objects.filter(pk=response.data.get('user_id')).first()
            if user:
                log_tds_action(request, TDSAuditLog.ACTION_LOGIN, actor=user, detail='trusted device')
        return response


# ─────────────────────────────────────────────────────────────────────────────
# Token refresh / verify
# ─────────────────────────────────────────────────────────────────────────────

class TDSTokenRefreshView(TokenRefreshView):
    """
    POST /api/auth/token/refresh

    Now the backbone of the 'remember me' flow: the tds_refresh cookie is
    httpOnly, so page JS can't read it to put it in the request body itself --
    instead, if the body doesn't already carry a `refresh` value (the normal
    case for a same-origin browser call), we pull it from the cookie before
    handing off to simplejwt's serializer. Non-browser API clients (Postman,
    scripts) can still pass `refresh` in the body directly, same as before.

    On success, re-sets the tds_access cookie to the new token so cookie auth
    doesn't go stale independently of a Bearer-header caller's copy.
    """
    def post(self, request, *args, **kwargs):
        from apps.services.device_service import REFRESH_COOKIE_NAME, set_access_cookie

        data = request.data
        if not data.get('refresh') and REFRESH_COOKIE_NAME in request.COOKIES:
            data = dict(data)
            data['refresh'] = request.COOKIES[REFRESH_COOKIE_NAME]

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)

        response = Response(serializer.validated_data, status=status.HTTP_200_OK)
        if response.data.get('access'):
            set_access_cookie(response, response.data['access'])
        return response


TDSTokenVerifyView = TokenVerifyView


# ─────────────────────────────────────────────────────────────────────────────
# /auth/me — return current user info (used by nav bar population)
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me_view(request):
    """
    GET /api/auth/me

    Returns basic profile info for the authenticated user.
    Called by auth.js#populateNavUser() on every protected page.
    """
    user = request.user
    return Response({
        'user_id':   user.user_id,
        'email':     user.email,
        'full_name': user.full_name,
        'role':      user.role,
    })
