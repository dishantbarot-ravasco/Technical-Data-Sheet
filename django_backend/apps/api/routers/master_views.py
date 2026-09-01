"""
apps/api/routers/master_views.py — Read-only endpoints for master/reference data.

Ported from FastAPI routers/master.py.  No auth required on GET endpoints;
PATCH /customers/{id} requires IsEditor.

Endpoints:
  GET  /api/bootstrap
  GET  /api/purposes
  GET  /api/belt-types
  GET  /api/brands
  GET  /api/standards
  GET  /api/standards/{id}
  GET  /api/standards/{id}/cover-grades
  GET  /api/cover-grades/{id}
  GET  /api/fabric-types
  GET  /api/fabric-types/{id}/styles
  GET  /api/fabric-types/{id}/belt-ratings
  GET  /api/belt-ratings/{id}
  GET  /api/customers
  POST /api/customers
  PATCH /api/customers/{id}
  GET  /api/reel-types
  GET  /api/packing-types
  GET  /api/container-types
  GET  /api/shipping-constraints
  GET  /api/parameters
"""
import logging
import re

from django.views.decorators.cache import cache_page
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError

# PERFORMANCE: every endpoint below reads from tables that change rarely
# (reference/lookup data seeded once, or edited only occasionally through
# Django Admin) but are read on nearly every generate-TDS page load and
# every dropdown open. cache_page caches the full rendered response per
# distinct URL (path + querystring), so parameterized endpoints like
# list_cover_grades/<standard_id> still get one cache entry per id.
#
# 1 hour is long enough to meaningfully cut DB load, short enough that an
# admin edit to a reference table (cover grade, reel type, etc.) shows up
# without needing a manual cache-bust. cache_page must be the OUTERMOST
# decorator — it caches the already-rendered HttpResponse, so placing it
# under @api_view would try to cache DRF's un-rendered Response and error.
CACHE_TTL_SECONDS = 60 * 60

# Upper bound on how many rows customers' search fetches before ranking —
# see the PERFORMANCE note at its call site in customers() below.
_SEARCH_CANDIDATE_CAP = 500

from apps.core.models import (
    BeltRating, CoverGrade, Customer, FabricStyle, FabricType, IndusBrand,
    PackingType, Purpose, BeltType, ReelType, Standard, TDSParameter,
    ContainerType, SpliceStepLookup, SpliceMethodConfig,
)
from apps.api.permissions import IsEditor
from apps.services.calculations import get_container_constraints

logger = logging.getLogger(__name__)


# ── Serialiser helpers ───────────────────────────────────────────────────────
# These convert ORM instances to plain dicts that match the FastAPI Pydantic schemas.

def _purpose(p):
    return {"purpose_id": p.purpose_id, "purpose_type": p.purpose_type}

def _belt_type(b):
    return {"belt_id": b.belt_id, "belt_type": b.belt_type}

def _brand(b):
    return {"brand_id": b.brand_id, "brand_name": b.brand_name}

def _standard(s):
    return {
        "standard_id":          s.standard_id,
        "standard_name":        s.standard_name,
        "standard_edition":     s.standard_edition,
        "standard_description": s.standard_description,
        "standard_country":     s.standard_country,
        "brand_id":             s.brand_id,
    }

def _cover_grade_brief(g):
    return {
        "id":                g.id,
        "standard_id":       g.standard_id,
        "grade_code":        g.grade_code,
        "grade_description": g.grade_description,
        "specific_gravity":  float(g.specific_gravity),
    }

def _eav_value(v):
    return {
        "parameter_id":    v.parameter_id,
        "parameter_name":  v.parameter.parameter_name,
        "parameter_group": v.parameter.parameter_group,
        "spec_value":      v.spec_value,
        "indus_value":     v.indus_value,
    }

def _cover_grade_full(grade):
    values = sorted(grade.values.select_related('parameter').all(),
                    key=lambda x: (x.parameter.parameter_group, x.parameter.display_order))
    return {
        "id":                grade.id,
        "standard_id":       grade.standard_id,
        "grade_code":        grade.grade_code,
        "grade_description": grade.grade_description,
        "specific_gravity":  float(grade.specific_gravity),
        "values":            [_eav_value(v) for v in values],
    }

