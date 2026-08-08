"""
ASGI config for TDS Automation — Django Backend.

Exposes the ASGI callable as the module-level variable ``application``, which
an ASGI server (e.g. uvicorn/daphne) would import and run. This project is
currently deployed with gunicorn against config.wsgi (see wsgi.py + render.yaml),
NOT this file — ASGI support is kept available for later (e.g. WebSockets or
async views) but is not part of the live request path today.
"""
import os
from django.core.asgi import get_asgi_application

# Same settings module as manage.py/wsgi.py — always config/settings.py.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
application = get_asgi_application()
