"""
apps/api/routers/totp_urls.py — URL patterns for TOTP 2FA endpoints.

Mounted at /api/ so the full paths are:
  POST /api/auth/2fa/verify         → exchange pre_auth_token + TOTP for full JWT
  POST /api/auth/2fa/enroll-confirm → confirm enrollment + issue full JWT
"""
from django.urls import path
from .totp_views import verify_totp, confirm_totp_enrollment

urlpatterns = [
    path('auth/2fa/verify',          verify_totp,              name='totp-verify'),
    path('auth/2fa/enroll-confirm',  confirm_totp_enrollment,  name='totp-enroll-confirm'),
]
