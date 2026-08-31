"""
apps/core/management/commands/create_admin.py

Bootstraps the very first TDSUser admin account on a database that has none.

WHY THIS EXISTS: apps/api/routers/users_views.py's create_user() is the only
other code path that creates a TDSUser, and it's gated behind @IsAdmin — a
real chicken-and-egg problem the moment `users` is genuinely empty (a fresh
production deploy). Django's own admin site can't fill this gap either:
TDSUserAdmin (apps/core/admin.py) deliberately EXCLUDES password_hash from
its form ("Never expose the password hash in the admin UI"), so there is no
way to give a TDSUser row a working, checkable password through the Django
admin at all. This command hashes the password the exact same way
create_user() does (bcrypt, 12 rounds) so the resulting row logs in through
the normal /api/auth/login flow like any other user.

Usage:
    python manage.py create_admin --email you@ravasco.com --password 'Str0ngPassw0rd!'
    python manage.py create_admin --email you@ravasco.com --password '...' --full-name "Your Name"

Refuses to run if that email already has a row (use the app's own change-password
/ admin-panel role-edit flows for an existing account instead).
"""
import bcrypt
from django.core.management.base import BaseCommand, CommandError

from apps.api.permissions import is_allowed_email_domain
from apps.core.models import TDSUser


class Command(BaseCommand):
    help = "Create the first admin TDSUser on a database that has none (bootstrap only)."

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--password', required=True)
        parser.add_argument('--full-name', default='')
        parser.add_argument('--designation', default='')

    def handle(self, *args, **options):
        email = options['email'].strip().lower()
        password = options['password']

        if not is_allowed_email_domain(email):
            raise CommandError(
                f"'{email}' is not on the allowed email domain "
                f"(see ALLOWED_EMAIL_DOMAIN in settings.py)."
            )
        if len(password) < 8:
            raise CommandError("Password must be at least 8 characters.")
        if TDSUser.objects.filter(email=email).exists():
            raise CommandError(
                f"A user with email '{email}' already exists — this command is "
                f"for bootstrapping the first account only."
            )

        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('utf-8')
        user = TDSUser.objects.create(
            email=email,
            password_hash=password_hash,
            full_name=options['full_name'] or None,
            designation=options['designation'] or None,
            role='admin',
            is_active=True,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Created admin user_id={user.user_id} ({user.email}). "
            f"You can now sign in at /index.html — the first login from a new "
            f"device still goes through email OTP verification as normal."
        ))
