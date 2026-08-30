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
set_access_cookie(response, access_token)   → None (the httpOnly JWT cookie)
get_client_ip(request)                → str  (also used by apps/core/audit_log.py)
send_device_otp(user)                 → str  (plaintext OTP, already emailed)
send_new_device_notification(user, request) → None (informational only)
notify_admins_new_device_login(user, request) → None (informational only)
"""

from __future__ import annotations

import logging
import secrets
import threading

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.core.models import TrustedDevice
from apps.services.otp_service import generate_otp
from apps.services.email_service import render_email

log = logging.getLogger(__name__)

DEVICE_COOKIE_NAME    = 'tds_device'
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year in seconds

# 'Remember me' refresh-token cookie. Scoped to /api/auth/ only (path below) --
# it's never needed outside the login/refresh/logout endpoints, so there's no
# reason for the browser to attach it to every other API request.
REFRESH_COOKIE_NAME = 'tds_refresh'
REFRESH_COOKIE_PATH = '/api/auth/'


# ── Internal helpers ──────────────────────────────────────────────────────────

def get_client_ip(request) -> str:
    """
    Extract the client IP, honouring X-Forwarded-For for the app's reverse
    proxy (Render).

    SECURITY (fixed): this used to trust the FIRST (leftmost) entry of
    X-Forwarded-For, which is exactly the part of the header a client
    controls directly -- anyone can send `X-Forwarded-For: 1.2.3.4` and have
    it recorded verbatim in audit logs / "new device" security emails, with
    no proxy involved at all. Reverse proxies APPEND to this header rather
    than replacing it, so with exactly one trusted proxy in front of the app
    (Render's edge), the entry it appends -- and therefore the real peer it
    saw -- is the LAST one. This is still a heuristic (it assumes exactly one
    trusted hop); if the deployment ever sits behind an additional proxy/CDN,
    this needs to count hops accordingly rather than always taking the last.
    """
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        parts = [p.strip() for p in xff.split(',') if p.strip()]
        if parts:
            return parts[-1]
    return request.META.get('REMOTE_ADDR', '') or ''


# Backward-compatible private alias — kept so nothing else in this module
# (or importing it) needs to change.
_get_client_ip = get_client_ip


def _get_device_name(request) -> str:
    """Return the User-Agent string, truncated to 512 chars for storage."""
    return request.META.get('HTTP_USER_AGENT', 'Unknown device')[:512]


def set_access_cookie(response, access_token: str) -> None:
    """
    Set the httpOnly tds_access cookie carrying the JWT access token.

    This is the piece that was documented ("Phase 5 — httpOnly cookie auth")
    but never actually implemented anywhere reachable: TDSCookieJWTAuthentication
    (apps/api/auth_backend.py) has always been ready to *read* this cookie on
    every request, but nothing ever *wrote* it, so the app has been relying on
    the Bearer token living in the frontend's sessionStorage instead — readable
    by any script on the page (e.g. via an XSS bug). Call this from every place
    that currently returns an access_token in a login/verify response, so the
    browser gets a cookie it can't read or leak via JS, and the frontend no
    longer needs to keep a copy of the token in sessionStorage at all.

    max_age mirrors SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'] so the cookie expires
    at the same time the token inside it would stop being valid anyway.
    """
    from django.conf import settings
    max_age = int(settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds())
    response.set_cookie(
        key      = settings.TDS_COOKIE_NAME,
        value    = access_token,
        max_age  = max_age,
        httponly = True,
        secure   = settings.TDS_COOKIE_SECURE,
        samesite = settings.TDS_COOKIE_SAMESITE,
        path     = '/',
    )


# ── Public API ────────────────────────────────────────────────────────────────

def set_refresh_cookie(response, refresh_token: str) -> None:
    """
    Set the httpOnly tds_refresh cookie carrying the JWT refresh token.

    This is what lets a trusted device stay signed in past the 12h access
    token without re-entering a password: when the access token expires,
    the frontend silently POSTs this cookie to /api/auth/token/refresh to
    get a new one (see auth.js#requireAuth's recovery path and api.js's
    apiFetch retry-on-401). httpOnly + Secure + SameSite=Lax, same as
    set_access_cookie -- never readable by page JS, so an XSS bug still
    can't exfiltrate it. max_age mirrors SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].
    """
    from django.conf import settings
    max_age = int(settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds())
    response.set_cookie(
        key      = REFRESH_COOKIE_NAME,
        value    = refresh_token,
        max_age  = max_age,
        httponly = True,
        secure   = settings.TDS_COOKIE_SECURE,
        samesite = settings.TDS_COOKIE_SAMESITE,
        path     = REFRESH_COOKIE_PATH,
    )


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
        device = TrustedDevice.objects.only('pk').get(device_token=device_token, user_id=user_id)
        # PERF (fixed): this used to call device.save() -- a full SELECT +
        # UPDATE-every-field write -- on every single authenticated request
        # from a trusted device, purely to bump last_used_at. A targeted
        # .update() is a single lightweight UPDATE and can't clobber any other
        # field a concurrent request might be writing at the same time.
        TrustedDevice.objects.filter(pk=device.pk).update(last_used_at=timezone.now())
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
    code to the user. Returns the plaintext code (which has already been queued
    for sending — never log this value).

    The OTP is generated and committed to the DB synchronously (it must be
    checkable the instant this call returns — see the reliability comment in
    auth_serializers.py), but the actual send_mail() call runs on a background
    thread. Talking to Gmail's SMTP server routinely takes several seconds
    (observed ~4s in testing), and there is no correctness reason for the
    login request to sit blocked on that round-trip: the frontend only needs
    {status: 'device_verify'} to show the code-entry screen, not delivery
    confirmation. Previously the whole request waited on the send, which made
    the "Sign In" button appear frozen long enough that a user would
    reasonably refresh or re-click before the response ever arrived — losing
    the in-flight JS that was about to show the OTP screen, even though the
    email had already gone out. Caller-visible behavior on a send failure is
    unchanged: it was already swallowed by the caller either way (see
    auth_serializers.py's TDSTokenObtainPairSerializer.validate()).
    """
    otp  = generate_otp(user.email)        # from otp_service — handles bcrypt + DB
    name = user.full_name or user.email.split('@')[0]

    subject = 'Your TDS Login Verification Code'
    _html_body, body = render_email(
        greeting=f"Hi {name},",
        body_paragraphs=[
            "A sign-in attempt was made to your Ravasco TDS account from a new device or browser.",
            "Your one-time verification code is provided below. It expires in 10 minutes.",
        ],
        highlight_value=otp,
        highlight_label="Verification Code",
        after_highlight_paragraphs=[
            "If this was you, enter the code and this device will be remembered for future logins.",
            "If you did not attempt to sign in, please contact your administrator immediately.",
        ],
    )

    def _send():
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
            # DEV CONVENIENCE: mirrors otp_service.py's password-reset OTP,
            # which already falls back to printing the code to the console
            # when SMTP isn't configured/working in DEBUG.
            if settings.DEBUG:
                print(f"\n{'='*50}\nLogin OTP for {user.email}: {otp}\n{'='*50}\n")

    # RELIABILITY (fixed): daemon=True previously meant that if gunicorn sent
    # SIGTERM to this worker mid-send (e.g. a Render redeploy landing right
    # after a login), the interpreter could exit and abruptly kill this
    # thread before the OTP email ever left — silently dropping a
    # security-relevant notification with no retry and no log of what
    # happened. A non-daemon thread makes gunicorn's graceful shutdown wait
    # for it to finish (bounded by EMAIL_TIMEOUT=10s in settings.py) before
    # the worker process actually exits, instead of racing it. This thread
    # never touches the DB, so there's no connection-cleanup concern the way
    # there is for batch_export_views.py's job-runner thread.
    threading.Thread(target=_send, daemon=False).start()

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
    _html_body, body = render_email(
        greeting=f"Hi {name},",
        body_paragraphs=[
            "A new device was added to your Ravasco TDS account.",
            f"Device: {device_name}",
            f"IP Address: {ip}",
            f"Time: {now}",
            "This device will be trusted automatically for future logins.",
            "If this was you, no action is needed. If you did not do this, please "
            "contact your administrator immediately and change your password.",
        ],
    )

    def _send():
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

    # Backgrounded for the same reason as send_device_otp(): this fires during
    # /api/auth/device-verify, and an SMTP round-trip (~4s observed) has no
    # business making that request — and the "Verify & Sign In" button — sit
    # blocked when the caller never even looks at the result.
    # daemon=False so a worker restart mid-send doesn't silently kill this
    # notification instead of letting it finish — see send_device_otp()'s
    # comment above for the full reasoning.
    threading.Thread(target=_send, daemon=False).start()


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

    subject = f'[TDS Admin Alert] New Device Login: {name}'
    _html_body, body = render_email(
        greeting="Hi,",
        body_paragraphs=[
            "A user signed in to the Ravasco TDS system from a device that was not previously trusted.",
            f"User: {name} ({user.email}, role: {user.role})",
            f"Device: {device_name}",
            f"IP Address: {ip}",
            f"Time: {now}",
            "This device has been trusted for future logins on that account. If this "
            "looks suspicious, contact the user directly or revoke the device from the admin panel.",
        ],
    )

    def _send():
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

    # daemon=False — see send_device_otp()'s comment for why.
    threading.Thread(target=_send, daemon=False).start()
