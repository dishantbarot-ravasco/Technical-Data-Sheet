"""
apps/api/routers/google_oauth_urls.py — URL patterns for Google OAuth 2.0.

Mounted at /api/ so the full paths are:
  /api/auth/google/login/      → redirect to Google consent screen
  /api/auth/google/callback/   → Google redirects here with code & state
"""
from django.urls import path
from .google_oauth_views import google_login, google_callback

urlpatterns = [
    path('auth/google/login/',    google_login,    name='google-oauth-login'),
    path('auth/google/callback/', google_callback, name='google-oauth-callback'),
]
