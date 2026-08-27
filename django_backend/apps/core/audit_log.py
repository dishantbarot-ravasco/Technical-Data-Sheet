"""
apps/core/audit_log.py — Lightweight audit trail for all TDS state changes.

WHAT IT LOGS
  Every create / approve / decline / delete action on a TDS record, plus
  packing recomputes and batch submissions.  The log is append-only — rows
  are never updated or deleted.

SETUP — three steps:

  1. Run the migration to create the audit_log table:

       python run_django.py makemigrations core
       python run_django.py migrate

  2. In each view that mutates a TDS, import and call log_tds_action():

       from apps.core.audit_log import log_tds_action
       log_tds_action(request, 'approve', tds)

  3. (Optional) Register TDSAuditLog in apps/core/admin.py to browse it in
     the Django admin panel:

       from apps.core.audit_log import TDSAuditLog
       @admin.register(TDSAuditLog)
       class TDSAuditLogAdmin(admin.ModelAdmin):
           list_display = ('timestamp', 'action', 'tds_number', 'actor_email', 'ip_address')
           list_filter  = ('action',)
           readonly_fields = [f.name for f in TDSAuditLog._meta.get_fields()]
           def has_add_permission(self, request): return False
           def has_change_permission(self, request, obj=None): return False
           def has_delete_permission(self, request, obj=None): return False
"""

import logging

from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Model ─────────────────────────────────────────────────────────────────────

class TDSAuditLog(models.Model):
    """
    Append-only audit trail — one row per significant TDS action.

    Fields:
      timestamp    — UTC datetime of the action
      action       — one of the ACTION_* constants below
      tds_id       — nullable so batch-create failures still produce a row
      tds_number   — denormalised for readability (survives TDS deletion)
      actor_id     — TDSUser.pk of the person who triggered the action
      actor_email  — denormalised for readability
      ip_address   — from X-Forwarded-For or REMOTE_ADDR
      detail       — free-text (e.g. decline reason, field names changed)
    """

    ACTION_CREATE   = 'create'
    ACTION_UPDATE   = 'update'
    ACTION_APPROVE  = 'approve'
    ACTION_DECLINE  = 'decline'
    ACTION_DELETE   = 'delete'
    ACTION_PACKING  = 'packing_recompute'
    ACTION_BATCH    = 'batch_create'
    ACTION_DOWNLOAD = 'download_pdf'

    ACTION_CHOICES = [
        (ACTION_CREATE,   'Create'),
        (ACTION_UPDATE,   'Update'),
        (ACTION_APPROVE,  'Approve'),
        (ACTION_DECLINE,  'Decline'),
        (ACTION_DELETE,   'Delete'),
        (ACTION_PACKING,  'Packing Recompute'),
        (ACTION_BATCH,    'Batch Create'),
        (ACTION_DOWNLOAD, 'Download PDF'),
    ]

    timestamp    = models.DateTimeField(default=timezone.now, db_index=True)
    action       = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True)
    tds_id       = models.IntegerField(null=True, blank=True, db_index=True)
    tds_number   = models.CharField(max_length=20, blank=True)
    actor_id     = models.IntegerField(null=True, blank=True)
    actor_email  = models.CharField(max_length=254, blank=True)
    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    detail       = models.TextField(blank=True)

    class Meta:
        db_table   = 'tds_audit_log'
        managed    = True          # Django DOES manage this table
        ordering   = ['-timestamp']
        indexes    = [
            models.Index(fields=['tds_id', 'timestamp']),
            models.Index(fields=['actor_id', 'timestamp']),
        ]

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.action} TDS#{self.tds_number} by {self.actor_email}"


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_client_ip(request):
    """
    Extract the real client IP, honouring X-Forwarded-For (Render / proxy).

    Delegates to apps.services.device_service.get_client_ip so there's a
    single implementation (this used to be a second, independent copy of the
    same logic, which also meant it carried the same X-Forwarded-For
    spoofing bug that one had until it was fixed there — see that
    function's docstring for the full reasoning).
    """
    from apps.services.device_service import get_client_ip
    return get_client_ip(request)


def log_tds_action(request, action, tds=None, detail=''):
    """
    Write one audit row.  Never raises — failures are logged and swallowed so
    a broken audit system can't block a legitimate user action.

    Usage:
        log_tds_action(request, TDSAuditLog.ACTION_APPROVE, tds=tds_instance)
        log_tds_action(request, TDSAuditLog.ACTION_DELETE,  tds=tds_instance, detail='User requested deletion')
        log_tds_action(request, TDSAuditLog.ACTION_BATCH,   detail=f'{len(belt_list)} belts')

    Args:
        request  — DRF/Django request (provides actor + IP)
        action   — one of TDSAuditLog.ACTION_* constants
        tds      — TDSInput instance, or None for batch-level actions
        detail   — optional free-text annotation
    """
    try:
        user = getattr(request, 'user', None)
        TDSAuditLog.objects.create(
            action      = action,
            tds_id      = tds.tds_id      if tds else None,
            tds_number  = tds.tds_number  if tds else '',
            actor_id    = user.pk         if user and user.is_authenticated else None,
            actor_email = getattr(user, 'email', '') or '',
            ip_address  = _get_client_ip(request),
            detail      = detail,
        )
    except Exception:
        logger.exception("audit_log: failed to write audit row (action=%s)", action)