def _fabric_type(f):
    return {
        "id":           f.id,
        "fabric_code":  f.fabric_code,
        "description":  f.description,
        "manufacturer": f.manufacturer,
    }

def _fabric_style(s):
    return {
        "id":             s.id,
        "fabric_type_id": s.fabric_type_id,
        "style_name":     s.style_name,
    }

def _belt_rating_brief(r):
    return {
        "id":             r.id,
        "fabric_type_id": r.fabric_type_id,
        "rating_name":    r.rating_name,
    }

def _belt_rating_full(rating):
    values = sorted(rating.values.select_related('parameter').all(),
                    key=lambda x: x.parameter.display_order)
    return {
        "id":             rating.id,
        "fabric_type_id": rating.fabric_type_id,
        "rating_name":    rating.rating_name,
        "values":         [_eav_value(v) for v in values],
    }

def _customer_brief(c):
    return {
        "customer_id":    c.customer_id,
        "customer_name":  c.customer_name,
        "contact_person": c.contact_person,
        "application":    c.application,
        "plant_location": c.plant_location,
    }

def _reel_type(r):
    return {
        "id":                   r.id,
        "reel_name":            r.reel_name,
        "formula_key":          r.formula_key,
        "num_rolls_base":       r.num_rolls_base,
        "core_diameter_m":      float(r.core_diameter_m),
        "center_to_center_m":   float(r.center_to_center_m) if r.center_to_center_m else None,
        "max_roll_diameter_m":  float(r.max_roll_diameter_m),
    }

def _packing_type(p):
    return {
        "id":           p.id,
        "packing_name": p.packing_name,
        "is_available": p.is_available,
    }


def _container_type(c):
    return {
        "id":           c.id,
        "name":         c.name,
        "max_height_m": float(c.max_height_m),
        "max_width_m":  float(c.max_width_m),
    }


def _build_splicing_config():
    """
    Return the live splice step table + method buffers from the DB.
    Falls back to IS 14206 hardcoded defaults only if the tables are empty,
    so the frontend and PDF always agree.
    """
    steps = list(
        SpliceStepLookup.objects
        .order_by('max_fabric_rating_kn_m')
        .values('max_fabric_rating_kn_m', 'step_length_mm')
    )
    # Default IS 14206 table — used only when DB table is empty
    if not steps:
        steps = [
            {"max_fabric_rating_kn_m": 100, "step_length_mm": 150},
            {"max_fabric_rating_kn_m": 125, "step_length_mm": 200},
            {"max_fabric_rating_kn_m": 160, "step_length_mm": 200},
            {"max_fabric_rating_kn_m": 200, "step_length_mm": 250},
            {"max_fabric_rating_kn_m": 250, "step_length_mm": 300},
            {"max_fabric_rating_kn_m": 300, "step_length_mm": 350},
            {"max_fabric_rating_kn_m": 315, "step_length_mm": 350},
            {"max_fabric_rating_kn_m": 350, "step_length_mm": 400},
            {"max_fabric_rating_kn_m": 400, "step_length_mm": 400},
        ]

    buffers = {"hot": 50, "cold": 75}  # IS 14206 defaults
    for cfg in SpliceMethodConfig.objects.all():
        buffers[cfg.vulcanization_method.lower()] = cfg.buffer_mm

    return {"step_table": steps, "buffers": buffers}


