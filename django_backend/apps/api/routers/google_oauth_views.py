"""
apps/api/routers/google_oauth_views.py — Google OAuth 2.0 login.

Endpoints
---------
GET /api/auth/google/login/
    Redirects the browser to Google's consent screen.

GET /api/auth/google/callback/
    Google redirects here with ?code=...&state=...
    Verifies state, exchanges code, reads email, looks up TDSUser,
    then applies the device-aware 2FA gate:
      - Trusted device  → redirect to home.html with full JWT in response
      - New device      → store pending_user_id in session, redirect to
                          /index.html?step=device_verify

Design decisions
----------------
- No auto-registration: the email MUST already exist in the TDSUser table.
  If not found, the user is redirected to the login page with an error param.
- CSRF protection: the `state` parameter is stored in the Django session and
  verified on callback.
- PKCE: google-auth-oauthlib auto-generates a code_verifier in google_login.
  We save it to the session and restore it in google_callback before
  fetch_token() — otherwise Google returns "Missing code verifier".
- Device-aware 2FA: same device trust logic as email/password login.
  Google OAuth on a new device also triggers the email OTP challenge.
- Plain Django views (NOT DRF @api_view): DRF wraps the request object and
  interferes with session saves before HttpResponseRedirect. Using plain
  Django views ensures request.session.save() works correctly.
"""
import logging
import os
from urllib.parse import quote

import requests as http_requests
from django.conf import settings
from django.http import HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt

from google_auth_oauthlib.flow import Flow

from apps.core.models import TDSUser
from apps.services.device_service import is_trusted_device

# Allow google-auth-oauthlib to relax scope validation
# (Google sometimes returns full-URI scopes instead of short names)
os.environ.setdefault('OAUTHLIB_RELAX_TOKEN_SCOPE', '1')

log = logging.getLogger(__name__)

_SCOPES         = ['openid', 'email', 'profile']
_FRONTEND_LOGIN = '/index.html'


def _make_flow() -> Flow:
    """Build a google-auth-oauthlib Flow from application settings."""
    return Flow.from_client_config(
        {
            'web': {
                'client_id':     settings.GOOGLE_CLIENT_ID,
                'client_secret': settings.GOOGLE_CLIENT_SECRET,
                'auth_uri':      'https://accounts.google.com/o/oauth2/auth',
                'token_uri':     'https://oauth2.googleapis.com/token',
                'redirect_uris': [settings.GOOGLE_OAUTH_REDIRECT_URI],
            }
        },
        scopes=_SCOPES,
    )


def google_login(request):
    """
    Redirect the browser to Google's OAuth consent screen.
    Stores the OAuth `state` AND the PKCE `code_verifier` in the Django
    session for use in google_callback.
    """
    flow = _make_flow()
    flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI

    auth_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='select_account',
    )

    # Persist state + PKCE code_verifier in the session BEFORE leaving our domain.
    # google-auth-oauthlib auto-generates a code_verifier and sends
    # code_challenge=sha256(verifier) to Google.  Google will require the matching
    # code_verifier in the token-exchange step.  Since google_callback creates a
    # brand-new Flow object, it would have no verifier — we must round-trip it
    # through the session.
    request.session['google_oauth_state']         = state
    request.session['google_oauth_code_verifier'] = flow.code_verifier  # None if PKCE off
    request.session.modified = True
    request.session.save()   # force-save before the browser leaves our domain
    log.info("Google OAuth: redirecting to consent screen")
    return HttpResponseRedirect(auth_url)


