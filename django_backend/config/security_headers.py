"""
config/security_headers.py — Adds hardened HTTP security headers to every response.

Wire into settings.py MIDDLEWARE list, AFTER WhiteNoise but BEFORE Django's
common middleware:

    MIDDLEWARE = [
        ...
        'whitenoise.middleware.WhiteNoiseMiddleware',
        'config.security_headers.SecurityHeadersMiddleware',   # ← add here
        'django.middleware.common.CommonMiddleware',
        ...
    ]

Headers added:
  X-Content-Type-Options    — prevents MIME-type sniffing (XSS vector)
  X-Frame-Options           — blocks clickjacking via <iframe>
  Referrer-Policy           — limits what URL is sent to third parties
  Permissions-Policy        — opts out of browser features the app doesn't need
  Strict-Transport-Security — HTTPS-only, 1 year, includeSubDomains (PRODUCTION only)
  Content-Security-Policy   — restricts script/style/image sources

CSP notes:
  - 'self' covers all local assets (WhiteNoise-served JS/CSS/images).
  - 'unsafe-inline' on style-src is required because the app uses inline
    <style> blocks and style="..." attributes throughout.  Remove once the
    CSS is moved to external files.
  - Google Fonts is explicitly allowed (fonts.googleapis.com, fonts.gstatic.com).
  - No external script sources are allowed — all JS is served from 'self'.
  - frame-ancestors 'none' blocks embedding in other pages (equivalent to
    X-Frame-Options: DENY but honoured by modern browsers).
  - object-src 'none' blocks Flash and other plugin objects entirely.

To customise the CSP in settings.py:
    CSP_EXTRA_DIRECTIVES = "connect-src 'self' https://api.example.com;"
"""

import os

from django.conf import settings


class SecurityHeadersMiddleware:
    """
    Attach hardened security headers to every response.
    Runs after Django generates the response, before it is sent to the client.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._is_prod = not settings.DEBUG

    def __call__(self, request):
        response = self.get_response(request)
        self._add_headers(response)
        return response

    # ── Header builders ───────────────────────────────────────────

    def _add_headers(self, response):
        h = response

        # Prevent MIME sniffing — XSS mitigation
        h['X-Content-Type-Options'] = 'nosniff'

        # Clickjacking protection (belt-and-suspenders with CSP frame-ancestors)
        h['X-Frame-Options'] = 'DENY'

        # Don't send the full URL as Referer to third-party sites
        h['Referrer-Policy'] = 'strict-origin-when-cross-origin'

        # Opt out of browser features the app doesn't use
        h['Permissions-Policy'] = (
            'camera=(), microphone=(), geolocation=(), '
            'payment=(), usb=(), bluetooth=()'
        )

        # HSTS — production only (causes issues on HTTP dev environments)
        if self._is_prod:
            h['Strict-Transport-Security'] = (
                'max-age=31536000; includeSubDomains; preload'
            )

        # Content Security Policy
        h['Content-Security-Policy'] = self._build_csp()

    def _build_csp(self):
        directives = [
            "default-src 'self'",
            # Allow inline styles (needed until inline CSS is extracted to files)
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com",
            "img-src 'self' data: blob:",          # data: URIs for logos/base64 images
            "script-src 'self'",                   # NO inline scripts, NO eval
            "connect-src 'self'",                  # fetch/XHR to same origin only
            "frame-src 'none'",                    # no iframes loaded (TDS preview uses same origin)
            "frame-ancestors 'none'",              # this page cannot be embedded anywhere
            "object-src 'none'",                   # no Flash / plugins
            "base-uri 'self'",                     # block base-tag injection
            "form-action 'self'",                  # form POST targets must be same origin
        ]

        # Allow site-specific additions from settings (e.g. analytics endpoints)
        extra = getattr(settings, 'CSP_EXTRA_DIRECTIVES', '')
        if extra:
            directives.append(extra.rstrip(';'))

        return '; '.join(directives)
