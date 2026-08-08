"""
apps/api/routers/packing_urls.py — URL patterns for packing computation endpoints.
Included at /api/ by apps/api/urls.py.
"""
from django.urls import path
from . import packing_views as v

urlpatterns = [
    path('tds/<int:tds_id>/packing', v.compute_packing_for_tds,    name='packing-compute'),
    path('tds/<int:tds_id>/packing/', v.recompute_packing_for_tds, name='packing-recompute'),
]
