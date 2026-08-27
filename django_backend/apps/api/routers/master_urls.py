"""
apps/api/routers/master_urls.py — URL patterns for master/reference data.
Included at /api/ by apps/api/urls.py.
"""
from django.urls import path
from . import master_views as v

urlpatterns = [
    path('bootstrap',                                   v.bootstrap,           name='bootstrap'),
    path('purposes',                                    v.list_purposes,       name='purposes'),
    path('belt-types',                                  v.list_belt_types,     name='belt-types'),
    path('brands',                                      v.list_brands,         name='brands'),
    path('standards',                                   v.list_standards,      name='standards-list'),
    path('standards/<int:standard_id>',                 v.get_standard,        name='standard-detail'),
    path('standards/<int:standard_id>/cover-grades',   v.list_cover_grades,   name='cover-grades-list'),
    path('cover-grades/<int:grade_id>',                 v.get_cover_grade,     name='cover-grade-detail'),
    path('fabric-types',                                v.list_fabric_types,   name='fabric-types'),
    path('fabric-types/<int:fabric_type_id>/styles',   v.list_fabric_styles,  name='fabric-styles'),
    path('fabric-types/<int:fabric_type_id>/belt-ratings', v.list_belt_ratings, name='belt-ratings-list'),
    path('belt-ratings/<int:rating_id>',                v.get_belt_rating,     name='belt-rating-detail'),
    path('customers',                                   v.customers,           name='customers'),
    path('customers/<int:customer_id>',                 v.update_customer,     name='customer-update'),
    path('reel-types',                                  v.list_reel_types,     name='reel-types'),
    path('packing-types',                               v.list_packing_types,  name='packing-types'),
    path('container-types',                             v.list_container_types, name='container-types'),
    path('shipping-constraints',                        v.shipping_constraints, name='shipping-constraints'),
    path('splicing-config',                             v.get_splicing_config, name='splicing-config'),
    path('parameters',                                  v.list_parameters,     name='parameters'),
]
