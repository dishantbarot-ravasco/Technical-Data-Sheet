"""
apps/api/auth_views.py — Authentication endpoints.

Endpoints
---------
POST /api/auth/login          — credentials → TOTP intermediate state
POST /api/auth/token/refresh  — refresh an expiring access token
POST /api/auth/token/verify   — verify a token is still valid
POST /api/auth/logout         — clear the httpOnly JWT cookie

Phase 4 login flow:
  The login view no longer returns a full JWT.  It returns ONE OF:
    { "status": "totp_required",      "pre_auth_token": "..." }
    { "status": "totp_setup_required", "enrollment_token": "...", "qr_uri": "..." }

  The full JWT is issued only after TOTP verification:
    POST /api/auth/2fa/verify         (TDSJWTAuthentication — totp_views.py)
    POST /api/auth/2fa/enroll-confirm (TDSJWTAuthentication — totp_views.py)

Phase 5 logout:
  The httpOnly cookie (tds_access) is set by the 2FA views after successful
  verification.  Logout clears it server-side and returns 204 No Content.
"""

import logging
from django.conf import settings
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
# Login — returns TOTP intermediate state (Phase 4)
# ─────────────────────────────────────────────────────────────────────────────

from rest_framework_simplejwt.views import TokenObtainPairView

class TDSLoginView(TokenObtainPairView):
    """
    POST /api/auth/login
    Body: { "email": "...", "password": "..." }

    Returns:
      { "status": "totp_required",       "pre_auth_token": "..." }
      { "status": "totp_setup_required", "enrollment_token": "...", "qr_uri": "..." }
    """
    permission_classes  = [AllowAny]
    serializer_class    = TDSTokenObtainPairSerializer
    throttle_classes    = [LoginRateThrottle]


# ─────────────────────────────────────────────────────────────────────────────
# Standard simplejwt views — no customisation needed
# ─────────────────────────────────────────────────────────────────────────────

TDSTokenRefreshView = TokenRefreshView
TDSTokenVerifyView  = TokenVerifyView


# ─────────────────────────────────────────────────────────────────────────────
# Logout — clears the httpOnly cookie (Phase 5)
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([AllowAny])  # AllowAny so expired-cookie sessions can still log out
def logout_view(request):
    """
    POST /api/auth/logout

    Deletes the httpOnly JWT cookie (tds_access) and returns 204 No Content.
    The frontend (auth.js) calls this before clearing sessionStorage and
    redirecting to the login page.

    Permission: AllowAny — we want to allow logout even if the token has
    just expired so the user is never stuck with a stale cookie.
    """
    response = Response(status=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key      = settings.TDS_COOKIE_NAME,
        path     = '/',
        samesite = settings.TDS_COOKIE_SAMESITE,
    )
    logger.info(
        'logout: cookie cleared for %s',
        getattr(request.user, 'email', 'anonymous'),
    )
    return response


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
