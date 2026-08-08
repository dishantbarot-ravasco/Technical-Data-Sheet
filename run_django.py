"""
run_django.py — Development server startup for Django backend.

Usage (from TDS Automation App/ root, with venv active):
    python tds_app/run_django.py              → starts on http://127.0.0.1:8000
    python tds_app/run_django.py migrate      → run migrations
    python tds_app/run_django.py <cmd>        → any manage.py command

Django is now the sole server (FastAPI has been replaced).
Default port is 8000.  This must match GOOGLE_OAUTH_REDIRECT_URI in .env
and the Authorized Redirect URI in Google Cloud Console.
"""
import os
import sys
from pathlib import Path

# ── Ensure django_backend/ is on sys.path so 'config' and 'apps' are importable
django_backend = Path(__file__).parent / 'django_backend'
sys.path.insert(0, str(django_backend))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

from django.core.management import execute_from_command_line

if __name__ == '__main__':
    # python run_django.py          → runserver on 127.0.0.1:8001
    # python run_django.py migrate  → run migrations
    # python run_django.py <cmd>    → any manage.py command
    args = sys.argv[1:]
    if not args:
        execute_from_command_line(['manage.py', 'runserver', '127.0.0.1:8000'])
    else:
        execute_from_command_line(['manage.py'] + args)
