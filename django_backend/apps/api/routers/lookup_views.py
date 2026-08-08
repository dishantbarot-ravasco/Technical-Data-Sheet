"""
apps/api/routers/lookup_views.py — TDS preview data assembly and dimensional spec resolution.

Ported from FastAPI routers/lookup.py.  No authentication required.

Endpoints:
  POST /api/tds/lookup
  GET  /api/tds/dimensional-specs
"""
import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, ValidationError

from apps.core.models import (
    Standard, CoverGrade, BeltRating, FabricType, FabricStyle,
    DimensionalParameterSpec, TDSParameter,
)
from apps.services.calculations import parse_belt_rating, auto_select_fabric_style

logger = logging.getLogger(__name__)


def _std_code(name: str) -> str:
    """Convert a human-readable standard name into a short internal code string."""
    n = name.upper()
    if "INHOUSE" in n or "IN-HOUSE" in n or "IN HOUSE" in n:
        return "ISO14890_INHOUSE"
    if "IS 1891" in n:
        return "IS1891"
    if "ISO 14890" in n:
        return "ISO14890"
    if "DIN 22102" in n:
        return "DIN22102"
    if "AS 1332" in n:
        return "AS1332"
    if "SANS 1173" in n:
        return "SANS1173"
    if "ASTM D378" in n:
        return "ASTMD378"
    return "ISO14890"


# EAV field maps: parameter_id → (spec_field_name, indus_field_name)
# None spec_field = no spec column needed for that parameter in the preview.

CG_MAP = {
    25: ("spec_top_tensile_mpa",      "indus_top_tensile_mpa"),
    26: ("spec_bot_tensile_mpa",      "indus_bot_tensile_mpa"),
    27: ("spec_top_elong_pct",        "indus_top_elong_pct"),
    28: ("spec_bot_elong_pct",        "indus_bot_elong_pct"),
    29: ("spec_top_hardness",         "indus_top_hardness"),
    30: ("spec_bot_hardness",         "indus_bot_hardness"),
    31: ("spec_abrasion_loss_mm3",    "indus_abrasion_loss_mm3"),
    33: ("spec_pct_chg_top_ts",       "indus_pct_chg_top_ts"),
    34: ("spec_pct_chg_bot_ts",       "indus_pct_chg_bot_ts"),
    35: ("spec_pct_chg_top_elong",    "indus_pct_chg_top_elong"),
    36: ("spec_pct_chg_bot_elong",    "indus_pct_chg_bot_elong"),
    43: ("spec_top_cover_to_ply",     "indus_top_cover_to_ply"),
    44: ("spec_ply_to_ply",           "indus_ply_to_ply"),
    45: ("spec_bot_cover_to_ply",     "indus_bot_cover_to_ply"),
}

BR_MAP = {
    4:  (None,                        "carcass_thickness_mm"),
    5:  (None,                        "interply_skim_mm"),
    37: ("spec_warp_kn_m",            "indus_warp_kn_m"),
    38: ("spec_weft_kn_m",            "indus_weft_kn_m"),
    39: ("spec_elong_break_warp_pct", "indus_elong_break_warp_pct"),
    40: ("spec_elong_10pct_ref_warp", "indus_elong_10pct_ref_warp"),
    41: ("spec_elong_break_weft_pct", "indus_elong_break_weft_pct"),
    42: ("spec_elastic_modulus_kn_m", "indus_elastic_modulus_kn_m"),
    46: ("spec_troughability_fl",     "indus_troughability_650mm"),
    47: (None,                        "indus_troughability_2200mm"),
    48: (None,                        "pulley_type_a_mm"),
    49: (None,                        "pulley_type_b_mm"),
    50: (None,                        "pulley_type_c_mm"),
}


