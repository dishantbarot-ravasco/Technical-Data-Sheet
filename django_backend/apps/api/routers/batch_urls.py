"""
apps/api/routers/batch_urls.py — URL patterns for TDS batch endpoints.

Registered in apps/api/urls.py BEFORE tds_urls so that
  /api/tds/batch/ and /api/tds/batch/<id>/
are matched before the <int:tds_id> wildcard in tds_urls.py.
"""
from django.urls import path
from .batch_views import create_batch, get_batch, text_import_batch
from .batch_export_views import start_export, export_status, download_export

urlpatterns = [
    # POST /api/tds/batch/                          — create a new batch (ID-based payload)
    # POST /api/tds/batch/text-import/              — create a batch from text-parsed names
    # GET  /api/tds/batch/<id>/                     — retrieve a batch with all its TDS records
    # POST /api/tds/batch/<id>/export/              — start an async PDF export job (see batch_export_views.py)
    #      body: {"export_type": "zip"|"merged_zip"|"print_all", "copy": "customer"|"internal", ...}
    # GET  /api/tds/batch/export/<job_id>/status/   — poll an export job
    # GET  /api/tds/batch/export/<job_id>/download/ — download the finished export
    path('tds/batch/',                          create_batch,    name='batch-create'),
    path('tds/batch/text-import/',              text_import_batch, name='batch-text-import'),
    path('tds/batch/<int:batch_id>/',           get_batch,       name='batch-detail'),
    path('tds/batch/<int:batch_id>/export/',    start_export,    name='batch-export-start'),
    path('tds/batch/export/<int:job_id>/status/',   export_status,   name='batch-export-status'),
    path('tds/batch/export/<int:job_id>/download/', download_export, name='batch-export-download'),
]
