"""
start.py — TDS Automation App entry point.

FastAPI has been retired. Django is now the single backend.

Usage (from TDS Automation App/ root, with venv active):
    python tds_app/start.py

Opens http://127.0.0.1:8000
"""
import os
import sys
from pathlib import Path

django_backend = Path(__file__).parent / 'django_backend'
sys.path.insert(0, str(django_backend))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

from django.core.management import execute_from_command_line

if __name__ == '__main__':
    execute_from_command_line(['manage.py', 'runserver', '127.0.0.1:8000'])
