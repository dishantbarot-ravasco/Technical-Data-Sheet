"""
apps/services/calculations.py — Pure business-logic functions for TDS computed fields.

All functions are stateless and take plain Python values.
Django ORM replaces SQLAlchemy sessions — no db argument needed.

References:
  - Reference/Formulae.md
  - project memory: key business rules
"""

import math
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass


def round_half_up(value: float, decimals: int = 0):
    """
    Round using standard round-half-up (0.5 -> 1), not Python's built-in
    round(), which uses banker's rounding (round-half-to-even: 0.5 -> 0,
    1.5 -> 2, 2.5 -> 2, ...). Splice-length-style engineering values that land
    exactly on a .5 boundary should round consistently upward, matching how
    IS 14206 and most people read "round" -- not silently alternate direction
    depending on whether the preceding digit is odd or even.
    Returns an int when decimals=0 (matching round()'s own behavior), else a float.
    """
    q = Decimal('1') if decimals == 0 else Decimal('1').scaleb(-decimals)
    result = Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)
    return int(result) if decimals == 0 else float(result)


# ── Endless belt length cap ─────────────────────────────────────────────────
# Endless belts are pre-vulcanized into a closed loop before shipping, so they
# must physically fit as a single loop inside a container/reel — unlike
# Open-End belts, which ship on a reel and can be arbitrarily long. 100 m is
# the maximum loop length this business supports. This is the single shared
# constant — both the frontend (js/generate-tds.js, generate-tds.html) and
# every backend creation path (tds_views.create_tds, batch_views.create_batch,
# batch_views.text_import_batch) must enforce the same number.
ENDLESS_MAX_BELT_LENGTH_M = 100


def validate_endless_belt_length(construction_type: str, belt_length_m) -> None:
    """
    Raise ValueError if construction_type is 'Endless' and belt_length_m
    exceeds ENDLESS_MAX_BELT_LENGTH_M. No-op for any other construction type,
    or if belt_length_m is missing (required-field checks handle that case).
    """
    if (construction_type or '').strip().lower() != 'endless':
        return
    if belt_length_m is None:
        return
    if float(belt_length_m) > ENDLESS_MAX_BELT_LENGTH_M:
        raise ValueError(
            f"Endless belts cannot exceed {ENDLESS_MAX_BELT_LENGTH_M} m "
            f"(got {belt_length_m} m)."
        )


def validate_international_shipping_fields(purpose_type: str, shipping_region, container_type_id) -> None:
    """
    Raise ValueError if purpose_type is 'International' and either
    shipping_region or container_type_id is missing.

    BUG FIX: apps/core/models.py's own docstring for TDSInput says
    "International orders require shipping_region + container_type on
    TDSInput", and the frontend (generate-tds.js's submitTDS()) does mark
    these fields required in the UI when Purpose = International - but
    nothing enforced this server-side. A direct API call (or any client bug
    that skips the UI's own check) could silently create an "International"
    TDS with shipping_region=None and container_type_id=None: the exact data
    the PDF's shipping-constraint section and packing's international
    reel/weight caps (see recalcPacking()'s _isInternational() branch and
    get_container_constraints() below) depend on to even run. Enforce the
    same rule the model docstring already promises, the same way
    validate_endless_belt_length() enforces its own documented constraint.
    """
    if (purpose_type or '').strip().lower() != 'international':
        return
    missing = []
    if not shipping_region:
        missing.append('shipping_region')
    if not container_type_id:
        missing.append('container_type_id')
    if missing:
        raise ValueError(
            "International orders require " + " and ".join(missing) +
            f" ({'this field is' if len(missing) == 1 else 'these fields are'} mandatory for shipping/packing constraints)."
        )


# ── International container constraints ───────────────────────────────────────

@dataclass(frozen=True)
class ContainerConstraints:
    max_height_m: float         # caps reel diameter (m)
    max_width_m: float          # caps reel width: (belt_width_mm + 100) / 1000 must be ≤ this
    max_gross_weight_kg: float  # caps total gross weight (kg)


