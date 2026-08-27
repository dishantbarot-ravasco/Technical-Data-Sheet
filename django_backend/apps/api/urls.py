"""
API URL registry — /api/ prefix is added in config/urls.py.

Phase 1:  health check
Phase 3:  auth endpoints (login, refresh, verify, device-verify, logout, Google OAuth)
Phase 4:  all FastAPI routers ported here (master, lookup, tds, packing, users, pdf)

ORDERING NOTE: lookup_urls must be included BEFORE tds_urls.
  /api/tds/lookup and /api/tds/dimensional-specs must be matched before
  the <int:tds_id> capture in tds_urls.py would (incorrectly) try to
  parse "lookup" or "dimensional-specs" as an integer.
"""
from django.urls import path, include
from . import views
from .auth_views import TDSLoginView, TDSTokenRefreshView, TDSTokenVerifyView

urlpatterns = [
    # ── Phase 1 — health check ────────────────────────────────────────────────
    path('health/', views.health_check, name='health-check'),

    # ── Phase 3 — authentication ──────────────────────────────────────────────
    path('auth/login',          TDSLoginView.as_view(),        name='auth-login'),
    path('auth/token/refresh',  TDSTokenRefreshView.as_view(), name='token-refresh'),
    path('auth/token/verify',   TDSTokenVerifyView.as_view(),  name='token-verify'),

    # ── Device trust (OTP verify + logout) ───────────────────────────────────
    path('', include('apps.api.routers.device_urls')),

    # ── Google OAuth 2.0 ──────────────────────────────────────────────────────
    path('', include('apps.api.routers.google_oauth_urls')),

    # ── Phase 4 — lookup FIRST (prevents <int:tds_id> swallowing 'lookup') ───
    path('', include('apps.api.routers.lookup_urls')),

    # ── Phase 4 — remaining routers ───────────────────────────────────────────
    path('', include('apps.api.routers.master_urls')),

    # batch_urls BEFORE tds_urls: /api/tds/batch/... must be matched before
    # tds_urls' <int:tds_id> wildcard patterns could interfere (same reasoning
    # as the lookup_urls-before-tds_urls note above). This router was fully
    # implemented (create_batch, get_batch, download_batch_zip, text_import_batch)
    # but was never included here, so every /api/tds/batch/... call 404'd —
    # this is what broke multi-belt batch preview/ZIP download on the frontend.
    path('', include('apps.api.routers.batch_urls')),

    path('', include('apps.api.routers.tds_urls')),
    path('', include('apps.api.routers.packing_urls')),
    path('', include('apps.api.routers.users_urls')),
    path('', include('apps.api.routers.pdf_urls')),
    path('', include('apps.api.routers.qap_urls')),

    # ── Free-scheduler-triggered daily report (secret-protected, no login) ────
    path('', include('apps.api.routers.reports_urls')),
]
