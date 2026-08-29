"""
apps/api/routers/revisions_views.py — TDS version-history read endpoints.

Endpoints:
  GET /api/tds/{id}/revisions               — lightweight list of past revisions
  GET /api/tds/{id}/revisions/{revision_number} — one revision's full snapshot

Revisions are created server-side in tds_views.py::_update_tds() — this
module is read-only. Access mirrors GET /api/tds/{id}: any authenticated
role (including viewer) can look at a TDS's history, same as its current
detail.
"""
import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import NotFound

from apps.core.models import TDSInput, TDSRevision

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
