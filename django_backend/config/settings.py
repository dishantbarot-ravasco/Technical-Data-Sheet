"""
Django settings for TDS Automation App — Django Backend
Reads credentials from tds_app/.env (same file FastAPI uses).
"""
import os
from pathlib import Path
from datetime import timedelta

import dj_database_url
from dotenv import load_dotenv

# ── Paths ──────────────────────────────────────────────────────────────────────
# django_backend/config/settings.py
#   BASE_DIR  = django_backend/
#   TDS_APP_DIR = tds_app/
BASE_DIR    = Path(__file__).resolve().parent.parent
TDS_APP_DIR = BASE_DIR.parent

# Load .env from tds_app/ — same file FastAPI uses
load_dotenv(TDS_APP_DIR / '.env')

# ── Core ───────────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ['TDS_SECRET_KEY']
# Default to 'production' so a missing .env never silently enables debug mode.
# Set APP_ENV=development in .env to turn on the dev conveniences below.
DEBUG      = os.environ.get('APP_ENV', 'production') == 'development'

# SECURITY (fixed): '.onrender.com' used to be included as a wildcard,
# accepting a Host header for ANY Render subdomain (including someone else's
# app, or a throwaway app an attacker provisions) rather than just this one.
# RENDER_EXTERNAL_HOSTNAME (set automatically by Render at runtime) already
# gives the exact host this deployment is actually served on, so the
# wildcard was both redundant and unnecessarily broad.
ALLOWED_HOSTS = [
    'localhost',
    '127.0.0.1',
    os.environ.get('RENDER_EXTERNAL_HOSTNAME', ''),          # specific Render host
]
ALLOWED_HOSTS = [h for h in ALLOWED_HOSTS if h]             # remove empty strings

# ── Installed Apps ─────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',

    # Local apps
    'apps.core',
    'apps.api',
]

# ── Middleware ─────────────────────────────────────────────────────────────────
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',          # must be first
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware', 
    'config.security_headers.SecurityHeadersMiddleware',    
    'django.contrib.sessions.middleware.SessionMiddleware', # serves frontend static files
    'django.middleware.common.CommonMiddleware',
    # Full CsrfViewMiddleware is intentionally NOT used app-wide:
    # every /api/ endpoint authenticates via JWT bearer token / httpOnly
    # tds_access cookie (SameSite=Lax), never Django's session CSRF token, and
    # the global middleware doesn't know that — it 403s every unsafe-method API
    # call regardless of auth type. That's what broke the API the last time the
    # standard CsrfViewMiddleware was added here.
    #
    # AdminOnlyCsrfMiddleware (config/middleware.py) restores the exact same,
    # unmodified Django CSRF check but only for requests under
    # /internal-mgmt-rvsc/ (Django Admin) — a real session+form app that
    # already renders {% csrf_token %} everywhere and expects this check.
    # Every /api/ request skips it entirely, so API behavior is unchanged;
    # Admin no longer relies solely on its URL being obscure.
    'config.middleware.AdminOnlyCsrfMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Dev: force browser to always fetch fresh assets (mirrors FastAPI NoCacheMiddleware)
if DEBUG:
    MIDDLEWARE.insert(1, 'config.middleware.NoCacheMiddleware')

# ── URL & WSGI ─────────────────────────────────────────────────────────────────
ROOT_URLCONF      = 'config.urls'
WSGI_APPLICATION  = 'config.wsgi.application'

# Disable automatic trailing-slash redirects so that POST /api/tds and
# POST /api/users/ (etc.) are not 301-redirected and lose their request body.
# Our URL patterns are explicit about trailing slashes where they differ
# (e.g. GET /api/tds vs POST /api/tds/ is intentional).
APPEND_SLASH = False

