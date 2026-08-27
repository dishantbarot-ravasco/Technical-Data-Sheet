"""
apps/api/auth_backend.py — Custom Django authentication backend + JWT authentication.

Three classes live here:

1. TDSUserBackend
   Django authentication backend: authenticate(request, email=..., password=...)
   Verifies bcrypt passwords against the `users` table (TDSUser model).

2. TDSJWTAuthentication
   Subclass of simplejwt's JWTAuthentication. Looks up TDSUser by the `sub`
   claim (user_id) instead of Django's auth.User.

3. TDSCookieJWTAuthentication  ← NEW (Phase 5)
   Extends TDSJWTAuthentication. Tries the httpOnly cookie first, falls back
   to the Authorization: Bearer header. This lets the old sessionStorage
   approach keep working while new logins use the secure cookie.

4. tds_user_authentication_rule
   Replaces SIMPLE_JWT['USER_AUTHENTICATION_RULE'] so simplejwt checks
   TDSUser.is_active rather than auth.User.is_active.
"""

import logging
import bcrypt
from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, AuthenticationFailed
from apps.core.models import TDSUser

logger = logging.getLogger(__name__)


def _verify_password(plain: str, hashed: str) -> bool:
    """
    Verify a bcrypt password hash using the bcrypt library directly.

    passlib 1.7.4 is incompatible with bcrypt >= 4.0 (its wrap-bug detection
    sends a >72-byte test string which bcrypt 4.x refuses).  We call bcrypt
    directly to avoid that entirely.
    """
    try:
        return bcrypt.checkpw(
            plain.encode('utf-8'),
            hashed.encode('utf-8'),
        )
    except Exception as exc:
        logger.warning('bcrypt.checkpw error: %s', exc)
        return False


def _dummy_verify() -> None:
    """Constant-time no-op to prevent user-enumeration timing attacks."""
    dummy_hash = b'$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    try:
        bcrypt.checkpw(b'dummy', dummy_hash)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. Django authentication backend (used at login time)
# ─────────────────────────────────────────────────────────────────────────────

class TDSUserBackend:
    """
    Custom auth backend: authenticate(request, email=..., password=...)

    Returns a TDSUser instance on success, None on failure.
    Django calls each backend in AUTHENTICATION_BACKENDS order and stops at
    the first non-None result.
    """

    def authenticate(self, request, email: str = None, password: str = None):
        if not email or not password:
            return None

        try:
            user = TDSUser.objects.get(email=email)
        except TDSUser.DoesNotExist:
            _dummy_verify()
            return None

        # SECURITY (fixed): this used to return immediately for an inactive
        # user without running _verify_password/_dummy_verify, which meant an
        # inactive account's login attempt returned faster than a wrong-password
        # attempt on an active account — a timing side-channel that partially
        # defeats the account-enumeration protection _dummy_verify() exists to
        # provide. Always do the bcrypt work first, then check is_active.
        password_ok = _verify_password(password, user.password_hash)

        if not user.is_active:
            return None
        if not password_ok:
            return None

        return user

    def get_user(self, user_id: int):
        """Required by Django session machinery."""
        try:
            return TDSUser.objects.get(pk=user_id)
        except TDSUser.DoesNotExist:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# 2. JWT authentication (Bearer header — existing behaviour)
# ─────────────────────────────────────────────────────────────────────────────

class TDSJWTAuthentication(JWTAuthentication):
    """
    Override simplejwt's JWTAuthentication to resolve TDSUser from the `sub`
    claim instead of Django's auth.User.
    """

    def get_user(self, validated_token):
        """Look up TDSUser by the `sub` claim (user_id as a string)."""
        try:
            user_id = int(validated_token['sub'])
        except (KeyError, ValueError, TypeError):
            raise InvalidToken('Token contains no valid `sub` claim.')

        try:
            user = TDSUser.objects.get(pk=user_id)
        except TDSUser.DoesNotExist:
            raise AuthenticationFailed('User not found.', code='user_not_found')

        if not user.is_active:
            raise AuthenticationFailed('User account is inactive.', code='user_inactive')

        return user


# ─────────────────────────────────────────────────────────────────────────────
# 3. Cookie-first JWT authentication (Phase 5)
# ─────────────────────────────────────────────────────────────────────────────

class TDSCookieJWTAuthentication(TDSJWTAuthentication):
    """
    Extend TDSJWTAuthentication to check the httpOnly cookie before
    falling back to the Authorization: Bearer header.

    Priority:
      1. Cookie named settings.TDS_COOKIE_NAME — used by all new logins
         after Phase 5 where the httpOnly cookie is set on 2FA completion.
      2. Authorization: Bearer header — backward-compat for any remaining
         sessionStorage-based calls or API testing tools.

    SameSite=Lax on the cookie means the browser will not send it on
    cross-site POST requests, which is sufficient CSRF protection for
    an internal LAN app without HTTPS. We do NOT add additional CSRF
    token enforcement here to keep the API simple.
    """

    def authenticate(self, request):
        # 1. Try httpOnly cookie
        cookie_val = request.COOKIES.get(settings.TDS_COOKIE_NAME, '')
        if cookie_val:
            try:
                validated = self.get_validated_token(cookie_val.encode('utf-8'))
                return self.get_user(validated), validated
            except Exception:
                # Invalid / expired cookie — fall through to Bearer header.
                # Do NOT raise here; a bad cookie shouldn't block Bearer-based calls.
                logger.debug("Cookie JWT invalid or expired — trying Bearer header")

        # 2. Fall back to Bearer header (existing behaviour)
        return super().authenticate(request)


# ─────────────────────────────────────────────────────────────────────────────
# 4. SimpleJWT USER_AUTHENTICATION_RULE
# ─────────────────────────────────────────────────────────────────────────────

def tds_user_authentication_rule(user) -> bool:
    """
    Called by simplejwt after get_user() to decide whether the user may
    use the token. Checks TDSUser.is_active.
    """
    return user is not None and bool(user.is_active)
