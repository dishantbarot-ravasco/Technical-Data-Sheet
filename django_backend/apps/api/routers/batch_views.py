"""
apps/api/routers/batch_views.py — Bulk TDS batch creation.

Endpoints:
  POST /api/tds/batch/      — validate all belt rows, then create TDSBatch +
                              N TDSInput records atomically.
  GET  /api/tds/batch/{id}/ — retrieve batch summary with all linked TDS records.

Design rules:
  - ALL belt rows are validated before any DB write (collect-all-errors approach).
  - Shared config (make_of_fabric, splicing, packing, shipping) applies to every belt.
  - Per-belt: doc_number, belt_type, construction_type, width, fabric, rating,
              covers, carcass (nullable → auto from EAV), length, num_joints,
              container_type.
  - Fabric style auto-selected: per_ply = kN/plies; pick lowest style >= per_ply.
  - Carcass: auto from EAV param_id=4; white-box override via carcass_thickness_mm.
  - Packing and splicing computed server-side using existing services.
  - next_tds_number() called inside transaction for each belt — safe via SELECT FOR UPDATE.
  - Single-belt TDS records are unaffected (batch_id remains NULL on those).
"""

import re
import logging
from datetime import date

from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, ValidationError

from apps.core.models import (
    BeltRating, BeltRatingValue, BeltType, CoverGrade, Customer,
    FabricStyle, FabricType, IndusBrand, PackingType, Purpose,
    ReelType, Standard, TDSBatch, TDSInput,
)
from apps.api.permissions import IsCreator
from apps.services.calculations import (
    belt_weight_per_metre, parse_belt_rating, auto_select_fabric_style,
    validate_endless_belt_length,
)
from apps.services.sections import CUSTOMER_COPY_EXCLUDE_GROUPS
from apps.services.splicing_service import compute_splicing
from apps.services.packing_service import compute_packing
from apps.services.tds_number import next_tds_number
from apps.core.audit_log import log_tds_action, TDSAuditLog

logger = logging.getLogger(__name__)

# belt_rating_values parameter IDs (same constants as tds_views.py)
PARAM_CARCASS_THICKNESS = 4
PARAM_INTERPLY_SKIM     = 5


# ── Internal helpers ──────────────────────────────────────────────────────────

def _require_shared(model, pk_val, label, errors):
    """
    Fetch by PK for shared-level validation.
    On failure, record the error and return None so we can keep checking.
    """
    if not pk_val:
        errors.setdefault(label, []).append(f"{label} is required.")
        return None
    obj = model.objects.filter(pk=pk_val).first()
    if not obj:
        errors.setdefault(label, []).append(f"{label} with id={pk_val} not found.")
    return obj


def _is_number(v) -> bool:
    """True if v can be parsed as a float (int() below is always a subset of this)."""
    if v is None:
        return False
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _parse_rating(rating_name: str):
    """
    Extract (kn: float, plies: int) from a rating_name like 'EP 315/3' or 'NN 630/4'.
    Raises ValidationError if the format is unrecognised.

    Thin wrapper around apps.services.calculations.parse_belt_rating() — that's
    now the single shared implementation used by this file, tds_views.py
    (single-belt create), and lookup_views.py (live preview), so all three
    can never disagree on how a rating_name is parsed.
    """
    try:
        return parse_belt_rating(rating_name)
    except ValueError as exc:
        raise ValidationError({'detail': str(exc)})


def _auto_fabric_style(fabric_type_id: int, kn: float, plies: int):
    """
    Server-side fabric style selection.

    Thin wrapper around apps.services.calculations.auto_select_fabric_style() —
    see that function's docstring for the algorithm. Shared with tds_views.py's
    single-belt create_tds so both flows pick the fabric style the same way.
    """
    return auto_select_fabric_style(fabric_type_id, kn, plies)


def _fetch_carcass_eav(belt_rating_id: int):
    """
    Return (carcass_mm: float|None, interply_mm: float|None) from EAV rows
    for param_id 4 (carcass) and 5 (interply skim).
    """
    values = BeltRatingValue.objects.filter(
        belt_rating_id=belt_rating_id,
        parameter_id__in=(PARAM_CARCASS_THICKNESS, PARAM_INTERPLY_SKIM),
    )
    carcass  = None
    interply = None
    for v in values:
        raw = v.indus_value
        if raw in (None, 'Not Specified', '—', ''):
            continue
        try:
            val = float(raw)
        except (ValueError, TypeError):
            continue
        if v.parameter_id == PARAM_CARCASS_THICKNESS:
            carcass = val
        elif v.parameter_id == PARAM_INTERPLY_SKIM:
            interply = val
    return carcass, interply


def _belt_description(width, fabric_code, rating_name, top, bottom, grade_code, edge,
                       belt_type_name, construction_type='Open-End'):
    """
    Canonical belt description shown on the TDS PDF header.

    Both cover thicknesses carry an explicit 'mm' suffix (top used to be a
    bare number, e.g. "...X 6.0 X 3.0mm X..." - inconsistent with every other
    dimension in the string, which always states its unit).

    End type mirrors the single-belt form's updateBeltDescription() in
    js/generate-tds.js: Open-End belts show just the belt type name, Endless
    belts get an "Endless " prefix (e.g. "Endless Flat Belt") - without this,
    a batch-created Endless belt's description was indistinguishable from an
    Open-End one on the QAP/TDS PDF.
    """
    belt_type_label = f"Endless {belt_type_name}" if construction_type == 'Endless' else belt_type_name
    return (
        f"{width}mm X {fabric_code} X {rating_name} X "
        f"{top}mm X {bottom}mm X {grade_code} X {edge} X {belt_type_label}"
    )


def _batch_brief(batch):
    return {
        "batch_id":             batch.batch_id,
        "make_of_fabric":       batch.make_of_fabric,
        "splicing_required":    batch.splicing_required,
        "vulcanization_method": batch.vulcanization_method,
        "reel_type_id":         batch.reel_type_id,
        "packing_type_id":      batch.packing_type_id,
        "shipping_region":      batch.shipping_region,
        "created_by_id":        batch.created_by_id,
        "created_at":           batch.created_at.isoformat() if batch.created_at else None,
    }


