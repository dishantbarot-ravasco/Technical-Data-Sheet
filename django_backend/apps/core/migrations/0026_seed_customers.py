"""
apps/core/migrations/0026_seed_customers.py

Seeds the `customers` table (737 real Ravasco customer records) — see
fixtures/0026_customers.json.

WHY THIS EXISTS: unlike 0025_seed_reference_catalog.py, `customers` was
deliberately left OUT of that migration — Customer is transactional/growing
data (new rows get added live through the generate-TDS form), not static
reference/catalog data like standards or cover grades, so it seemed wrong to
bundle it with a "reference catalog" migration. That reasoning missed that
these 737 rows ARE real business history (actual past/current customers,
verified by inspection — no test-fixture junk), not sample data, and the
team expects it present the moment the app goes live, the same way they
expect a real brand or cover grade list on day one. A fresh deploy without
this migration comes up with zero customers and forces re-creating all 737
by hand through the form one at a time.

Uses the same bulk_create + sequence-fixup approach as 0025 (not loaddata)
for consistency, even though `customers.customer_id` has no known PK/save()
mismatch bug the way the three junction tables in 0024 did — it's simply the
more efficient plain-INSERT path for 737 rows either way.

IDEMPOTENT BY DESIGN: guarded on `Customer.objects.exists()`, safe to run
for real everywhere (including this project's own database, where it
correctly detects existing data and does nothing).
"""
import os

from django.core.serializers import deserialize
from django.db import migrations


FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), 'fixtures', '0026_customers.json',
)


def load_customers(apps, schema_editor):
    Customer = apps.get_model('core', 'Customer')
    if Customer.objects.exists():
        # Already seeded — nothing to do. See module docstring.
        return

    with open(FIXTURE_PATH, encoding='utf-8') as f:
        raw = f.read()

    objs = [d.object for d in deserialize('json', raw)]
    Customer.objects.bulk_create(objs)

    # Advance customer_id's identity sequence past the seeded rows' explicit
    # PKs — see 0025_seed_reference_catalog.py's identical step for why
    # bulk_create() with explicit PKs needs this (Postgres's sequence
    # otherwise still starts at 1 and collides with the first real insert).
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
    # customers by the time anyone considers rolling this back. Matches
    # 0025_seed_reference_catalog.py's same choice.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0025_seed_reference_catalog'),
    ]

    operations = [
        migrations.RunPython(load_customers, noop_reverse),
    ]
