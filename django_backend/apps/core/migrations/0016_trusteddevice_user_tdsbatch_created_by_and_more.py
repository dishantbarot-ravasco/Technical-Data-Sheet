# Hand-written migration — converts three plain IntegerField columns
# (TrustedDevice.user_id, TDSBatch.created_by_id, QAPRecord.tds_id) into real
# ForeignKey/OneToOneField relationships, now that 'users' and 'tds_inputs'
# are managed=True (see migrations 0014/0015).
#
# WHY NOT A NORMAL AUTO-GENERATED MIGRATION:
# Renaming a field AND changing its type in the same step (user_id -> user,
# created_by_id -> created_by, tds_id -> tds) is invisible to Django's
# rename-detection heuristic (it only fires for a pure rename with the field
# type unchanged), so `makemigrations` sees this as "remove field X, add
# field Y" — which would DROP each column and recreate it, destroying the
# existing user_id/created_by_id/tds_id data for every real row.
#
# SeparateDatabaseAndState fixes this cleanly: state_operations updates only
# Django's model bookkeeping (so `.user`, `.created_by`, `.tds` become valid,
# while `.user_id` / `.created_by_id` / `.tds_id` keep working exactly as
# before via Django's automatic `<fk_name>_id` accessor) with zero DDL, and
# database_operations runs the one real, safe schema change needed — ADD
# CONSTRAINT on the column that already exists — nothing is dropped, no data
# is touched, no column is recreated.
#
# Pre-flight verified (see conversation) before writing this file:
#   - zero orphaned rows in any of the three columns
#   - trusted_devices.user_id already has an index (trusted_devices_user_id_98526639)
#   - tds_batch.created_by_id has NO index yet — added below to match what a
#     normal ForeignKey field would create
#   - qap_records.tds_id already has a UNIQUE constraint (qap_records_tds_id_key)
#     matching OneToOneField's implied uniqueness — nothing more needed there

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_beltrating_fabric_type_beltratingvalue_belt_rating_and_more'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name='trusteddevice', name='user_id'),
                migrations.AddField(
                    model_name='trusteddevice',
                    name='user',
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='trusted_devices',
                        to='core.tdsuser',
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE trusted_devices "
                        "ADD CONSTRAINT trusted_devices_user_id_fk_users "
                        "FOREIGN KEY (user_id) REFERENCES users (user_id) "
                        "ON DELETE CASCADE;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE trusted_devices "
                        "DROP CONSTRAINT trusted_devices_user_id_fk_users;"
                    ),
                ),
            ],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name='tdsbatch', name='created_by_id'),
                migrations.AddField(
                    model_name='tdsbatch',
                    name='created_by',
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='batches',
                        to='core.tdsuser',
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "CREATE INDEX tds_batch_created_by_id_idx "
                        "ON tds_batch (created_by_id);"
                    ),
                    reverse_sql="DROP INDEX tds_batch_created_by_id_idx;",
                ),
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE tds_batch "
                        "ADD CONSTRAINT tds_batch_created_by_id_fk_users "
                        "FOREIGN KEY (created_by_id) REFERENCES users (user_id) "
                        "ON DELETE CASCADE;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE tds_batch "
                        "DROP CONSTRAINT tds_batch_created_by_id_fk_users;"
                    ),
                ),
            ],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name='qaprecord', name='tds_id'),
                migrations.AddField(
                    model_name='qaprecord',
                    name='tds',
                    field=models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='qap_record',
                        to='core.tdsinput',
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE qap_records "
                        "ADD CONSTRAINT qap_records_tds_id_fk_tds_inputs "
                        "FOREIGN KEY (tds_id) REFERENCES tds_inputs (tds_id) "
                        "ON DELETE CASCADE;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE qap_records "
                        "DROP CONSTRAINT qap_records_tds_id_fk_tds_inputs;"
                    ),
                ),
            ],
        ),
    ]
