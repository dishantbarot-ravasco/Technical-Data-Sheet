"""
apps/core/migrations/0028_backfill_customers.py

Backfills the 737 real customers 0026_seed_customers.py was supposed to load
but didn't, on any database where that migration already ran.

THE BUG: 0026's guard was `if Customer.objects.exists(): return` — meant to
make it safe to re-run against this project's own local dev DB (which
already has the 737 rows). But `customers` isn't purely static reference
data the way `standards`/`cover_grades` are — real users create new customer
rows live through the generate-TDS form's "Add new customer" flow from day
one. On the actual production deploy, exactly one such row (created while
manually testing the freshly-deployed app, before this migration file even
existed) was already sitting in `customers` by the time 0026 ran — so its
"any row exists" guard saw a non-empty table and concluded "already seeded",
skipping the real load entirely and leaving just that one manual row behind.

THE FIX: bulk_create(..., ignore_conflicts=True) instead of an
exists()-based skip. This inserts every fixture row whose customer_id isn't
already present — safe to run against a database with organic rows already
in it (like production, which now has that one manual "dishant" customer at
a different customer_id than anything in the fixture), a database with none
of this data yet (a fresh environment that never got 0026's buggy version),
and a database that already has all 737 rows (this migration then inserts
nothing, since every fixture PK already exists) — so it's idempotent in the
correct sense: "make sure these rows exist", not "run only if the table
looks untouched". Reuses 0026's fixture file rather than duplicating it.
"""
import os

from django.core.serializers import deserialize
from django.db import migrations


FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), 'fixtures', '0026_customers.json',
)


def backfill_customers(apps, schema_editor):
    Customer = apps.get_model('core', 'Customer')

    with open(FIXTURE_PATH, encoding='utf-8') as f:
        raw = f.read()

    objs = [d.object for d in deserialize('json', raw)]
    Customer.objects.bulk_create(objs, ignore_conflicts=True)

    # Advance customer_id's identity sequence past the highest PK now in the
    # table (whether that came from this backfill or was already there) —
    # see 0026's identical step for why bulk_create() with explicit PKs
    # needs this.
    with schema_editor.connection.cursor() as cursor:
        table = Customer._meta.db_table
        column = Customer._meta.pk.column
        quoted_table = schema_editor.connection.ops.quote_name(table)
        quoted_column = schema_editor.connection.ops.quote_name(column)
        cursor.execute(
            f"SELECT setval(pg_get_serial_sequence(%s, %s), "
            f"(SELECT MAX({quoted_column}) FROM {quoted_table}), true)",
            [table, column],
        )


def noop_reverse(apps, schema_editor):
    # Not reversible — real TDS records may already reference these
    # customers by the time anyone considers rolling this back.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0027_fix_customers_identity_generation'),
    ]

    operations = [
        migrations.RunPython(backfill_customers, noop_reverse),
    ]