def get_container_constraints(
    container_type_id: int,
    shipping_region: str,
) -> ContainerConstraints:
    """
    Resolve physical + weight limits for an international shipment.

    Raises ValueError if the combination is not found in the DB.
    """
    from apps.core.models import ContainerType, RegionContainerWeightLimit

    ct = ContainerType.objects.filter(pk=container_type_id).first()
    if ct is None:
        raise ValueError(f"Container type id={container_type_id} not found.")

    wl = RegionContainerWeightLimit.objects.filter(
        container_type_id=container_type_id,
        region=shipping_region,
    ).first()
    if wl is None:
        raise ValueError(
            f"No weight limit found for region='{shipping_region}' + "
            f"container_type_id={container_type_id}."
        )

    return ContainerConstraints(
        max_height_m=float(ct.max_height_m),
        max_width_m=float(ct.max_width_m),
        max_gross_weight_kg=float(wl.max_gross_weight_kg),
    )


# ── Belt Weight ───────────────────────────────────────────────────────────────

def belt_weight_per_metre(
    specific_gravity: float,
    width_mm: int,
    total_thickness_mm: float,
) -> float:
    """
    NET weight of belt per linear metre (kg/m).

    Formula: W_net = SG × total_thickness_mm × (width_mm / 1000)

    ROBUSTNESS (fixed): this used to round to 3 decimal places, one fewer than
    the belt_weight_per_m_kg column's own DecimalField(decimal_places=4) — i.e.
    the DB schema was built to hold more precision than this function was
    keeping. Rounding this intermediate value early (before total_belt_weight()
    multiplies it by belt length) also compounds into a visible discrepancy on
    very long belts. Matching the DB's 4 decimal places keeps this value at
    the precision the schema already expects.
    """
    return round(specific_gravity * total_thickness_mm * (width_mm / 1000), 4)


def belt_gross_weight_per_metre(
    specific_gravity: float,
    width_mm: int,
    total_thickness_mm: float,
) -> float:
    """
    GROSS weight of belt per linear metre (kg/m).

    Formula: W_gross = SG × (total_thickness_mm + 0.5) × (width_mm / 1000)

    See belt_weight_per_metre() above for why this rounds to 4 decimal places.
    """
    return round(specific_gravity * (total_thickness_mm + 0.5) * (width_mm / 1000), 4)


def total_belt_weight(weight_per_m: float, length_m: float) -> float:
    """Total belt weight in kg."""
    return round(weight_per_m * length_m, 2)


# ── Reel Dimensions ───────────────────────────────────────────────────────────

def reel_diameter_circular(
    d_m: float,
    belt_length_m: float,
    k: float = 0.3,
) -> float:
    """
    Outer diameter of a circular reel (metres).

    D = sqrt((4/π) × d × L + k²)
    """
    return round(math.sqrt((4 / math.pi) * d_m * belt_length_m + k ** 2), 3)


def reel_diameter_twin(
    d_m: float,
    belt_length_m: float,
    k: float = 0.3,
) -> float:
    """
    Outer diameter of each twin reel (metres).

    D = sqrt((4/π) × d × (L/2) + k²)
    """
    return round(math.sqrt((4 / math.pi) * d_m * (belt_length_m / 2) + k ** 2), 3)


def reel_diameter_elliptical(
    d_m: float,
    belt_length_m: float,
    k: float = 0.3,
    l: float = 1.32,
) -> float:
    """
    Outer diameter (height) of an elliptical reel (metres).

    D = sqrt((4/π) × d × L + (k + 2l/π)²) − (2l/π)
    """
    term = k + (2 * l / math.pi)
    return round(math.sqrt((4 / math.pi) * d_m * belt_length_m + term ** 2) - (2 * l / math.pi), 3)


