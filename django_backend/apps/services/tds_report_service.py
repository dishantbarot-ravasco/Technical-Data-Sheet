"""
apps/services/tds_report_service.py — Daily TDS report email logic.

Single source of truth for building + sending the daily admin report, used by:
  - the management command `send_daily_tds_report` (manual/local runs, backfills)
  - apps/api/routers/reports_views.trigger_daily_report (hit by a free external
    scheduler in production — Render's free web plan has no built-in cron,
    and Render's own Cron Jobs have a $1/month minimum, so a shared-secret
    HTTP endpoint + a free scheduler like cron-job.org or a scheduled GitHub
    Action is the free way to fire this once a day)

What the report includes, per day:
  - Total number of TDS documents created that day
  - A breakdown of how many used each cover grade
  - A breakdown of how many were for each customer
"""
import datetime

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.core.models import TDSInput, TDSUser


def send_daily_tds_report(report_date: datetime.date | None = None) -> dict:
    """
    Build and email the daily TDS report to every active admin.

    report_date defaults to "today" in the server's TIME_ZONE (Asia/Kolkata).
    Returns a small dict summary (used by both the API endpoint's JSON
    response and the management command's stdout) — never raises for
    "no admins" / "nothing created today", only for actual send failures.
    """
    if report_date is None:
        report_date = timezone.localdate()

    qs = (
        TDSInput.objects
        .filter(created_at__date=report_date)
        .select_related('cover_grade', 'cover_grade__standard', 'customer')
    )
    total = qs.count()

    admin_emails = list(
        TDSUser.objects.filter(role='admin', is_active=True).values_list('email', flat=True)
    )
    if not admin_emails:
        return {'date': report_date.isoformat(), 'total': total, 'admins_notified': 0,
                'skipped_reason': 'no active admin users found'}

    if total == 0:
        body = (
            f"Daily TDS Report — {report_date.isoformat()}\n\n"
            f"No TDS documents were created today.\n\n"
            f"— This is a system-generated email from the Ravasco TDS System."
        )
    else:
        by_grade = {}
        by_customer = {}
        for rec in qs:
            grade_label = (
                f"{rec.cover_grade.grade_code} ({rec.cover_grade.standard.standard_name})"
                if rec.cover_grade_id else "Unknown"
            )
            by_grade[grade_label] = by_grade.get(grade_label, 0) + 1

            customer_label = rec.customer.customer_name if rec.customer_id else "No customer set"
            by_customer[customer_label] = by_customer.get(customer_label, 0) + 1

        grade_lines = "\n".join(f"    {name}: {count}" for name, count in sorted(by_grade.items(), key=lambda x: -x[1]))
        customer_lines = "\n".join(f"    {name}: {count}" for name, count in sorted(by_customer.items(), key=lambda x: -x[1]))

        body = (
            f"Daily TDS Report — {report_date.isoformat()}\n\n"
            f"Total TDS created today: {total}\n\n"
            f"By cover grade:\n{grade_lines}\n\n"
            f"By customer:\n{customer_lines}\n\n"
            f"— This is a system-generated email from the Ravasco TDS System."
        )

    subject = f"[TDS Daily Report] {report_date.isoformat()} — {total} TDS created"

    send_mail(
        subject        = subject,
        message        = body,
        from_email     = settings.DEFAULT_FROM_EMAIL,
        recipient_list = admin_emails,
        fail_silently  = False,
    )

    return {'date': report_date.isoformat(), 'total': total, 'admins_notified': len(admin_emails)}
