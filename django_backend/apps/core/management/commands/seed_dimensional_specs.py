"""
apps/core/management/commands/seed_dimensional_specs.py

Django management command that seeds `dimensional_parameter_specs` with
tolerance data for every standard currently in the database.

Usage:
    python tds_app/run_django.py seed_dimensional_specs
    python tds_app/run_django.py seed_dimensional_specs --dry-run
    python tds_app/run_django.py seed_dimensional_specs --replace

Algorithm
---------
1. Load every Standard row and classify it by pattern-matching the name.
2. For each standard, upsert rows for parameter IDs 1, 2, 3, 4, 6.
   (Parameter 5 = interply skim is read from EAV, not this table.)
3. Default: skip rows that already exist (idempotent).
   With --replace: overwrite tolerance_value if it differs.

Standard classification (case-insensitive keyword match):
    IS 1891        → IS1891
    ISO 14890      → ISO14890
    DIN 22102      → DIN22102
    ARPM           → ARPM
    ASTM D378      → ASTMD378   (same rules as ARPM)
    AS 1332        → AS1332
    SANS 1173      → SANS1173
    INHOUSE / IN-HOUSE → INHOUSE  (same as ISO 14890)
    anything else  → ISO14890   (safe fallback)

Tolerance data source: patch_dimensional_specs_width_standard.sql
"""

import logging
from django.core.management.base import BaseCommand
from django.db import transaction, connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tolerance tables
# Each entry: (min_value, max_value, tolerance_value)
# None = no bound on that side.
# ---------------------------------------------------------------------------