# ── Views ─────────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([AllowAny])
def bootstrap(request):
    """
    Return all static dropdown data in a single DB round-trip.

    NOT cache_page'd (unlike the other endpoints in this file): this bundles
    in `customers`, which changes whenever a user types a new one into the
    generate-TDS form (POST /api/customers) — caching the whole response
    would hide a just-created customer from the autocomplete for up to
    CACHE_TTL_SECONDS. Every other list here is served from the individually
    cached endpoints below anyway when called directly.
    """
    standards     = list(Standard.objects.order_by('standard_id'))
    purposes      = list(Purpose.objects.order_by('purpose_id'))
    belt_types    = list(BeltType.objects.order_by('belt_id'))
    brands        = list(IndusBrand.objects.order_by('brand_id'))
    fabric_types  = list(FabricType.objects.order_by('fabric_code'))
    reel_types      = list(ReelType.objects.order_by('id'))
    packing_types   = list(PackingType.objects.filter(is_available=True).order_by('id'))
    container_types = list(ContainerType.objects.order_by('id'))
    customers       = list(Customer.objects.order_by('customer_name')[:100])

    return Response({
        "standards":       [_standard(s)       for s in standards],
        "purposes":        [_purpose(p)        for p in purposes],
        "belt_types":      [_belt_type(b)      for b in belt_types],
        "brands":          [_brand(b)          for b in brands],
        "fabric_types":    [_fabric_type(f)    for f in fabric_types],
        "reel_types":      [_reel_type(r)      for r in reel_types],
        "packing_types":   [_packing_type(p)   for p in packing_types],
        "container_types": [_container_type(c) for c in container_types],
        "customers":       [_customer_brief(c) for c in customers],
        "splicing_config": _build_splicing_config(),
    })


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_purposes(request):
    objs = Purpose.objects.order_by('purpose_id')
    return Response([_purpose(p) for p in objs])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_belt_types(request):
    objs = BeltType.objects.order_by('belt_id')
    return Response([_belt_type(b) for b in objs])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_brands(request):
    objs = IndusBrand.objects.order_by('brand_id')
    return Response([_brand(b) for b in objs])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_standards(request):
    objs = Standard.objects.order_by('standard_id')
    return Response([_standard(s) for s in objs])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def get_standard(request, standard_id):
    obj = Standard.objects.filter(pk=standard_id).first()
    if not obj:
        raise NotFound(f"Standard {standard_id} not found")
    return Response(_standard(obj))


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_cover_grades(request, standard_id):
    if not Standard.objects.filter(pk=standard_id).exists():
        raise NotFound(f"Standard {standard_id} not found")
    objs = CoverGrade.objects.filter(standard_id=standard_id).order_by('grade_code')
    return Response([_cover_grade_brief(g) for g in objs])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def get_cover_grade(request, grade_id):
    grade = CoverGrade.objects.filter(pk=grade_id).first()
    if not grade:
        raise NotFound(f"Cover grade {grade_id} not found")
    return Response(_cover_grade_full(grade))


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_fabric_types(request):
    objs = FabricType.objects.order_by('fabric_code')
    return Response([_fabric_type(f) for f in objs])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_fabric_styles(request, fabric_type_id):
    if not FabricType.objects.filter(pk=fabric_type_id).exists():
        raise NotFound(f"Fabric type {fabric_type_id} not found")
    objs = FabricStyle.objects.filter(fabric_type_id=fabric_type_id).order_by('style_name')
    return Response([_fabric_style(s) for s in objs])


def _rating_sort_key(rating_name):
    """
    rating_name is free text like 'EP 1000/5' - sorting it as a string puts
    'EP 1000/5' before 'EP 200/3' (since '1' < '2' lexicographically). Parse
    out the kN/m and ply-count numbers so the dropdown lists ratings in the
    ascending numeric order users actually expect.
    """
    m = re.search(r'(\d+(?:\.\d+)?)\s*/\s*(\d+)', rating_name or '')
    if not m:
        return (float('inf'), float('inf'), rating_name or '')
    return (float(m.group(1)), int(m.group(2)), rating_name or '')


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_belt_ratings(request, fabric_type_id):
    if not FabricType.objects.filter(pk=fabric_type_id).exists():
        raise NotFound(f"Fabric type {fabric_type_id} not found")
    objs = list(BeltRating.objects.filter(fabric_type_id=fabric_type_id))
    objs.sort(key=lambda r: _rating_sort_key(r.rating_name))
    return Response([_belt_rating_brief(r) for r in objs])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def get_belt_rating(request, rating_id):
    rating = BeltRating.objects.filter(pk=rating_id).first()
    if not rating:
        raise NotFound(f"Belt rating {rating_id} not found")
    return Response(_belt_rating_full(rating))


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def resolve_belt_ratings(request):
    """
    GET /api/belt-ratings/resolve?rating=1000/5

    Find every BeltRating across ALL fabric types whose rating_name ends
    with the given bare "<kN>/<plies>" number (e.g. "1000/5") -- used by
    generate-tds.js's belt-description paste-parser to figure out which
    Fabric Type a pasted rating belongs to when Fabric Type hasn't been
    selected yet.

    This only exists because the display convention strips the fabric-code
    prefix off rating_name (see calculations.strip_fabric_prefix) -- before
    that, the pasted text carried its own fabric code as a leading word and
    no cross-fabric search was needed. rating_name's format guarantees the
    number is always the exact suffix after "<fabric_code> ", so an
    iendswith match on " <rating>" can't accidentally match a different
    number sharing a substring (e.g. querying "1000/5" cannot match a stored
    "1000/50" -- the trailing digit differs).
    """
    rating_text = (request.query_params.get('rating') or '').strip()
    if not rating_text:
        raise ValidationError({'detail': 'rating query parameter is required.'})

    matches = list(BeltRating.objects.filter(rating_name__iendswith=f" {rating_text}"))
    matches.sort(key=lambda r: _rating_sort_key(r.rating_name))
    return Response([_belt_rating_brief(r) for r in matches])


