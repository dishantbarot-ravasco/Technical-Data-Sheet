"""
Migration 0021 — Fix the stale chk_user_role check constraint.

BUG: the `users` table's chk_user_role constraint was created outside Django
(part of the original pre-migration schema) as
    CHECK (role = ANY (ARRAY['admin', 'user', 'viewer']))
back when the non-admin/non-viewer role was literally named 'user'. The
application layer was renamed to 'tds_creator' at some point (see
apps/core/models.py's TDSUser.role default, apps/api/permissions.py's
IsEditor/IsCreator, and apps/api/routers/users_views.py's _VALID_ROLES) but
this DB constraint was never updated to match — no migration ever touched it.

IMPACT: any attempt to create (or update) a user with role='tds_creator' —
the default role, and the one every "create user" flow uses for a non-admin
account — fails with a raw psycopg2.errors.CheckViolation / 500, because the
DB still only permits 'user'. Confirmed against the live dev DB: it holds
exactly one row (the admin), and zero tds_creator/viewer accounts.

FIX: drop the stale constraint and recreate it with the roles the
application actually uses today.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0020_fix_qap_textile_fabric_sn'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE users DROP CONSTRAINT IF EXISTS chk_user_role;
                ALTER TABLE users ADD CONSTRAINT chk_user_role
                    CHECK (role = ANY (ARRAY['admin', 'tds_creator', 'viewer']));
            """,
            reverse_sql="""
                ALTER TABLE users DROP CONSTRAINT IF EXISTS chk_user_role;
                ALTER TABLE users ADD CONSTRAINT chk_user_role
                    CHECK (role = ANY (ARRAY['admin', 'user', 'viewer']));
            """,
        ),
    ]