_SPECS = {
    # ── Belt Width (parameter_id=1) ─────────────────────────────────────
    "belt_width": {
        "IS1891":    [(None, 600,  "+/-5 mm"),  (650,  None, "+/-1%")],
        "ISO14890":  [(None, 500,  "+/-5 mm"),  (600,  None, "+/-1%")],
        "DIN22102":  [(None, 500,  "+/-5 mm"),  (600,  None, "+/-1%")],
        "ARPM":      [(None, None, "Not Specified")],
        "ASTMD378":  [(None, None, "Not Specified")],
        "AS1332":    [(None, 650,  "+/-6 mm"),  (651,  None, "+/-1%")],
        "SANS1173":  [(None, 599,  "+/-5 mm"),  (600,  None, "+/-1%")],
        "INHOUSE":   [(None, 500,  "+/-5 mm"),  (600,  None, "+/-1%")],
    },
    # ── Top Cover Thickness (parameter_id=2) ────────────────────────────
    "top_cover": {
        "IS1891":    [(None, 4.0,  "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
        "ISO14890":  [(None, 4.0,  "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
        "DIN22102":  [(None, 4.0,  "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
        "ARPM":      [(None, None, "Not Specified")],
        "ASTMD378":  [(None, None, "Not Specified")],
        "AS1332":    [(None, 1.0,  "+No Limit / -0.1 mm"), (1.01, 4.0, "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
        "SANS1173":  [(None, 4.0,  "-0.2 mm"), (4.01, None, "-5%")],
        "INHOUSE":   [(None, 4.0,  "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
    },
    # ── Bottom Cover Thickness (parameter_id=3) ─────────────────────────
    "bottom_cover": {
        "IS1891":    [(None, 4.0,  "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
        "ISO14890":  [(None, 4.0,  "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
        "DIN22102":  [(None, 4.0,  "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
        "ARPM":      [(None, None, "Not Specified")],
        "ASTMD378":  [(None, None, "Not Specified")],
        "AS1332":    [(None, 1.0,  "+No Limit / -0.1 mm"), (1.01, 4.0, "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
        "SANS1173":  [(None, 4.0,  "-0.2 mm"), (4.01, None, "-5%")],
        "INHOUSE":   [(None, 4.0,  "+No Limit / -0.2 mm"), (4.01, None, "+No Limit / -5%")],
    },
    # ── Carcass Thickness (parameter_id=4) ──────────────────────────────
    "carcass": {
        "IS1891":    [(None, 5.0,  "+/-0.5 mm"), (5.01, None, "+/-10%")],
        "ISO14890":  [(None, None, "Not Specified")],
        "DIN22102":  [(None, None, "Not Specified")],
        "ARPM":      [(None, None, "Not Specified")],
        "ASTMD378":  [(None, None, "Not Specified")],
        "AS1332":    [(None, None, "Not Specified")],
        "SANS1173":  [(None, None, "Not Specified")],
        "INHOUSE":   [(None, None, "Not Specified")],
    },
    # ── Total Belt Thickness (parameter_id=6) ───────────────────────────
    "total_thickness": {
        "IS1891":    [(None, 10.0, "Spread ≤1 mm"), (10.01, None, "≤10% of mean")],
        "ISO14890":  [(None, 10.0, "Spread ≤1 mm"), (10.01, None, "≤10% of mean")],
        "DIN22102":  [(None, 10.0, "Spread ≤1 mm"), (10.01, None, "≤10% of mean")],
        "ARPM":      [(None, None, "Not Specified")],
        "ASTMD378":  [(None, None, "Not Specified")],
        "AS1332":    [(None, 10.0, "Spread ≤1 mm"), (10.01, None, "≤10% of mean")],
        "SANS1173":  [(None, 10.0, "+/-1 mm"),           (10.01, None, "+/-10% of nominal")],
        "INHOUSE":   [(None, 10.0, "Spread ≤1 mm"), (10.01, None, "≤10% of mean")],
    },
}

# parameter_id for each group
_PARAM_IDS = {
    "belt_width":      1,
    "top_cover":       2,
    "bottom_cover":    3,
    "carcass":         4,
    "total_thickness": 6,
}


def _classify(standard_name: str) -> str:
    """Return the internal code for a standard name."""
    n = standard_name.upper()
    if "INHOUSE" in n or "IN-HOUSE" in n or "IN HOUSE" in n:
        return "INHOUSE"
    if "IS 1891" in n or "IS1891" in n:
        return "IS1891"
    if "ISO 14890" in n or "ISO14890" in n:
        return "ISO14890"
    if "DIN 22102" in n or "DIN22102" in n:
        return "DIN22102"
    if "ARPM" in n:
        return "ARPM"
    if "ASTM" in n and "D378" in n:
        return "ASTMD378"
    if "AS 1332" in n or "AS1332" in n:
        return "AS1332"
    if "SANS 1173" in n or "SANS1173" in n:
        return "SANS1173"
    return "ISO14890"


class Command(BaseCommand):
    help = "Seed dimensional_parameter_specs from built-in tolerance tables."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print what would be inserted/updated without touching the DB.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            default=False,
            help="Overwrite existing rows if tolerance_value differs (uses UPDATE).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        replace = options["replace"]

        from apps.core.models import Standard

        standards = list(Standard.objects.all())
        if not standards:
            self.stderr.write("No standards found in DB — nothing to seed.")
            return

        self.stdout.write(f"Found {len(standards)} standard(s).")

        # Build the full list of rows to upsert
        rows_to_upsert = []
        for std in standards:
            code = _classify(std.standard_name)
            self.stdout.write(
                f"  std_id={std.standard_id:>3}  {std.standard_name!r:<40}  → {code}"
            )
            for group, param_id in _PARAM_IDS.items():
                for (lo, hi, tol) in _SPECS[group].get(code, []):
                    rows_to_upsert.append((std.standard_id, param_id, lo, hi, tol))

        if not rows_to_upsert:
            self.stdout.write("Nothing to insert.")
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"\n[DRY RUN] Would upsert {len(rows_to_upsert)} row(s). Nothing written.")
            )
            return

        # Use raw SQL with ON CONFLICT DO NOTHING (or DO UPDATE when --replace).
        # This avoids Python-side float equality issues entirely.
        if replace:
            sql = """
                INSERT INTO dimensional_parameter_specs
                    (standard_id, parameter_id, min_value, max_value, tolerance_value)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (standard_id, parameter_id,
                             COALESCE(min_value, -1e308),
                             COALESCE(max_value,  1e308))
                DO UPDATE SET tolerance_value = EXCLUDED.tolerance_value
                WHERE dimensional_parameter_specs.tolerance_value IS DISTINCT FROM EXCLUDED.tolerance_value
            """
        else:
            sql = """
                INSERT INTO dimensional_parameter_specs
                    (standard_id, parameter_id, min_value, max_value, tolerance_value)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """

        with connection.cursor() as cur:
            cur.executemany(sql, rows_to_upsert)
            affected = cur.rowcount   # -1 on executemany in psycopg2; best-effort

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. Attempted {len(rows_to_upsert)} upsert(s). "
                f"(Existing rows were {'overwritten' if replace else 'skipped'}.)"
            )
        )
