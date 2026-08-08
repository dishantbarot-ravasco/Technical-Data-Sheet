"""
apps/core/management/commands/wipe_test_tds_data.py

Deletes every TDS document generated during local testing/development,
while leaving ALL master/reference data untouched (standards, cover grades,
belt ratings, fabric types, brands, customers, users, etc.).

What this deletes:
    1. TDSInput   — every generated TDS/belt record (table: tds_inputs)
    2. TDSBatch   — every batch header from the Bulk TDS flow (table: tds_batches)

What this does NOT touch:
    - Any master/reference table (Standard, CoverGrade, BeltType, BeltRating,
      FabricType, FabricStyle, IndusBrand, Customer, ReelType, PackingType,
      ContainerType, ConstructionType, Purpose, TDSUser, ...)
    - Nothing else has a foreign key pointing AT TDSInput or TDSBatch (checked
      against apps/core/models.py), so deleting these two tables cannot cascade
      into anything else or get blocked by PROTECT anywhere.

Safety:
    - Defaults to DRY RUN — only prints counts, deletes nothing.
    - Requires --confirm to actually delete.
    - --reset-sequence additionally resets the tds_number counter (the
      year=0 sentinel row in TDSSequence) back to 0, so the next TDS created
      after the wipe starts again at "0001" instead of continuing from
      wherever it left off. Omit this if you want numbering to keep
      incrementing from where local testing left it.

Usage:
    python run_django.py wipe_test_tds_data                          # dry run (safe, default)
    python run_django.py wipe_test_tds_data --confirm                # actually delete
    python run_django.py wipe_test_tds_data --confirm --reset-sequence
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.models import TDSInput, TDSBatch, TDSSequence


class Command(BaseCommand):
    help = "Delete all TDSInput/TDSBatch test records (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true', default=False,
                             help='Actually perform the deletion. Without this, only counts are printed.')
        parser.add_argument('--reset-sequence', action='store_true', default=False,
                             help='Also reset the TDS numbering counter back to 0 (next TDS = "0001").')

    def handle(self, *args, **options):
        confirm = options['confirm']
        reset_sequence = options['reset_sequence']

        tds_count = TDSInput.objects.count()
        batch_count = TDSBatch.objects.count()
        seq = TDSSequence.objects.filter(year=0).first()
        current_last_number = seq.last_number if seq else None

        self.stdout.write(self.style.MIGRATE_HEADING("=== Current state ==="))
        self.stdout.write(f"  tds_inputs rows:  {tds_count}")
        self.stdout.write(f"  tds_batches rows: {batch_count}")
        self.stdout.write(f"  TDS number counter (year=0 sentinel): {current_last_number!r}")

        if not confirm:
            self.stdout.write(self.style.WARNING(
                "\n[DRY RUN] Nothing deleted. Re-run with --confirm to actually delete "
                "these rows. Add --reset-sequence to also restart TDS numbering at 0001."
            ))
            return

        if tds_count == 0 and batch_count == 0:
            self.stdout.write(self.style.SUCCESS("\nNothing to delete — both tables are already empty."))
        else:
            with transaction.atomic():
                deleted_tds, _ = TDSInput.objects.all().delete()
                deleted_batch, _ = TDSBatch.objects.all().delete()
                if reset_sequence and seq is not None:
                    seq.last_number = 0
                    seq.save()

            self.stdout.write(self.style.SUCCESS(
                f"\nDeleted {tds_count} tds_inputs row(s) and {batch_count} tds_batches row(s)."
            ))
            if reset_sequence:
                self.stdout.write(self.style.SUCCESS("TDS number counter reset to 0 — next TDS will be '0001'."))
            else:
                self.stdout.write(
                    f"TDS number counter left as-is at {current_last_number!r} "
                    f"— next TDS will be '{(current_last_number or 0) + 1:04d}'. "
                    f"Re-run with --reset-sequence if you want it to start at 0001 instead."
                )
