"""
apps/core/apps.py — Django AppConfig for the 'core' app.

apps.core holds the domain model layer: every ORM model in models.py (belt
standards, cover grades, TDS records, users, etc.) plus the Django Admin
registrations in admin.py. It has no views/URLs of its own — apps.api is the
HTTP layer that sits on top of these models.
"""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    """
    Registers apps.core with Django.

    label='core' is why migrations live in apps/core/migrations/ and why
    `python manage.py makemigrations` / `migrate` are invoked with the short
    name `core`, not the full dotted path `apps.core` (Django derives the app
    label from this Meta-like class attribute, not from the Python path).
    """
    default_auto_field = 'django.db.models.BigAutoField'   # new PK fields default to BigAutoField
    name  = 'apps.core'    # full Python import path
    label = 'core'         # short label used by manage.py commands and migration folder naming
    verbose_name = 'TDS Core'   # display name in Django Admin
