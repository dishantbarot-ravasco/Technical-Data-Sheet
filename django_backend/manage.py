#!/usr/bin/env python
"""
manage.py — Django's command-line entry point for this project.

This is the file every `python manage.py <command>` call goes through:
  python manage.py runserver          → start the dev server
  python manage.py makemigrations     → generate migration files from models.py changes
  python manage.py migrate            → apply migrations to the database
  python manage.py collectstatic      → gather static files for WhiteNoise/production
  python manage.py createsuperuser    → create a Django Admin login

It does exactly one job: point Django at this project's settings module
(config/settings.py) and hand off to Django's own command-line machinery.
There is no project-specific logic here — this file is unchanged Django
boilerplate and should stay that way.
"""
import os
import sys
from pathlib import Path


def main():
    """Set the settings module, then delegate to Django's CLI dispatcher."""
    # Tells every Django API call (models, ORM, settings) which settings
    # file to use — always config/settings.py for this project.
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        # This almost always means the virtualenv isn't active, or
        # dependencies from requirements.txt haven't been installed yet.
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    # Hand off sys.argv (e.g. ['manage.py', 'migrate']) to Django, which
    # parses the command name and routes it to the matching command class.
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
