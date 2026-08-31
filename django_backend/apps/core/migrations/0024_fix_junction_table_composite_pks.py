"""
apps/core/migrations/0024_fix_junction_table_composite_pks.py

Fixes a schema bug in three junction/lookup tables — purpose_belt_type,
brand_belt_type, brand_parameters — found while building the next migration
(0025_seed_reference_catalog) to seed a fresh database's reference data.

THE BUG: models.py declares each table's brand/purpose FK as
`OneToOneField(primary_key=True)`, and 0001_initial.py's CreateModel takes
that literally, generating a real single-column PostgreSQL PRIMARY KEY
constraint on just that column. But the REAL, currently-running schema for
these tables (created outside Django originally — see 0001_initial.py's own
header comment on why these tables were "provisioned externally") has a
genuinely COMPOSITE primary key: `(purpose_id, belt_id)`,
`(brand_id, belt_id)`, and `(brand_id, parameter_id)` respectively —
verified directly against pg_constraint on the project's working database.

A single-column PRIMARY KEY on e.g. brand_id structurally allows only ONE
brand_parameters row per brand — but the real catalog needs ~50 (one per
displayed parameter). This has been invisible until now because nothing at
runtime ever calls .save()/loaddata on these rows (they're read-only
lookups) — .filter()/.get() reads don't care what Django thinks the
"primary key" is. It surfaced only when 0025_seed_reference_catalog tried to
load real data into a genuinely fresh database (a first-time Render deploy,
a new developer's DB, CI's from-scratch Postgres) and Postgres correctly
rejected the second, third, ... row for the same brand/purpose under the
wrong single-column constraint.

Deliberately NOT a model.py change: making Django's OWN concept of "this
model's primary key" match reality would need composite primary key support
(new/still-maturing in Django 5.2) and would touch code that reads
`.pk`/`.purpose_id` semantics elsewhere. Nothing in this codebase needs
Django's ORM-level notion of pk to be composite — it only needs the DATABASE
constraint to correctly allow multiple rows per brand/purpose, which is a
pure DDL fix. Hence SeparateDatabaseAndState: the migration STATE (what
Django believes the model looks like, for makemigrations diffing) is left
completely unchanged, matching models.py exactly as before; only the
DATABASE gets the corrected constraint.

IDEMPOTENT BY DESIGN: each fix function inspects the live pg_constraint
catalog and skips straight past any table that already has the correct
2-column primary key — true for this project's own database today (its
physical schema was never wrong; only a from-scratch `migrate` would have
recreated the bug). Safe to run unconditionally everywhere.
"""
from django.db import migrations


# (table, pk_columns) — order doesn't matter, no FK relationships between
# these three tables.
_TABLES = [
    ('purpose_belt_type', ['purpose_id', 'belt_id']),
    ('brand_belt_type',   ['brand_id', 'belt_id']),
    ('brand_parameters',  ['brand_id', 'parameter_id']),
]


def fix_composite_pks(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for table, pk_columns in _TABLES:
            cursor.execute(
                """
                SELECT array_agg(a.attname ORDER BY a.attnum)
                FROM pg_constraint con
                JOIN pg_class rel      ON rel.oid = con.conrelid
                JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, attnum_ord) ON true
                JOIN pg_attribute a    ON a.attrelid = rel.oid AND a.attnum = k.attnum
                WHERE rel.relname = %s AND con.contype = 'p'
                """,
                [table],
            )
            row = cursor.fetchone()
            current_columns = row[0] if row and row[0] else []

            if sorted(current_columns) == sorted(pk_columns):
                # Already correct (e.g. this project's own database, whose
                # physical schema was never actually wrong) — nothing to do.
                continue

            constraint_name = f"{table}_pkey"
            columns_sql = ', '.join(pk_columns)
            cursor.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{constraint_name}"')
            cursor.execute(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{constraint_name}" PRIMARY KEY ({columns_sql})'
            )


def noop_reverse(apps, schema_editor):
    # Not reversible to the broken single-column constraint on purpose —
    # there would be no correct single row per brand/purpose to reverse to
    # once real data exists. See module docstring.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_add_batch_export_job_progress'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(fix_composite_pks, noop_reverse),
            ],
            state_operations=[],   # model state is already correct — see docstring
        ),
    ]
