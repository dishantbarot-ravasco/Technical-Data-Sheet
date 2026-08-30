"""
config/urls.py — Root URL configuration for TDS Automation — Django Backend.

This is the top of the URL-resolution tree: every incoming request is matched
against `urlpatterns` below, top to bottom, first match wins. Three
sub-systems plug in here:
  1. Django Admin       (apps/core's models, via django.contrib.admin)
  2. The DRF API        (apps/api/urls.py — see that file for the full endpoint list)
  3. The static frontend (plain HTML/CSS/JS in tds_app/frontend/, no build step)

URL structure (originally mirrored the old FastAPI app's routing):
  /                         → redirects to frontend/index.html  (login page)
  /home.html                → frontend/home.html
  /internal-mgmt-rvsc/      → Django admin panel (obscure path — do not publicise)
  /api/...                  → DRF API (all endpoints, see apps/api/urls.py)
  /<anything else>          → served as a static file from frontend/ if it exists

In production, WhiteNoise's middleware (see config/middleware.py's position in
settings.MIDDLEWARE) intercepts most static-file requests before they even
reach this URLconf, so the catch-all `serve` view below is mainly a
development-time fallback.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import RedirectView
from django.views.static import serve


# Frontend directory (tds_app/frontend/) — defined once in settings.py as
# FRONTEND_DIR and reused here so this file doesn't hardcode a path.
FRONTEND_DIR = settings.FRONTEND_DIR


urlpatterns = [
    # Root ("/") → redirect straight to the login page. 302, not permanent,
    # so browsers don't cache this redirect if the target ever changes.
    path('', RedirectView.as_view(url='/index.html', permanent=False)),

    # Django Admin — deliberately mounted at an obscure path instead of the
    # Django default '/admin/' to cut down on automated bot/scanner traffic.
    # Only share this URL with team members who actually need admin access.
    # (CSRF protection for this path specifically comes from
    # AdminOnlyCsrfMiddleware — see config/middleware.py.)
    path('internal-mgmt-rvsc/', admin.site.urls),

    # Every /api/... request is delegated to apps/api/urls.py, which in turn
    # includes one router module per feature area (tds, batch, users, etc.).
    # See that file's module docstring for the full endpoint map and the
    # ordering rules that keep more-specific paths from being swallowed by
    # <int:id>-style wildcard patterns.
    path('api/', include('apps.api.urls')),

    # Catch-all: anything not matched above is looked up as a literal file
    # inside frontend/ (index.html, home.html, js/api.js, css/style.css, …).
    # This replicates what the old FastAPI app's StaticFiles mount did, and
    # is what lets this be a single-process deployment with no separate
    # frontend build/host step.
    re_path(r'^(?P<path>.+)$', serve, {'document_root': str(FRONTEND_DIR)}),
]
