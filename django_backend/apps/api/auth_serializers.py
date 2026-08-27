"""
apps/api/auth_serializers.py — Custom JWT token serializer.

Produces tokens whose payload matches the FastAPI format:
    { "sub": "<user_id_as_string>", "role": "<role>" }

The frontend JS reads `sub` and `role` directly from the decoded token — the
payload shape must stay identical during the migration period.

simplejwt's default TokenObtainPairSerializer sets sub = username (a string)
and adds no `role`.  We override get_token() to set:
    token['sub']  = str(user.user_id)
    token['role'] = user.role

validate() implements the device-aware 2FA gate:
  - Trusted device  → return {status: 'ok', access_token, refresh, ...}
  - New device      → send OTP, store pending_user_id in session,
                       return {status: 'device_verify'}
"""

import logging
from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.services.device_service import is_trusted_device, send_device_otp

logger = logging.getLogger(__name__)


class TDSTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Override simplejwt's token serializer to:
      1. Authenticate via email + bcrypt (TDSUserBackend)
      2. Embed sub = user_id (string) and role in the JWT payload
      3. Apply device-aware 2FA gate before issuing a full JWT
    """

    # Replace username_field so DRF knows the primary credential field name
    username_field = 'email'

    # Declare the fields we accept
    email    = serializers.EmailField(write_only=True)
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})

    @classmethod
    def get_token(cls, user):
        """Add custom claims to the token payload."""
        token = super().get_token(user)

        # Match FastAPI payload exactly
        token['sub']  = str(user.user_id)
        token['role'] = user.role

        # Extra convenience claims (read-only, not used for auth decisions)
        token['email']     = user.email
        token['full_name'] = user.full_name or ''

        return token

    def validate(self, attrs):
        email    = attrs.get('email', '').strip().lower()
        password = attrs.get('password', '')

        request = self.context.get('request')

        # authenticate() calls TDSUserBackend.authenticate(email=..., password=...)
        user = authenticate(request=request, email=email, password=password)

        if user is None:
            raise serializers.ValidationError(
                {'detail': 'Invalid email or password.'},
                code='authentication_failed',
            )

        # ── Device trust gate ─────────────────────────────────────────────────
        if is_trusted_device(request, user.user_id):
            # Known device — issue full JWT immediately
            refresh = self.get_token(user)
            logger.info("auth: trusted device login for user_id=%s", user.user_id)
            return {
                'status':       'ok',
                'access_token': str(refresh.access_token),
                'refresh':      str(refresh),
                'user_id':      user.user_id,
                'role':         user.role,
                'full_name':    user.full_name or '',
                'email':        user.email,
            }
        else:
            # New device — trigger OTP email, hold login until verified.
            #
            # RELIABILITY (fixed): pending_user_id used to be written into the
            # session only *after* send_device_otp() succeeded. send_device_otp()
            # already generates the OTP and commits its hash to otp_codes before
            # it ever attempts to email it (see device_service.send_device_otp),
            # so the code legitimately exists and is checkable the moment this
            # call returns — success or failure of the *email transport* is a
            # separate concern from whether verification can proceed. With the
            # old ordering, any transient email failure (SMTP hiccup, provider
            # outage, or simply no mail server configured in a dev/test
            # environment) permanently dead-ended the login: the client got a
            # "could not send email" error and no session, so there was no way
            # to retry verification even if the code became known through
            # another channel (server logs, an admin reading it off, the
            # DEBUG-mode console fallback used for local testing). Setting the
            # session first — before the send attempt — means a failed send
            # still surfaces its own error to the user, but doesn't also throw
            # away the one piece of state (the pending session) needed to
            # actually complete verification once the code is known.
            if request is not None:
                request.session['pending_user_id'] = user.user_id
                request.session.modified = True

            try:
                send_device_otp(user)
            except Exception as exc:
                # BUG FIX: this used to raise a ValidationError (400) here,
                # which — found by actually running the new-device login flow
                # end-to-end with SMTP unreachable — left the user completely
                # stuck. index.html's login handler only calls
                # showDeviceVerify() (which renders the OTP-entry step) when
                # login() resolves with {status: 'device_verify'}; on any
                # thrown error it just displays the message and re-enables
                # "Sign In", with no field anywhere for the user to type a
                # code into. But send_device_otp() -> generate_otp() above
                # already committed a valid, checkable OTP before the send
                # attempt (and prints it to the console in DEBUG — see
                # device_service.send_device_otp's own except block), and
                # pending_user_id was already stored in the session a few
                # lines up — so the backend was fully able to complete
                # verification, the frontend just had no way to reach that
                # screen. Return the same {status: 'device_verify'} every
                # other path returns instead of failing the request, so the
                # UI shows the OTP step regardless of whether the email
                # transport itself succeeded.
                logger.error("auth: failed to send device OTP for user_id=%s: %s", user.user_id, exc)
            else:
                logger.info("auth: new device — OTP sent for user_id=%s", user.user_id)
            return {'status': 'device_verify'}
