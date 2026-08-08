"""
apps/api/routers/totp_views.py — TOTP 2FA verification endpoints.

Endpoints
---------
POST /api/auth/2fa/verify
    Exchange a valid pre_auth_token + TOTP code for a full JWT.
    Used after email/password or Google OAuth login when TOTP is already enrolled.

POST /api/auth/2fa/enroll-confirm
    Confirm a new TOTP enrollment: verify the first TOTP code, mark the secret
    as confirmed, and issue a full JWT.
    Used when a user has no confirmed TOTP (first login, or reset enrollment).

Both endpoints set an httpOnly cookie (Phase 5) in addition to returning
the token in the response body (backward-compat with sessionStorage approach).
"""
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from apps.core.models import TDSUser
from apps.services.totp_service import (
    PreAuthToken,
    EnrollToken,
    confirm_enrollment,
    verify_totp_code,
    make_full_jwt,
)

log = logging.getLogger(__name__)


class TOTPThrottle(AnonRateThrottle):
    """10 attempts per minute per IP — prevents brute-force of 6-digit codes."""
    scope = 'otp_verify'


def _set_auth_cookie(response, access_token_str: str) -> None:
    """
    Attach the httpOnly access-token cookie to a DRF Response.
    Called after any successful 2FA completion.
    """
    response.set_cookie(
        key      = settings.TDS_COOKIE_NAME,
        value    = access_token_str,
        max_age  = settings.TDS_COOKIE_MAX_AGE,
        httponly = settings.TDS_COOKIE_HTTPONLY,
        secure   = settings.TDS_COOKIE_SECURE,
        samesite = settings.TDS_COOKIE_SAMESITE,
        path     = '/',
    )


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([TOTPThrottle])
def verify_totp(request):
    """
    POST /api/auth/2fa/verify
    Body: { "pre_auth_token": "...", "code": "123456" }

    Verify the TOTP code for a user who already has 2FA enrolled.
    Returns a full JWT on success (and sets httpOnly cookie).
    """
    pre_auth_token_str = request.data.get('pre_auth_token', '').strip()
    code               = request.data.get('code', '').strip()

    if not pre_auth_token_str or not code:
        return Response({'detail': 'pre_auth_token and code are required.'}, status=400)

    # Validate and decode the pre-auth token
    try:
        token_obj = PreAuthToken(pre_auth_token_str)
        user_id   = int(token_obj['sub'])
    except Exception:
        log.warning("verify_totp: invalid or expired pre_auth_token")
        return Response(
            {'detail': 'Session expired. Please sign in again.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # Verify TOTP code
    if not verify_totp_code(user_id, code):
        log.warning("verify_totp: wrong code for user_id=%s", user_id)
        return Response(
            {'detail': 'Invalid or expired authenticator code. Please try again.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Fetch the user record
    try:
        user = TDSUser.objects.get(pk=user_id, is_active=True)
    except TDSUser.DoesNotExist:
        return Response({'detail': 'User not found or inactive.'}, status=400)

    jwt_data = make_full_jwt(user)
    log.info("verify_totp: 2FA success for user_id=%s role=%s", user_id, user.role)

    response = Response(jwt_data)
    _set_auth_cookie(response, jwt_data['access_token'])
    return response


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([TOTPThrottle])
def confirm_totp_enrollment(request):
    """
    POST /api/auth/2fa/enroll-confirm
    Body: { "enrollment_token": "...", "code": "123456" }

    Verify the first TOTP code, confirm the enrollment, and issue a full JWT.
    """
    enrollment_token_str = request.data.get('enrollment_token', '').strip()
    code                 = request.data.get('code', '').strip()

    if not enrollment_token_str or not code:
        return Response({'detail': 'enrollment_token and code are required.'}, status=400)

    # Validate and decode the enrollment token
    try:
        token_obj = EnrollToken(enrollment_token_str)
        user_id   = int(token_obj['sub'])
    except Exception:
        log.warning("confirm_totp_enrollment: invalid or expired enrollment_token")
        return Response(
            {'detail': 'Enrollment session expired. Please sign in again.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # Verify code and mark enrollment confirmed
    if not confirm_enrollment(user_id, code):
        log.warning("confirm_totp_enrollment: wrong code for user_id=%s", user_id)
        return Response(
            {'detail': 'Invalid authenticator code. Make sure you scanned the QR code and try again.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Fetch the user record
    try:
        user = TDSUser.objects.get(pk=user_id, is_active=True)
    except TDSUser.DoesNotExist:
        return Response({'detail': 'User not found or inactive.'}, status=400)

    jwt_data = make_full_jwt(user)
    log.info("confirm_totp_enrollment: enrollment confirmed for user_id=%s", user_id)

    response = Response(jwt_data)
    _set_auth_cookie(response, jwt_data['access_token'])
    return response