def _customer_search_tier(name, search_lc):
    """
    0 = name starts with the query, 1 = some word in the name starts with it
    (e.g. "Alliance Fibres" for "f"), 2 = query only appears mid-word.

    Needed because plain `.order_by('customer_name')[:limit]` sorts and
    truncates alphabetically BEFORE relevance is considered - a search for a
    common letter like "s" can have far more than `limit` alphabetically-early
    matches that merely contain an "s" somewhere, so real "starts with S"
    matches get cut off by the slice and never even reach this ranking. Rank
    first, slice second.
    """
    name_lc = (name or '').lower()
    if name_lc.startswith(search_lc):
        return 0
    if any(word.startswith(search_lc) for word in name_lc.split()):
        return 1
    return 2


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
# SECURITY (fixed): this was AllowAny, so GET exposed customer PII
# (contact_person, plant_location) and POST let anyone create customer
# records with no login and no rate limit. Every real caller (getCustomers/
# createCustomer in api.js) is already only reached from logged-in pages, so
# this brings the enforcement in line with what the frontend already assumes.
def customers(request):
    if request.method == 'GET':
        search = request.query_params.get('search')
        try:
            limit = int(request.query_params.get('limit', 50))
        except (TypeError, ValueError):
            raise ValidationError({'detail': 'limit must be an integer.'})
        limit  = max(1, min(200, limit))
        qs = Customer.objects.all()
        if search:
            search_lc = search.lower()
            # PERFORMANCE: this used to materialize every matching row into
            # Python before ranking/sorting, with no upper bound on the
            # initial fetch (only the final result was capped at `limit`) --
            # a broad search term (e.g. a common letter) against a large
            # customers table would pull thousands of rows into memory just
            # to rank and throw most of them away. Capping the candidate set
            # at _SEARCH_CANDIDATE_CAP bounds that while still ranking
            # correctly for any realistic `limit` (<= 200); it only changes
            # behavior if a single search term has more matches than the cap,
            # in which case the lowest-relevance-tier matches beyond the cap
            # (already the ones this endpoint is least likely to want to
            # return) may not be considered.
            objs = list(
                qs.filter(customer_name__icontains=search)
                  .order_by('customer_name')[:_SEARCH_CANDIDATE_CAP]
            )
            objs.sort(key=lambda c: (
                _customer_search_tier(c.customer_name, search_lc),
                (c.customer_name or '').lower(),
            ))
            objs = objs[:limit]
        else:
            objs = list(qs.order_by('customer_name')[:limit])
        return Response([_customer_brief(c) for c in objs])

    # POST — create customer
    data    = request.data
    name    = (data.get('customer_name') or '').strip()
    if not name:
        raise ValidationError({'customer_name': 'This field is required.'})
    obj = Customer.objects.create(
        customer_name  = name,
        contact_person = data.get('contact_person'),
        application    = data.get('application'),
        plant_location = data.get('plant_location'),
    )
    return Response(_customer_brief(obj), status=status.HTTP_201_CREATED)


