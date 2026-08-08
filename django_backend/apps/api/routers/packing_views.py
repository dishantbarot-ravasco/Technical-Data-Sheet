"""
apps/api/routers/packing_views.py — Packing & Logistics endpoints.

Ported from FastAPI routers/packing.py.

Endpoints:
  POST  /api/tds/{id}/packing  — compute and save packing fields
  PATCH /api/tds/{id}/packing  — recompute packing (overwrite existing)
"""
import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, ValidationError

from apps.core.models import PackingType, ReelType, TDSInput
from apps.services.packing_service import compute_packing

logger = logging.getLogger(__name__)


def _packing_result(record):
    return {
        "tds_id":                   record.tds_id,
        "reel_type_id":             record.reel_type_id,
        "packing_type_id":          record.packing_type_id,
        "num_rolls":                record.num_rolls,
        "length_per_roll_m":        float(record.length_per_roll_m) if record.length_per_roll_m else None,
        "roll_dimensions":          record.roll_dimensions,
        "net_weight_kg":            float(record.net_weight_kg) if record.net_weight_kg else None,
        "gross_weight_kg":          float(record.gross_weight_kg) if record.gross_weight_kg else None,
        "gross_weight_per_roll_kg": float(record.gross_weight_per_roll_kg) if record.gross_weight_per_roll_kg else None,
    }


def _compute_and_save(tds_id, reel_type_id, packing_type_id):
    """Shared computation logic for POST and PATCH /tds/{id}/packing."""
    record = TDSInput.objects.filter(pk=tds_id).first()
    if not record:
        raise NotFound(f"TDS {tds_id} not found")
    if record.status not in ('draft',):
        from rest_framework import status as drf_status
        from rest_framework.response import Response
        raise ValidationError({'detail': f"TDS {tds_id} is '{record.status}' — only draft records can be updated"})

    reel = ReelType.objects.filter(pk=reel_type_id).first()
    if not reel:
        raise NotFound(f"reel_type_id={reel_type_id} not found")

    ptype = PackingType.objects.filter(pk=packing_type_id).first()
    if not ptype:
        raise NotFound(f"packing_type_id={packing_type_id} not found")
    if not ptype.is_available:
        raise ValidationError({'detail': f"Packing type '{ptype.packing_name}' is not yet available"})

    if record.belt_weight_per_m_kg is None:
        raise ValidationError({
            'detail': "belt_weight_per_m_kg is NULL on this TDS — cannot compute packing weights."
        })

    result = compute_packing(
        reel_type_id         = reel_type_id,
        packing_type_id      = packing_type_id,
        purpose_id           = record.purpose_id,
        total_thickness_mm   = float(record.total_thickness_mm),
        belt_length_m        = float(record.belt_length_m),
        belt_width_mm        = record.belt_width_mm,
        belt_weight_per_m_kg = float(record.belt_weight_per_m_kg),
    )

    record.reel_type_id             = reel_type_id
    record.packing_type_id          = packing_type_id
    record.num_rolls                = result.num_rolls
    record.length_per_roll_m        = result.length_per_roll_m
    record.roll_dimensions          = result.roll_dimensions
    record.net_weight_kg            = result.net_weight_kg
    record.gross_weight_kg          = result.gross_weight_kg
    record.gross_weight_per_roll_kg = result.gross_weight_per_roll_kg
    record.save()
    return record


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def compute_packing_for_tds(request, tds_id):
    """Compute and save packing fields for an existing draft TDS."""
    data           = request.data
    reel_type_id   = data.get('reel_type_id')
    packing_type_id = data.get('packing_type_id')
    if not reel_type_id or not packing_type_id:
        raise ValidationError({'detail': 'reel_type_id and packing_type_id are required.'})
    record = _compute_and_save(tds_id, int(reel_type_id), int(packing_type_id))
    return Response(_packing_result(record))


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def recompute_packing_for_tds(request, tds_id):
    """Recompute packing fields, overwriting any previously saved values."""
    data           = request.data
    reel_type_id   = data.get('reel_type_id')
    packing_type_id = data.get('packing_type_id')
    if not reel_type_id or not packing_type_id:
        raise ValidationError({'detail': 'reel_type_id and packing_type_id are required.'})
    record = _compute_and_save(tds_id, int(reel_type_id), int(packing_type_id))
    return Response(_packing_result(record))