# ── Database ───────────────────────────────────────────────────────────────────
# Same PostgreSQL instance the FastAPI app uses.
# conn_max_age=600 keeps connections alive for 10 min (connection pooling).
DATABASES = {
    'default': dj_database_url.config(
        default=os.environ['DATABASE_URL'],
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# ── Templates ──────────────────────────────────────────────────────────────────
# Django templates are used for Django Admin only.
# PDF generation uses Jinja2 directly via pdf_renderer.py (not Django TEMPLATES).
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# ── Static & Frontend ──────────────────────────────────────────────────────────
# WhiteNoise serves two sets of files:
#   1. WHITENOISE_ROOT (frontend/)  — served at root paths: /index.html, /home.html, etc.
#   2. STATIC_ROOT (staticfiles/)   — served at /django-static/ (Django admin assets)
#
# In development, Django's `serve` view in config/urls.py acts as a fallback
# (the re_path at the bottom of urlpatterns). In production WhiteNoise intercepts
# requests at the middleware level before URLs are even matched, so it is faster
# and the serve fallback is never reached for static files.
STATIC_URL     = '/django-static/'
STATIC_ROOT    = BASE_DIR / 'staticfiles'          # target for collectstatic
FRONTEND_DIR   = TDS_APP_DIR / 'frontend'
WHITENOISE_ROOT = str(FRONTEND_DIR)               # serves frontend/ at root URL paths

# ── Django REST Framework ──────────────────────────────────────────────────────
REST_FRAMEWORK = {
    # Phase 5: cookie-first JWT auth (tries httpOnly cookie, falls back to Bearer header).
    # TDSCookieJWTAuthentication extends TDSJWTAuthentication so Bearer-based calls
    # (API tools, older sessions) continue to work without any changes.
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.api.auth_backend.TDSCookieJWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    # Human-readable errors in DEBUG mode; JSON-only in production
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
        *(['rest_framework.renderers.BrowsableAPIRenderer'] if DEBUG else []),
    ],
    'EXCEPTION_HANDLER': 'apps.api.exceptions.custom_exception_handler',
    # Prevent DRF from intercepting ?format=html as a content-type negotiation
    # parameter. Without this, ?format=html causes DRF to look for a renderer
    # with .format == 'html' (none exist), returning 404 before the view runs.
    # Our pdf_views.py reads request.GET.get('format', 'pdf') directly.
    'URL_FORMAT_OVERRIDE': None,

    # ── Rate limiting ──────────────────────────────────────────────────────────
    # Scoped throttles are applied per-view using custom throttle classes.
    # Rates: login=5/min, otp_request=3/min, otp_verify=10/min.
    # The general anon/user fallbacks cover all other endpoints.
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon':        '60/minute',   # general unauthenticated fallback
        'user':        '200/minute',  # general authenticated fallback
        'login':       '5/minute',    # POST /api/auth/login
        'otp_request': '3/minute',    # POST /api/auth/request-otp
        'otp_verify':  '10/minute',   # POST /api/auth/verify-otp
    },
}

# ── SimpleJWT ──────────────────────────────────────────────────────────────────
# Matches FastAPI: HS256, 12h TTL, same signing key.
#
# KEY FIELD NOTES:
#   USER_ID_FIELD  — field on TDSUser that holds the PK  → 'user_id'
#   USER_ID_CLAIM  — claim simplejwt writes in the token → kept as 'user_id'
#                    (our serializer also writes 'sub' for FastAPI compatibility;
#                     TDSJWTAuthentication reads 'sub', not 'user_id')
# SECURITY (fixed): JWT_SIGNING_KEY defaults to SECRET_KEY for zero-downtime
# compatibility with existing deployments (changing it invalidates every
# currently-issued token, forcing a re-login), but it's a distinct env var
# now, so it CAN be set independently going forward — SECRET_KEY also signs
# Django's session/CSRF/password-reset tokens, so reusing it for JWTs means a
# leak or weakness in one context compromises the other. Set JWT_SIGNING_KEY
# in the environment (e.g. `python -c "import secrets; print(secrets.token_urlsafe(64))"`)
# to fully separate them.
JWT_SIGNING_KEY = os.environ.get('JWT_SIGNING_KEY', SECRET_KEY)

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':      timedelta(hours=12),
    # 30 days -- backs the persistent 'remember me' tds_refresh cookie
    # (see device_service.py#set_refresh_cookie). A trusted device can now
    # stay signed in for up to 30 days without re-entering the password;
    # logout still clears this cookie immediately (device_views.logout_view).
    'REFRESH_TOKEN_LIFETIME':     timedelta(days=30),
    'ALGORITHM':                  'HS256',
    'SIGNING_KEY':                JWT_SIGNING_KEY,
    'AUTH_HEADER_TYPES':          ('Bearer',),
    'USER_ID_FIELD':              'user_id',   # ← TDSUser PK field name
    'USER_ID_CLAIM':              'user_id',   # ← claim simplejwt auto-sets
    'AUTH_TOKEN_CLASSES':         ('rest_framework_simplejwt.tokens.AccessToken',),
    'UPDATE_LAST_LOGIN':          False,
    'USER_AUTHENTICATION_RULE':   'apps.api.auth_backend.tds_user_authentication_rule',
}

# ── CORS ───────────────────────────────────────────────────────────────────────
# SECURITY: never wildcard-allow origins while credentials are allowed (that lets
# django-cors-headers reflect ANY Origin back with credentials attached -- a full
# CORS/CSRF bypass). Local dev works fine off the explicit allowlist below (it
# already includes the Live Server ports), so we no longer tie this to DEBUG.
_render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '')
CORS_ALLOW_ALL_ORIGINS  = False
CORS_ALLOWED_ORIGINS    = [
    'http://localhost:5500',
    'http://127.0.0.1:5500',
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    *([f'https://{_render_host}'] if _render_host else []),
]
CORS_ALLOW_CREDENTIALS  = True

# ── Google OAuth 2.0 ───────────────────────────────────────────────────────────
# Set these in your .env (dev) or Render environment variables (production).
GOOGLE_CLIENT_ID          = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET      = os.environ.get('GOOGLE_CLIENT_SECRET', '')
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get(
    'GOOGLE_OAUTH_REDIRECT_URI',
    'http://127.0.0.1:8000/api/auth/google/callback/',
)

