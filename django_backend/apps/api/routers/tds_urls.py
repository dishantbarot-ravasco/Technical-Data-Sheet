"""
apps/api/routers/tds_urls.py — URL patterns for TDS CRUD endpoints.
Included at /api/ by apps/api/urls.py.

Note: lookup URLs (/tds/lookup, /tds/dimensional-specs) are in lookup_urls.py
and must be registered BEFORE these so they are matched first.
"""
from django.urls import path
from . import tds_views as v

urlpatterns = [
    path('tds',                       v.create_tds,     name='tds-create'),
    path('tds/',                      v.list_tds,       name='tds-list'),
    path('tds/<int:tds_id>',          v.get_tds,        name='tds-detail'),
    path('tds/<int:tds_id>/approve',  v.approve_tds,    name='tds-approve'),
    path('tds/<int:tds_id>/decline',  v.decline_tds,    name='tds-decline'),
    path('tds/<int:tds_id>/status',   v.update_status,  name='tds-status'),
    path('tds/<int:tds_id>/',         v.delete_tds,     name='tds-delete'),
]
