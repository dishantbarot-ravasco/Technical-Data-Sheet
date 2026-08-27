"""
apps/api/routers/batch_urls.py — URL patterns for TDS batch endpoints.

Registered in apps/api/urls.py BEFORE tds_urls so that
  /api/tds/batch/ and /api/tds/batch/<id>/
are matched before the <int:tds_id> wildcard in tds_urls.py.
"""
from django.urls import path
from .batch_views import (
    create_batch, get_batch, download_batch_zip, download_batch_merged_zip,
    text_import_batch, print_all_batch,
)

urlpatterns = [
    # POST /api/tds/batch/                          — create a new batch (ID-based payload)
    # POST /api/tds/batch/text-import/              — create a batch from text-parsed names
    # GET  /api/tds/batch/<id>/                     — retrieve a batch with all its TDS records
    # GET  /api/tds/batch/<id>/download-zip/        — download all PDFs as a ZIP archive (?copy=customer|internal, ?doc_type=/?ref_no=/?ref_date= for QAP)
    # GET  /api/tds/batch/<id>/download-merged-zip/ — download one merged TDS PDF + per-belt QAP PDFs as a ZIP (same params)
    # GET  /api/tds/batch/<id>/print-all/           — merge all PDFs into one for printing (?copy=customer|internal)
    path('tds/batch/',                                    create_batch,              name='batch-create'),
    path('tds/batch/text-import/',                        text_import_batch,         name='batch-text-import'),
    path('tds/batch/<int:batch_id>/',                     get_batch,                 name='batch-detail'),
    path('tds/batch/<int:batch_id>/download-zip/',        download_batch_zip,        name='batch-download-zip'),
    path('tds/batch/<int:batch_id>/download-merged-zip/', download_batch_merged_zip, name='batch-download-merged-zip'),
    path('tds/batch/<int:batch_id>/print-all/',           print_all_batch,           name='batch-print-all'),
]
