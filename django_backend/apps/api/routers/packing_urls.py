"""
apps/api/routers/packing_urls.py — URL patterns for packing computation endpoints.
Included at /api/ by apps/api/urls.py.

Neither of these endpoints currently has a frontend caller (packing is computed
inline as part of create_tds/create_batch instead) — they exist for direct API
use / future UI features.
"""
from django.urls import path
from . import packing_views as v

urlpatterns = [
    # ROBUSTNESS (fixed): the two endpoints below used to be distinguished only
    # by a trailing slash (POST 'tds/<id>/packing' vs PATCH 'tds/<id>/packing/'),
    # which is fragile and easy to misroute or confuse. They're kept, unchanged,
    # for any existing caller, but the two paths below are the clear, intended
    # names going forward — same views, no behavior change.
    path('tds/<int:tds_id>/packing/compute',   v.compute_packing_for_tds,    name='packing-compute'),
    path('tds/<int:tds_id>/packing/recompute', v.recompute_packing_for_tds, name='packing-recompute'),

    # Legacy paths — kept so nothing currently pointed at these breaks.
    path('tds/<int:tds_id>/packing',  v.compute_packing_for_tds,    name='packing-compute-legacy'),
    path('tds/<int:tds_id>/packing/', v.recompute_packing_for_tds, name='packing-recompute-legacy'),
]
