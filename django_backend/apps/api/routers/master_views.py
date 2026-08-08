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

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError

from apps.core.models import (
    BeltRating, BeltRatingValue, BrandParameter, CoverGrade, CoverGradeValue,
    Customer, FabricStyle, FabricType, IndusBrand,
    PackingType, Purpose, BeltType, ReelType, Standard, TDSParameter,
    ContainerType,
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


# ── Views ─────────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([AllowAny])
def bootstrap(request):
    """Return all static dropdown data in a single DB round-trip."""
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
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def list_purposes(request):
    objs = Purpose.objects.order_by('purpose_id')
    return Response([_purpose(p) for p in objs])


@api_view(['GET'])
@permission_classes([AllowAny])
def list_belt_types(request):
    objs = BeltType.objects.order_by('belt_id')
    return Response([_belt_type(b) for b in objs])


@api_view(['GET'])
@permission_classes([AllowAny])
def list_brands(request):
    objs = IndusBrand.objects.order_by('brand_id')
    return Response([_brand(b) for b in objs])


@api_view(['GET'])
@permission_classes([AllowAny])
def list_standards(request):
    objs = Standard.objects.order_by('standard_id')
    return Response([_standard(s) for s in objs])


@api_view(['GET'])
@permission_classes([AllowAny])
def get_standard(request, standard_id):
    obj = Standard.objects.filter(pk=standard_id).first()
    if not obj:
        raise NotFound(f"Standard {standard_id} not found")
    return Response(_standard(obj))


@api_view(['GET'])
@permission_classes([AllowAny])
def list_cover_grades(request, standard_id):
    if not Standard.objects.filter(pk=standard_id).exists():
        raise NotFound(f"Standard {standard_id} not found")
    objs = CoverGrade.objects.filter(standard_id=standard_id).order_by('grade_code')
    return Response([_cover_grade_brief(g) for g in objs])


@api_view(['GET'])
@permission_classes([AllowAny])
def get_cover_grade(request, grade_id):
    grade = CoverGrade.objects.filter(pk=grade_id).first()
    if not grade:
        raise NotFound(f"Cover grade {grade_id} not found")
    return Response(_cover_grade_full(grade))


@api_view(['GET'])
@permission_classes([AllowAny])
def list_fabric_types(request):
    objs = FabricType.objects.order_by('fabric_code')
    return Response([_fabric_type(f) for f in objs])


@api_view(['GET'])
@permission_classes([AllowAny])
def list_fabric_styles(request, fabric_type_id):
    if not FabricType.objects.filter(pk=fabric_type_id).exists():
        raise NotFound(f"Fabric type {fabric_type_id} not found")
    objs = FabricStyle.objects.filter(fabric_type_id=fabric_type_id).order_by('style_name')
    return Response([_fabric_style(s) for s in objs])


@api_view(['GET'])
@permission_classes([AllowAny])
def list_belt_ratings(request, fabric_type_id):
    if not FabricType.objects.filter(pk=fabric_type_id).exists():
        raise NotFound(f"Fabric type {fabric_type_id} not found")
    objs = BeltRating.objects.filter(fabric_type_id=fabric_type_id).order_by('rating_name')
    return Response([_belt_rating_brief(r) for r in objs])


@api_view(['GET'])
@permission_classes([AllowAny])
def get_belt_rating(request, rating_id):
    rating = BeltRating.objects.filter(pk=rating_id).first()
    if not rating:
        raise NotFound(f"Belt rating {rating_id} not found")
    return Response(_belt_rating_full(rating))


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def customers(request):
    if request.method == 'GET':
        search = request.query_params.get('search')
        limit  = int(request.query_params.get('limit', 50))
        limit  = max(1, min(200, limit))
        qs = Customer.objects.all()
        if search:
            qs = qs.filter(customer_name__icontains=search)
        qs = qs.order_by('customer_name')[:limit]
        return Response([_customer_brief(c) for c in qs])

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


@api_view(['GET'])
@permission_classes([AllowAny])
def list_reel_types(request):
    objs = ReelType.objects.order_by('id')
    return Response([_reel_type(r) for r in objs])


@api_view(['GET'])
@permission_classes([AllowAny])
def list_packing_types(request):
    available_only_str = request.query_params.get('available_only', 'true').lower()
    available_only = available_only_str not in ('false', '0', 'no')
    qs = PackingType.objects.all()
    if available_only:
        qs = qs.filter(is_available=True)
    return Response([_packing_type(p) for p in qs.order_by('id')])


@api_view(['GET'])
@permission_classes([AllowAny])
def list_container_types(request):
    objs = ContainerType.objects.order_by('id')
    return Response([_container_type(c) for c in objs])


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


@api_view(['GET'])
@permission_classes([AllowAny])
def list_parameters(request):
    """
    Return all TDS parameters for a brand, grouped by parameter_group.
    brand_id defaults to 1 (INDUS SUPER BRUTE).
    """
    brand_id = int(request.query_params.get('brand_id', 1))
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
