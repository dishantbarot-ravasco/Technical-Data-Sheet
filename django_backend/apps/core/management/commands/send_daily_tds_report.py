"""
apps/core/management/commands/send_daily_tds_report.py

Emails every active admin (role='admin') a daily summary of TDS activity —
see apps/services/tds_report_service.py for exactly what's included.

This is the manual/local entry point. In production, the same logic is
triggered automatically once a day by a free external scheduler hitting
GET /api/internal/send-daily-report/?secret=... (see reports_views.py) —
that endpoint calls the exact same send_daily_tds_report() function, so
both paths always produce an identical report.

Usage:
    python run_django.py send_daily_tds_report
    python run_django.py send_daily_tds_report --date 2026-08-07   # backfill a specific day
"""
import datetime

from django.core.management.base import BaseCommand

from apps.services.tds_report_service import send_daily_tds_report


class Command(BaseCommand):
    help = "Email all admins a daily summary of TDS created, by cover grade and customer."

    def add_arguments(self, parser):
        parser.add_argument(
            '--date', type=str, default=None,
            help='YYYY-MM-DD to report on (defaults to today, in the server TIME_ZONE).',
        )

    def handle(self, *args, **options):
        report_date = datetime.date.fromisoformat(options['date']) if options['date'] else None
        result = send_daily_tds_report(report_date)

        if result.get('skipped_reason'):
            self.stdout.write(self.style.WARNING(f"No active admin users found — nothing to send to."))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Sent daily report ({result['total']} TDS) for {result['date']} to {result['admins_notified']} admin(s)."
        ))