def reel_diameter(
    formula_key: str,
    total_thickness_mm: float,
    belt_length_m: float,
    k_m: float = 0.3,
    center_to_center_m: float = 1.32,
) -> float:
    """
    Dispatch to the correct reel formula by formula_key.

    Args:
        formula_key        : 'circular' | 'twin' | 'elliptical'
        total_thickness_mm : tds_inputs.total_thickness_mm
        belt_length_m      : tds_inputs.belt_length_m
        k_m                : core diameter in metres (reel_types.core_diameter_m)
        center_to_center_m : l value for elliptical reel (ignored for other types)
    """
    d_m = total_thickness_mm / 1000
    if formula_key == "twin":
        return reel_diameter_twin(d_m, belt_length_m, k_m)
    elif formula_key == "elliptical":
        return reel_diameter_elliptical(d_m, belt_length_m, k_m, center_to_center_m)
    else:  # circular (default)
        return reel_diameter_circular(d_m, belt_length_m, k_m)


# ── Belt Rating Parsing + Fabric Style Auto-Selection ──────────────────────────
#
# This used to be implemented three different ways in three different files
# (tds_views.py, batch_views.py, and the frontend's generate-tds.js), each with
# a slightly different regex and — in tds_views.py's case — a fallback of
# `num_plies * 100` when parsing failed, which has no engineering basis and
# would silently produce a wrong splice length. This is now the ONE place that
# parses a rating_name and picks a fabric style; every caller (single-belt
# create_tds, bulk batch import, and the live lookup preview) uses this.

import re as _re


def parse_belt_rating(rating_name: str) -> tuple:
    """
    Extract (kn: float, plies: int) from a rating_name like 'EP 315/3' or 'NN 630/4'.

    Raises ValueError if the format is unrecognised — callers that are about
    to persist a record should turn this into a 400 (ValidationError) rather
    than guessing a fallback number.
    """
    m = _re.search(r'(\d+(?:\.\d+)?)/(\d+)', rating_name or '')
    if not m:
        raise ValueError(
            f"Cannot parse kN/plies from belt rating '{rating_name}'. "
            f"Expected format: 'EP <kN>/<plies>'."
        )
    return float(m.group(1)), int(m.group(2))


def strip_fabric_prefix(rating_name: str) -> str:
    """
    Return just the "<kN>/<plies>" portion of a BeltRating.rating_name
    (format: "<fabric_code> <kN>/<plies>", e.g. "EP 1000/5" -> "1000/5"),
    dropping the leading fabric-code token.

    Fabric Type is always its own selected field alongside a belt rating, so
    repeating the fabric code inside the rating text is redundant to show —
    this is purely a display-layer strip; BeltRating.rating_name itself is
    never modified in the DB, and parse_belt_rating() above doesn't need or
    use the prefix either. Mirrored in frontend/js/generate-tds.js and
    frontend/js/search-tds.js (`stripFabricPrefix()`) — keep the three in
    sync if this format ever changes.
    """
    if not rating_name:
        return rating_name
    return _re.sub(r'^\S+\s+', '', rating_name.strip())


def auto_select_fabric_style(fabric_type_id: int, kn: float, plies: int):
    """
    Server-side fabric style selection — the single source of truth for both
    the single-belt form and the bulk batch-import flow.

    Algorithm:
      per_ply = kn / plies
      Among all FabricStyle rows for this fabric_type, extract the FIRST
      numeric value found in style_name (e.g. "EP 200" -> 200, "EP 200/3" -> 200
      — taking only the first number, not concatenating every digit in the
      string). Return the id of the style with the *lowest* numeric value that
      is still >= per_ply (the tightest-fitting match a specifier would pick).
      If all numeric values are below per_ply, return None (no style assigned).

    Example: rating EP 1000/5 -> per_ply=200. Styles [EP 201, EP 250] -> EP 201.
    """
    from apps.core.models import FabricStyle

    if not plies:
        return None
    per_ply = kn / plies
    styles = FabricStyle.objects.filter(fabric_type_id=fabric_type_id)

    candidates = []
    for s in styles:
        m = _re.search(r'(\d+(?:\.\d+)?)', s.style_name)
        if m:
            candidates.append((float(m.group(1)), s.id))

    candidates.sort(key=lambda x: x[0])  # ascending

    for val, style_id in candidates:
        if val >= per_ply:
            return style_id

    return None  # no qualifying style; fabric_style_id stays NULL


