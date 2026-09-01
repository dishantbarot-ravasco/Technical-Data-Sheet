"""
apps/api/routers/tds_views.py — TDS document CRUD endpoints.

Ported from FastAPI routers/tds_inputs.py.

Endpoints:
  POST   /api/tds
  GET    /api/tds
  GET    /api/tds/{id}
  PATCH  /api/tds/{id}          (edit an existing TDS)
  PATCH  /api/tds/{id}/approve
  PATCH  /api/tds/{id}/decline
  PATCH  /api/tds/{id}/status
  DELETE /api/tds/{id}
"""
import logging
from datetime import datetime, timezone, date
from decimal import Decimal

from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError, PermissionDenied

from apps.core.models import (
    BeltRating, BeltRatingValue, CoverGrade, FabricType,
    IndusBrand, BeltType, PackingType, Purpose, ReelType,
    Standard, TDSInput, TDSRevision,
)
from apps.api.permissions import IsEditor, IsCreator
from apps.services.calculations import (
    belt_weight_per_metre, parse_belt_rating, auto_select_fabric_style,
    validate_endless_belt_length, validate_international_shipping_fields,
)
from apps.services.splicing_service import compute_splicing
from apps.services.packing_service import compute_packing, validate_custom_roll_lengths
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
        "roll_lengths_m":           t.roll_lengths_m,
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
        "current_revision":         t.current_revision,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _require_obj(model, pk_val, label):
    """Fetch by PK or raise NotFound."""
    obj = model.objects.filter(pk=pk_val).first()
    if not obj:
        raise NotFound(f"{label} with id={pk_val} not found")
    return obj


def _json_safe_value(v):
    """
    Convert a single TDSInput field value into something JSONField can store
    as-is — used when snapshotting a record's pre-edit state into
    TDSRevision.snapshot (see _update_tds()). Decimal -> float, date/datetime
    -> isoformat string, everything else (None, str, int, bool, list — e.g.
    roll_lengths_m, which is already a plain JSON-safe list) passes through.
    """
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _values_differ(old_val, new_val) -> bool:
    """
    Used by _update_tds() to detect a genuine field change (vs. a no-op save)
    for both the audit-log summary and version-history snapshotting.

    BUG FIX: a plain str(old_val) != str(new_val) comparison — the previous
    approach — misfires for Decimal fields: str(Decimal('300.00')) == '300.00'
    but the equal float value 300.0 (as sent by any JSON client, including the
    frontend, which has no Decimal type) stringifies to '300.0'. Those never
    match even when the numeric values are identical, so nearly every edit
    spuriously flagged nearly every Decimal field as "changed" — verified by
    round-tripping a record's own current values back through this function
    and finding nothing detected as a no-op. Comparing Decimal fields
    numerically (old_val != Decimal(str(new_val))) fixes this while every
    other field type keeps the original string comparison, which is still
    right for None vs '' and other type-noise cases the old comment described.
    """
    if isinstance(old_val, Decimal):
        try:
            return old_val != Decimal(str(new_val)) if new_val is not None else old_val is not None
        except Exception:
            return str(old_val) != str(new_val)
    return str(old_val) != str(new_val)


def _validate_positive_dimensions(p):
    """
    Reject physically nonsensical belt dimensions before they reach the
    weight/packing calculations or the DB — belt_width_mm/belt_length_m/
    num_plies must be strictly positive (a belt can't have zero width,
    length, or plies), and the cover/carcass thickness fields can't be
    negative. Non-numeric values are left alone here; the existing
    float()/int() casts further down already turn those into a clean 400.
    """
    errors = {}

    def _num(key):
        val = p.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    for field in ('belt_width_mm', 'belt_length_m'):
        n = _num(field)
        if n is not None and n <= 0:
            errors[field] = f"{field} must be greater than 0."

    for field in ('top_cover_mm', 'bottom_cover_mm', 'carcass_thickness_mm'):
        n = _num(field)
        if n is not None and n < 0:
            errors[field] = f"{field} cannot be negative."

    num_plies = p.get('num_plies')
    if num_plies is not None:
        try:
            if int(num_plies) < 1:
                errors['num_plies'] = "num_plies must be at least 1."
        except (TypeError, ValueError):
            pass  # non-numeric — handled by the later blanket numeric check

    if errors:
        raise ValidationError(errors)


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

