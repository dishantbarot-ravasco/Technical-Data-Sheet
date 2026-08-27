"""
apps/api/routers/tds_views.py — TDS document CRUD endpoints.

Ported from FastAPI routers/tds_inputs.py.

Endpoints:
  POST   /api/tds
  GET    /api/tds
  GET    /api/tds/{id}
  PATCH  /api/tds/{id}/approve
  PATCH  /api/tds/{id}/decline
  PATCH  /api/tds/{id}/status
  DELETE /api/tds/{id}
"""
import math
import logging
from datetime import datetime, timezone, date

from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError

from apps.core.models import (
    BeltRating, BeltRatingValue, CoverGrade, Customer, FabricType,
    IndusBrand, BeltType, PackingType, Purpose, ReelType,
    Standard, TDSInput, TDSUser,
)
from apps.api.permissions import IsEditor, IsCreator
from apps.services.calculations import (
    belt_weight_per_metre, parse_belt_rating, auto_select_fabric_style,
    validate_endless_belt_length,
)
from apps.services.splicing_service import compute_splicing
from apps.services.packing_service import compute_packing
from apps.services.tds_number import next_tds_number
from apps.core.audit_log import log_tds_action, TDSAuditLog

logger = logging.getLogger(__name__)

# belt_rating_values parameter_ids
PARAM_CARCASS_THICKNESS = 4
PARAM_INTERPLY_SKIM = 5


# ── Serialiser helpers ────────────────────────────────────────────────────────

def _user_brief(u):
    if u is None:
        return None
    return {
        "user_id":     u.user_id,
        "email":       u.email,
        "full_name":   u.full_name or '',
        "role":        u.role,
        "designation": u.designation or '',
    }

def _tds_brief(t):
    return {
        "tds_id":              t.tds_id,
        "tds_number":          t.tds_number,
        "tds_doc_number":      t.tds_doc_number,
        "tds_date":            str(t.tds_date) if t.tds_date else None,
        "status":              t.status,
        "construction_type":   t.construction_type,
        "standard_id":         t.standard_id,
        "customer_id":         t.customer_id,
        "belt_width_mm":       t.belt_width_mm,
        "belt_length_m":       float(t.belt_length_m) if t.belt_length_m else None,
        "created_by":          t.created_by_id,
        "created_by_id":       t.created_by_id,
        "created_at":          t.created_at.isoformat() if t.created_at else None,
        # Nested objects — frontend accesses t.customer?.customer_name, t.standard?.standard_name, etc.
        "customer":        {"customer_name": t.customer.customer_name}    if t.customer_id else None,
        "standard":        {"standard_name": t.standard.standard_name}    if t.standard_id else None,
        "belt_rating":     {"rating_name":   t.belt_rating.rating_name}   if t.belt_rating_id else None,
        # Nested user — admin panel uses t.created_by_user?.full_name
        "created_by_user": _user_brief(t.created_by)                      if t.created_by_id else None,
    }

