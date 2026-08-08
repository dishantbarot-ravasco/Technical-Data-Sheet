"""
apps/api/routers/pdf_urls.py — URL patterns for PDF generation endpoints.
Included at /api/ by apps/api/urls.py.
"""
from django.urls import path
from . import pdf_views as v

urlpatterns = [
    path('tds/<int:tds_id>/pdf', v.generate_pdf, name='tds-pdf'),
]