# ── Splice Length ─────────────────────────────────────────────────────────────

# Step length lookup table (IS 14206 Part I:1995)
_STEP_LENGTH_TABLE: list = [
    (100,  150),
    (125,  200),
    (160,  200),
    (200,  250),
    (250,  300),
    (300,  350),
    (315,  350),
    (350,  400),
    (400,  400),
]
_STEP_LENGTH_MAX = 400  # mm, fallback for fabric_rating > 400 kN/m/ply


def step_length_mm(belt_rating_kn_m: float, num_plies: int) -> int:
    """
    Determine step length (mm) from IS 14206 lookup table.

    rating_per_ply = belt_rating_kn_m / num_plies
    Then look up the table: first row where rating_per_ply ≤ threshold.
    """
    if num_plies <= 0:
        raise ValueError("num_plies must be > 0")
    rating_per_ply = belt_rating_kn_m / num_plies
    for threshold, step in _STEP_LENGTH_TABLE:
        if rating_per_ply <= threshold:
            return step
    return _STEP_LENGTH_MAX


def get_splice_buffer(method: str) -> int:
    """
    Fetch splice buffer (mm) from splice_method_config table.
    Falls back to hardcoded values (hot=50, cold=75) if the table is absent or empty.
    """
    from apps.core.models import SpliceMethodConfig
    try:
        row = SpliceMethodConfig.objects.filter(
            vulcanization_method=(method or "hot").lower()
        ).first()
        if row is not None:
            return row.buffer_mm
    except Exception:
        pass
    return 50 if (method or "hot").lower() == "hot" else 75


def splice_length_mm(
    belt_width_mm: int,
    belt_rating_kn_m: float,
    num_plies: int,
    splice_type: str = 'hot',
    buffer: int = None,
) -> tuple:
    """
    Calculate splice length and the step length used.

    Formula (IS 14206 Part I:1995):
        splice_length = round(0.3 x W + step_length x (N-1) + buffer)

    Returns:
        (splice_length_mm, step_length_mm)
    """
    if buffer is None:
        buffer = 50 if splice_type == 'hot' else 75
    step = step_length_mm(belt_rating_kn_m, num_plies)
    N = num_plies
    W = belt_width_mm
    length = round_half_up(0.3 * W + step * (N - 1) + buffer)
    return length, step


def total_extra_length_m(
    num_joints: int,
    splice_len_mm: int,
) -> float:
    """Total extra belt length needed for all splices (metres)."""
    return round(num_joints * splice_len_mm / 1000, 3)


# ── IS 1891 Sampling Plan ─────────────────────────────────────────────────────

_SAMPLING_TABLE: list = [
    (500,   1),
    (1000,  2),
    (2000,  3),
    (3500,  4),
    (5000,  5),
    (7000,  6),
    (10000, 7),
]


def is1891_sampling_count(belt_length_m: float) -> int:
    """
    Fallback: number of test samples from hardcoded IS 1891 table.
    Prefer get_sampling_count() when a DB session is available.
    """
    for threshold, count in _SAMPLING_TABLE:
        if belt_length_m <= threshold:
            return count
    return 7


def get_sampling_count(belt_length_m: float) -> int:
    """
    DB-first IS 1891 sampling count from sampling_plan_lookup table.
    Falls back to is1891_sampling_count() if the table is absent or empty.
    """
    from apps.core.models import SamplingPlanLookup
    try:
        row = SamplingPlanLookup.objects.filter(
            max_belt_length_m__gte=belt_length_m
        ).order_by('max_belt_length_m').first()
        if row is not None:
            return row.sample_count
    except Exception:
        pass
    return is1891_sampling_count(belt_length_m)