def _tds_full(t):
    """Full TDS response matching FastAPI TDSOut schema."""
    return {
        "tds_id":                   t.tds_id,
        "tds_number":               t.tds_number,
        "tds_doc_number":           t.tds_doc_number,
        "tds_date":                 str(t.tds_date) if t.tds_date else None,
        "status":                   t.status,
        "construction_type":        t.construction_type,
        # FK IDs
        "purpose_id":               t.purpose_id,
        "belt_type_id":             t.belt_type_id,
        "brand_id":                 t.brand_id,
        "standard_id":              t.standard_id,
        "customer_id":              t.customer_id,
        "cover_grade_id":           t.cover_grade_id,
        "fabric_type_id":           t.fabric_type_id,
        "fabric_style_id":          t.fabric_style_id,
        "belt_rating_id":           t.belt_rating_id,
        "reel_type_id":             t.reel_type_id,
        "packing_type_id":          t.packing_type_id,
        "container_type_id":        t.container_type_id,
        # Flat denormalized names (kept for backward compat)
        "purpose_type":             t.purpose.purpose_type if t.purpose_id else None,
        "belt_type":                t.belt_type.belt_type if t.belt_type_id else None,
        "brand_name":               t.brand.brand_name if t.brand_id else None,
        "standard_name":            t.standard.standard_name if t.standard_id else None,
        "customer_name":            t.customer.customer_name if t.customer_id else None,
        "cover_grade_code":         t.cover_grade.grade_code if t.cover_grade_id else None,
        "fabric_code":              t.fabric_type.fabric_code if t.fabric_type_id else None,
        "belt_rating_name":         t.belt_rating.rating_name if t.belt_rating_id else None,
        # Nested objects — frontend populateModal() accesses t.standard?.standard_name, etc.
        "standard":     {"standard_name": t.standard.standard_name}    if t.standard_id    else None,
        "purpose":      {"purpose_type":  t.purpose.purpose_type}       if t.purpose_id     else None,
        "customer": {
            "customer_name":  t.customer.customer_name  if t.customer_id else None,
            # application & plant_location are columns on the customers table (Customer model),
            # nested here because the frontend reads t.customer?.application / t.customer?.plant_location
            "application":    t.customer.application    if t.customer_id else None,
            "plant_location": t.customer.plant_location if t.customer_id else None,
        },
        "belt_rating":  {"rating_name":  t.belt_rating.rating_name}    if t.belt_rating_id else None,
        "fabric_type":  {"fabric_code":  t.fabric_type.fabric_code}     if t.fabric_type_id else None,
        "cover_grade":  {"grade_code":   t.cover_grade.grade_code}      if t.cover_grade_id else None,
        "reel_type":    {"reel_name":    t.reel_type.reel_name}          if t.reel_type_id   else None,
        "packing_type": {"packing_name": t.packing_type.packing_name}   if t.packing_type_id else None,
        # Belt dimensions
        "belt_description":         t.belt_description,
        "belt_length_m":            float(t.belt_length_m) if t.belt_length_m else None,
        "belt_weight_per_m_kg":     float(t.belt_weight_per_m_kg) if t.belt_weight_per_m_kg else None,
        "make_of_fabric":           t.make_of_fabric,
        "belt_width_mm":            t.belt_width_mm,
        "num_plies":                t.num_plies,
        "top_cover_mm":             float(t.top_cover_mm) if t.top_cover_mm else None,
        "bottom_cover_mm":          float(t.bottom_cover_mm) if t.bottom_cover_mm else None,
        "carcass_from_rating":      float(t.carcass_from_rating) if t.carcass_from_rating else None,
        "carcass_thickness_mm":     float(t.carcass_thickness_mm) if t.carcass_thickness_mm else None,
        "interply_skim_mm":         float(t.interply_skim_mm) if t.interply_skim_mm else None,
        "total_thickness_mm":       float(t.total_thickness_mm) if t.total_thickness_mm else None,
        "breaker_top":              t.breaker_top,
        "breaker_top_plies":        t.breaker_top_plies,
        "breaker_bottom":           t.breaker_bottom,
        "breaker_bottom_plies":     t.breaker_bottom_plies,
        "edge_construction":        t.edge_construction,
        # Packing
        "num_rolls":                t.num_rolls,
        "length_per_roll_m":        float(t.length_per_roll_m) if t.length_per_roll_m else None,
        "roll_dimensions":          t.roll_dimensions,
        "net_weight_kg":            float(t.net_weight_kg) if t.net_weight_kg else None,
        "gross_weight_kg":          float(t.gross_weight_kg) if t.gross_weight_kg else None,
        "gross_weight_per_roll_kg": float(t.gross_weight_per_roll_kg) if t.gross_weight_per_roll_kg else None,
        # International
        "shipping_region":          t.shipping_region,
        # Splicing
        "splicing_required":        t.splicing_required,
        "vulcanization_method":     t.vulcanization_method,
        "num_joints":               t.num_joints,
        "step_length_mm":           t.step_length_mm,
        "splice_length_mm":         t.splice_length_mm,
        "total_extra_length_m":     float(t.total_extra_length_m) if t.total_extra_length_m else None,
        # Audit
        "created_by":               _user_brief(t.created_by) if t.created_by_id else None,
        "approved_by":              _user_brief(t.approved_by) if t.approved_by_id else None,
        "approved_at":              t.approved_at.isoformat() if t.approved_at else None,
        "created_at":               t.created_at.isoformat() if t.created_at else None,
        "updated_at":               t.updated_at.isoformat() if t.updated_at else None,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _require_obj(model, pk_val, label):
    """Fetch by PK or raise NotFound."""
    obj = model.objects.filter(pk=pk_val).first()
    if not obj:
        raise NotFound(f"{label} with id={pk_val} not found")
    return obj


def _load_full(tds_id):
    """
    Load TDS with all FK relationships in one query to avoid N+1 calls.
    select_related follows FK relationships; prefetch_related follows reverse FK / M2M.
    """
    record = (
        TDSInput.objects
        .select_related(
            'purpose', 'belt_type', 'brand', 'standard', 'customer',
            'cover_grade', 'fabric_type', 'fabric_style', 'belt_rating',
            'reel_type', 'packing_type', 'created_by', 'approved_by',
        )
        .filter(pk=tds_id)
        .first()
    )
    if not record:
        raise NotFound(f"TDS {tds_id} not found")
    return record


# ── Views ─────────────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsCreator])
def create_tds(request):
    """
    Create a new Technical Data Sheet record.

    Validates FK references, runs server-side computations (thickness, weight,
    packing, splicing), assigns a TDS number atomically, then saves.
    """
    p = request.data
    current_user = request.user

    # ── Validate FK references ────────────────────────────────────────────────
    _require_obj(Standard,  p.get('standard_id'),   "Standard")
    _require_obj(BeltType,  p.get('belt_type_id'),  "Belt type")
    _require_obj(IndusBrand, p.get('brand_id'),     "Brand")
    _require_obj(Purpose,   p.get('purpose_id'),    "Purpose")

    cover_grade = _require_obj(CoverGrade, p.get('cover_grade_id'), "Cover grade")
    if cover_grade.standard_id != p.get('standard_id'):
        raise ValidationError({'detail': f"Cover grade belongs to standard_id={cover_grade.standard_id}, not {p.get('standard_id')}."})

    belt_rating = _require_obj(BeltRating, p.get('belt_rating_id'), "Belt rating")
    _require_obj(FabricType, p.get('fabric_type_id'), "Fabric type")
    if belt_rating.fabric_type_id != p.get('fabric_type_id'):
        raise ValidationError({'detail': f"Belt rating belongs to fabric_type_id={belt_rating.fabric_type_id}, not {p.get('fabric_type_id')}."})

    # ── Endless belts are a closed loop and must fit within a fixed max length —
    #    the frontend clamps this in the UI, but that's advisory only (a plain
    #    number input's `max` attribute doesn't block typed values), so it must
    #    also be enforced here, server-side, using the same shared constant the
    #    frontend and the batch-import paths use.
    try:
        validate_endless_belt_length(p.get('construction_type'), p.get('belt_length_m'))
    except ValueError as exc:
        raise ValidationError({'belt_length_m': str(exc)})

    # ── Parse kN/plies from the rating name ONCE, reused below for both the
    #    fabric-style auto-selection and the splicing calculation. This is the
    #    single shared parser (apps.services.calculations.parse_belt_rating) —
    #    it used to be implemented separately here (with a `num_plies * 100`
    #    guess as a fallback when parsing failed) and again in batch_views.py
    #    with a different regex. Both now call the same function, so a
    #    single-belt TDS and a bulk-imported one can never disagree, and a
    #    genuinely unparseable rating_name is now a clear 400 instead of a
    #    silently wrong splice length.
    try:
        belt_kn, belt_plies = parse_belt_rating(belt_rating.rating_name)
    except ValueError as exc:
        raise ValidationError({'detail': str(exc)})

    # ── Fabric style: always server-computed from the belt rating, the same
    #    way batch import already does it — never trust a client-supplied
    #    fabric_style_id, so the two entry points can't drift apart.
    fabric_style_id = auto_select_fabric_style(belt_rating.fabric_type_id, belt_kn, belt_plies)

    # ── Interply skim: always from belt_rating_values ─────────────────────────
    interply_skim_row = BeltRatingValue.objects.filter(
        belt_rating_id=p.get('belt_rating_id'),
        parameter_id=PARAM_INTERPLY_SKIM,
    ).first()

    # ROBUSTNESS (fixed): all the numeric fields below used to be cast with a
    # bare float()/int(), so a malformed value (e.g. belt_width_mm: "abc")
    # crashed with an unhandled ValueError -> generic 500 instead of a clean,
    # attributable 400 — same fix pattern as validate_endless_belt_length above.
    try:
        interply_skim_mm = (
            float(interply_skim_row.indus_value)
            if interply_skim_row and interply_skim_row.indus_value not in (None, "Not Specified", "—")
            else None
        )

        # ── Server-computed total thickness ───────────────────────────────────
        total_thickness_mm = (
            float(p.get('top_cover_mm', 0))
            + float(p.get('bottom_cover_mm', 0))
            + float(p.get('carcass_thickness_mm', 0))
        )

        # ── Belt weight per metre ──────────────────────────────────────────────
        weight_per_m = p.get('belt_weight_per_m_kg')
        if weight_per_m is not None:
            weight_per_m = float(weight_per_m)
        else:
            weight_per_m = belt_weight_per_metre(
                specific_gravity=float(cover_grade.specific_gravity),
                width_mm=int(p.get('belt_width_mm', 0)),
                total_thickness_mm=total_thickness_mm,
            )
    except (TypeError, ValueError):
        raise ValidationError({'detail': 'top_cover_mm, bottom_cover_mm, carcass_thickness_mm, belt_width_mm, and belt_weight_per_m_kg must all be numeric.'})

    # ── Auto-compute packing if reel/packing type supplied but num_rolls absent
    reel_type_id    = p.get('reel_type_id')
    packing_type_id = p.get('packing_type_id')
    num_rolls       = p.get('num_rolls')

    packing_num_rolls             = num_rolls
    packing_len_per_roll          = p.get('length_per_roll_m')
    packing_roll_dims             = p.get('roll_dimensions')
    packing_net_weight            = p.get('net_weight_kg')
    packing_gross_weight          = p.get('gross_weight_kg')
    packing_gross_weight_per_roll = None

    if reel_type_id and packing_type_id and num_rolls is None:
        reel = ReelType.objects.filter(pk=reel_type_id).first()
        if not reel:
            raise NotFound(f"reel_type_id={reel_type_id} not found")
        ptype = PackingType.objects.filter(pk=packing_type_id).first()
        if not ptype:
            raise NotFound(f"packing_type_id={packing_type_id} not found")
        if not ptype.is_available:
            raise ValidationError({'detail': f"Packing type '{ptype.packing_name}' is not yet available"})

        try:
            pr = compute_packing(
                reel_type_id=reel_type_id,
                packing_type_id=packing_type_id,
                purpose_id=p.get('purpose_id'),
                total_thickness_mm=total_thickness_mm,
                belt_length_m=float(p.get('belt_length_m', 0)),
                belt_width_mm=int(p.get('belt_width_mm', 0)),
                belt_weight_per_m_kg=float(weight_per_m),
            )
        except (TypeError, ValueError) as exc:
            # compute_packing raises ValueError for bad/missing reel config or
            # non-numeric input (see packing_service.py) — surface it as a
            # clean 400 instead of letting it fall through as a generic 500.
            raise ValidationError({'detail': str(exc)})
        packing_num_rolls             = pr.num_rolls
        packing_len_per_roll          = pr.length_per_roll_m
        packing_roll_dims             = pr.roll_dimensions
        packing_net_weight            = pr.net_weight_kg
        packing_gross_weight          = pr.gross_weight_kg
        packing_gross_weight_per_roll = pr.gross_weight_per_roll_kg

    # Fallback: derive gross_weight_per_roll when num_rolls was sent directly
    if packing_gross_weight_per_roll is None and packing_gross_weight and packing_num_rolls:
        packing_gross_weight_per_roll = math.ceil(
            float(packing_gross_weight) / int(packing_num_rolls) * 2
        ) / 2

    # ── Auto-compute splicing ─────────────────────────────────────────────────
    step_len  = p.get('step_length_mm')
    spl_len   = p.get('splice_length_mm')
    extra_len = p.get('total_extra_length_m')

    if p.get('splicing_required') and p.get('num_joints') and p.get('vulcanization_method'):
        if spl_len is None:
            # Reuse the belt_kn/belt_plies already parsed above (single shared
            # parser — no more separate ad-hoc split()/guess fallback here).
            sr = compute_splicing(
                belt_rating_kn_m=belt_kn,
                num_plies=belt_plies,
                belt_width_mm=int(p.get('belt_width_mm', 0)),
                num_joints=int(p.get('num_joints', 0)),
                vulcanization_method=p.get('vulcanization_method'),
            )
            step_len  = sr.step_length_mm
            spl_len   = sr.splice_length_mm
            extra_len = sr.total_extra_length_m

    # ── Assign TDS number atomically (has its own transaction.atomic() inside) ─
    tds_number = next_tds_number()

    # ── Build and save the TDS record ─────────────────────────────────────────
    record = TDSInput(
        tds_number           = tds_number,
        tds_doc_number       = p.get('tds_doc_number'),
        tds_date             = date.today(),
        status               = 'draft',
        construction_type    = p.get('construction_type', 'Open-End'),
        purpose_id           = p.get('purpose_id'),
        belt_type_id         = p.get('belt_type_id'),
        brand_id             = p.get('brand_id'),
        standard_id          = p.get('standard_id'),
        customer_id          = p.get('customer_id'),
        cover_grade_id       = p.get('cover_grade_id'),
        fabric_type_id       = p.get('fabric_type_id'),
        fabric_style_id      = fabric_style_id,  # server-computed above, never client-supplied
        belt_rating_id       = p.get('belt_rating_id'),
        belt_description     = p.get('belt_description'),
        belt_length_m        = p.get('belt_length_m'),
        belt_weight_per_m_kg = weight_per_m,
        make_of_fabric       = p.get('make_of_fabric', 'MIT'),
        belt_width_mm        = p.get('belt_width_mm'),
        num_plies            = p.get('num_plies'),
        top_cover_mm         = p.get('top_cover_mm'),
        bottom_cover_mm      = p.get('bottom_cover_mm'),
        carcass_from_rating  = p.get('carcass_from_rating'),
        carcass_thickness_mm = p.get('carcass_thickness_mm'),
        interply_skim_mm     = interply_skim_mm,
        total_thickness_mm   = total_thickness_mm,
        breaker_top          = bool(p.get('breaker_top', False)),
        breaker_top_plies    = p.get('breaker_top_plies'),
        breaker_bottom       = bool(p.get('breaker_bottom', False)),
        breaker_bottom_plies = p.get('breaker_bottom_plies'),
        edge_construction    = p.get('edge_construction', 'Moulded'),
        reel_type_id         = reel_type_id,
        packing_type_id      = packing_type_id,
        num_rolls            = packing_num_rolls,
        length_per_roll_m    = packing_len_per_roll,
        roll_dimensions      = packing_roll_dims,
        net_weight_kg        = packing_net_weight,
        gross_weight_kg      = packing_gross_weight,
        gross_weight_per_roll_kg = packing_gross_weight_per_roll,
        splicing_required    = bool(p.get('splicing_required', False)),
        vulcanization_method = p.get('vulcanization_method'),
        num_joints           = p.get('num_joints'),
        step_length_mm       = step_len,
        splice_length_mm     = spl_len,
        total_extra_length_m = extra_len,
        shipping_region      = p.get('shipping_region'),
        container_type_id    = p.get('container_type_id'),
    )
    # FK with db_column='created_by' — use _id suffix
    record.created_by_id = current_user.user_id
    record.save()

    log_tds_action(request, TDSAuditLog.ACTION_CREATE, tds=record)
    return Response(_tds_full(_load_full(record.tds_id)), status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_tds(request):
    """List TDS records, newest first, with optional filters and pagination."""
    params = request.query_params
    qs = (
        TDSInput.objects
        .select_related('standard', 'customer', 'belt_rating', 'created_by')
        .order_by('-created_at')
    )
    if params.get('status'):
        qs = qs.filter(status=params['status'])
    if params.get('standard_id'):
        qs = qs.filter(standard_id=params['standard_id'])
    if params.get('customer'):
        qs = qs.filter(customer__customer_name__icontains=params['customer'])

    try:
        limit  = max(1, min(200, int(params.get('limit', 50))))
        offset = max(0, int(params.get('offset', 0)))
    except (TypeError, ValueError):
        raise ValidationError({'detail': 'limit and offset must be integers.'})
    qs = qs[offset:offset + limit]

    return Response([_tds_brief(t) for t in qs])


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_tds(request, tds_id):
    """Get the full detail of a single TDS record."""
    return Response(_tds_full(_load_full(tds_id)))


@api_view(['PATCH'])
@permission_classes([IsEditor])
def approve_tds(request, tds_id):
    """Approve a draft TDS."""
    record = TDSInput.objects.filter(pk=tds_id).first()
    if not record:
        raise NotFound(f"TDS {tds_id} not found")
    if record.status != 'draft':
        return Response(
            {'detail': f"TDS {tds_id} is already '{record.status}'"},
            status=status.HTTP_409_CONFLICT
        )
    # Always set the approver to the authenticated user making this request.
    # Never accept a client-supplied approved_by — that would allow audit log
    # manipulation (an editor crediting a different user as the approver).
    record.approved_by_id = request.user.user_id
    record.status      = 'approved'
    record.approved_at = datetime.now(timezone.utc)
    record.save()
    log_tds_action(request, TDSAuditLog.ACTION_APPROVE, tds=record)
    return Response(_tds_full(_load_full(tds_id)))


@api_view(['PATCH'])
@permission_classes([IsEditor])
def decline_tds(request, tds_id):
    """Decline (reject) a TDS."""
    record = TDSInput.objects.filter(pk=tds_id).first()
    if not record:
        raise NotFound(f"TDS {tds_id} not found")
    if record.status == 'declined':
        return Response(
            {'detail': f"TDS {tds_id} is already declined"},
            status=status.HTTP_409_CONFLICT
        )
    record.status = 'declined'
    record.save()
    reason = request.data.get('reason', '')
    log_tds_action(request, TDSAuditLog.ACTION_DECLINE, tds=record, detail=reason)
    return Response(_tds_full(_load_full(tds_id)))


@api_view(['PATCH'])
@permission_classes([IsEditor])
def update_status(request, tds_id):
    """
    Set TDS status to any valid value.

    Deliberately permissive (no draft/approved/sent/... transition graph
    enforced) — this is a general-purpose correction tool for editors,
    unlike approve_tds/decline_tds which each enforce their own specific
    precondition. What WAS missing (fixed below): unlike approve/decline,
    this never wrote an audit log entry, so a status change made through this
    endpoint left no trace in the same history approve/decline do — every
    other mutation in this file is audited, this one silently wasn't.
    """
    new_status = request.query_params.get('new_status') or request.data.get('new_status')
    valid = {"approved", "sent", "archived", "declined", "draft"}
    if new_status not in valid:
        raise ValidationError({'detail': f"status must be one of {valid}"})
    record = TDSInput.objects.filter(pk=tds_id).first()
    if not record:
        raise NotFound(f"TDS {tds_id} not found")
    old_status = record.status
    record.status = new_status
    record.save()
    log_tds_action(
        request, TDSAuditLog.ACTION_UPDATE, tds=record,
        detail=f"status changed: {old_status} -> {new_status}",
    )
    return Response(_tds_full(_load_full(tds_id)))


@api_view(['DELETE'])
@permission_classes([IsEditor])
def delete_tds(request, tds_id):
    """Permanently delete a TDS record."""
    record = TDSInput.objects.filter(pk=tds_id).first()
    if not record:
        raise NotFound(f"TDS {tds_id} not found")
    log_tds_action(request, TDSAuditLog.ACTION_DELETE, tds=record, detail='User requested deletion')
    record.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)
