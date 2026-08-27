"""
apps/api/routers/device_views.py — Device trust endpoints.

Endpoints
---------
POST /api/auth/device-verify
    Verify the 6-digit email OTP sent when logging in from a new device.
    On success:
      - Creates a TrustedDevice row (permanent trust for this browser/device)
      - Sets the httpOnly `tds_device` cookie (365-day expiry)
      - Sends a 'new device signed in' notification email (informational)
      - Alerts all admin accounts of the new device login (informational)
      - Returns a full JWT (same shape as a trusted-device login)

POST /api/auth/logout
    Flushes the Django session.
    Does NOT clear the tds_device cookie — device stays trusted for next login.
    Frontend clears its sessionStorage JWT separately.
"""

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.models import TDSUser
from apps.services.otp_service import verify_otp
from apps.services.device_service import (
    register_device, send_new_device_notification, notify_admins_new_device_login,
)
from apps.api.auth_serializers import TDSTokenObtainPairSerializer

log = logging.getLogger(__name__)


class DeviceVerifyThrottle(AnonRateThrottle):
    """10 OTP attempts per minute per IP — prevents brute-force of 6-digit codes."""
    scope = 'otp_verify'


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([DeviceVerifyThrottle])
def device_verify(request):
    """
    POST /api/auth/device-verify
    Body: { "code": "123456" }

    Requires: a valid Django session containing `pending_user_id`
    (set by TDSTokenObtainPairSerializer.validate() on a new-device login attempt).

    On success, this endpoint:
      1. Verifies the 6-digit OTP against otp_codes table
      2. Registers the device (TrustedDevice row + tds_device cookie)
      3. Sends a new-device notification email
      4. Returns a full JWT so the frontend can proceed to home.html
    """
    code = request.data.get('code', '').strip()
    if not code:
        return Response({'detail': 'Verification code is required.'}, status=400)

    # Retrieve the pending user from the Django session
    user_id = request.session.get('pending_user_id')
    if not user_id:
        log.warning("device_verify: no pending_user_id in session")
        return Response(
            {'detail': 'Session expired. Please sign in again.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # Load the user record
    try:
        user = TDSUser.objects.get(pk=user_id, is_active=True)
    except TDSUser.DoesNotExist:
        return Response({'detail': 'User not found or inactive.'}, status=400)

    # Verify the OTP (bcrypt check + expiry + attempt counter in otp_service)
    if not verify_otp(user.email, code):
        log.warning("device_verify: wrong or expired code for user_id=%s", user_id)
        return Response(
            {'detail': 'Invalid or expired code. Please try again.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # OTP is valid — build JWT
    refresh  = TDSTokenObtainPairSerializer.get_token(user)
    jwt_data = {
        'status':       'ok',
        'access_token': str(refresh.access_token),
        'refresh':      str(refresh),
        'user_id':      user.user_id,
        'role':         user.role,
        'full_name':    user.full_name or '',
        'email':        user.email,
    }

    response = Response(jwt_data)

    # Register this device — creates TrustedDevice row + sets tds_device cookie
    register_device(response, user_id, request)

    # Set the httpOnly tds_access / tds_refresh cookies so the frontend can
    # rely on cookie auth instead of keeping the JWT in sessionStorage, and so
    # this device stays signed in past the 12h access token (see
    # device_service.py #set_access_cookie / #set_refresh_cookie).
    from apps.services.device_service import set_access_cookie, set_refresh_cookie
    set_access_cookie(response, jwt_data['access_token'])
    set_refresh_cookie(response, jwt_data['refresh'])

    # Send informational 'new device logged in' email (non-blocking)
    send_new_device_notification(user, request)

    # Alert admins that a new device was trusted on this account (non-blocking)
    notify_admins_new_device_login(user, request)

    # Audit: this completes a login (password + email OTP) that started back
    # in TDSLoginView — that view only logs ACTION_LOGIN for the *trusted*-
    # device branch, so this is where a new-device login gets its entry.
    from apps.core.audit_log import log_tds_action, TDSAuditLog
    log_tds_action(request, TDSAuditLog.ACTION_LOGIN, actor=user, detail='new device (email OTP verified)')

    # Clear pending session state
    request.session.pop('pending_user_id', None)

    log.info("device_verify: success user_id=%s role=%s", user_id, user.role)
    return response


@api_view(['POST'])
@permission_classes([AllowAny])
def logout_view(request):
    """
    POST /api/auth/logout
    Flushes the Django session AND clears the tds_access / tds_refresh httpOnly
    cookies.

    SECURITY (fixed): this used to only flush the Django session, which never
    held the JWT in the first place — the tds_access / tds_refresh cookies
    were left completely untouched, so a stateless JWT kept authenticating
    every request right up to its natural expiry (up to 30 days for the
    refresh cookie) even after the user clicked "Logout". Both cookies are
    now explicitly cleared here.

    Does NOT clear the tds_device cookie — device stays trusted for next
    login (skips the OTP step again), matching the existing "remember this
    browser" behavior. Frontend clears sessionStorage separately.
    """
    from django.conf import settings
    from apps.services.device_service import REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH

    # Audit: log BEFORE flushing the session / clearing cookies, while
    # request.user (resolved from the still-valid tds_access cookie earlier
    # in the request) is still available to credit as the actor.
    from apps.core.audit_log import log_tds_action, TDSAuditLog
    log_tds_action(request, TDSAuditLog.ACTION_LOGOUT, actor=getattr(request, 'user', None))

    request.session.flush()
    response = Response({'detail': 'Logged out successfully.'})
    response.delete_cookie(key=settings.TDS_COOKIE_NAME, path='/', samesite=settings.TDS_COOKIE_SAMESITE)
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH, samesite=settings.TDS_COOKIE_SAMESITE)
    return response
