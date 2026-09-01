"""
apps/api/routers/batch_export_views.py — Async batch PDF export (ZIP / merged
ZIP / print-all).

Endpoints:
  POST /api/tds/batch/{batch_id}/export/         — start an export job
  GET  /api/tds/batch/export/{job_id}/status/    — poll job status
  GET  /api/tds/batch/export/{job_id}/download/  — download the finished file

Why async: batch_views.py used to render these synchronously inside the
request (see git history for download_batch_zip / download_batch_merged_zip /
print_all_batch, previously in this file). Measured WeasyPrint render cost is
~2s per PDF on dev hardware — likely 2-4x slower on Render's free-tier shared
CPU — and a ZIP export renders up to 2 PDFs (TDS + QAP) per belt, so a batch
of only ~15 belts could already exceed gunicorn's 120s request timeout and
come back as a 502 with no way to recover the work already done.

There's no task queue/broker (Celery, Redis) provisioned on the current
Render free-tier deployment, so this uses a plain background thread — the
same pattern already used for outbound email in apps/services/device_service.py
— plus the BatchExportJob DB row (apps/core/models.py) as the job-status/
result store shared across gunicorn's worker *processes*. See that model's
docstring for the full reasoning.
"""
import io
import re
import logging
import threading
import zipfile
from datetime import timedelta

from django.utils import timezone
from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, ValidationError

from apps.core.models import BatchExportJob, TDSBatch, TDSInput
from apps.services.sections import CUSTOMER_COPY_EXCLUDE_GROUPS

logger = logging.getLogger(__name__)

_SAFE = re.compile(r'[<>:"/\\|?*]')

# Finished/failed job rows older than this are swept whenever a new job
# starts — the closest thing to a TTL we have without a scheduled task.
_JOB_TTL = timedelta(hours=2)


def _resolve_copy_type(request):
    """
    Read ?copy=customer|internal (default 'customer') and return the
    exclude_groups list to pass into PDF rendering.

    'customer' → CUSTOMER_COPY_EXCLUDE_GROUPS (fabric/sampling/packing/splicing
                 detail omitted — this is the copy meant to leave the building).
    'internal' → None (nothing excluded — every section included).

    Defaults to 'customer' so a plain GET with no query string (e.g. an old
    bookmarked link) doesn't accidentally hand out internal-only detail.
    Same two names and same default are used by the single-record preview
    (frontend/tds-preview.html's DEFAULT_UNCHECKED_GROUPS) and by
    getPdfUrl()/downloadPdf() (frontend/js/api.js) — keep them in sync.
    """
    explicit = [g for g in request.GET.getlist('exclude_groups') if g]
    if explicit:
        return explicit

    copy_type = (request.GET.get('copy') or 'customer').strip().lower()
    if copy_type == 'internal':
        return None
    return list(CUSTOMER_COPY_EXCLUDE_GROUPS)


# ── Export builders ─────────────────────────────────────────────────────────
# Each takes (batch_id, params) and returns (filename, content_type, bytes),
# or raises an exception whose message is stored as the job's error_message.

def _get_exclude_groups(params):
    if params.get('exclude_groups'):
        return list(params['exclude_groups'])
    return None if params.get('copy') == 'internal' else list(CUSTOMER_COPY_EXCLUDE_GROUPS)


