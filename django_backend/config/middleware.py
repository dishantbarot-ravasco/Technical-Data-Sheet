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

# File extensions that define app behavior/contracts (which endpoints the
# frontend calls, how it parses responses, etc.) — see
# frontend_cache_headers() below for why these specifically need to always
# revalidate rather than sit on WhiteNoise's default max-age.
_ALWAYS_REVALIDATE_EXTENSIONS = ('.html', '.js', '.mjs', '.css')


def frontend_cache_headers(headers, path, url):
    """
    WHITENOISE_ADD_HEADERS_FUNCTION hook (see WHITENOISE_ROOT in settings.py).

    WhiteNoise's default Cache-Control is `max-age=60, public` for every file
    under frontend/ (there's no per-file hashing/immutable-file scheme here,
    since this app has no frontend build step). A page load within that
    60-second window reuses the browser's cached copy with no request to the
    server at all — normally harmless, but for .html/.js/.css specifically
    this is exactly what let a browser serve JS calling a since-retired API
    endpoint after a deploy removed it (see the batch-download 404 incident:
    apps/api/routers/batch_urls.py's `download-zip` route was replaced with
    the async export-job flow, but generate-tds.js's cache-busting `?v=`
    query string on the <script> tag wasn't bumped when that shipped, so nothing
    forced a fresh fetch). Overriding Cache-Control to `no-cache` for these
    extensions doesn't disable caching — it forces a conditional GET
    (If-None-Match/If-Modified-Since) on every load, so a browser always
    finds out within one request whether the server has something newer,
    instead of trusting a stale copy for up to 60 seconds after every deploy.
    Images/fonts/etc. keep the default max-age, since they don't define
    behavior and rarely change.
    """
    if path.endswith(_ALWAYS_REVALIDATE_EXTENSIONS):
        headers['Cache-Control'] = 'no-cache, public'


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