# ── Views ─────────────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsCreator])
def create_batch(request):
    """
    Create a TDSBatch and N TDSInput records in one atomic transaction.

    Expected JSON body:
    {
      "shared": {
        "purpose_id":          <int>,
        "brand_id":            <int>,
        "standard_id":         <int>,
        "make_of_fabric":      "MIT" | "SRF",          // default "MIT"
        "splicing_required":   true | false,
        "vulcanization_method": "Hot" | "Cold" | null,
        "reel_type_id":        <int> | null,
        "packing_type_id":     <int> | null,
        "shipping_region":     <str> | null
      },
      "customer": {
        "customer_id": <int>         // use existing customer  OR
        // alternative: create new customer inline:
        "customer_name":  <str>,
        "contact_person": <str> | null,
        "application":    <str> | null,
        "plant_location": <str> | null
      },
      "belts": [
        {
          "tds_doc_number":       <str> | null,
          "belt_type_id":         <int>,
          "construction_type":    "Open-End" | "Endless",   // default "Open-End"
          "belt_width_mm":        <int>,
          "fabric_type_id":       <int>,
          "belt_rating_id":       <int>,
          "carcass_thickness_mm": <float> | null,   // null → auto from EAV
          "top_cover_mm":         <float>,
          "bottom_cover_mm":      <float>,          // default 0
          "cover_grade_id":       <int>,
          "edge_construction":    "Moulded" | "Cut",  // default "Moulded"
          "breaker_top_plies":    <int> | null,     // null → no BOT
          "breaker_bottom_plies": <int> | null,     // null → no BOB
          "belt_length_m":        <float>,
          "num_joints":           <int> | null,     // required when splicing_required
          "container_type_id":    <int> | null      // for international shipments
        },
        ...
      ]
    }
    """
    data = request.data
    current_user = request.user

    shared        = data.get('shared') or {}
    customer_data = data.get('customer') or {}
    belts         = data.get('belts') or []

    if not belts:
        raise ValidationError({'belts': 'At least one belt row is required.'})
    # ROBUSTNESS (fixed): there used to be no upper bound here, so a very
    # large belt list would be processed entirely synchronously below
    # (including, later, per-belt PDF generation on export) with no server-side
    # limit -- a plausible request-timeout/DoS vector. 200 matches the same
    # cap already used for GET /tds list pagination (see tds_views.py).
    if len(belts) > 200:
        raise ValidationError({'belts': f'A single batch is limited to 200 belt rows (got {len(belts)}).'})

    # ── Step 1: Validate shared reference data ────────────────────────────────
    shared_errors = {}

    purpose_id  = shared.get('purpose_id')
    brand_id    = shared.get('brand_id')
    standard_id = shared.get('standard_id')

    _require_shared(Purpose,    purpose_id,  'purpose_id',  shared_errors)
    _require_shared(IndusBrand, brand_id,    'brand_id',    shared_errors)
    _require_shared(Standard,   standard_id, 'standard_id', shared_errors)

    make_of_fabric       = shared.get('make_of_fabric', 'MIT')
    splicing_required    = bool(shared.get('splicing_required', False))
    vulcanization_method = shared.get('vulcanization_method')
    reel_type_id         = shared.get('reel_type_id')
    packing_type_id      = shared.get('packing_type_id')
    shipping_region      = shared.get('shipping_region')

    if reel_type_id:
        _require_shared(ReelType, reel_type_id, 'reel_type_id', shared_errors)
    if packing_type_id:
        pt = _require_shared(PackingType, packing_type_id, 'packing_type_id', shared_errors)
        if pt and not pt.is_available:
            shared_errors.setdefault('packing_type_id', []).append(
                f"Packing type '{pt.packing_name}' is not yet available."
            )

    if splicing_required and not vulcanization_method:
        shared_errors.setdefault('vulcanization_method', []).append(
            'vulcanization_method is required when splicing_required=true.'
        )

    if shared_errors:
        raise ValidationError(shared_errors)

    # ── Step 2: Resolve / create customer ────────────────────────────────────
    customer_id_req = customer_data.get('customer_id')
    if customer_id_req:
        customer_obj = Customer.objects.filter(pk=customer_id_req).first()
        if not customer_obj:
            raise NotFound(f"Customer with id={customer_id_req} not found")
        customer_id = customer_obj.customer_id
    else:
        cname = (customer_data.get('customer_name') or '').strip()
        if not cname:
            raise ValidationError(
                {'customer': 'Provide customer_id OR customer_name to identify the customer.'}
            )
        customer_obj, _ = Customer.objects.get_or_create(
            customer_name=cname,
            defaults={
                'contact_person': customer_data.get('contact_person'),
                'application':    customer_data.get('application'),
                'plant_location': customer_data.get('plant_location'),
            },
        )
        customer_id = customer_obj.customer_id

    # ── Step 3: Validate every belt row (collect ALL errors before rejecting) ──
    belt_errors = {}

    # Cache FK lookups — same ID may appear on multiple rows
    _rating_cache     = {}
    _fabric_type_cache= {}
    _cover_grade_cache= {}
    _belt_type_cache  = {}

    def _cached(cache, model, pk):
        if pk not in cache:
            cache[pk] = model.objects.filter(pk=pk).first()
        return cache[pk]

    for i, b in enumerate(belts):
        row = f"belts[{i}]"

        # Required field presence
        for field in ('belt_width_mm', 'top_cover_mm', 'belt_length_m',
                      'cover_grade_id', 'belt_rating_id', 'fabric_type_id', 'belt_type_id'):
            if b.get(field) is None:
                belt_errors.setdefault(row, []).append(f'{field} is required')

        # ROBUSTNESS (fixed): these fields used to only be checked for presence
        # here, then cast with a bare int()/float() later inside the atomic
        # creation block below -- a non-numeric value (e.g. "abc") passed this
        # check and then crashed with an unhandled ValueError -> generic 500.
        # Validating the type here means Step 4 can never hit that anymore, and
        # the error is now attributable to the exact row.
        for field in ('belt_width_mm', 'top_cover_mm', 'belt_length_m'):
            if b.get(field) is not None and not _is_number(b.get(field)):
                belt_errors.setdefault(row, []).append(f'{field} must be a number')
        for field in ('bottom_cover_mm', 'carcass_thickness_mm'):
            if b.get(field) is not None and not _is_number(b.get(field)):
                belt_errors.setdefault(row, []).append(f'{field} must be a number')

        if splicing_required and not b.get('num_joints'):
            belt_errors.setdefault(row, []).append(
                'num_joints is required when splicing_required=true'
            )

        # Endless belts are a closed loop and must fit within a fixed max
        # length — same shared check used by the single-belt create_tds path
        # and the text-import path below, so all three entry points agree.
        try:
            validate_endless_belt_length(
                b.get('construction_type'), b.get('belt_length_m')
            )
        except ValueError as exc:
            belt_errors.setdefault(row, []).append(str(exc))

        # FK existence
        rating_id = b.get('belt_rating_id')
        ft_id     = b.get('fabric_type_id')
        cg_id     = b.get('cover_grade_id')
        bt_id     = b.get('belt_type_id')

        rating      = _cached(_rating_cache,      BeltRating,  rating_id) if rating_id else None
        fabric_type = _cached(_fabric_type_cache, FabricType,  ft_id)     if ft_id     else None
        cover_grade = _cached(_cover_grade_cache, CoverGrade,  cg_id)     if cg_id     else None
        belt_type   = _cached(_belt_type_cache,   BeltType,    bt_id)     if bt_id     else None

        if rating_id and not rating:
            belt_errors.setdefault(row, []).append(f'belt_rating_id={rating_id} not found')
        if ft_id and not fabric_type:
            belt_errors.setdefault(row, []).append(f'fabric_type_id={ft_id} not found')
        if cg_id and not cover_grade:
            belt_errors.setdefault(row, []).append(f'cover_grade_id={cg_id} not found')
        if bt_id and not belt_type:
            belt_errors.setdefault(row, []).append(f'belt_type_id={bt_id} not found')

        # Cross-validations
        if rating and ft_id and rating.fabric_type_id != ft_id:
            belt_errors.setdefault(row, []).append(
                f'BeltRating {rating_id} belongs to fabric_type_id={rating.fabric_type_id}, '
                f'but fabric_type_id={ft_id} was given'
            )
        if cover_grade and standard_id and cover_grade.standard_id != standard_id:
            belt_errors.setdefault(row, []).append(
                f'CoverGrade {cg_id} belongs to standard_id={cover_grade.standard_id}, '
                f'but standard_id={standard_id} was given'
            )

    if belt_errors:
        raise ValidationError(belt_errors)

    # ── Step 4: Atomic creation ───────────────────────────────────────────────
    with transaction.atomic():

        # 4a. Create the batch header record
        batch = TDSBatch.objects.create(
            make_of_fabric       = make_of_fabric,
            splicing_required    = splicing_required,
            vulcanization_method = vulcanization_method,
            reel_type_id         = reel_type_id,
            packing_type_id      = packing_type_id,
            shipping_region      = shipping_region,
            created_by_id        = current_user.user_id,
        )

        created_records = []

        # 4b. Create one TDSInput per belt row
        for b in belts:
            rating      = _rating_cache[b['belt_rating_id']]
            cover_grade = _cover_grade_cache[b['cover_grade_id']]
            fabric_type = _fabric_type_cache[b['fabric_type_id']]
            belt_type   = _belt_type_cache[b['belt_type_id']]

            # Parse kN and plies from rating name (e.g. "EP 315/3" → 315, 3)
            kn, plies = _parse_rating(rating.rating_name)

            # Carcass: auto from EAV; user may override (yellow → white)
            carcass_from_rating, interply_skim_mm = _fetch_carcass_eav(rating.id)
            user_carcass = b.get('carcass_thickness_mm')
            carcass_thickness_mm = (
                float(user_carcass) if user_carcass is not None
                else (carcass_from_rating or 0.0)
            )

            # Cover dimensions
            top_cover_mm    = float(b['top_cover_mm'])
            raw_bottom      = b.get('bottom_cover_mm')
            bottom_cover_mm = float(raw_bottom) if raw_bottom is not None else 0.0
            total_thickness_mm = top_cover_mm + bottom_cover_mm + carcass_thickness_mm

            # Auto fabric style
            fabric_style_id = _auto_fabric_style(b['fabric_type_id'], kn, plies)

            # Belt weight per metre
            weight_per_m = belt_weight_per_metre(
                specific_gravity   = float(cover_grade.specific_gravity),
                width_mm           = int(b['belt_width_mm']),
                total_thickness_mm = total_thickness_mm,
            )

            # Breaker plies (BOT / BOB)
            bot_plies = b.get('breaker_top_plies')     # None → no BOT
            bob_plies = b.get('breaker_bottom_plies')  # None → no BOB

            # Belt description string
            description = _belt_description(
                width              = b['belt_width_mm'],
                fabric_code        = fabric_type.fabric_code,
                rating_name        = rating.rating_name,
                top                = top_cover_mm,
                bottom             = bottom_cover_mm,
                grade_code         = cover_grade.grade_code,
                edge               = b.get('edge_construction', 'Moulded'),
                belt_type_name     = belt_type.belt_type,
                construction_type  = b.get('construction_type', 'Open-End'),
            )

            # Packing: compute per belt using shared reel/packing config
            packing_num_rolls    = None
            packing_len_per_roll = None
            packing_roll_dims    = None
            packing_net_weight   = None
            packing_gross_weight = None
            packing_gw_per_roll  = None

            if reel_type_id and packing_type_id:
                try:
                    pr = compute_packing(
                        reel_type_id         = reel_type_id,
                        packing_type_id      = packing_type_id,
                        purpose_id           = purpose_id,
                        total_thickness_mm   = total_thickness_mm,
                        belt_length_m        = float(b['belt_length_m']),
                        belt_width_mm        = int(b['belt_width_mm']),
                        belt_weight_per_m_kg = float(weight_per_m),
                    )
                    packing_num_rolls    = pr.num_rolls
                    packing_len_per_roll = pr.length_per_roll_m
                    packing_roll_dims    = pr.roll_dimensions
                    packing_net_weight   = pr.net_weight_kg
                    packing_gross_weight = pr.gross_weight_kg
                    packing_gw_per_roll  = pr.gross_weight_per_roll_kg
                except Exception:
                    logger.exception(
                        "compute_packing failed for belt_rating_id=%s belt_width_mm=%s",
                        b['belt_rating_id'], b['belt_width_mm'],
                    )

            # Splicing: shared method, per-belt joint count
            # num_joints = 1 per roll (typical, matches single-belt flow);
            # falls back to whatever the request sent if packing wasn't run.
            step_len   = None
            spl_len    = None
            extra_len  = None
            num_joints = packing_num_rolls if packing_num_rolls else b.get('num_joints')

            if splicing_required and num_joints and vulcanization_method:
                try:
                    sr = compute_splicing(
                        belt_rating_kn_m     = kn,
                        num_plies            = plies,
                        belt_width_mm        = int(b['belt_width_mm']),
                        num_joints           = int(num_joints),
                        vulcanization_method = vulcanization_method,
                    )
                    step_len  = sr.step_length_mm
                    spl_len   = sr.splice_length_mm
                    extra_len = sr.total_extra_length_m
                except Exception:
                    logger.exception(
                        "compute_splicing failed for belt_rating_id=%s", b['belt_rating_id']
                    )

            # Assign a collision-free TDS number (uses SELECT FOR UPDATE internally)
            tds_number = next_tds_number()

            # Build and save the TDS record
            record = TDSInput(
                tds_number           = tds_number,
                tds_doc_number       = b.get('tds_doc_number'),
                tds_date             = date.today(),
                status               = 'draft',
                construction_type    = b.get('construction_type', 'Open-End'),
                purpose_id           = purpose_id,
                belt_type_id         = b['belt_type_id'],
                brand_id             = brand_id,
                standard_id          = standard_id,
                customer_id          = customer_id,
                cover_grade_id       = b['cover_grade_id'],
                fabric_type_id       = b['fabric_type_id'],
                fabric_style_id      = fabric_style_id,
                belt_rating_id       = b['belt_rating_id'],
                belt_description     = description,
                belt_length_m        = b['belt_length_m'],
                belt_weight_per_m_kg = weight_per_m,
                make_of_fabric       = make_of_fabric,
                belt_width_mm        = b['belt_width_mm'],
                num_plies            = plies,
                top_cover_mm         = top_cover_mm,
                bottom_cover_mm      = bottom_cover_mm,
                carcass_from_rating  = carcass_from_rating,
                carcass_thickness_mm = carcass_thickness_mm,
                interply_skim_mm     = interply_skim_mm,
                total_thickness_mm   = total_thickness_mm,
                breaker_top          = bot_plies is not None,
                breaker_top_plies    = bot_plies,
                breaker_bottom       = bob_plies is not None,
                breaker_bottom_plies = bob_plies,
                edge_construction    = b.get('edge_construction', 'Moulded'),
                reel_type_id         = reel_type_id,
                packing_type_id      = packing_type_id,
                num_rolls            = packing_num_rolls,
                length_per_roll_m    = packing_len_per_roll,
                roll_dimensions      = packing_roll_dims,
                net_weight_kg        = packing_net_weight,
                gross_weight_kg      = packing_gross_weight,
                gross_weight_per_roll_kg = packing_gw_per_roll,
                shipping_region      = shipping_region,
                container_type_id    = b.get('container_type_id'),
                splicing_required    = splicing_required,
                vulcanization_method = vulcanization_method if splicing_required else None,
                num_joints           = num_joints,
                step_length_mm       = step_len,
                splice_length_mm     = spl_len,
                total_extra_length_m = extra_len,
                batch                = batch,   # sets batch_id column
            )
            # FK column is literally 'created_by' in the DB (db_column='created_by')
            # so Django's _id accessor is record.created_by_id → maps to that column.
            record.created_by_id = current_user.user_id
            record.save()
            created_records.append(record)

    logger.info(
        "TDSBatch #%s created by user_id=%s with %d belts",
        batch.batch_id, current_user.user_id, len(created_records),
    )
    log_tds_action(
        request, TDSAuditLog.ACTION_BATCH,
        detail=f'batch_id={batch.batch_id} belts={len(created_records)}',
    )

    # Matches get_batch()'s response shape (top-level customer_name, not nested
    # in `batch`) so both endpoints can be handled identically on the frontend.
    batch_customer_name = next(
        (r.customer.customer_name for r in created_records if r.customer_id), None
    )

    return Response(
        {
            "batch":         _batch_brief(batch),
            "customer_name": batch_customer_name,
            "tds_records": [
                {
                    "tds_id":         r.tds_id,
                    "tds_number":     r.tds_number,
                    "tds_doc_number": r.tds_doc_number,
                    "belt_description": r.belt_description,
                }
                for r in created_records
            ],
            "count": len(created_records),
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_batch(request, batch_id):
    """
    Retrieve a TDSBatch with a summary of every linked TDS record.
    """
    batch = TDSBatch.objects.filter(pk=batch_id).first()
    if not batch:
        raise NotFound(f"Batch {batch_id} not found")

    records = (
        TDSInput.objects
        .select_related(
            'belt_type', 'cover_grade', 'fabric_type', 'belt_rating',
            'reel_type', 'packing_type', 'customer',
        )
        .filter(batch_id=batch_id)
        .order_by('tds_id')
    )

    batch_customer_name = None
    tds_list = []
    for t in records:
        if batch_customer_name is None and t.customer_id:
            batch_customer_name = t.customer.customer_name
        tds_list.append({
            "tds_id":               t.tds_id,
            "tds_number":           t.tds_number,
            "tds_doc_number":       t.tds_doc_number,
            "tds_date":             str(t.tds_date) if t.tds_date else None,
            "status":               t.status,
            "belt_description":     t.belt_description,
            "belt_width_mm":        t.belt_width_mm,
            "belt_length_m":        float(t.belt_length_m) if t.belt_length_m else None,
            "belt_type":            t.belt_type.belt_type  if t.belt_type_id  else None,
            "cover_grade":          t.cover_grade.grade_code if t.cover_grade_id else None,
            "belt_rating":          t.belt_rating.rating_name if t.belt_rating_id else None,
            "fabric_code":          t.fabric_type.fabric_code if t.fabric_type_id else None,
            "num_plies":            t.num_plies,
            "top_cover_mm":         float(t.top_cover_mm)        if t.top_cover_mm        else None,
            "bottom_cover_mm":      float(t.bottom_cover_mm)     if t.bottom_cover_mm     else None,
            "carcass_thickness_mm": float(t.carcass_thickness_mm) if t.carcass_thickness_mm else None,
            "total_thickness_mm":   float(t.total_thickness_mm)  if t.total_thickness_mm  else None,
            "belt_weight_per_m_kg": float(t.belt_weight_per_m_kg) if t.belt_weight_per_m_kg else None,
            "num_joints":           t.num_joints,
            "step_length_mm":       t.step_length_mm,
            "splice_length_mm":     t.splice_length_mm,
            "num_rolls":            t.num_rolls,
            "net_weight_kg":        float(t.net_weight_kg)        if t.net_weight_kg        else None,
            "gross_weight_kg":      float(t.gross_weight_kg)      if t.gross_weight_kg      else None,
        })

    return Response({
        "batch":         _batch_brief(batch),
        "customer_name": batch_customer_name,
        "tds_records":   tds_list,
        "count":         len(tds_list),
    })


# ── Customer Copy / Internal Copy ───────────────────────────────────────────

def _resolve_copy_type(request):
    """
    Read ?copy=customer|internal (default 'customer') and return the
    exclude_groups list to pass into PDF rendering.

    'customer' → CUSTOMER_COPY_EXCLUDE_GROUPS (fabric/sampling/packing/splicing
                 detail omitted — this is the copy meant to leave the building).
    'internal' → None (nothing excluded — every section included).

    Defaults to 'customer' so a plain GET with no query string (e.g. an old
    bookmarked link) doesn't accidentally hand out internal-only detail.
    Same two names and same default are used by the single-record preview
    (frontend/tds-preview.html's DEFAULT_UNCHECKED_GROUPS) and by
    getPdfUrl()/downloadPdf() (frontend/js/api.js) — keep them in sync.
    """
    # If the frontend sends explicit exclude_groups params, honour them directly.
    # This allows the multi-preview page's per-section checkboxes to be respected
    # in batch ZIP / print-all, independently of the coarse customer|internal toggle.
    # Filter empty strings: a bare ?exclude_groups= param produces [''] which would
    # try to exclude a section named '' — harmless but noisy; strip them out first.
    explicit = [g for g in request.GET.getlist('exclude_groups') if g]
    if explicit:
        return explicit

    copy_type = (request.GET.get('copy') or 'customer').strip().lower()
    if copy_type == 'internal':
        return None
    return list(CUSTOMER_COPY_EXCLUDE_GROUPS)


# ── ZIP download ──────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_batch_zip(request, batch_id):
    """
    GET /api/tds/batch/{batch_id}/download-zip/?copy=customer|internal

    Generate TDS + QAP PDFs for every record in this batch, bundle them
    into a single ZIP archive, and stream it back as application/zip.
    Also includes one merged PDF combining every belt's TDS (each belt
    starting on its own page - the same merge print-all-batch/print-all
    produce) so the ZIP has both the per-belt files and a single
    print-ready document, without a separate request.

    ZIP filename    : {CustomerName}.zip   (same customer-name rule as single TDS)
    TDS filenames   : {tds_number}_{doc_number}_TDS.pdf
    QAP filenames   : {tds_number}_QAP.pdf   (skipped silently if no template mapped)
    Merged filename : {CustomerName}_Merged.pdf   (skipped silently if pypdf isn't
                       installed or every individual TDS PDF failed to render -
                       the ZIP still succeeds with just the per-belt files)
    """
    import io
    import zipfile
    from django.http import HttpResponse as DjangoHttpResponse

    try:
        from pypdf import PdfWriter
    except ImportError:
        PdfWriter = None

    batch = TDSBatch.objects.filter(pk=batch_id).first()
    if not batch:
        raise NotFound(f"Batch {batch_id} not found")

    # Lazy imports — avoids module-level circular imports.
    try:
        from apps.api.routers.pdf_views import render_tds_pdf_bytes
    except ImportError:
        return Response(
            {
                'detail': (
                    'Batch PDF download is not yet wired up. '
                    'Add render_tds_pdf_bytes(tds_id: int) -> bytes to '
                    'apps/api/routers/pdf_views.py — see the docstring in '
                    'download_batch_zip() in batch_views.py for the exact snippet.'
                )
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    from apps.services.qap_service import resolve_qap_template, build_qap_context
    from apps.services.pdf_renderer import render_qap_pdf

    exclude_groups = _resolve_copy_type(request)

    # PO / Enquiry for the QAP PDFs bundled below - same query params the
    # single-record QAP popup sends (see qap_views.generate_qap_pdf), read
    # fresh here too and never persisted. One PO/Enquiry pair applies to
    # every belt's QAP in this batch, since a batch is normally one order.
    qap_doc_type = request.GET.get('doc_type', 'PO')
    qap_ref_no   = request.GET.get('ref_no', '')
    qap_ref_date = request.GET.get('ref_date', '')

    records = (
        TDSInput.objects
        .filter(batch_id=batch_id)
        .select_related('customer', 'standard', 'cover_grade')
        .order_by('tds_id')
    )

    buf = io.BytesIO()
    failed  = []
    written = 0

    # Strip characters invalid in most filesystems (Windows-safe subset)
    _SAFE = re.compile(r'[<>:"/\\|?*]')

    # Accumulates the same already-rendered TDS PDF bytes used for the
    # per-belt files above, purely so the merged PDF doesn't cost a second
    # WeasyPrint render per belt - only the (cheap) pypdf concatenation
    # happens again, once, after the loop below.
    merger = PdfWriter() if PdfWriter is not None else None

    batch_customer_name = None
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for r in records:
            if batch_customer_name is None and r.customer_id:
                batch_customer_name = r.customer.customer_name

            # ── TDS PDF ───────────────────────────────────────────────────────
            try:
                tds_bytes = render_tds_pdf_bytes(r.tds_id, exclude_groups=exclude_groups)
                safe_doc  = _SAFE.sub('-', r.tds_doc_number or '')
                tds_name  = f"{r.tds_number}{'_' + safe_doc if safe_doc else ''}_TDS.pdf"
                zf.writestr(tds_name, tds_bytes)
                written += 1
                if merger is not None:
                    try:
                        merger.append(io.BytesIO(tds_bytes))
                    except Exception:
                        logger.exception(
                            "Merged-PDF append failed for tds_id=%s (batch=%s) - "
                            "this belt is still in the ZIP as its own file, just "
                            "not in the merged copy.", r.tds_id, batch_id
                        )
            except Exception:
                logger.exception(
                    "TDS PDF generation failed for tds_id=%s (batch=%s)", r.tds_id, batch_id
                )
                failed.append(r.tds_number)

            # ── QAP PDF (silently skipped if no template mapped) ──────────────
            try:
                qap_template = resolve_qap_template(r)
                if qap_template is not None:
                    qap_context = build_qap_context(
                        r, qap_template,
                        doc_type=qap_doc_type, ref_no=qap_ref_no, ref_date=qap_ref_date,
                    )
                    qap_bytes   = render_qap_pdf(qap_context)
                    qap_name    = f"{r.tds_number}_QAP.pdf"
                    zf.writestr(qap_name, qap_bytes)
            except Exception:
                logger.exception(
                    "QAP PDF generation failed for tds_id=%s (batch=%s)", r.tds_id, batch_id
                )
                # QAP failure does not count against written — TDS is still in the ZIP

    if written == 0:
        return Response(
            {'detail': 'No PDFs could be generated. Check server logs for WeasyPrint errors.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    safe_customer = _SAFE.sub('_', batch_customer_name or '').strip('_') or f'Batch_{batch_id}'

    # ── Merged PDF - added as one more entry in the same ZIP ────────────────
    # Silently skipped (ZIP still succeeds) if pypdf isn't installed or no
    # belt's PDF rendered successfully - merger.pages stays empty in that case.
    if merger is not None and len(merger.pages) > 0:
        try:
            merged_buf = io.BytesIO()
            merger.write(merged_buf)
            with zipfile.ZipFile(buf, 'a', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"{safe_customer}_Merged.pdf", merged_buf.getvalue())
        except Exception:
            logger.exception("Merged-PDF assembly failed for batch=%s", batch_id)
        finally:
            merger.close()

    buf.seek(0)
    resp = DjangoHttpResponse(buf.read(), content_type='application/zip')
    resp['Content-Disposition'] = f'attachment; filename="{safe_customer}.zip"'

    if failed:
        logger.warning(
            "Batch %s zip: %d TDS PDFs skipped (generation errors): %s",
            batch_id, len(failed), failed,
        )

    # BUG FIX: this function built `resp` above but never returned it, so it
    # fell through to an implicit `return None`. DRF's finalize_response()
    # then hit `assert isinstance(response, HttpResponseBase)` and raised,
    # turning every single call to "Download All PDFs (ZIP)" into a 500 -
    # the endpoint was completely non-functional. The sibling
    # download_batch_merged_zip() below already does `return resp` correctly;
    # this was a straightforward missing-return in the plain (non-merged) ZIP
    # endpoint.
    return resp


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_batch_merged_zip(request, batch_id):
    """
    GET /api/tds/batch/{batch_id}/download-merged-zip/?copy=customer|internal

    Companion to download_batch_zip() (per-belt files) and print_all_batch()
    (merged PDF, opened for printing rather than saved) - this is "Download
    Merged PDF" on the batch preview page: a ZIP containing ONE merged TDS
    PDF (every belt, each starting its own page - same merge as print-all)
    plus each belt's individual QAP PDF, since a single PDF can't sensibly
    hold both a merged multi-belt TDS and per-belt QAP documents together.

    Same PO/Enquiry query params as download_batch_zip (doc_type/ref_no/
    ref_date) - read fresh here too, never persisted, applied to every QAP
    in the batch.

    ZIP filename    : {CustomerName}_Merged.zip
    Merged filename : {CustomerName}_Merged.pdf
    QAP filenames   : {tds_number}_QAP.pdf   (skipped silently if no template mapped)
    """
    import io
    import zipfile
    from django.http import HttpResponse as DjangoHttpResponse

    try:
        from pypdf import PdfWriter
    except ImportError:
        return Response(
            {
                'detail': (
                    "Download Merged PDF requires the 'pypdf' package, which "
                    "isn't installed yet. Run: pip install pypdf  (see requirements.txt)"
                )
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    batch = TDSBatch.objects.filter(pk=batch_id).first()
    if not batch:
        raise NotFound(f"Batch {batch_id} not found")

    try:
        from apps.api.routers.pdf_views import render_tds_pdf_bytes
    except ImportError:
        return Response(
            {'detail': 'Batch PDF generation is not yet wired up (render_tds_pdf_bytes missing).'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    from apps.services.qap_service import resolve_qap_template, build_qap_context
    from apps.services.pdf_renderer import render_qap_pdf

    exclude_groups = _resolve_copy_type(request)

    # PO / Enquiry for the QAP PDFs bundled below - same params as
    # download_batch_zip, read fresh and never persisted.
    qap_doc_type = request.GET.get('doc_type', 'PO')
    qap_ref_no   = request.GET.get('ref_no', '')
    qap_ref_date = request.GET.get('ref_date', '')

    records = (
        TDSInput.objects
        .filter(batch_id=batch_id)
        .select_related('customer', 'standard', 'cover_grade')
        .order_by('tds_id')
    )

    _SAFE = re.compile(r'[<>:"/\\|?*]')

    merger  = PdfWriter()
    merged  = 0
    failed  = []
    batch_customer_name = None
    qap_bundle = []   # [(filename, bytes), ...] added to the ZIP alongside the merged PDF

    for r in records:
        if batch_customer_name is None and r.customer_id:
            batch_customer_name = r.customer.customer_name

        # ── TDS PDF → merged into one document ──────────────────────────────
        try:
            tds_bytes = render_tds_pdf_bytes(r.tds_id, exclude_groups=exclude_groups)
            merger.append(io.BytesIO(tds_bytes))
            merged += 1
        except Exception:
            logger.exception(
                "TDS PDF generation failed for tds_id=%s (batch=%s merged-zip)", r.tds_id, batch_id
            )
            failed.append(r.tds_number)

        # ── QAP PDF (silently skipped if no template mapped) ────────────────
        try:
            qap_template = resolve_qap_template(r)
            if qap_template is not None:
                qap_context = build_qap_context(
                    r, qap_template,
                    doc_type=qap_doc_type, ref_no=qap_ref_no, ref_date=qap_ref_date,
                )
                qap_bytes = render_qap_pdf(qap_context)
                qap_bundle.append((f"{r.tds_number}_QAP.pdf", qap_bytes))
        except Exception:
            logger.exception(
                "QAP PDF generation failed for tds_id=%s (batch=%s merged-zip)", r.tds_id, batch_id
            )

    if merged == 0:
        merger.close()
        return Response(
            {'detail': 'No PDFs could be generated. Check server logs for WeasyPrint errors.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    safe_customer = _SAFE.sub('_', batch_customer_name or '').strip('_') or f'Batch_{batch_id}'

    merged_buf = io.BytesIO()
    merger.write(merged_buf)
    merger.close()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{safe_customer}_Merged.pdf", merged_buf.getvalue())
        for qap_name, qap_bytes in qap_bundle:
            zf.writestr(qap_name, qap_bytes)
    buf.seek(0)

    resp = DjangoHttpResponse(buf.read(), content_type='application/zip')
    resp['Content-Disposition'] = f'attachment; filename="{safe_customer}_Merged.zip"'

    if failed:
        logger.warning(
            "Batch %s merged-zip: %d TDS PDFs skipped (generation errors): %s",
            batch_id, len(failed), failed,
        )

    return resp


# ── Print All (merged single PDF) ───────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def print_all_batch(request, batch_id):
    """
    GET /api/tds/batch/{batch_id}/print-all/?copy=customer|internal

    Generate every TDS record's PDF (same rendering as the ZIP download, same
    ?copy= semantics — see _resolve_copy_type() above), then merge them into
    ONE PDF (each belt starting on its own page) and return it inline so the
    frontend can open a single print-ready tab instead of one tab per belt —
    most browsers block more than one or two popup tabs per click anyway, so
    a single merged document is the only reliable "print the whole batch" UX.

    Requires the `pypdf` package (see requirements.txt) purely to concatenate
    already-rendered PDFs — no WeasyPrint re-rendering happens here.
    """
    import io

    try:
        from pypdf import PdfWriter
    except ImportError:
        return Response(
            {
                'detail': (
                    "Print All requires the 'pypdf' package, which isn't "
                    "installed yet. Run: pip install pypdf  (see requirements.txt)"
                )
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    batch = TDSBatch.objects.filter(pk=batch_id).first()
    if not batch:
        raise NotFound(f"Batch {batch_id} not found")

    try:
        from apps.api.routers.pdf_views import render_tds_pdf_bytes
    except ImportError:
        return Response(
            {'detail': 'Batch PDF generation is not yet wired up (render_tds_pdf_bytes missing).'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    exclude_groups = _resolve_copy_type(request)
    records = TDSInput.objects.filter(batch_id=batch_id).order_by('tds_id')

    writer  = PdfWriter()
    failed  = []
    merged  = 0

    for r in records:
        try:
            pdf_bytes = render_tds_pdf_bytes(r.tds_id, exclude_groups=exclude_groups)
            writer.append(io.BytesIO(pdf_bytes))
            merged += 1
        except Exception:
            logger.exception(
                "PDF generation failed for tds_id=%s (batch=%s print-all)", r.tds_id, batch_id
            )
            failed.append(r.tds_number)

    if merged == 0:
        return Response(
            {'detail': 'No PDFs could be generated. Check server logs for WeasyPrint errors.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    out = io.BytesIO()
    writer.write(out)
    writer.close()
    out.seek(0)

    from django.http import HttpResponse as DjangoHttpResponse
    resp = DjangoHttpResponse(out.read(), content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="TDS_Batch_{batch_id}_print_all.pdf"'

    if failed:
        logger.warning(
            "Batch %s print-all: %d PDFs skipped (generation errors): %s",
            batch_id, len(failed), failed,
        )

    return resp


# ── Text-import resolver ──────────────────────────────────────────────────────

def _resolve_belt_line(line, standard_id, line_num, errors):
    """
    Resolve one parsed belt line (dict of text values) to a dict of integer IDs.
    Appends plain-English messages to `errors`. Returns the resolved dict, or None
    if any required name could not be matched.

    Fields supported (matching the 14-field text format):
      width, fabric, rating, top, bottom, grade, edge, end_type, belt_type, length,
      bot_plies, bob_plies, carcass_mm (optional), doc_no (optional)
    """
    row_errors = []

    # Fabric → FabricType by fabric_code (case-insensitive)
    fabric_text = (line.get('fabric') or '').strip()
    fabric_obj  = FabricType.objects.filter(fabric_code__iexact=fabric_text).first()
    if not fabric_obj:
        row_errors.append(
            f"Fabric code '{fabric_text}' was not found in the system — "
            f"please check the fabric code (e.g. EP, NN, Steel)"
        )

    # Rating → BeltRating by rating_name, scoped to the resolved fabric
    rating_text = (line.get('rating') or '').strip()
    rating_obj  = None
    if fabric_obj:
        rating_obj = BeltRating.objects.filter(
            rating_name__iexact=rating_text,
            fabric_type_id=fabric_obj.id,
        ).first()
        if not rating_obj:
            row_errors.append(
                f"Belt rating '{rating_text}' was not found for fabric '{fabric_text}' — "
                f"verify the rating name (e.g. EP 315/3, EP 400/3, EP 630/4)"
            )

    # Grade → CoverGrade by grade_code, scoped to the shared standard
    grade_text = (line.get('grade') or '').strip()
    grade_obj  = None
    if standard_id:
        grade_obj = CoverGrade.objects.filter(
            grade_code__iexact=grade_text,
            standard_id=standard_id,
        ).first()
    if not grade_obj:
        row_errors.append(
            f"Cover grade '{grade_text}' was not found in the selected standard — "
            f"check the grade code (e.g. M24, N17, HR, FR)"
        )

    # Belt type → BeltType by belt_type (case-insensitive; partial match fallback)
    belt_type_text = (line.get('belt_type') or '').strip()
    belt_type_obj  = (
        BeltType.objects.filter(belt_type__iexact=belt_type_text).first()
        or BeltType.objects.filter(belt_type__icontains=belt_type_text).first()
    )
    if not belt_type_obj:
        row_errors.append(
            f"Belt type '{belt_type_text}' was not found — "
            f"use a name from the master data (e.g. Flat, Troughed)"
        )

    # Numeric fields
    width = top = bottom = length = None
    try:
        width = int(line.get('width'))
    except (TypeError, ValueError):
        row_errors.append('Belt Width (mm) must be a whole number (e.g. 1200)')

    try:
        top = float(line.get('top'))
    except (TypeError, ValueError):
        row_errors.append('Top Cover (mm) must be a number (e.g. 6)')

    try:
        bottom = float(line.get('bottom'))
    except (TypeError, ValueError):
        row_errors.append('Bottom Cover (mm) must be a number (e.g. 3)')

    try:
        length = float(line.get('length'))
    except (TypeError, ValueError):
        row_errors.append('Belt Length (m) must be a number (e.g. 300)')

    # Optional per-belt carcass override
    carcass_mm = None
    raw_carcass = line.get('carcass_mm')
    if raw_carcass not in (None, '', 'null'):
        try:
            carcass_mm = float(raw_carcass)
        except (TypeError, ValueError):
            row_errors.append(
                f"Carcass (mm) must be a decimal number if provided (e.g. 5.4) — "
                f"leave blank to auto-calculate from the belt rating"
            )

    # tds_doc_number is now shared at batch level (from the Customer & Document Info section)

    # BOT / BOB plies (1 by default if blank / "0")
    def _int_ply(val):
        try:
            v = int(val)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    bot_plies = _int_ply(line.get('bot_plies'))
    bob_plies = _int_ply(line.get('bob_plies'))

    # Edge construction
    edge_raw = (line.get('edge') or '').strip().lower()
    edge_construction = 'Cut Edge' if 'cut' in edge_raw else 'Moulded Edge'

    # End type
    end_raw = (line.get('end_type') or '').strip().lower()
    construction_type = 'Endless' if 'endless' in end_raw else 'Open-End'

    if row_errors:
        for msg in row_errors:
            errors.append(f"Line {line_num}: {msg}")
        return None

    return {
        'fabric_type_id':       fabric_obj.id,
        'belt_rating_id':       rating_obj.id,
        'cover_grade_id':       grade_obj.id,
        'belt_type_id':         belt_type_obj.belt_id,
        'belt_width_mm':        width,
        'top_cover_mm':         top,
        'bottom_cover_mm':      bottom,
        'belt_length_m':        length,
        'edge_construction':    edge_construction,
        'construction_type':    construction_type,
        'breaker_top_plies':    bot_plies,
        'breaker_bottom_plies': bob_plies,
        'carcass_thickness_mm': carcass_mm,   # None → auto from EAV for this belt
    }


# ── Text-import batch endpoint ────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def text_import_batch(request):
    """
    Text-import variant of create_batch.

    Accepts belt specifications as text-parsed name strings and resolves them
    to database IDs before creating the batch atomically.

    Expected JSON body:
    {
      "shared": {
        "purpose_id":           <int>,
        "brand_id":             <int>,
        "standard_id":          <int>,
        "make_of_fabric":       "MIT" | "SRF",      // default "MIT"
        "splicing_required":    true | false,
        "vulcanization_method": "Hot" | "Cold" | null,
        "num_joints":           <int> | null,       // default 2 when splicing on
        "reel_type_id":         <int> | null,
        "packing_type_id":      <int> | null,
        "shipping_region":      <str> | null
      },
      "customer": {
        "customer_id": <int>     // existing customer  OR
        "customer_name": <str>,  // create inline
        "contact_person": <str> | null,
        "application":    <str> | null,
        "plant_location": <str> | null
      },
      "belt_lines": [
        {
          "line_num":  <int>,
          "width":     "<int>",     // belt_width_mm
          "fabric":    "<str>",     // FabricType.fabric_code  (case-insensitive)
          "rating":    "<str>",     // BeltRating.rating_name  (case-insensitive)
          "top":       "<float>",   // top_cover_mm
          "bottom":    "<float>",   // bottom_cover_mm
          "grade":     "<str>",     // CoverGrade.grade_code   (case-insensitive)
          "edge":      "<str>",     // "Cut" → "Cut Edge"  |  else → "Moulded Edge"
          "end_type":  "<str>",     // "Endless" → "Endless"  |  else → "Open-End"
          "belt_type": "<str>",     // BeltType.belt_type      (case-insensitive)
          "length":    "<float>",   // belt_length_m
          "bot_plies":  "<int>" | null,  // breaker_top_plies    (optional, default 1)
          "bob_plies":  "<int>" | null,  // breaker_bottom_plies (optional, default 1)
          "carcass_mm": "<float>" | null, // per-belt carcass override (optional, null → auto from EAV)
          "doc_no":     "<str>" | null    // per-belt TDS document number (optional)
        },
        ...
      ]
    }
    """
    data          = request.data
    current_user  = request.user
    shared        = data.get('shared') or {}
    customer_data = data.get('customer') or {}
    belt_lines    = data.get('belt_lines') or []

    if not belt_lines:
        raise ValidationError({'belt_lines': 'At least one belt line is required.'})
    # ROBUSTNESS (fixed): same cap as create_batch above — prevents an
    # unbounded text-pasted batch from being processed entirely synchronously.
    if len(belt_lines) > 200:
        raise ValidationError({'belt_lines': f'A single batch is limited to 200 belt lines (got {len(belt_lines)}).'})

    # ── Step 1: Validate shared reference data ───────────────────────────────
    shared_errors = {}

    purpose_id  = shared.get('purpose_id')
    brand_id    = shared.get('brand_id')
    standard_id = shared.get('standard_id')

    _require_shared(Purpose,    purpose_id,  'purpose_id',  shared_errors)
    _require_shared(IndusBrand, brand_id,    'brand_id',    shared_errors)
    _require_shared(Standard,   standard_id, 'standard_id', shared_errors)

    make_of_fabric       = shared.get('make_of_fabric', 'MIT')
    tds_doc_number_shared = (shared.get('tds_doc_number') or '').strip() or None
    splicing_required    = bool(shared.get('splicing_required', False))
    vulcanization_method = shared.get('vulcanization_method')
    num_joints_shared    = shared.get('num_joints', 1)
    reel_type_id         = shared.get('reel_type_id')
    packing_type_id      = shared.get('packing_type_id')
    shipping_region      = shared.get('shipping_region')
    container_type_id    = shared.get('container_type_id')

    if reel_type_id:
        _require_shared(ReelType, reel_type_id, 'reel_type_id', shared_errors)
    if packing_type_id:
        pt = _require_shared(PackingType, packing_type_id, 'packing_type_id', shared_errors)
        if pt and not pt.is_available:
            shared_errors.setdefault('packing_type_id', []).append(
                f"Packing type '{pt.packing_name}' is not yet available."
            )

    if splicing_required and not vulcanization_method:
        shared_errors.setdefault('vulcanization_method', []).append(
            'vulcanization_method is required when splicing_required=true.'
        )

    if shared_errors:
        raise ValidationError(shared_errors)

    # ── Step 2: Resolve / create customer ────────────────────────────────────
    customer_id_req = customer_data.get('customer_id')
    if customer_id_req:
        customer_obj = Customer.objects.filter(pk=customer_id_req).first()
        if not customer_obj:
            raise NotFound(f"Customer with id={customer_id_req} not found")
        customer_id = customer_obj.customer_id
    else:
        cname = (customer_data.get('customer_name') or '').strip()
        if not cname:
            raise ValidationError(
                {'customer': 'Provide customer_id OR customer_name to identify the customer.'}
            )
        customer_obj, _ = Customer.objects.get_or_create(
            customer_name=cname,
            defaults={
                'contact_person': customer_data.get('contact_person'),
                'application':    customer_data.get('application'),
                'plant_location': customer_data.get('plant_location'),
            },
        )
        customer_id = customer_obj.customer_id

    # ── Step 3: Resolve text names → IDs for all belt lines ─────────────────
    resolver_errors = []
    resolved_belts  = []

    for line in belt_lines:
        line_num = line.get('line_num', '?')
        resolved = _resolve_belt_line(line, standard_id, line_num, resolver_errors)
        if resolved is not None:
            # Endless belts are a closed loop and must fit within a fixed max
            # length — same shared check used by create_tds and create_batch,
            # so the text-import path (used by the bulk multi-belt flow)
            # can't let a value like 300 m through when end_type is "Endless".
            try:
                validate_endless_belt_length(
                    resolved.get('construction_type'), resolved.get('belt_length_m')
                )
            except ValueError as exc:
                resolver_errors.append(f"Line {line_num}: {exc}")
                continue
            resolved_belts.append(resolved)

    if resolver_errors:
        raise ValidationError({'belt_lines': resolver_errors})

    if not resolved_belts:
        raise ValidationError({'belt_lines': 'No valid belt lines after name resolution.'})

    # ── Step 4: Atomic creation ──────────────────────────────────────────────
    with transaction.atomic():

        batch = TDSBatch.objects.create(
            make_of_fabric       = make_of_fabric,
            splicing_required    = splicing_required,
            vulcanization_method = vulcanization_method,
            reel_type_id         = reel_type_id,
            packing_type_id      = packing_type_id,
            shipping_region      = shipping_region,
            created_by_id        = current_user.user_id,
        )

        created_records = []

        for b in resolved_belts:
            rating      = BeltRating.objects.get(pk=b['belt_rating_id'])
            cover_grade = CoverGrade.objects.get(pk=b['cover_grade_id'])
            fabric_type = FabricType.objects.get(pk=b['fabric_type_id'])
            belt_type   = BeltType.objects.get(pk=b['belt_type_id'])

            kn, plies = _parse_rating(rating.rating_name)

            carcass_from_rating, interply_skim_mm = _fetch_carcass_eav(rating.id)

            # Per-belt carcass override takes priority over EAV auto value
            per_belt_carcass = b.get('carcass_thickness_mm')
            if per_belt_carcass is not None:
                carcass_thickness_mm = float(per_belt_carcass)
            else:
                carcass_thickness_mm = carcass_from_rating or 0.0

            top_cover_mm    = float(b['top_cover_mm'])
            bottom_cover_mm = float(b.get('bottom_cover_mm') or 0)
            total_thickness_mm = top_cover_mm + bottom_cover_mm + carcass_thickness_mm

            fabric_style_id = _auto_fabric_style(b['fabric_type_id'], kn, plies)

            weight_per_m = belt_weight_per_metre(
                specific_gravity   = float(cover_grade.specific_gravity),
                width_mm           = int(b['belt_width_mm']),
                total_thickness_mm = total_thickness_mm,
            )

            bot_plies = b.get('breaker_top_plies')
            bob_plies = b.get('breaker_bottom_plies')

            description = _belt_description(
                width              = b['belt_width_mm'],
                fabric_code        = fabric_type.fabric_code,
                rating_name        = rating.rating_name,
                top                = top_cover_mm,
                bottom             = bottom_cover_mm,
                grade_code         = cover_grade.grade_code,
                edge               = b.get('edge_construction', 'Moulded'),
                belt_type_name     = belt_type.belt_type,
                construction_type  = b.get('construction_type', 'Open-End'),
            )

            # Packing
            packing_num_rolls    = None
            packing_len_per_roll = None
            packing_roll_dims    = None
            packing_net_weight   = None
            packing_gross_weight = None
            packing_gw_per_roll  = None

            if reel_type_id and packing_type_id:
                try:
                    pr = compute_packing(
                        reel_type_id         = reel_type_id,
                        packing_type_id      = packing_type_id,
                        purpose_id           = purpose_id,
                        total_thickness_mm   = total_thickness_mm,
                        belt_length_m        = float(b['belt_length_m']),
                        belt_width_mm        = int(b['belt_width_mm']),
                        belt_weight_per_m_kg = float(weight_per_m),
                    )
                    packing_num_rolls    = pr.num_rolls
                    packing_len_per_roll = pr.length_per_roll_m
                    packing_roll_dims    = pr.roll_dimensions
                    packing_net_weight   = pr.net_weight_kg
                    packing_gross_weight = pr.gross_weight_kg
                    packing_gw_per_roll  = pr.gross_weight_per_roll_kg
                except Exception:
                    logger.exception(
                        "compute_packing failed for belt_rating_id=%s belt_width_mm=%s",
                        b['belt_rating_id'], b['belt_width_mm'],
                    )

            # Splicing — num_joints = 1 per roll (typical, auto-computed from packing);
            # falls back to shared value if packing wasn't computed.
            step_len   = None
            spl_len    = None
            extra_len  = None
            num_joints = packing_num_rolls if packing_num_rolls else (int(num_joints_shared) if num_joints_shared else None)

            if splicing_required and num_joints and vulcanization_method:
                try:
                    sr = compute_splicing(
                        belt_rating_kn_m     = kn,
                        num_plies            = plies,
                        belt_width_mm        = int(b['belt_width_mm']),
                        num_joints           = num_joints,
                        vulcanization_method = vulcanization_method,
                    )
                    step_len  = sr.step_length_mm
                    spl_len   = sr.splice_length_mm
                    extra_len = sr.total_extra_length_m
                except Exception:
                    logger.exception(
                        "compute_splicing failed for belt_rating_id=%s", b['belt_rating_id']
                    )

            tds_number = next_tds_number()

            record = TDSInput(
                tds_number           = tds_number,
                tds_doc_number       = tds_doc_number_shared,   # shared across batch (from Customer & Document Info)
                tds_date             = date.today(),
                status               = 'draft',
                construction_type    = b.get('construction_type', 'Open-End'),
                purpose_id           = purpose_id,
                belt_type_id         = b['belt_type_id'],
                brand_id             = brand_id,
                standard_id          = standard_id,
                customer_id          = customer_id,
                cover_grade_id       = b['cover_grade_id'],
                fabric_type_id       = b['fabric_type_id'],
                fabric_style_id      = fabric_style_id,
                belt_rating_id       = b['belt_rating_id'],
                belt_description     = description,
                belt_length_m        = b['belt_length_m'],
                belt_weight_per_m_kg = weight_per_m,
                make_of_fabric       = make_of_fabric,
                belt_width_mm        = b['belt_width_mm'],
                num_plies            = plies,
                top_cover_mm         = top_cover_mm,
                bottom_cover_mm      = bottom_cover_mm,
                carcass_from_rating  = carcass_from_rating,
                carcass_thickness_mm = carcass_thickness_mm,
                interply_skim_mm     = interply_skim_mm,
                total_thickness_mm   = total_thickness_mm,
                breaker_top          = bot_plies is not None,
                breaker_top_plies    = bot_plies,
                breaker_bottom       = bob_plies is not None,
                breaker_bottom_plies = bob_plies,
                edge_construction    = b.get('edge_construction', 'Moulded'),
                reel_type_id         = reel_type_id,
                packing_type_id      = packing_type_id,
                num_rolls            = packing_num_rolls,
                length_per_roll_m    = packing_len_per_roll,
                roll_dimensions      = packing_roll_dims,
                net_weight_kg        = packing_net_weight,
                gross_weight_kg      = packing_gross_weight,
                gross_weight_per_roll_kg = packing_gw_per_roll,
                shipping_region      = shipping_region,
                container_type_id    = container_type_id,
                splicing_required    = splicing_required,
                vulcanization_method = vulcanization_method if splicing_required else None,
                num_joints           = num_joints,
                step_length_mm       = step_len,
                splice_length_mm     = spl_len,
                total_extra_length_m = extra_len,
                batch                = batch,
            )
            record.created_by_id = current_user.user_id
            record.save()
            created_records.append(record)

    logger.info(
        "TDSBatch #%s (text-import) created by user_id=%s with %d belts",
        batch.batch_id, current_user.user_id, len(created_records),
    )

    batch_customer_name = next(
        (r.customer.customer_name for r in created_records if r.customer_id), None
    )

    return Response(
        {
            "batch":         _batch_brief(batch),
            "customer_name": batch_customer_name,
            "tds_records": [
                {
                    "tds_id":           r.tds_id,
                    "tds_number":       r.tds_number,
                    "tds_doc_number":   r.tds_doc_number,
                    "belt_description": r.belt_description,
                }
                for r in created_records
            ],
            "count": len(created_records),
        },
        status=status.HTTP_201_CREATED,
    )
