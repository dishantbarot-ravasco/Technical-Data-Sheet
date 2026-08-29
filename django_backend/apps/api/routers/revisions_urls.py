"""
apps/api/routers/revisions_urls.py — URL patterns for TDS version-history.
Included at /api/ by apps/api/urls.py.
"""
from django.urls import path
from . import revisions_views as v

urlpatterns = [
    path('tds/<int:tds_id>/revisions',                        v.list_revisions, name='tds-revisions-list'),
    path('tds/<int:tds_id>/revisions/<int:revision_number>',  v.get_revision,   name='tds-revision-detail'),
]
