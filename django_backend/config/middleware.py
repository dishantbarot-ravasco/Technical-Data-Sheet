"""
NoCacheMiddleware
-----------------
Development convenience: forces the browser to always fetch fresh assets
instead of serving stale cached versions.
Mirrors the NoCacheMiddleware from the FastAPI backend (backend/main.py).


AdminOnlyCsrfMiddleware
-----------------------
Restores real CSRF protection for Django Admin without touching the JWT API.

Background: CsrfViewMiddleware used to be removed from MIDDLEWARE app-wide,
reasoned as safe because every /api/ endpoint authenticates via a JWT bearer
token or the httpOnly tds_access cookie (SameSite=Lax), never via Django's
session-based CSRF token. That reasoning is correct for the API — but
Django's CsrfViewMiddleware doesn't know anything about JWT auth. It enforces
its check globally, on every unsafe-method (POST/PUT/PATCH/DELETE) request to
every view, regardless of how that view authenticates. So simply adding
CsrfViewMiddleware back to MIDDLEWARE breaks every API POST/PUT/DELETE call
(login, TDS create, batch import, etc.) with a 403 "CSRF token missing" —
this is exactly what happened last time and is why it was pulled out again.

Django Admin, meanwhile, IS a classic session + HTML-form app: its login page
and every model-add/edit form already render {% csrf_token %} and are built
assuming the standard CSRF check runs. Without CsrfViewMiddleware in the
stack, Admin has been relying solely on its URL (/internal-mgmt-rvsc/) being
obscure — no real CSRF protection at all.

This class re-enables Django's exact, unmodified CSRF check (by subclassing
CsrfViewMiddleware, not reimplementing it) but ONLY for requests under the
admin path prefix. Every /api/ request — and everything else in the app —
skips the check entirely, so nothing about existing API behavior changes.
"""
from django.middleware.csrf import CsrfViewMiddleware


class NoCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response['Pragma']        = 'no-cache'
        response['Expires']       = '0'
        return response


class AdminOnlyCsrfMiddleware(CsrfViewMiddleware):
    """CSRF enforcement scoped to Django Admin (/internal-mgmt-rvsc/) only."""

    ADMIN_PATH_PREFIX = '/internal-mgmt-rvsc/'

    def process_view(self, request, callback, callback_args, callback_kwargs):
        if not request.path.startswith(self.ADMIN_PATH_PREFIX):
            return None   # not an Admin request — skip CSRF checking entirely,
                           # exactly as if this middleware weren't installed.
        return super().process_view(request, callback, callback_args, callback_kwargs)
