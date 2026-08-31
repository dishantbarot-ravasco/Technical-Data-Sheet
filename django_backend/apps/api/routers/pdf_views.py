"""
apps/api/routers/pdf_views.py — PDF generation endpoint.

Django port of FastAPI routers/pdf.py.

Endpoint:
  GET /api/tds/{tds_id}/pdf
    ?exclude_groups=Splicing+Parameters   (repeatable)
    ?exclude_params=45                    (repeatable — parameter_id ints)
    ?exclude_gi_fields=gi_date            (repeatable)
    ?show_section=true
    ?show_test_method=true
    ?show_reference=true
    ?format=pdf|html                      (html = return raw HTML for debugging)
"""
import logging

from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from apps.services.pdf_service import build_tds_doc_data
from apps.services.pdf_renderer import render_tds_html, render_tds_pdf
from apps.core.audit_log import log_tds_action, TDSAuditLog
from apps.core.models import TDSInput

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def generate_pdf(request, tds_id):
    logger.debug("generate_pdf called tds_id=%s user=%s", tds_id, request.user)

    exclude_groups    = request.GET.getlist('exclude_groups')   or []
    exclude_gi_fields = request.GET.getlist('exclude_gi_fields') or []
    raw_params = request.GET.getlist('exclude_params')
    exclude_params = []
    for p in raw_params:
        try:
            exclude_params.append(int(p))
        except (ValueError, TypeError):
            pass

    def _bool(key, default=True):
        val = request.GET.get(key, '').lower()
        if val == 'false': return False
        if val == 'true':  return True
        return default

    show_section     = _bool('show_section')
    show_test_method = _bool('show_test_method')
    show_reference   = _bool('show_reference')
    fmt              = request.GET.get('format', 'pdf').lower()
    logger.debug("generate_pdf fmt=%r tds_id=%s", fmt, tds_id)

    try:
        doc = build_tds_doc_data(
            tds_id=tds_id,
            exclude_groups=exclude_groups or None,
            exclude_params=exclude_params or None,
            show_section=show_section,
            show_test_method=show_test_method,
            show_reference=show_reference,
        )
        logger.debug("TDS doc built: %s", doc.tds_number)
    except Exception as exc:
        logger.error("TDS doc build FAILED for tds_id=%s: %s: %s", tds_id, type(exc).__name__, exc, exc_info=True)
        # SECURITY (fixed): don't reflect the raw exception text to the client
        # (internal paths / library details); full detail is already logged above.
        return Response({'detail': f'TDS {tds_id} could not be found or built.'}, status=status.HTTP_404_NOT_FOUND)

    if fmt == 'html':
        try:
            html_str = render_tds_html(
                doc,
                exclude_groups=exclude_groups or None,
                exclude_gi_fields=exclude_gi_fields or None,
                show_test_method=show_test_method,
                show_reference=show_reference,
            )
            logger.debug("HTML rendered: %d bytes for %s", len(html_str), doc.tds_number)
        except Exception as exc:
            logger.error("HTML render failed for %s: %s", doc.tds_number, exc, exc_info=True)
            return Response({'detail': 'Failed to render the TDS document. Please try again.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return HttpResponse(html_str, content_type='text/html; charset=utf-8')

    # PDF path
    try:
        pdf_bytes = render_tds_pdf(
            doc,
            exclude_groups=exclude_groups or None,
            exclude_gi_fields=exclude_gi_fields or None,
            show_test_method=show_test_method,
            show_reference=show_reference,
        )
    except Exception as exc:
        logger.error("PDF render failed for %s: %s", doc.tds_number, exc, exc_info=True)
        # SECURITY (fixed): the exception text used to be embedded directly into
        # this HTML response unescaped (str(exc) could in principle contain
        # characters that alter the markup) and leaked internal error detail to
        # the client; full detail is already logged above.
        error_html = "<html><body><h2>PDF generation failed</h2><p>Please try again or contact support.</p></body></html>"
        return HttpResponse(error_html, content_type='text/html; charset=utf-8', status=500)

    filename = f"TDS-{doc.tds_number}.pdf"
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    tds_record = TDSInput.objects.filter(pk=tds_id).first()
    # First real download closes out the "still drafting, not yet issued" window —
    # from here on, _update_tds() will snapshot edits into TDSRevision instead of
    # applying them silently (see TDSInput.first_downloaded_at docstring).
    if tds_record and tds_record.first_downloaded_at is None:
        tds_record.first_downloaded_at = timezone.now()
        tds_record.save(update_fields=['first_downloaded_at'])
    log_tds_action(request, TDSAuditLog.ACTION_DOWNLOAD, tds=tds_record, detail=fmt)
    return response


def render_tds_pdf_bytes(tds_id: int, exclude_groups=None) -> bytes:
    """
    Return raw PDF bytes for one TDS record.

    Called by batch_export_views.py's export builders (_build_zip_export,
    _build_merged_zip_export, _build_print_all_export) to generate per-belt
    PDFs for the batch ZIP bundle / merged "print all" PDF. Uses the same
    pdf_service + pdf_renderer pipeline as the regular generate_pdf endpoint.

    exclude_groups: None (default) → every section included ("Internal Copy").
                    A list of group names (see apps.services.sections) →
                    those sections omitted ("Customer Copy").
    """
    doc = build_tds_doc_data(tds_id=tds_id, exclude_groups=exclude_groups)
    return render_tds_pdf(doc, exclude_groups=exclude_groups)
