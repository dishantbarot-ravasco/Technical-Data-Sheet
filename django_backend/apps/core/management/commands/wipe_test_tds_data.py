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
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
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

        # SECURITY: this command has no way to tell "test" rows apart from real
        # customer TDS history -- it deletes EVERY row in both tables. Refuse to
        # even consider running it against what looks like a production
        # deployment (DEBUG=False) unless an operator has explicitly opted in
        # via env var, on top of --confirm.
        if confirm and not settings.DEBUG and not os.environ.get('TDS_ALLOW_DESTRUCTIVE_COMMANDS'):
            raise CommandError(
                "Refusing to run: DEBUG=False (this looks like production) and "
                "TDS_ALLOW_DESTRUCTIVE_COMMANDS is not set. If you really intend to "
                "wipe TDS data on this environment, set "
                "TDS_ALLOW_DESTRUCTIVE_COMMANDS=1 in the environment and re-run."
            )

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

        if tds_count > 0 or batch_count > 0:
            # SECURITY: typed confirmation, not just a boolean flag -- forces the
            # operator to see and acknowledge the exact number of rows about to
            # be permanently deleted before it happens.
            expected = f"DELETE {tds_count + batch_count}"
            self.stdout.write(self.style.WARNING(
                f"\nThis will PERMANENTLY delete {tds_count} tds_inputs row(s) and "
                f"{batch_count} tds_batches row(s). This cannot be undone."
            ))
            typed = input(f'Type "{expected}" to proceed: ').strip()
            if typed != expected:
                self.stdout.write(self.style.ERROR("Confirmation text did not match. Aborting — nothing deleted."))
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