def _build_zip_export(batch_id, params, on_progress=None):
    """
    Per-belt TDS + QAP PDFs, plus one merged PDF of every belt's TDS, all in
    one ZIP. Moved verbatim (rendering logic unchanged) from batch_views.py's
    former download_batch_zip() — see that function's git history for the
    original inline-request version and the "missing return" bug fix note
    that applied to it.

    on_progress(current, total), if given, is called after each belt finishes
    (TDS + its QAP) so the job row can expose "N of M" progress to the
    frontend instead of a bare spinner for a job that can run for minutes.
    """
    try:
        from pypdf import PdfWriter
    except ImportError:
        raise RuntimeError(
            "Batch PDF download requires the 'pypdf' package, which isn't installed."
        )

    from apps.api.routers.pdf_views import render_tds_pdf_bytes
    from apps.services.qap_service import resolve_qap_template, build_qap_context
    from apps.services.pdf_renderer import render_qap_pdf
    from apps.services.pdf_service import tds_filename

    exclude_groups = _get_exclude_groups(params)
    qap_doc_type = params.get('doc_type', 'PO')
    qap_ref_no   = params.get('ref_no', '')
    qap_ref_date = params.get('ref_date', '')

    records = list(
        TDSInput.objects
        .filter(batch_id=batch_id)
        .select_related('customer', 'standard', 'cover_grade')
        .order_by('tds_id')
    )
    total = len(records)

    buf = io.BytesIO()
    failed  = []
    written = 0
    merger = PdfWriter()
    batch_customer_name = None
    # Guards against two belts landing on the identical filename inside the
    # zip -- e.g. neither has a TDS Document Number and both share the same
    # customer (and neither has been edited, so no "_rev_NN" to tell them
    # apart either), so tds_filename() would return the identical name for
    # both. zipfile happily writes a duplicate entry name, but most unzip
    # tools then only show one of the two, silently hiding a belt's PDF.
    # Disambiguate with the tds_number, which is always unique.
    used_names = set()

    def _unique_zip_name(name, tds_number):
        if name in used_names:
            stem, ext = name.rsplit('.', 1)
            name = f"{stem}_{tds_number}.{ext}"
        used_names.add(name)
        return name

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, r in enumerate(records, start=1):
            if batch_customer_name is None and r.customer_id:
                batch_customer_name = r.customer.customer_name

            try:
                tds_bytes = render_tds_pdf_bytes(r.tds_id, exclude_groups=exclude_groups)
                # Same convention as the single-belt download (base name +
                # conditional "_rev_NN" -- see tds_filename()'s docstring).
                # Previously always "TDS-<number>" plus the doc number,
                # inconsistent with it, and never carried a revision suffix.
                tds_name = _unique_zip_name(tds_filename(r), r.tds_number)
                zf.writestr(tds_name, tds_bytes)
                written += 1
                try:
                    merger.append(io.BytesIO(tds_bytes))
                except Exception:
                    logger.exception(
                        "Merged-PDF append failed for tds_id=%s (batch=%s) - "
                        "this belt is still in the ZIP as its own file, just "
                        "not in the merged copy.", r.tds_id, batch_id
                    )
            except Exception:
                logger.exception(
                    "TDS PDF generation failed for tds_id=%s (batch=%s)", r.tds_id, batch_id
                )
                failed.append(r.tds_number)

            try:
                qap_template = resolve_qap_template(r)
                if qap_template is not None:
                    qap_context = build_qap_context(
                        r, qap_template,
                        doc_type=qap_doc_type, ref_no=qap_ref_no, ref_date=qap_ref_date,
                    )
                    qap_bytes = render_qap_pdf(qap_context)
                    qap_name  = _unique_zip_name(tds_filename(r, doc_suffix='_QAP'), r.tds_number)
                    zf.writestr(qap_name, qap_bytes)
            except Exception:
                logger.exception(
                    "QAP PDF generation failed for tds_id=%s (batch=%s)", r.tds_id, batch_id
                )

            if on_progress:
                on_progress(i, total)

    if written == 0:
        raise RuntimeError('No PDFs could be generated. Check server logs for WeasyPrint errors.')

    safe_customer = _SAFE.sub('_', batch_customer_name or '').strip('_') or f'Batch_{batch_id}'

    if len(merger.pages) > 0:
        try:
            merged_buf = io.BytesIO()
            merger.write(merged_buf)
            with zipfile.ZipFile(buf, 'a', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"{safe_customer}_Merged.pdf", merged_buf.getvalue())
        except Exception:
            logger.exception("Merged-PDF assembly failed for batch=%s", batch_id)
        finally:
            merger.close()
    else:
        merger.close()

    if failed:
        logger.warning(
            "Batch %s zip: %d TDS PDFs skipped (generation errors): %s",
            batch_id, len(failed), failed,
        )

    buf.seek(0)
    return f"{safe_customer}.zip", 'application/zip', buf.read()