def _validate_and_compute_tds_fields(p):
    """
    Shared by create_tds() and _update_tds(): validates FK references and
    physical dimensions, then runs every server-side computation (fabric
    style, interply skim, total thickness, belt weight, packing, splicing)
    that both endpoints must apply identically.

    Extracted from what used to be two independently-maintained ~280-line
    copies of this exact pipeline in create_tds() and _update_tds() — any
    business-rule change (e.g. a splicing tolerance fix) previously had to be
    applied in both places by hand, with nothing enforcing that it was. A
    single shared function makes that class of drift impossible: create and
    edit can now never disagree about how a given input computes.

    Returns a dict of the computed/derived values the caller needs to build
    (create_tds) or apply (update_tds) the TDSInput fields. Raises
    ValidationError / NotFound the same way the two callers already did.
    """
    # ── Validate FK references ────────────────────────────────────────────────
    _require_obj(Standard,  p.get('standard_id'),   "Standard")
    _require_obj(BeltType,  p.get('belt_type_id'),  "Belt type")
    _require_obj(IndusBrand, p.get('brand_id'),     "Brand")
    purpose = _require_obj(Purpose, p.get('purpose_id'), "Purpose")

    cover_grade = _require_obj(CoverGrade, p.get('cover_grade_id'), "Cover grade")
    if cover_grade.standard_id != p.get('standard_id'):
        raise ValidationError({'detail': f"Cover grade belongs to standard_id={cover_grade.standard_id}, not {p.get('standard_id')}."})

    belt_rating = _require_obj(BeltRating, p.get('belt_rating_id'), "Belt rating")
    _require_obj(FabricType, p.get('fabric_type_id'), "Fabric type")
    if belt_rating.fabric_type_id != p.get('fabric_type_id'):
        raise ValidationError({'detail': f"Belt rating belongs to fabric_type_id={belt_rating.fabric_type_id}, not {p.get('fabric_type_id')}."})

    # ── Reject physically nonsensical dimensions (zero/negative width,
    #    length, plies, or negative thickness) before they reach the weight
    #    and packing calculations below.
    _validate_positive_dimensions(p)

    # ── Endless belts are a closed loop and must fit within a fixed max length —
    #    the frontend clamps this in the UI, but that's advisory only (a plain
    #    number input's `max` attribute doesn't block typed values), so it must
    #    also be enforced here, server-side, using the same shared constant the
    #    frontend and the batch-import paths use.
    try:
        validate_endless_belt_length(p.get('construction_type'), p.get('belt_length_m'))
    except ValueError as exc:
        raise ValidationError({'belt_length_m': str(exc)})

    # ── International orders require shipping_region + container_type_id —
    #    same "UI-only enforcement is advisory, not a real guarantee" gap as
    #    the Endless-length check above: generate-tds.js's submitTDS() marks
    #    these fields required when Purpose = International, but a direct API
    #    call could skip that and silently create/save an international TDS
    #    with no shipping data. See validate_international_shipping_fields().
    try:
        validate_international_shipping_fields(
            purpose.purpose_type, p.get('shipping_region'), p.get('container_type_id')
        )
    except ValueError as exc:
        raise ValidationError({'detail': str(exc)})

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

    # ── Manual override: unequal roll lengths ─────────────────────────────────
    # A custom, non-uniform split (e.g. [200, 100] instead of an even 150/150)
    # takes priority over whatever the block above just computed.
    roll_lengths_m = p.get('roll_lengths_m')
    if roll_lengths_m:
        if not reel_type_id:
            raise ValidationError({'detail': 'reel_type_id is required when specifying custom roll lengths.'})
        try:
            rv = validate_custom_roll_lengths(
                reel_type_id=reel_type_id,
                total_thickness_mm=total_thickness_mm,
                belt_length_m=float(p.get('belt_length_m', 0)),
                belt_width_mm=int(p.get('belt_width_mm', 0)),
                roll_lengths_m=roll_lengths_m,
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError({'detail': str(exc)})
        packing_num_rolls    = len(roll_lengths_m)
        packing_len_per_roll = round(sum(map(float, roll_lengths_m)) / len(roll_lengths_m), 2)
        packing_roll_dims    = rv.roll_dimensions
        # BUG FIX: packing_gross_weight_per_roll may already hold a value
        # computed by the compute_packing() branch above for its OWN
        # (different) num_rolls -- e.g. reel_type_id+packing_type_id with
        # num_rolls=None auto-computes 2 rolls, then this override splits the
        # belt into 3 unequal rolls instead. Without resetting it here, the
        # PDF would show "Number of Rolls: 3" next to a "Gross Weight per
        # Roll" that was actually averaged over 2 -- an internally
        # inconsistent number with no error raised. Resetting to None lets
        # the fallback just below recompute it against the correct,
        # just-updated packing_num_rolls.
        packing_gross_weight_per_roll = None

    # Fallback: derive gross_weight_per_roll when num_rolls was sent directly
    #
    # BUG FIX: this used to round up to the nearest 0.5 kg
    # (math.ceil(x*2)/2) — the old convention packing_service.compute_packing()
    # deliberately moved away from for weight fields (see its own
    # gross_weight_per_roll_kg, and the CLAUDE.md note on precise weight
    # rounding). This fallback path only runs when num_rolls is supplied
    # directly (skipping compute_packing's own branch above), so it had
    # silently regressed back to the round-up-to-half-kg convention here,
    # producing a per-roll weight that doesn't reconcile with
    # packing_gross_weight / packing_num_rolls (e.g. gross_weight_kg=1000.32,
    # num_rolls=3 → 333.5/roll × 3 = 1000.5, off by 0.18kg) — the exact
    # Belt-Specs-vs-Packing mismatch class this file's precise-rounding
    # convention exists to prevent. round(x, 2) matches compute_packing().
    if packing_gross_weight_per_roll is None and packing_gross_weight and packing_num_rolls:
        packing_gross_weight_per_roll = round(
            float(packing_gross_weight) / int(packing_num_rolls), 2
        )

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

    return {
        'fabric_style_id':               fabric_style_id,
        'interply_skim_mm':              interply_skim_mm,
        'total_thickness_mm':            total_thickness_mm,
        'weight_per_m':                  weight_per_m,
        'reel_type_id':                  reel_type_id,
        'packing_type_id':               packing_type_id,
        'packing_num_rolls':             packing_num_rolls,
        'packing_len_per_roll':          packing_len_per_roll,
        'roll_lengths_m':                roll_lengths_m,
        'packing_roll_dims':             packing_roll_dims,
        'packing_net_weight':            packing_net_weight,
        'packing_gross_weight':          packing_gross_weight,
        'packing_gross_weight_per_roll': packing_gross_weight_per_roll,
        'step_len':                      step_len,
        'spl_len':                       spl_len,
        'extra_len':                     extra_len,
    }


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

    computed = _validate_and_compute_tds_fields(p)
    fabric_style_id               = computed['fabric_style_id']
    interply_skim_mm              = computed['interply_skim_mm']
    total_thickness_mm            = computed['total_thickness_mm']
    weight_per_m                  = computed['weight_per_m']
    reel_type_id                  = computed['reel_type_id']
    packing_type_id               = computed['packing_type_id']
    packing_num_rolls             = computed['packing_num_rolls']
    packing_len_per_roll          = computed['packing_len_per_roll']
    roll_lengths_m                = computed['roll_lengths_m']
    packing_roll_dims             = computed['packing_roll_dims']
    packing_net_weight            = computed['packing_net_weight']
    packing_gross_weight          = computed['packing_gross_weight']
    packing_gross_weight_per_roll = computed['packing_gross_weight_per_roll']
    step_len                      = computed['step_len']
    spl_len                       = computed['spl_len']
    extra_len                     = computed['extra_len']

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
        edge_construction    = p.get('edge_construction', 'Moulded Edge'),
        reel_type_id         = reel_type_id,
        packing_type_id      = packing_type_id,
        num_rolls            = packing_num_rolls,
        length_per_roll_m    = packing_len_per_roll,
        roll_lengths_m       = roll_lengths_m,
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


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def get_tds(request, tds_id):
    """
    GET   /api/tds/{id}  — full detail of a single TDS record.
    PATCH /api/tds/{id}  — edit an existing TDS in place (see _update_tds()
                            below). Requires admin/tds_creator — checked
                            manually here rather than via a second
                            @permission_classes, since GET and PATCH share
                            this one view/URL but need different access
                            levels (any authenticated user can view; only an
                            editor/creator can change the content).
    """
    if request.method == 'PATCH':
        if getattr(request.user, 'role', None) not in ('admin', 'tds_creator'):
            raise PermissionDenied('Only admins and TDS creators can edit a TDS.')
        return _update_tds(request, tds_id)
    return Response(_tds_full(_load_full(tds_id)))


def _update_tds(request, tds_id):
    """
    Edit an existing TDS record in place.

    Shares its validation/computation pipeline with create_tds() via
    _validate_and_compute_tds_fields() — previously a ~280-line hand-kept-in-
    sync duplicate of create_tds()'s body, which risked business-rule changes
    being applied to only one of the two endpoints.

    tds_number, tds_date, status, and created_by are intentionally left
    untouched — editing a TDS never renumbers it, backdates it, changes who
    originally created it, or alters its workflow status. There is no
    approve/decline-style read-only gating here by design: any existing TDS
    can be edited regardless of status.
    """
    record = TDSInput.objects.filter(pk=tds_id).first()
    if not record:
        raise NotFound(f"TDS {tds_id} not found")

    p = request.data

    computed = _validate_and_compute_tds_fields(p)
    fabric_style_id               = computed['fabric_style_id']
    interply_skim_mm              = computed['interply_skim_mm']
    total_thickness_mm            = computed['total_thickness_mm']
    weight_per_m                  = computed['weight_per_m']
    reel_type_id                  = computed['reel_type_id']
    packing_type_id               = computed['packing_type_id']
    packing_num_rolls             = computed['packing_num_rolls']
    packing_len_per_roll          = computed['packing_len_per_roll']
    roll_lengths_m                = computed['roll_lengths_m']
    packing_roll_dims             = computed['packing_roll_dims']
    packing_net_weight            = computed['packing_net_weight']
    packing_gross_weight          = computed['packing_gross_weight']
    packing_gross_weight_per_roll = computed['packing_gross_weight_per_roll']
    step_len                      = computed['step_len']
    spl_len                       = computed['spl_len']
    extra_len                     = computed['extra_len']

    # ── Apply the new values, tracking what actually changed for the audit log ─
    new_values = {
        'tds_doc_number':          p.get('tds_doc_number'),
        'construction_type':       p.get('construction_type', 'Open-End'),
        'purpose_id':              p.get('purpose_id'),
        'belt_type_id':            p.get('belt_type_id'),
        'brand_id':                p.get('brand_id'),
        'standard_id':             p.get('standard_id'),
        'customer_id':             p.get('customer_id'),
        'cover_grade_id':          p.get('cover_grade_id'),
        'fabric_type_id':          p.get('fabric_type_id'),
        'fabric_style_id':         fabric_style_id,  # server-computed, never client-supplied
        'belt_rating_id':          p.get('belt_rating_id'),
        'belt_description':       p.get('belt_description'),
        'belt_length_m':          p.get('belt_length_m'),
        'belt_weight_per_m_kg':   weight_per_m,
        'make_of_fabric':         p.get('make_of_fabric', 'MIT'),
        'belt_width_mm':          p.get('belt_width_mm'),
        'num_plies':              p.get('num_plies'),
        'top_cover_mm':           p.get('top_cover_mm'),
        'bottom_cover_mm':        p.get('bottom_cover_mm'),
        'carcass_from_rating':    p.get('carcass_from_rating'),
        'carcass_thickness_mm':   p.get('carcass_thickness_mm'),
        'interply_skim_mm':       interply_skim_mm,
        'total_thickness_mm':     total_thickness_mm,
        'breaker_top':            bool(p.get('breaker_top', False)),
        'breaker_top_plies':      p.get('breaker_top_plies'),
        'breaker_bottom':         bool(p.get('breaker_bottom', False)),
        'breaker_bottom_plies':   p.get('breaker_bottom_plies'),
        'edge_construction':      p.get('edge_construction', 'Moulded Edge'),
        'reel_type_id':           reel_type_id,
        'packing_type_id':        packing_type_id,
        'num_rolls':              packing_num_rolls,
        'length_per_roll_m':      packing_len_per_roll,
        'roll_lengths_m':         roll_lengths_m,
        'roll_dimensions':        packing_roll_dims,
        'net_weight_kg':          packing_net_weight,
        'gross_weight_kg':        packing_gross_weight,
        'gross_weight_per_roll_kg': packing_gross_weight_per_roll,
        'splicing_required':      bool(p.get('splicing_required', False)),
        'vulcanization_method':   p.get('vulcanization_method'),
        'num_joints':             p.get('num_joints'),
        'step_length_mm':         step_len,
        'splice_length_mm':       spl_len,
        'total_extra_length_m':   extra_len,
        'shipping_region':        p.get('shipping_region'),
        'container_type_id':      p.get('container_type_id'),
    }

    changed_fields = []
    old_snapshot = {}
    for field, new_val in new_values.items():
        old_val = getattr(record, field)
        if _values_differ(old_val, new_val):
            changed_fields.append(field)
        old_snapshot[field] = _json_safe_value(old_val)
        setattr(record, field, new_val)

    detail = f"fields changed: {', '.join(changed_fields)}" if changed_fields else "saved; no field values actually changed"

    # Version history: only snapshot a genuine change — a no-op save (nothing
    # in new_values actually differed) shouldn't create an empty revision.
    # Also skip the snapshot entirely while the record has never been really
    # downloaded (first_downloaded_at is NULL): these are pre-issue tweaks
    # made while still previewing, not a correction to an issued document, so
    # they'd just be revision-history noise. Once a real download happens
    # (see generate_pdf()/download_export()), every subsequent edit snapshots
    # as before, however long after — that's what makes it a "revision".
    # BUG FIX: the revision-snapshot create() and the record's own save() used
    # to be two separate uncommitted-together statements — if record.save()
    # raised (e.g. a DB constraint violation on one of the new field values),
    # the TDSRevision row describing this edit was already permanently
    # committed even though the edit it describes never actually applied,
    # leaving current_revision on the live row out of sync with the max
    # revision number actually stored in tds_revisions. Wrapping both in one
    # atomic block means either both persist or neither does.
    with transaction.atomic():
        if changed_fields and record.first_downloaded_at is not None:
            TDSRevision.objects.create(
                tds=record, revision_number=record.current_revision,
                snapshot=old_snapshot, edited_by=request.user, change_summary=detail,
            )
            record.current_revision += 1

        record.save()

    log_tds_action(request, TDSAuditLog.ACTION_UPDATE, tds=record, detail=detail)
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
