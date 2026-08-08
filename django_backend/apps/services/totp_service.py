"""
apps/services/totp_service.py — TOTP (Google Authenticator) 2FA service.

Provides:
  get_totp_status(user)             → 'enrolled' | 'not_enrolled'
  create_enrollment(user)           → (enrollment_token_str, qr_uri_str)
  confirm_enrollment(user_id, code) → bool  (marks confirmed in DB)
  verify_totp_code(user_id, code)   → bool
  make_pre_auth_token(user)         → str (short-lived JWT, 5 min)
  make_full_jwt(user)               → dict (access_token, refresh, user meta)

Token types
-----------
PreAuthToken — issued after password OK, before TOTP verify. 5-min TTL.
EnrollToken  — issued during TOTP enrollment. 10-min TTL.

Both are signed with the same SECRET_KEY as regular JWTs but have a
distinct `token_type` claim ('pre_auth' / 'enroll') so the TOTP verify
views can reject regular access tokens being replayed.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pyotp
from rest_framework_simplejwt.tokens import Token, RefreshToken

log = logging.getLogger(__name__)

ISSUER_NAME = 'Ravasco TDS'


# ── Short-lived intermediate token types ──────────────────────────────────────

class PreAuthToken(Token):
    """Issued after password check; frontend exchanges it for a full JWT
    by supplying a valid TOTP code at POST /api/auth/2fa/verify."""
    token_type = 'pre_auth'
    lifetime   = timedelta(minutes=5)


class EnrollToken(Token):
    """Issued when a user has no confirmed TOTP; frontend shows QR code and
    calls POST /api/auth/2fa/enroll-confirm with this token + TOTP code."""
    token_type = 'enroll'
    lifetime   = timedelta(minutes=10)


# ── TOTP status ───────────────────────────────────────────────────────────────

def get_totp_status(user) -> str:
    """
    Return 'enrolled' if the user has a confirmed TOTP secret,
    otherwise 'not_enrolled'.
    """
    from apps.core.models import UserTOTP
    try:
        rec = UserTOTP.objects.get(user_id=user.user_id)
        return 'enrolled' if rec.confirmed else 'not_enrolled'
    except UserTOTP.DoesNotExist:
        return 'not_enrolled'


# ── Enrollment ────────────────────────────────────────────────────────────────

def create_enrollment(user) -> tuple[str, str]:
    """
    Generate a fresh TOTP secret, store it as unconfirmed in DB, and return
    (enrollment_token, qr_uri) so the frontend can display a QR code.

    Any previous unconfirmed enrollment for this user is replaced.
    """
    from apps.core.models import UserTOTP

    # Wipe any incomplete prior enrollment (user refreshed the QR page, etc.)
    UserTOTP.objects.filter(user_id=user.user_id, confirmed=False).delete()

    secret = pyotp.random_base32()
    UserTOTP.objects.create(
        user_id   = user.user_id,
        secret    = secret,
        confirmed = False,
    )

    totp   = pyotp.TOTP(secret)
    qr_uri = totp.provisioning_uri(name=user.email, issuer_name=ISSUER_NAME)

    token        = EnrollToken()
    token['sub'] = str(user.user_id)
    enrollment_token = str(token)

    log.info("TOTP enrollment started for user_id=%s email=%s", user.user_id, user.email)
    return enrollment_token, qr_uri


def confirm_enrollment(user_id: int, code: str) -> bool:
    """
    Verify the given TOTP code against the user's unconfirmed secret and,
    if correct, mark the enrollment as confirmed.
    Returns True on success, False otherwise.
    """
    from apps.core.models import UserTOTP
    try:
        rec = UserTOTP.objects.get(user_id=user_id, confirmed=False)
    except UserTOTP.DoesNotExist:
        log.warning("confirm_enrollment: no pending enrollment for user_id=%s", user_id)
        return False

    totp = pyotp.TOTP(rec.secret)
    if not totp.verify(code.strip(), valid_window=1):
        log.debug("confirm_enrollment: wrong code for user_id=%s", user_id)
        return False

    rec.confirmed = True
    rec.save(update_fields=['confirmed'])
    log.info("TOTP enrollment confirmed for user_id=%s", user_id)
    return True


# ── Verification ──────────────────────────────────────────────────────────────

def verify_totp_code(user_id: int, code: str) -> bool:
    """
    Verify a TOTP code for a user who has a confirmed TOTP secret.
    Returns True on success, False otherwise.
    valid_window=1 allows one 30-second window before/after (handles clock drift).
    """
    from apps.core.models import UserTOTP
    try:
        rec = UserTOTP.objects.get(user_id=user_id, confirmed=True)
    except UserTOTP.DoesNotExist:
        log.warning("verify_totp_code: no confirmed TOTP for user_id=%s", user_id)
        return False

    totp = pyotp.TOTP(rec.secret)
    if not totp.verify(code.strip(), valid_window=1):
        log.debug("verify_totp_code: wrong code for user_id=%s", user_id)
        return False

    log.info("TOTP verified for user_id=%s", user_id)
    return True


# ── Token helpers ─────────────────────────────────────────────────────────────

def make_pre_auth_token(user) -> str:
    """Issue a short-lived pre-auth token after a successful password check."""
    token        = PreAuthToken()
    token['sub'] = str(user.user_id)
    return str(token)


def make_full_jwt(user) -> dict:
    """
    Issue a full JWT pair (access + refresh) for a user that has passed 2FA.
    Sets the same custom claims as TDSTokenObtainPairSerializer.get_token().
    Returns the dict shape that auth.js expects.
    """
    # Build refresh token with simplejwt — sets USER_ID_CLAIM automatically
    refresh = RefreshToken.for_user(user)

    # Add our custom claims to the refresh token first; access_token inherits them
    refresh['sub']       = str(user.user_id)
    refresh['role']      = user.role
    refresh['email']     = user.email
    refresh['full_name'] = user.full_name or ''

    # access_token property copies all non-system claims from refresh
    access = refresh.access_token

    return {
        'access_token': str(access),
        'refresh':      str(refresh),
        'user_id':      user.user_id,
        'role':         user.role,
        'full_name':    user.full_name or '',
        'email':        user.email,
    }
