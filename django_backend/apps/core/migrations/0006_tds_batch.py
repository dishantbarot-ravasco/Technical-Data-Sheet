"""
Migration 0006 — TDS Batch support

What this does
──────────────
1. Creates the `tds_batch` table (Django-managed, owned by this migration).
2. Adds a nullable `batch_id` column to the existing `tds_inputs` table via
   RunSQL — because tds_inputs is managed=False (Django never runs DDL on it
   directly), the only safe way to add a column is a raw ALTER TABLE.

Rollback safety
───────────────
- `tds_batch` is dropped by `reverse_sql` for CreateModel (automatic).
- The batch_id column is dropped by the RunSQL reverse_sql.
- All existing tds_inputs rows are untouched (batch_id defaults to NULL).
- The single-belt create_tds workflow is completely unaffected.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_fix_fk_warnings'),
    ]

    operations = [

        # ── 1. Create tds_batch table ─────────────────────────────────────────
        migrations.CreateModel(
            name='TDSBatch',
            fields=[
                ('batch_id', models.AutoField(primary_key=True, serialize=False)),
                ('make_of_fabric', models.TextField(default='MIT')),
                ('splicing_required', models.BooleanField(default=False)),
                ('vulcanization_method', models.TextField(blank=True, null=True)),
                ('reel_type', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='batches',
                    to='core.reeltype',
                )),
                ('packing_type', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='batches',
                    to='core.packingtype',
                )),
                ('shipping_region', models.TextField(blank=True, null=True)),
                ('created_by_id', models.IntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'tds_batch',
                'managed': True,
            },
        ),

        # ── 2. Add batch_id column to tds_inputs (managed=False table) ────────
        # IF NOT EXISTS guards against a re-run on a DB that already has it.
        # The FK reference to tds_batch is safe: tds_batch is created above
        # in the same transaction.
        migrations.RunSQL(
            sql="""
                ALTER TABLE tds_inputs
                ADD COLUMN IF NOT EXISTS batch_id INTEGER
                REFERENCES tds_batch(batch_id) ON DELETE SET NULL;
            """,
            reverse_sql="""
                ALTER TABLE tds_inputs
                DROP COLUMN IF EXISTS batch_id;
            """,
        ),
    ]
