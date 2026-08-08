"""
apps/services/device_service.py — Device trust and email OTP for new-device login.

Implements Instagram/Google-style device-aware 2FA:
  - Devices that have already verified their OTP are trusted permanently
    (until the user logs out from that device, or an admin revokes it).
  - New/unknown devices receive a 6-digit email OTP challenge before getting a JWT.
  - Google OAuth logins on new devices go through the same OTP challenge.

Public API
----------
is_trusted_device(request, user_id)   → bool
register_device(response, user_id, request) → str  (device_token)
send_device_otp(user)                 → str  (plaintext OTP, already emailed)
send_new_device_notification(user, request) → None (informational only)
notify_admins_new_device_login(user, request) → None (informational only)
"""

from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.core.models import TrustedDevice
from apps.services.otp_service import generate_otp

log = logging.getLogger(__name__)

DEVICE_COOKIE_NAME    = 'tds_device'
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year in seconds


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client_ip(request) -> str:
    """Extract real client IP; respects X-Forwarded-For for reverse-proxy setups."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''


def _get_device_name(request) -> str:
    """Return the User-Agent string, truncated to 512 chars for storage."""
    return request.META.get('HTTP_USER_AGENT', 'Unknown device')[:512]


# ── Public API ────────────────────────────────────────────────────────────────

def is_trusted_device(request, user_id: int) -> bool:
    """
    Return True if the incoming request carries a valid tds_device cookie that
    matches a TrustedDevice row owned by user_id.

    Also bumps last_used_at (via save()) so stale-device cleanup is accurate.
    """
    device_token = request.COOKIES.get(DEVICE_COOKIE_NAME, '').strip()
    if not device_token:
        return False
    try:
        device = TrustedDevice.objects.get(device_token=device_token, user_id=user_id)
        device.save()   # auto_now updates last_used_at; created_at stays (auto_now_add)
        return True
    except TrustedDevice.DoesNotExist:
        return False


def register_device(response, user_id: int, request) -> str:
    """
    Create a new TrustedDevice row and set the tds_device httpOnly cookie on
    the given DRF/Django Response object.

    Returns the plaintext device token (only for logging — never expose it).
    """
    device_token = secrets.token_hex(32)   # 64 hex chars, 256-bit entropy
    ip           = _get_client_ip(request) or None
    device_name  = _get_device_name(request)

    TrustedDevice.objects.create(
        user_id      = user_id,
        device_token = device_token,
        device_name  = device_name,
        ip_address   = ip,
    )

    response.set_cookie(
        key      = DEVICE_COOKIE_NAME,
        value    = device_token,
        max_age  = DEVICE_COOKIE_MAX_AGE,
        httponly = True,
        secure   = getattr(settings, 'TDS_DEVICE_COOKIE_SECURE', False),
        samesite = 'Lax',
        path     = '/',
    )

    log.info("register_device: new device registered user_id=%s ip=%s", user_id, ip)
    return device_token


def send_device_otp(user) -> str:
    """
    Generate a 6-digit OTP, store its hash in otp_codes, and email the plaintext
    code to the user. Returns the plaintext code (which has already been sent —
    never log this value).
    """
    otp  = generate_otp(user.email)        # from otp_service — handles bcrypt + DB
    name = user.full_name or user.email.split('@')[0]

    subject = 'Your TDS Login Verification Code'
    body = (
        f"Hi {name},\n\n"
        f"Someone is trying to sign in to your Ravasco TDS account from a new device "
        f"or browser.\n\n"
        f"Your one-time verification code is:\n\n"
        f"    {otp}\n\n"
        f"This code expires in 10 minutes.\n\n"
        f"If you made this request, enter the code and your device will be "
        f"remembered for future logins — you won't need to verify again on this device.\n\n"
        f"If you did NOT attempt to log in, please contact your administrator immediately.\n\n"
        f"— Ravasco TDS System"
    )

    try:
        send_mail(
            subject        = subject,
            message        = body,
            from_email     = settings.DEFAULT_FROM_EMAIL,
            recipient_list = [user.email],
            fail_silently  = False,
        )
        log.info("send_device_otp: verification email sent to %s", user.email)
    except Exception as exc:
        log.error("send_device_otp: email send failed for %s: %s", user.email, exc)
        raise   # caller (serializer) catches this and returns a user-friendly 400

    return otp


def send_new_device_notification(user, request) -> None:
    """
    Send a 'new device logged in' notification email after a successful device
    verification. Purely informational — errors are logged but not propagated
    (we don't want a broken notification to block the login).
    """
    ip          = _get_client_ip(request) or 'unknown'
    device_name = _get_device_name(request)[:80]   # first 80 chars in email body
    name        = user.full_name or user.email.split('@')[0]
    now         = timezone.now().strftime('%Y-%m-%d %H:%M UTC')

    subject = 'New Device Signed In to Your TDS Account'
    body = (
        f"Hi {name},\n\n"
        f"A new device was just added to your Ravasco TDS account.\n\n"
        f"  Device : {device_name}\n"
        f"  IP     : {ip}\n"
        f"  Time   : {now}\n\n"
        f"This device will be trusted automatically on all future logins.\n\n"
        f"If this was you, no action is needed.\n\n"
        f"If you did NOT do this, please contact your administrator immediately "
        f"and change your password.\n\n"
        f"— Ravasco TDS System"
    )

    try:
        send_mail(
            subject        = subject,
            message        = body,
            from_email     = settings.DEFAULT_FROM_EMAIL,
            recipient_list = [user.email],
            fail_silently  = True,   # informational — never block login on this
        )
        log.info("send_new_device_notification: sent to %s", user.email)
    except Exception as exc:
        log.warning("send_new_device_notification: failed for %s: %s", user.email, exc)


def notify_admins_new_device_login(user, request) -> None:
    """
    Instagram/Google-style admin alert: whenever ANY user (admin or not) signs
    in from a new device, email every active admin (role='admin') with who
    logged in, from what device, and from where.

    This is separate from send_new_device_notification(), which emails the
    person who logged in about their OWN new device. This one is for
    oversight — so an admin knows every time a new device gets trusted on
    the account, not just when it happens to their own login.

    Purely informational — errors are logged but never propagated (a broken
    admin alert must not block a legitimate login).
    """
    from apps.core.models import TDSUser   # local import avoids any import-cycle risk

    ip          = _get_client_ip(request) or 'unknown'
    device_name = _get_device_name(request)[:80]
    name        = user.full_name or user.email.split('@')[0]
    now         = timezone.now().strftime('%Y-%m-%d %H:%M UTC')

    admin_emails = list(
        TDSUser.objects
        .filter(role='admin', is_active=True)
        .exclude(email=user.email)   # don't double-email an admin about their own login
        .values_list('email', flat=True)
    )
    if not admin_emails:
        return

    subject = f'[TDS Admin Alert] New Device Login — {name}'
    body = (
        f"Hi,\n\n"
        f"A user just signed in to the Ravasco TDS system from a device that "
        f"was not previously trusted.\n\n"
        f"  User   : {name} ({user.email}, role: {user.role})\n"
        f"  Device : {device_name}\n"
        f"  IP     : {ip}\n"
        f"  Time   : {now}\n\n"
        f"This device has been trusted for future logins by that account. "
        f"If this looks suspicious, contact the user directly or revoke the "
        f"device from the admin panel.\n\n"
        f"— This is a system-generated email from the Ravasco TDS System."
    )

    try:
        send_mail(
            subject        = subject,
            message        = body,
            from_email     = settings.DEFAULT_FROM_EMAIL,
            recipient_list = admin_emails,
            fail_silently  = True,   # informational — never block login on this
        )
        log.info("notify_admins_new_device_login: sent to %s admin(s) re: %s", len(admin_emails), user.email)
    except Exception as exc:
        log.warning("notify_admins_new_device_login: failed re: %s: %s", user.email, exc)