@csrf_exempt
def google_callback(request):
    """
    Handle the OAuth callback from Google.

    Steps:
      1. Verify state (CSRF)
      2. Restore PKCE code_verifier from session
      3. Exchange code for access token
      4. Fetch user email from Google's userinfo endpoint
      5. Look up TDSUser — reject if not registered
      6. Check device trust:
           Trusted  → build JWT, redirect to index.html?oauth_token=...
           New      → send OTP, store pending_user_id, redirect to ?step=device_verify
    """
    # User denied access on Google's consent screen
    if request.GET.get('error'):
        log.warning("Google OAuth: user denied access (%s)", request.GET.get('error'))
        return HttpResponseRedirect(f'{_FRONTEND_LOGIN}?oauth_error=cancelled')

    code  = request.GET.get('code', '')
    state = request.GET.get('state', '')

    # CSRF guard: state must match what we stored in the session
    expected_state = request.session.pop('google_oauth_state', None)
    if not expected_state or state != expected_state:
        log.warning("Google OAuth: state mismatch — possible CSRF attack")
        return HttpResponseRedirect(f'{_FRONTEND_LOGIN}?oauth_error=state_mismatch')

    # Restore the PKCE code_verifier from the session so fetch_token can send it.
    # Without this, Google returns (invalid_grant) Missing code verifier.
    code_verifier = request.session.pop('google_oauth_code_verifier', None)

    # Exchange the authorization code for tokens
    try:
        flow = _make_flow()
        flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI
        if code_verifier:
            flow.code_verifier = code_verifier   # fetch_token passes this automatically
        flow.fetch_token(code=code)
        credentials = flow.credentials
    except Exception as exc:
        log.error("Google OAuth token exchange failed: %s", exc, exc_info=True)
        return HttpResponseRedirect(f'{_FRONTEND_LOGIN}?oauth_error=token_failed')

    # Fetch user info from Google
    try:
        resp = http_requests.get(
            'https://www.googleapis.com/oauth2/v3/userinfo',
            headers={'Authorization': f'Bearer {credentials.token}'},
            timeout=10,
        )
        resp.raise_for_status()
        userinfo       = resp.json()
        email          = userinfo.get('email', '').lower().strip()
        email_verified = userinfo.get('email_verified', False)
    except Exception as exc:
        log.error("Google OAuth userinfo fetch failed: %s", exc, exc_info=True)
        return HttpResponseRedirect(f'{_FRONTEND_LOGIN}?oauth_error=userinfo_failed')

    if not email or not email_verified:
        log.warning("Google OAuth: email missing or not verified")
        return HttpResponseRedirect(f'{_FRONTEND_LOGIN}?oauth_error=unverified_email')

    # Look up TDSUser — no auto-registration
    try:
        user = TDSUser.objects.get(email=email, is_active=True)
    except TDSUser.DoesNotExist:
        log.warning("Google OAuth: email %s not registered or inactive", email)
        return HttpResponseRedirect(f'{_FRONTEND_LOGIN}?oauth_error=not_registered')

    log.info("Google OAuth: %s authenticated, checking device trust", email)

    # ── Device trust gate ─────────────────────────────────────────────────────
    if is_trusted_device(request, user.user_id):
        # Trusted device — issue JWT and redirect straight to home
        from apps.api.auth_serializers import TDSTokenObtainPairSerializer
        refresh = TDSTokenObtainPairSerializer.get_token(user)
        access  = str(refresh.access_token)
        log.info("Google OAuth: trusted device for user_id=%s — issuing JWT", user.user_id)
        # index.html reads oauth_token, stores it in sessionStorage, then goes to home.html
        return HttpResponseRedirect(f'{_FRONTEND_LOGIN}?oauth_token={quote(access, safe="")}')
    else:
        # New device — send OTP and redirect to device-verify step
        from apps.services.device_service import send_device_otp
        try:
            send_device_otp(user)
        except Exception:
            log.error("Google OAuth: failed to send OTP to user_id=%s", user.user_id, exc_info=True)
            return HttpResponseRedirect(f'{_FRONTEND_LOGIN}?oauth_error=email_failed')

        request.session['pending_user_id'] = user.user_id
        request.session.modified = True

        log.info("Google OAuth: new device for user_id=%s — OTP sent", user.user_id)
        return HttpResponseRedirect(f'{_FRONTEND_LOGIN}?step=device_verify')