def _build_merged_zip_export(batch_id, params, on_progress=None):
    """
    One merged TDS PDF (every belt, each starting its own page) plus each
    belt's individual QAP, in one ZIP. Moved verbatim from batch_views.py's
    former download_batch_merged_zip().

    on_progress(current, total) — see _build_zip_export()'s docstring.
    """
    try:
        from pypdf import PdfWriter
    except ImportError:
        raise RuntimeError(
            "Download Merged PDF requires the 'pypdf' package, which isn't installed."
        )

    from apps.api.routers.pdf_views import render_tds_pdf_bytes
    from apps.services.qap_service import resolve_qap_template, build_qap_context
    from apps.services.pdf_renderer import render_qap_pdf
    from apps.services.pdf_service import tds_filename

    exclude_groups = _get_exclude_groups(params)
    qap_doc_type = params.get('doc_type', 'PO')
    qap_ref_no   = params.get('ref_no', '')
    qap_ref_date = params.get('ref_date', '')

    records = list(
        TDSInput.objects
        .filter(batch_id=batch_id)
        .select_related('customer', 'standard', 'cover_grade')
        .order_by('tds_id')
    )
    total = len(records)

    merger  = PdfWriter()
    merged  = 0
    failed  = []
    batch_customer_name = None
    qap_bundle = []
    # Same duplicate-name guard as _build_zip_export() -- two belts with
    # neither a TDS Document Number nor distinct customers (and no revision
    # history to tell them apart either) would otherwise both produce the
    # identical "<customer>_QAP.pdf" and one would silently disappear from
    # the zip.
    used_qap_names = set()

    for i, r in enumerate(records, start=1):
        if batch_customer_name is None and r.customer_id:
            batch_customer_name = r.customer.customer_name

        try:
            tds_bytes = render_tds_pdf_bytes(r.tds_id, exclude_groups=exclude_groups)
            merger.append(io.BytesIO(tds_bytes))
            merged += 1
        except Exception:
            logger.exception(
                "TDS PDF generation failed for tds_id=%s (batch=%s merged-zip)", r.tds_id, batch_id
            )
            failed.append(r.tds_number)

        try:
            qap_template = resolve_qap_template(r)
            if qap_template is not None:
                qap_context = build_qap_context(
                    r, qap_template,
                    doc_type=qap_doc_type, ref_no=qap_ref_no, ref_date=qap_ref_date,
                )
                qap_bytes = render_qap_pdf(qap_context)
                qap_name  = tds_filename(r, doc_suffix='_QAP')
                if qap_name in used_qap_names:
                    stem, ext = qap_name.rsplit('.', 1)
                    qap_name = f"{stem}_{r.tds_number}.{ext}"
                used_qap_names.add(qap_name)
                qap_bundle.append((qap_name, qap_bytes))
        except Exception:
            logger.exception(
                "QAP PDF generation failed for tds_id=%s (batch=%s merged-zip)", r.tds_id, batch_id
            )

        if on_progress:
            on_progress(i, total)

    if merged == 0:
        merger.close()
        raise RuntimeError('No PDFs could be generated. Check server logs for WeasyPrint errors.')

    safe_customer = _SAFE.sub('_', batch_customer_name or '').strip('_') or f'Batch_{batch_id}'

    merged_buf = io.BytesIO()
    merger.write(merged_buf)
    merger.close()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{safe_customer}_Merged.pdf", merged_buf.getvalue())
        for qap_name, qap_bytes in qap_bundle:
            zf.writestr(qap_name, qap_bytes)
    buf.seek(0)

    if failed:
        logger.warning(
            "Batch %s merged-zip: %d TDS PDFs skipped (generation errors): %s",
            batch_id, len(failed), failed,
        )

    return f"{safe_customer}_Merged.zip", 'application/zip', buf.read()


def _build_print_all_export(batch_id, params, on_progress=None):
    """
    Every belt's TDS PDF merged into one PDF (each belt starting its own
    page). Moved verbatim from batch_views.py's former print_all_batch().

    on_progress(current, total) — see _build_zip_export()'s docstring.
    """
    try:
        from pypdf import PdfWriter
    except ImportError:
        raise RuntimeError(
            "Print All requires the 'pypdf' package, which isn't installed."
        )

    from apps.api.routers.pdf_views import render_tds_pdf_bytes

    exclude_groups = _get_exclude_groups(params)
    records = list(TDSInput.objects.filter(batch_id=batch_id).order_by('tds_id'))
    total = len(records)

    writer  = PdfWriter()
    failed  = []
    merged  = 0

    for i, r in enumerate(records, start=1):
        try:
            pdf_bytes = render_tds_pdf_bytes(r.tds_id, exclude_groups=exclude_groups)
            writer.append(io.BytesIO(pdf_bytes))
            merged += 1
        except Exception:
            logger.exception(
                "PDF generation failed for tds_id=%s (batch=%s print-all)", r.tds_id, batch_id
            )
            failed.append(r.tds_number)

        if on_progress:
            on_progress(i, total)

    if merged == 0:
        raise RuntimeError('No PDFs could be generated. Check server logs for WeasyPrint errors.')

    out = io.BytesIO()
    writer.write(out)
    writer.close()
    out.seek(0)

    if failed:
        logger.warning(
            "Batch %s print-all: %d PDFs skipped (generation errors): %s",
            batch_id, len(failed), failed,
        )

    return f"TDS_Batch_{batch_id}_print_all.pdf", 'application/pdf', out.read()


_EXPORT_BUILDERS = {
    'zip':        _build_zip_export,
    'merged_zip': _build_merged_zip_export,
    'print_all':  _build_print_all_export,
}


# ── Job runner ───────────────────────────────────────────────────────────────