# ── Session ────────────────────────────────────────────────────────────────────
# DB-backed sessions required for the Google OAuth PKCE code_verifier round-trip
# and for the pending_user_id stored during device-verify OTP flow.
# SESSION_SAVE_EVERY_REQUEST is essential: without it, sessions modified inside
# the google_login view may not be committed before the HttpResponseRedirect
# leaves our domain, causing a state-mismatch on callback.
SESSION_ENGINE             = 'django.contrib.sessions.backends.db'
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_HTTPONLY    = True
SESSION_COOKIE_SAMESITE    = 'Lax'
SESSION_COOKIE_SECURE      = not DEBUG   # True in production (HTTPS)

# ── JWT access cookie (Phase 5) ───────────────────────────────────────────────
# httpOnly cookie that carries the JWT access token after 2FA completion.
# TDSCookieJWTAuthentication reads this cookie on every authenticated request.
TDS_COOKIE_NAME     = 'tds_access'   # must match the name used in set_cookie()
TDS_COOKIE_SAMESITE = 'Lax'          # safe for same-origin; blocks cross-site POST
TDS_COOKIE_SECURE   = not DEBUG      # True in production (HTTPS), False in dev

# ── Device cookie ──────────────────────────────────────────────────────────────
# The `tds_device` httpOnly cookie that identifies a trusted browser/device.
# Must only be sent over HTTPS in production.
TDS_DEVICE_COOKIE_SECURE = not DEBUG

# ── CSRF (Django Admin only — see AdminOnlyCsrfMiddleware) ────────────────────
# Only ever set/checked for requests under /internal-mgmt-rvsc/; the JWT API
# never touches this cookie. Kept HttpOnly=False (Django's default) since the
# admin's own {% csrf_token %} template tag needs no JS cookie access anyway.
CSRF_COOKIE_SECURE   = not DEBUG   # True in production (HTTPS)
CSRF_COOKIE_SAMESITE = 'Lax'

# ── Authentication Backends ────────────────────────────────────────────────────
# TDSUserBackend: authenticates via email + bcrypt against the `users` table.
# ModelBackend:   kept so Django Admin (superuser) still works.
AUTHENTICATION_BACKENDS = [
    'apps.api.auth_backend.TDSUserBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# ── Email / SMTP ───────────────────────────────────────────────────────────────
# Used by the OTP / password-reset service.
# Port 465 = SSL  |  Port 587 = STARTTLS
_smtp_port            = int(os.environ.get('SMTP_PORT', '465'))
EMAIL_HOST            = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
EMAIL_PORT            = _smtp_port
EMAIL_HOST_USER       = os.environ.get('SMTP_USER', '')
EMAIL_HOST_PASSWORD   = os.environ.get('SMTP_PASS', '')
EMAIL_USE_SSL         = _smtp_port == 465
EMAIL_USE_TLS         = _smtp_port == 587
DEFAULT_FROM_EMAIL    = os.environ.get('SMTP_FROM', EMAIL_HOST_USER)
# SECURITY/RELIABILITY (fixed): Django's SMTP backend has no timeout by
# default (timeout=None -> blocks on the OS's own TCP timeout, which can be
# minutes). request_otp/login/password-reset all send mail synchronously
# inside the request, so a slow or unreachable mail server previously meant
# that request -- and, on the single-worker dev server, every other request
# -- hung until the connection eventually timed out or the client gave up.
# A fixed timeout turns that into a fast, clean failure (still surfaced to
# the user as an error, and to the console OTP fallback in DEBUG) instead of
# an indefinite hang.
EMAIL_TIMEOUT         = 10

# ── Daily report cron secret ───────────────────────────────────────────────────
# Shared secret required by GET/POST /api/internal/send-daily-report/ (see
# apps/api/routers/reports_views.py). Set this in Render's env vars and give
# the SAME value only to your external scheduler (cron-job.org, GitHub Actions
# secret, etc.) — never expose it to the frontend/browser. Left blank by
# default, which makes the endpoint refuse every request until it's set.
REPORT_CRON_SECRET = os.environ.get('REPORT_CRON_SECRET', '')

# ── Security Headers ───────────────────────────────────────────────────────────
# SECURE_PROXY_SSL_HEADER tells Django to trust the X-Forwarded-Proto header
# that Render's load balancer sets when terminating TLS.
SECURE_BROWSER_XSS_FILTER   = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS              = 'DENY'
if not DEBUG:
    SECURE_PROXY_SSL_HEADER        = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_HSTS_SECONDS            = 31536000      # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD            = True

# ── Internationalisation ───────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'Asia/Kolkata'
USE_I18N      = True
USE_TZ        = True

# ── Misc ───────────────────────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Logging ────────────────────────────────────────────────────────────────────
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{levelname}] {asctime} {module}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class':     'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class':       'logging.handlers.RotatingFileHandler',
            'filename':    str(LOGS_DIR / 'app.log'),
            'maxBytes':    10 * 1024 * 1024,  # 10 MB per file
            'backupCount': 5,
            'formatter':   'verbose',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level':    'INFO',
    },
    'loggers': {
        'django': {
            'handlers':  ['console', 'file'],
            'level':     'INFO',
            'propagate': False,
        },
        'apps': {
            'handlers':  ['console', 'file'],
            'level':     'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
    },
}
