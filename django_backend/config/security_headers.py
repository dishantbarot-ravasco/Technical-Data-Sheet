"""
config/security_headers.py — Adds hardened HTTP security headers to every response.

Wire into settings.py MIDDLEWARE list BEFORE WhiteNoise (this ordering was
previously reversed — see the BUG FIX comment on MIDDLEWARE in settings.py
for why "after WhiteNoise" silently meant "never runs for any static HTML
page", which is most of this app's actual attack surface):

    MIDDLEWARE = [
        ...
        'config.security_headers.SecurityHeadersMiddleware',   # ← before WhiteNoise
        'whitenoise.middleware.WhiteNoiseMiddleware',
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
  - 'unsafe-inline' on style-src AND script-src is required because the
    frontend has no build step (see CLAUDE.md — static HTML+vanilla JS,
    WhiteNoise-served, no bundler/templating): every page's substantial
    logic lives in an inline <script type="module"> block or inline
    style="..." attributes. A nonce-based CSP (the usual alternative to
    'unsafe-inline') needs a per-request templating layer to stamp a fresh
    nonce into each <script> tag, which this deployment doesn't have. This
    is a real, honest trade-off, not an oversight: the CSP still blocks
    loading any script/style from an untrusted remote origin, and still
    restricts object-src/frame-ancestors/base-uri/form-action — it just
    can't stop an already-injected inline script from running. Revisit if
    the frontend ever gains a build step.
  - Google Fonts is explicitly allowed (fonts.googleapis.com, fonts.gstatic.com).
  - frame-src 'self' allows the app's own same-origin PDF-preview <iframe>s
    (generate-tds.html's src="/api/tds/{id}/pdf", tds-preview.html's
    srcdoc-based preview) — no external frame sources are allowed.
  - frame-ancestors 'none' blocks embedding in other pages (equivalent to
    X-Frame-Options: DENY but honoured by modern browsers).
  - object-src 'none' blocks Flash and other plugin objects entirely.

To customise the CSP in settings.py:
    CSP_EXTRA_DIRECTIVES = "connect-src 'self' https://api.example.com;"
"""


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
            # 'unsafe-inline' required — see module docstring's CSP notes.
            # eval() is still blocked (no 'unsafe-eval'); the codebase
            # doesn't use eval()/new Function() anywhere.
            "script-src 'self' 'unsafe-inline'",
            "connect-src 'self'",                  # fetch/XHR to same origin only
            "frame-src 'self'",                    # same-origin PDF preview iframes only
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
