"""
apps/core/management/commands/check_cover_grade_usage.py

Read-only diagnostic: for a given set of (possibly mistaken) CoverGrade rows,
report whether anything downstream actually references them, so you can tell
whether they're safe to delete before touching the database.

"Downstream" means the two tables that have a foreign key to cover_grades:
  1. cover_grade_values — the EAV spec/actual values attached to the grade
     (deleting the CoverGrade would cascade-delete these too, per the model's
     on_delete=CASCADE).
  2. tds_input — any TDS document actually generated using that grade
     (the model's on_delete=PROTECT means Django will REFUSE to delete a
     CoverGrade that's referenced here — this is the one that actually
     matters for "is it safe to delete").

Nothing here writes or deletes anything. It only reports counts.

Usage:
    python run_django.py check_cover_grade_usage
    python run_django.py check_cover_grade_usage --grade-code "F" --standard-contains SANS
    python run_django.py check_cover_grade_usage --grade-code "w/HAR" --standard-contains DIN

With no arguments, it runs the two checks the user actually asked about:
  - grade_code containing "HAR" under a standard containing "DIN"
  - grade_code exactly "F" under a standard containing "SANS"
It also prints EVERY CoverGrade row matching those loose filters, in case the
exact grade_code differs from what's assumed here (e.g. "DIN w/HAR" stored as
a single grade_code vs. "w/HAR" under a DIN standard) — don't assume either
guess is correct; read the printed rows.
"""
from django.core.management.base import BaseCommand

from apps.core.models import CoverGrade


class Command(BaseCommand):
    help = "Read-only: report whether specific CoverGrade rows are referenced by cover_grade_values or tds_input."

    def add_arguments(self, parser):
        parser.add_argument('--grade-code', type=str, default=None,
                             help='Exact or partial grade_code to search for (case-insensitive contains match).')
        parser.add_argument('--standard-contains', type=str, default=None,
                             help='Partial standard name to scope the search (case-insensitive contains match).')

    def handle(self, *args, **options):
        grade_code_filter = options.get('grade_code')
        standard_filter    = options.get('standard_contains')

        if grade_code_filter or standard_filter:
            searches = [(grade_code_filter, standard_filter)]
        else:
            # The two the user asked about, by name. Loose "contains" filters
            # deliberately — see module docstring on why exact codes aren't
            # assumed here.
            searches = [
                ('HAR', 'DIN'),
                ('F',   'SANS'),
            ]

        any_found = False
        for grade_code_part, standard_part in searches:
            qs = CoverGrade.objects.select_related('standard').all()
            if grade_code_part:
                qs = qs.filter(grade_code__icontains=grade_code_part)
            if standard_part:
                qs = qs.filter(standard__standard_name__icontains=standard_part)

            rows = list(qs.order_by('standard__standard_name', 'grade_code'))
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n=== grade_code contains {grade_code_part!r}, standard contains {standard_part!r} ==="
            ))
            if not rows:
                self.stdout.write(self.style.WARNING("  No matching CoverGrade rows found."))
                continue

            any_found = True
            for cg in rows:
                value_count = cg.values.count()               # cover_grade_values (CASCADE on delete)
                tds_count   = cg.tds_inputs.count()            # tds_input (PROTECT on delete)
                verdict = (
                    self.style.ERROR(f"IN USE by {tds_count} TDS record(s) — deleting this grade will FAIL "
                                      f"(on_delete=PROTECT) until those records are reassigned or removed")
                    if tds_count > 0
                    else self.style.SUCCESS("not referenced by any TDS record — safe to delete as far as tds_input is concerned")
                )
                self.stdout.write(
                    f"  CoverGrade id={cg.id}  grade_code={cg.grade_code!r}  "
                    f"standard={cg.standard.standard_name!r}\n"
                    f"    cover_grade_values rows: {value_count}  (would cascade-delete with the grade)\n"
                    f"    tds_input rows:          {tds_count}\n"
                    f"    verdict: {verdict}"
                )

        if not any_found:
            self.stdout.write(self.style.WARNING(
                "\nNothing matched at all — the grade_code/standard names may be spelled "
                "differently than assumed. Try, e.g.:\n"
                "  python run_django.py check_cover_grade_usage --grade-code HAR\n"
                "  python run_django.py check_cover_grade_usage --standard-contains DIN\n"
                "to list broader matches."
            ))