@api_view(['PATCH'])
@permission_classes([IsEditor])
def update_customer(request, customer_id):
    obj = Customer.objects.filter(pk=customer_id).first()
    if not obj:
        raise NotFound(f"Customer {customer_id} not found")
    data = request.data
    for field in ('contact_person', 'application', 'plant_location'):
        if field in data and data[field] is not None:
            setattr(obj, field, data[field])
    obj.save()
    return Response(_customer_brief(obj))


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_reel_types(request):
    objs = ReelType.objects.order_by('id')
    return Response([_reel_type(r) for r in objs])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_packing_types(request):
    available_only_str = request.query_params.get('available_only', 'true').lower()
    available_only = available_only_str not in ('false', '0', 'no')
    qs = PackingType.objects.all()
    if available_only:
        qs = qs.filter(is_available=True)
    return Response([_packing_type(p) for p in qs.order_by('id')])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_container_types(request):
    objs = ContainerType.objects.order_by('id')
    return Response([_container_type(c) for c in objs])


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def shipping_constraints(request):
    """
    GET /api/shipping-constraints?container_type_id=&region=

    Returns the max_height_m / max_width_m / max_gross_weight_kg trio for an
    international shipment, straight from the DB (container_types +
    region_container_weight_limits), via calculations.get_container_constraints().

    This is what the generate-TDS "Packing Preview" panel calls for the live
    international-shipping check, instead of the frontend keeping its own
    hardcoded copy of these numbers — a hardcoded copy can silently drift out
    of sync with the database, this call never can.
    """
    container_type_id = request.query_params.get('container_type_id')
    region             = request.query_params.get('region')
    if not container_type_id or not region:
        raise ValidationError({'detail': 'container_type_id and region are required.'})

    try:
        c = get_container_constraints(int(container_type_id), region)
    except ValueError as exc:
        raise NotFound(str(exc))

    return Response({
        "max_height_m":        c.max_height_m,
        "max_width_m":         c.max_width_m,
        "max_gross_weight_kg": c.max_gross_weight_kg,
    })


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def get_splicing_config(request):
    """
    GET /api/splicing-config

    Returns the splice step lookup table and method buffers from the DB so
    the frontend calculators never have to hardcode them.  The same data is
    also embedded in /api/bootstrap (splicing_config key) so generate-tds.js
    gets it for free with zero extra requests.

    AllowAny, matching every other endpoint in this file (see module
    docstring: "No auth required on GET endpoints") and matching bootstrap's
    own AllowAny — this view previously required IsAuthenticated, but
    @cache_page sits above @permission_classes and short-circuits on a cache
    hit by returning the stored HttpResponse without ever re-invoking the
    view, so the permission check silently stopped applying after the first
    cache fill anyway. Since the identical data is already public via
    bootstrap, AllowAny here just makes the real behavior match the
    intended one instead of leaving a permission check that only worked
    for the first request per cache period.

    Response shape:
        {
          "step_table": [{"max_fabric_rating_kn_m": 100, "step_length_mm": 150}, ...],
          "buffers":    {"hot": 50, "cold": 75}
        }
    """
    return Response(_build_splicing_config())


@cache_page(CACHE_TTL_SECONDS)
@api_view(['GET'])
@permission_classes([AllowAny])
def list_parameters(request):
    """
    Return all TDS parameters for a brand, grouped by parameter_group.
    brand_id defaults to 1 (INDUS SUPER BRUTE).
    """
    try:
        brand_id = int(request.query_params.get('brand_id', 1))
    except (TypeError, ValueError):
        raise ValidationError({'detail': 'brand_id must be an integer.'})
    rows = (
        TDSParameter.objects
        .filter(brand_parameters__brand_id=brand_id)
        .order_by('parameter_group', 'brand_parameters__display_order')
        .distinct()
    )
    result = {}
    for p in rows:
        result.setdefault(p.parameter_group, []).append({
            "parameter_id":   p.parameter_id,
            "parameter_name": p.parameter_name,
        })
    return Response(result)
