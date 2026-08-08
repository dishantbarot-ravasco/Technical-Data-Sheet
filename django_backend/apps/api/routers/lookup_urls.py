"""
apps/api/routers/lookup_urls.py — URL patterns for TDS lookup endpoints.
Included at /api/ by apps/api/urls.py.
"""
from django.urls import path
from . import lookup_views as v

urlpatterns = [
    path('tds/lookup',             v.tds_lookup,         name='tds-lookup'),
    path('tds/dimensional-specs',  v.dimensional_specs,  name='dimensional-specs'),
]
