"""
Management command: backfill_qap_record_mark

One-off data fix: populates QAPItem.record_mark (the QAP's "D" column) for the
General Purpose (GP) template, transcribed from the sample QAP PDF the user
provided (QAP-0074-style document, Grade DIN X).

Heat Resistant / Fire Resistant (ISO) categories are intentionally left blank —
the source spreadsheet's D-column values for those categories were not
available at the time this was written. Re-run seed_qap_templates with an
updated SAMPLE_QAP.xlsx (once column 10 is captured there) to backfill those,
or extend MARKED_COMPONENTS below and re-run this command — it's idempotent.

Usage:
    python run_django.py backfill_qap_record_mark
    python run_django.py backfill_qap_record_mark --dry-run
"""

from django.core.management.base import BaseCommand

# (section_code, component-substring) pairs whose D column should carry a mark.
# Matched case-insensitively against QAPItem.component, scoped to GP template's
# items only. Using component text rather than `sn` because a couple of SN
# labels repeat (e.g. two rows are both labelled "1.1") — component text is
# the only reliably unique key against the source document.
MARK = '✓'  # ✓

MARKED_COMPONENTS = [
    ('1.0', 'Textile Fabric'),
    ('1.0', 'Dipped Textile Fabric'),
    ('1.0', 'Rubber Compound'),
    ('2.0', 'Rubber Coating of Fabrics'),
    ('2.0', 'Rubber Cover Sheeting'),
    ('2.0', 'Belt Building'),
    ('2.0', 'Cured Belt Inspection'),
    ('3.0', 'Dimension & Visual'),
    ('3.0', 'Tensile Strength'),
    ('3.0', 'Adhesion'),
    ('3.0', 'Troughability'),
    ('3.0', 'Cover Rubber Properties'),
]


class Command(BaseCommand):
    help = "Backfill the QAP 'D' column (record_mark) for the GP template from the sample PDF."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                             help='Report what would change without saving.')

    def handle(self, *args, **options):
        from apps.core.models import QAPItem

        dry_run = options['dry_run']
        gp_items = QAPItem.objects.filter(section__template__category='GP').select_related('section')

        matched_ids = set()
        updated = 0

        for section_code, needle in MARKED_COMPONENTS:
            rows = [
                item for item in gp_items
                if item.section.section_code == section_code
                and needle.lower() in item.component.lower()
            ]
            if not rows:
                self.stderr.write(self.style.WARNING(
                    f'No GP item found in section {section_code} matching "{needle}" — skipped.'
                ))
                continue
            if len(rows) > 1:
                self.stderr.write(self.style.WARNING(
                    f'{len(rows)} GP items in section {section_code} matched "{needle}" — '
                    f'marking all of them: {[r.sn for r in rows]}'
                ))
            for item in rows:
                matched_ids.add(item.pk)
                if item.record_mark != MARK:
                    self.stdout.write(f'  {section_code} {item.sn:>5} {item.component!r} -> record_mark = "{MARK}"')
                    if not dry_run:
                        item.record_mark = MARK
                        item.save(update_fields=['record_mark'])
                    updated += 1

        unmarked = gp_items.exclude(pk__in=matched_ids)
        if not dry_run:
            cleared = unmarked.exclude(record_mark='').update(record_mark='')
            if cleared:
                self.stdout.write(f'Cleared record_mark on {cleared} other GP item(s) not in the marked list.')

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{verb} {updated} GP item(s).'))
