"""
apps/api/routers/revisions_views.py — TDS version-history read endpoints.

Endpoints:
  GET /api/tds/{id}/revisions                       — lightweight list of past revisions
  GET /api/tds/{id}/revisions/{revision_number}      — one revision's full snapshot
  GET /api/tds/{id}/revisions/{revision_number}/pdf  — that revision's spec sheet as a PDF

Revisions are created server-side in tds_views.py::_update_tds() — this
module is read-only. Access mirrors GET /api/tds/{id}: any authenticated
role (including viewer) can look at a TDS's history, same as its current
detail.
"""
import logging

from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import NotFound

from apps.core.models import TDSInput, TDSRevision
from apps.core.audit_log import log_tds_action, TDSAuditLog
from apps.services.pdf_service import build_tds_doc_data, revision_pdf_filename
from apps.services.pdf_renderer import render_tds_pdf

logger = logging.getLogger(__name__)


def _user_brief(u):
    if u is None:
        return None
    return {"user_id": u.user_id, "email": u.email, "full_name": u.full_name or ''}


def _revision_brief(rev):
    return {
        "revision_number": rev.revision_number,
        "edited_by":       _user_brief(rev.edited_by),
        "edited_at":       rev.edited_at.isoformat() if rev.edited_at else None,
        "change_summary":  rev.change_summary,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_revisions(request, tds_id):
    """GET /api/tds/{id}/revisions — lightweight history list, newest first."""
    if not TDSInput.objects.filter(pk=tds_id).exists():
        raise NotFound(f"TDS {tds_id} not found")
    revisions = TDSRevision.objects.filter(tds_id=tds_id).select_related('edited_by')
    return Response([_revision_brief(r) for r in revisions])


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_revision(request, tds_id, revision_number):
    """GET /api/tds/{id}/revisions/{n} — one revision's metadata plus full snapshot."""
    rev = (
        TDSRevision.objects
        .select_related('edited_by')
        .filter(tds_id=tds_id, revision_number=revision_number)
        .first()
    )
    if not rev:
        raise NotFound(f"Revision {revision_number} for TDS {tds_id} not found")
    data = _revision_brief(rev)
    data["snapshot"] = rev.snapshot
    return Response(data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def generate_revision_pdf(request, tds_id, revision_number):
    """
    GET /api/tds/{id}/revisions/{n}/pdf — spec sheet for a past revision.

    Rebuilds the PDF off the CURRENT TDSInput row with the revision's
    snapshot fields overlaid on top (see pdf_service.build_tds_doc_data's
    `overrides` param) — this correctly restores that revision's own
    dimensions/cover/carcass/packing/splicing values AND re-derives its EAV
    spec values (cover grade, fabric, belt rating, standard) because those
    FK ids are themselves part of the snapshot. It is not a byte-for-byte
    reproduction of the PDF as it looked back then: reference/lookup tables
    (spec tolerances, test methods) are joined live, so a later correction
    to master data would show up here too — the header banner says so.
    """
    tds_record = TDSInput.objects.filter(pk=tds_id).select_related('customer').first()
    if not tds_record:
        raise NotFound(f"TDS {tds_id} not found")
    rev = TDSRevision.objects.filter(tds_id=tds_id, revision_number=revision_number).first()
    if not rev:
        raise NotFound(f"Revision {revision_number} for TDS {tds_id} not found")

    exclude_groups = request.GET.getlist('exclude_groups') or None

    # rev.edited_at is stored UTC-aware (USE_TZ=True) - localtime() converts it
    # to TIME_ZONE (Asia/Kolkata) before formatting, same as every other
    # user-facing timestamp in this app. Formatting the raw UTC value directly
    # would print a time 5:30 behind what the record was actually saved at.
    when = timezone.localtime(rev.edited_at).strftime('%d %b %Y, %H:%M') if rev.edited_at else 'an earlier date'
    # revision_number is the value current_revision held right before the
    # edit that created this snapshot -- 0 means "the original, never-edited
    # state" (see the matching "no suffix until an edit has actually
    # happened" rule in pdf_views.py::generate_pdf's filename).
    if revision_number == 0:
        banner = f"HISTORICAL REVISION - ORIGINAL VERSION - state as saved on {when}"
    else:
        banner = f"HISTORICAL REVISION {revision_number:02d} - state as saved on {when}"

    try:
        doc = build_tds_doc_data(
            tds_id=tds_id,
            exclude_groups=exclude_groups,
            overrides=rev.snapshot,
            revision_banner=banner,
        )
        pdf_bytes = render_tds_pdf(doc, exclude_groups=exclude_groups)
    except Exception as exc:
        logger.error("Revision PDF render failed for tds_id=%s rev=%s: %s", tds_id, revision_number, exc, exc_info=True)
        return Response({'detail': 'Failed to render this revision as a PDF.'}, status=500)

    filename = revision_pdf_filename(tds_record, revision_number)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    log_tds_action(request, TDSAuditLog.ACTION_DOWNLOAD, tds=tds_record, detail=f'revision {revision_number}')
    return response