def _run_export_job(job_id):
    """
    Executed on a background thread (see start_export()). Deliberately swallows
    every exception from the builder — there is no request/response left to
    propagate it to by the time this runs, so it's recorded on the job row
    instead, exactly like device_service.py's backgrounded email sends log
    rather than raise.

    Django opens a new DB connection per thread on first use and does not
    close it automatically outside the request/response cycle — unlike the
    backgrounded email sends in device_service.py, this function does touch
    the DB (the job row itself), so it explicitly closes that connection in
    `finally` rather than leaking one open connection per export job.
    """
    from django.db import connections
    try:
        try:
            job = BatchExportJob.objects.get(pk=job_id)
        except BatchExportJob.DoesNotExist:
            return

        job.status = 'running'
        job.save(update_fields=['status'])

        def _on_progress(current, total):
            # Best-effort — a progress row failing to save must never abort
            # the export itself (correctness of the finished file matters
            # far more than the frontend's progress counter).
            try:
                BatchExportJob.objects.filter(pk=job_id).update(
                    progress_current=current, progress_total=total,
                )
            except Exception:
                logger.exception("Batch export job %s: failed to save progress", job_id)

        try:
            builder = _EXPORT_BUILDERS[job.export_type]
            filename, content_type, file_bytes = builder(job.batch_id, job.params, on_progress=_on_progress)
        except Exception as exc:
            logger.exception("Batch export job %s failed (batch=%s, type=%s)", job_id, job.batch_id, job.export_type)
            job.status = 'failed'
            job.error_message = str(exc) or exc.__class__.__name__
            job.completed_at = timezone.now()
            job.save(update_fields=['status', 'error_message', 'completed_at'])
            return

        job.status = 'done'
        job.filename = filename
        job.content_type = content_type
        job.file_bytes = file_bytes
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'filename', 'content_type', 'file_bytes', 'completed_at'])
    finally:
        connections.close_all()


def _sweep_old_jobs():
    BatchExportJob.objects.filter(created_at__lt=timezone.now() - _JOB_TTL).delete()


# ── Views ─────────────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def start_export(request, batch_id):
    """
    POST /api/tds/batch/{batch_id}/export/

    Body: {"export_type": "zip"|"merged_zip"|"print_all",
           "copy": "customer"|"internal", "exclude_groups": [...],
           "doc_type": "PO"|"Enquiry", "ref_no": "...", "ref_date": "..."}

    Creates a BatchExportJob row and starts rendering on a background thread,
    returning immediately with the job id for the frontend to poll rather
    than blocking the request for however long WeasyPrint takes for however
    many belts are in this batch (see module docstring).
    """
    if not TDSBatch.objects.filter(pk=batch_id).exists():
        raise NotFound(f"Batch {batch_id} not found")

    export_type = request.data.get('export_type')
    if export_type not in _EXPORT_BUILDERS:
        raise ValidationError({'export_type': f"must be one of {list(_EXPORT_BUILDERS)}"})

    params = {
        'copy':           request.data.get('copy'),
        'exclude_groups': request.data.get('exclude_groups'),
        'doc_type':       request.data.get('doc_type', 'PO'),
        'ref_no':         request.data.get('ref_no', ''),
        'ref_date':       request.data.get('ref_date', ''),
    }

    _sweep_old_jobs()

    job = BatchExportJob.objects.create(
        batch_id=batch_id, export_type=export_type, params=params,
        created_by=request.user,
    )

    thread = threading.Thread(target=_run_export_job, args=(job.job_id,), daemon=True)
    thread.start()

    return Response({'job_id': job.job_id, 'status': job.status}, status=status.HTTP_202_ACCEPTED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def export_status(request, job_id):
    """GET /api/tds/batch/export/{job_id}/status/"""
    job = BatchExportJob.objects.filter(pk=job_id).first()
    if not job:
        raise NotFound(f"Export job {job_id} not found")
    return Response({
        'job_id': job.job_id,
        'status': job.status,
        'error_message': job.error_message,
        'filename': job.filename,
        'progress_current': job.progress_current,
        'progress_total': job.progress_total,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_export(request, job_id):
    """
    GET /api/tds/batch/export/{job_id}/download/

    Only serves the file once status='done' — a client polling status first
    (as it should) will never hit the pending/running/failed cases, but they
    are handled explicitly rather than left to a confusing 500 on a None
    file_bytes.
    """
    job = BatchExportJob.objects.filter(pk=job_id).first()
    if not job:
        raise NotFound(f"Export job {job_id} not found")

    if job.status == 'failed':
        return Response({'detail': job.error_message or 'Export failed.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    if job.status != 'done':
        return Response({'detail': f"Export is still {job.status}."}, status=status.HTTP_409_CONFLICT)

    # Same "first real download" bookkeeping as the single-TDS download in
    # pdf_views.py::generate_pdf() — a batch export always covers every
    # TDSInput in the batch, so mark them all at once.
    TDSInput.objects.filter(batch_id=job.batch_id, first_downloaded_at__isnull=True) \
        .update(first_downloaded_at=timezone.now())

    resp = HttpResponse(bytes(job.file_bytes), content_type=job.content_type or 'application/octet-stream')
    disposition = 'inline' if job.content_type == 'application/pdf' else 'attachment'
    resp['Content-Disposition'] = f'{disposition}; filename="{job.filename}"'
    return resp
