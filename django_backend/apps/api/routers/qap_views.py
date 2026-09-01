"""
apps/api/routers/qap_views.py — QAP PDF generation endpoint.

Endpoint:
  GET /api/tds/<tds_id>/qap/pdf
    Returns a PDF of the Quality Assurance Plan for the given TDS.
    The template is resolved automatically from the TDS standard_id.

  GET /api/tds/<tds_id>/qap/pdf?format=html
    Returns the raw HTML (useful for debugging layout).

  Query params (all optional, read fresh on every download — never persisted):
    doc_type  — 'PO' or 'ENQUIRY' (defaults to 'PO')
    ref_no    — the PO / Enquiry number typed in on the download popup
    ref_date  — the PO / Enquiry date typed in on the download popup
"""
import logging

from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from apps.core.models import TDSInput
from apps.services.qap_service import resolve_qap_template, build_qap_context
from apps.services.pdf_service import tds_filename_base
from apps.services.pdf_renderer import render_qap_html, render_qap_pdf

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def generate_qap_pdf(request, tds_id):
    """
    Generate the QAP PDF for the given TDS record.
    The QAP template is resolved from the TDS's standard_id.
    Returns 404 if the TDS does not exist.
    Returns 422 if no QAP template is mapped for this standard.
    """
    fmt = request.GET.get('format', 'pdf').lower()

    # ── PO / Enquiry — entered fresh on every download, intentionally not
    # stored anywhere (see qap_service.build_qap_context docstring). ─────────
    doc_type = request.GET.get('doc_type', 'PO')
    ref_no   = request.GET.get('ref_no', '')
    ref_date = request.GET.get('ref_date', '')

    # ── Fetch TDS ─────────────────────────────────────────────────────────────
    try:
        tds = (
            TDSInput.objects
            .select_related('customer', 'standard', 'cover_grade')
            .get(pk=tds_id)
        )
    except TDSInput.DoesNotExist:
        return Response({'detail': f'TDS {tds_id} not found.'}, status=status.HTTP_404_NOT_FOUND)

    # ── Resolve template ──────────────────────────────────────────────────────
    template = resolve_qap_template(tds)
    if template is None:
        return Response(
            {'detail': (
                f'No QAP template is configured for standard_id={tds.standard_id}. '
                'Run seed_qap_templates or add the standard to STANDARD_TO_QAP_CATEGORY.'
            )},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    # ── Build context ─────────────────────────────────────────────────────────
    try:
        context = build_qap_context(tds, template, doc_type=doc_type, ref_no=ref_no, ref_date=ref_date)
    except Exception as exc:
        logger.error(
            'build_qap_context failed for tds_id=%s: %s', tds_id, exc, exc_info=True
        )
        # SECURITY (fixed): don't reflect raw exception text to the client;
        # full detail is already logged above.
        return Response(
            {'detail': 'Failed to build the QAP document. Please try again.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # ── HTML debug mode ───────────────────────────────────────────────────────
    if fmt == 'html':
        try:
            html_str = render_qap_html(context)
        except Exception as exc:
            logger.error('QAP HTML render failed tds_id=%s: %s', tds_id, exc, exc_info=True)
            return Response(
                {'detail': 'Failed to render the QAP document. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return HttpResponse(html_str, content_type='text/html; charset=utf-8')

    # ── PDF ───────────────────────────────────────────────────────────────────
    try:
        pdf_bytes = render_qap_pdf(context)
    except Exception as exc:
        logger.error('QAP PDF render failed tds_id=%s: %s', tds_id, exc, exc_info=True)
        # SECURITY (fixed): stop embedding raw exception text into this HTML
        # response; full detail is already logged above.
        error_html = "<html><body><h2>QAP PDF generation failed</h2><p>Please try again or contact support.</p></body></html>"
        return HttpResponse(error_html, content_type='text/html; charset=utf-8', status=500)

    # Same base-name convention as the TDS PDF (doc number, else customer
    # name, else "TDS-<number>") plus a "_QAP" suffix so it doesn't collide
    # with that TDS's own PDF download in the same folder.
    filename = f"{tds_filename_base(tds)}_QAP.pdf"
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response