@api_view(['POST'])
@permission_classes([AllowAny])
def tds_lookup(request):
    """
    Assemble all EAV data into a flat preview object for the TDS creation form.
    Body: { standard_id, cover_grade_id, belt_rating_id }
    """
    data = request.data
    standard_id    = data.get('standard_id')
    cover_grade_id = data.get('cover_grade_id')
    belt_rating_id = data.get('belt_rating_id')

    if not all([standard_id, cover_grade_id, belt_rating_id]):
        raise ValidationError({'detail': 'standard_id, cover_grade_id, and belt_rating_id are required.'})

    std = Standard.objects.filter(pk=standard_id).first()
    if not std:
        raise NotFound("Standard not found")

    grade = CoverGrade.objects.filter(pk=cover_grade_id).first()
    if not grade:
        raise NotFound("Cover grade not found")

    rating = BeltRating.objects.filter(pk=belt_rating_id).first()
    if not rating:
        raise NotFound("Belt rating not found")

    ft = FabricType.objects.filter(pk=rating.fabric_type_id).first()

    # Build full standard name
    full_name = std.standard_name
    if std.standard_edition:
        full_name = f"{std.standard_name} : {std.standard_edition}"

    # Ageing condition from EAV (parameter_id=32)
    ageing_condition = "70 x 168 h"
    for v in grade.values.all():
        if v.parameter_id == 32:
            ageing_condition = v.indus_value
            break

    std_out = {
        "standard_id":      std.standard_id,
        "standard_name":    std.standard_name,
        "full_name":        full_name,
        "code":             _std_code(std.standard_name),
        "ageing_condition": ageing_condition,
    }

    cg_out = {
        "id":                grade.id,
        "grade_code":        grade.grade_code,
        "grade_description": grade.grade_description,
        "specific_gravity":  float(grade.specific_gravity),
    }
    for v in grade.values.all():
        m = CG_MAP.get(v.parameter_id)
        if m:
            spec_f, indus_f = m
            if spec_f:
                cg_out[spec_f] = v.spec_value
            cg_out[indus_f] = v.indus_value

    # Parse kN + num_plies from rating_name (e.g. "EP 315/3" → 315, 3) using the
    # one shared parser (apps.services.calculations.parse_belt_rating) — the
    # same function tds_views.py and batch_views.py use, so this live preview
    # can never disagree with what actually gets saved. This is a preview
    # endpoint, so a rating_name that doesn't match the expected format just
    # means num_plies/fabric_style come back as None rather than a hard error.
    try:
        belt_kn, num_plies = parse_belt_rating(rating.rating_name)
    except ValueError:
        belt_kn, num_plies = None, None

    br_out = {
        "id":          rating.id,
        "rating_name": rating.rating_name,
        "rating_code": rating.rating_name,
        "num_plies":   num_plies,
        "rating_kn_m": belt_kn,  # authoritative parsed kN/m — frontend should use this
                                 # instead of re-parsing rating_name with its own regex
    }
    for v in rating.values.all():
        m = BR_MAP.get(v.parameter_id)
        if m:
            spec_f, indus_f = m
            if spec_f:
                br_out[spec_f] = v.spec_value
            br_out[indus_f] = v.indus_value

    ft_out = None
    if ft:
        ft_out = {
            "id":                  ft.id,
            "code":                ft.fabric_code,
            "fabric_code":         ft.fabric_code,
            "full_name":           ft.description or ft.fabric_code,
            "description":         ft.description,
            "manufacturer":        ft.manufacturer,
            "free_shrinkage_warp": None,
        }

    # Auto-select fabric style the same way create_tds will when the record is
    # actually saved (apps.services.calculations.auto_select_fabric_style) —
    # so the live preview shows the style that's actually going to be used,
    # instead of the frontend re-deriving its own guess from dropdown text.
    fs_out = None
    if ft and belt_kn is not None and num_plies:
        style_id = auto_select_fabric_style(ft.id, belt_kn, num_plies)
        if style_id is not None:
            style = FabricStyle.objects.filter(pk=style_id).first()
            if style:
                fs_out = {"id": style.id, "style_name": style.style_name}

    return Response({
        "standard":     std_out,
        "cover_grade":  cg_out,
        "belt_rating":  br_out,
        "fabric_type":  ft_out,
        "fabric_style": fs_out,
        "fabric_spec":  None,
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def dimensional_specs(request):
    """
    Resolve the correct spec tolerance string for each dimensional parameter
    based on the user's entered values and the selected standard.

    NOTE: DimensionalParameterSpec uses `tolerance_value` (not `spec_value`).
    """
    params = request.query_params
    standard_id = params.get('standard_id')
    if not standard_id:
        raise ValidationError({'detail': 'standard_id is required.'})
    standard_id = int(standard_id)

    belt_width_mm = params.get('belt_width_mm')
    if belt_width_mm is None:
        raise ValidationError({'detail': 'belt_width_mm is required.'})

    # Map parameter_id → user-entered value
    param_values = {1: float(belt_width_mm)}
    for qkey, pid in [
        ('top_cover_mm', 2),
        ('bottom_cover_mm', 3),
        ('carcass_thickness_mm', 4),
        ('total_thickness_mm', 6),
    ]:
        val = params.get(qkey)
        if val is not None:
            param_values[pid] = float(val)

    # Load all dimensional spec rows for this standard, ordered for first-match correctness
    # (nulls_first on min_value ensures open lower bounds come first)
    from django.db.models import F
    all_rows = (
        DimensionalParameterSpec.objects
        .filter(standard_id=standard_id)
        .order_by('parameter_id', F('min_value').asc(nulls_first=True))
    )

    # Load parameter names
    param_ids = list({r.parameter_id for r in all_rows})
    param_name_map = {
        p.parameter_id: p.parameter_name
        for p in TDSParameter.objects.filter(parameter_id__in=param_ids)
    }

    result = {}
    for row in all_rows:
        key = str(row.parameter_id)
        if key in result:
            continue  # only first matching band
        val = param_values.get(row.parameter_id)
        if val is None:
            continue
        lo_ok = row.min_value is None or row.min_value <= val
        hi_ok = row.max_value is None or row.max_value >= val
        if lo_ok and hi_ok:
            result[key] = {
                "parameter_name": param_name_map.get(row.parameter_id, ""),
                "spec_value":     row.tolerance_value,   # field is tolerance_value, not spec_value
            }

    return Response(result)
