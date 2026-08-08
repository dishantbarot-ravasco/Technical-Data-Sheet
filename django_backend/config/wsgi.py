"""
WSGI config for TDS Automation — Django Backend.

Exposes the WSGI callable as the module-level variable ``application``. This
IS the live production entry point: render.yaml's startCommand runs
    gunicorn config.wsgi:application
so every request that reaches this app in production comes through here.
Locally, `manage.py runserver` uses its own dev server instead of this file,
but the app it serves is otherwise identical.
"""
import os
from django.core.wsgi import get_wsgi_application

# Same settings module as manage.py/asgi.py — always config/settings.py.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
application = get_wsgi_application()
