"""
apps/services/packing_service.py — Packing & Logistics computation for Domestic TDS.

Responsibilities
────────────────
1. Load reel constants from the DB (reel_types) via Django ORM.
2. Run the reel-diameter formula (circular / twin / elliptical).
3. If D > max_roll_diameter_m, back-calculate L_per_roll and ceil num_rolls.
4. Compute roll dimensions, Total Order Net Weight, and Total Order Gross Weight.
5. Return a PackingResult dataclass.

Weight formulas
───────────────
- Net weight/m   = SG × T × (W/1000)
- Gross weight/m = SG × (T+0.5) × (W/1000)   ← +0.5 only here, NOT in reel d
"""

import math
from dataclasses import dataclass

from apps.services.calculations import (
    reel_diameter, reel_diameter_circular, reel_diameter_elliptical,
)


# ── Result container ─────────────────────────────────────────────────────────

@dataclass
class PackingResult:
    """
    All computed packing values — written directly into tds_inputs columns.
    """
    num_rolls:                int
    length_per_roll_m:        float
    roll_height_m:            float
    roll_width_m:             float
    roll_dimensions:          str
    net_weight_kg:            float
    gross_weight_kg:          float
    gross_weight_per_roll_kg: float


# ── Internal helpers ─────────────────────────────────────────────────────────

def _round_up_half(v: float) -> float:
    """Round up to the nearest 0.5 (e.g. 1.2 → 1.5, 1.21 → 1.5, 1.7 → 2.0)."""
    return math.ceil(v * 2) / 2


def _max_length_circular(max_D: float, k: float, d_m: float) -> float:
    """Max belt length that fits on a circular reel at D = max_D."""
    return ((max_D ** 2 - k ** 2) * math.pi) / (4 * d_m)


def _max_length_elliptical(max_D: float, k: float, l: float, d_m: float) -> float:
    """Max belt length that fits on an elliptical reel at D = max_D."""
    offset  = 2 * l / math.pi
    inner_D = max_D + offset
    inner_k = k + offset
    return ((inner_D ** 2 - inner_k ** 2) * math.pi) / (4 * d_m)


# ── Public API ───────────────────────────────────────────────────────────────

def compute_packing(
    reel_type_id:         int,
    packing_type_id:      int,
    purpose_id:           int,
    total_thickness_mm:   float,
    belt_length_m:        float,
    belt_width_mm:        int,
    belt_weight_per_m_kg: float,
) -> PackingResult:
    """
    Compute all Packing & Logistics values for a TDS record.

    Parameters
    ──────────
    reel_type_id         : FK → reel_types  (1=Circular, 2=Twin, 3=Elliptical)
    packing_type_id      : FK → packing_types (1=Standard, 2=Metal, 3=Palette)
    purpose_id           : FK → purpose (1=Domestic, 2=International)
    total_thickness_mm   : tds_inputs.total_thickness_mm
    belt_length_m        : tds_inputs.belt_length_m
    belt_width_mm        : tds_inputs.belt_width_mm
    belt_weight_per_m_kg : tds_inputs.belt_weight_per_m_kg (already computed, net)

    Returns
    ───────
    PackingResult — write every field into the matching tds_inputs column.
    """
    from apps.core.models import ReelType

    # ── Load reel constants from DB ───────────────────────────────────────────
    reel = ReelType.objects.filter(pk=reel_type_id).first()
    if reel is None:
        raise ValueError(f"reel_type_id {reel_type_id} not found in reel_types")

    k          = float(reel.core_diameter_m)
    l          = float(reel.center_to_center_m) if reel.center_to_center_m else 0.0
    max_D      = float(reel.max_roll_diameter_m)
    base_rolls = reel.num_rolls_base

    # Guard the two values every downstream formula divides by. Without this,
    # a zero here (bad input data, or a misconfigured ReelType row) surfaces
    # as a raw ZeroDivisionError deep inside the math below instead of a
    # clear, attributable error message.
    if not total_thickness_mm or total_thickness_mm <= 0:
        raise ValueError(
            f"total_thickness_mm must be greater than 0 to compute packing (got {total_thickness_mm!r})"
        )
    if not base_rolls or base_rolls <= 0:
        raise ValueError(
            f"ReelType id={reel_type_id} has an invalid num_rolls_base ({base_rolls!r}); "
            "it must be a positive integer."
        )
    if not belt_length_m or belt_length_m <= 0:
        raise ValueError(
            f"belt_length_m must be greater than 0 to compute packing (got {belt_length_m!r})"
        )

    d_m = total_thickness_mm / 1000

    # ── Calculate reel outer diameter D ──────────────────────────────────────
    D = reel_diameter(
        formula_key=reel.formula_key,
        total_thickness_mm=total_thickness_mm,
        belt_length_m=belt_length_m,
        k_m=k,
        center_to_center_m=l,
    )

    # ── If D > max_roll_diameter_m → back-calculate L_per_roll and num_rolls ─
    if D > max_D:
        if reel.formula_key == "circular":
            L_max      = _max_length_circular(max_D, k, d_m)
            num_rolls  = math.ceil(belt_length_m / L_max)
            L_per_roll = belt_length_m / num_rolls

        elif reel.formula_key == "twin":
            L_per_single = _max_length_circular(max_D, k, d_m)
            num_pairs    = math.ceil(belt_length_m / (2 * L_per_single))
            num_rolls    = num_pairs * 2
            L_per_roll   = belt_length_m / num_rolls

        elif reel.formula_key == "elliptical":
            L_max      = _max_length_elliptical(max_D, k, l, d_m)
            num_rolls  = math.ceil(belt_length_m / L_max)
            L_per_roll = belt_length_m / num_rolls

        else:
            raise ValueError(f"Unknown formula_key: {reel.formula_key!r}")

        D = max_D  # cap for dimension display

    else:
        num_rolls  = base_rolls
        L_per_roll = belt_length_m / base_rolls   # base_rolls already guarded > 0 above

    # ── Roll dimensions (per individual roll) ─────────────────────────────────
    roll_height_m   = _round_up_half(D)
    roll_width_m    = _round_up_half((belt_width_mm + 100) / 1000)
    roll_dimensions = f"H: {roll_height_m:.2f} m × W: {roll_width_m:.2f} m"

    # ── Total Order Net Weight ────────────────────────────────────────────────
    # Kept as a precise decimal (not rounded up to the nearest 0.5) so this
    # figure reconciles exactly with Belt Specs' per-metre weight × length -
    # _round_up_half is for physical roll dimensions (reel height/width/length
    # per roll), not for weight, which should read as an exact computed value.
    net_weight_kg = round(belt_weight_per_m_kg * belt_length_m, 2)

    # ── Total Order Gross Weight ──────────────────────────────────────────────
    if total_thickness_mm > 0:
        gross_per_m = belt_weight_per_m_kg * (total_thickness_mm + 0.5) / total_thickness_mm
    else:
        gross_per_m = belt_weight_per_m_kg
    gross_weight_kg = round(gross_per_m * belt_length_m, 2)

    # ── Gross Weight per Roll ─────────────────────────────────────────────────
    gross_weight_per_roll_kg = (
        round(gross_weight_kg / num_rolls, 2) if num_rolls > 0 else None
    )

    return PackingResult(
        num_rolls                = num_rolls,
        length_per_roll_m        = _round_up_half(L_per_roll),
        roll_height_m            = roll_height_m,
        roll_width_m             = roll_width_m,
        roll_dimensions          = roll_dimensions,
        net_weight_kg            = net_weight_kg,
        gross_weight_kg          = gross_weight_kg,
        gross_weight_per_roll_kg = gross_weight_per_roll_kg,
    )


# ── Manual override: unequal roll lengths ───────────────────────────────────

@dataclass(frozen=True)
class RollLengthsValidation:
    """Result of validate_custom_roll_lengths() — display dimensions sized
    off the largest individual roll (conservative, matches how a single
    uniform roll_dimensions string already covers every roll today)."""
    roll_height_m:   float
    roll_width_m:    float
    roll_dimensions: str


def validate_custom_roll_lengths(
    reel_type_id:       int,
    total_thickness_mm: float,
    belt_length_m:       float,
    belt_width_mm:       int,
    roll_lengths_m:      list,
) -> RollLengthsValidation:
    """
    Validate a manually-specified list of UNEQUAL roll lengths for a belt
    (e.g. [200, 100] instead of the auto-computed equal [150, 150]).

    Every entry must still respect the reel's max_roll_diameter_m, and the
    lengths must sum to the belt's total length. Raises ValueError on any
    failure (same convention as compute_packing() above, so callers can
    catch-and-wrap identically).

    Per-roll diameter formula dispatch by reel.formula_key:
      - 'circular'   -> reel_diameter_circular  (length wound on that one roll)
      - 'elliptical' -> reel_diameter_elliptical (length wound on that one roll)
      - 'twin'       -> reel_diameter_circular, NOT reel_diameter_twin.
        compute_packing()'s own twin back-calc (above) sizes each individual
        drum with _max_length_circular (circular formula's inverse) rather
        than the twin formula — once you're looking at one drum's actual
        wound length, that drum behaves as an independent circular spool.
        reel_diameter_twin (which halves L internally) is only correct for
        checking whether the WHOLE belt fits split evenly across the base 2
        drums, not for validating an already-per-drum length. Twin rolls are
        also physically issued in pairs, so an even count is enforced too.
    """
    from apps.core.models import ReelType

    reel = ReelType.objects.filter(pk=reel_type_id).first()
    if reel is None:
        raise ValueError(f"reel_type_id {reel_type_id} not found in reel_types")

    if not roll_lengths_m:
        raise ValueError("roll_lengths_m must be a non-empty list.")

    try:
        lengths = [float(x) for x in roll_lengths_m]
    except (TypeError, ValueError):
        raise ValueError("roll_lengths_m must contain only numbers.")

    if any(length <= 0 for length in lengths):
        raise ValueError("Every roll length must be greater than 0.")

    if not total_thickness_mm or total_thickness_mm <= 0:
        raise ValueError(
            f"total_thickness_mm must be greater than 0 to validate roll lengths (got {total_thickness_mm!r})"
        )

    if abs(sum(lengths) - float(belt_length_m)) > 0.01:
        raise ValueError(
            f"Roll lengths sum to {sum(lengths):.2f} m, which must equal the "
            f"belt length of {float(belt_length_m):.2f} m."
        )

    if reel.formula_key == "twin" and len(lengths) % 2 != 0:
        raise ValueError("Twin reels require an even number of rolls.")

    d_m   = total_thickness_mm / 1000
    k     = float(reel.core_diameter_m)
    l     = float(reel.center_to_center_m) if reel.center_to_center_m else 0.0
    max_D = float(reel.max_roll_diameter_m)

    for length in lengths:
        if reel.formula_key == "elliptical":
            D = reel_diameter_elliptical(d_m, length, k, l)
        else:  # 'circular' and 'twin' both validate per-roll with the circular formula
            D = reel_diameter_circular(d_m, length, k)
        if D > max_D:
            raise ValueError(
                f"A roll of {length:.2f} m would produce a diameter of {D:.2f} m, "
                f"exceeding this reel's maximum of {max_D:.2f} m."
            )

    largest = max(lengths)
    if reel.formula_key == "elliptical":
        D_largest = reel_diameter_elliptical(d_m, largest, k, l)
    else:
        D_largest = reel_diameter_circular(d_m, largest, k)

    roll_height_m = _round_up_half(min(D_largest, max_D))
    roll_width_m  = _round_up_half((belt_width_mm + 100) / 1000)
    roll_dimensions = f"H: {roll_height_m:.2f} m × W: {roll_width_m:.2f} m"

    return RollLengthsValidation(
        roll_height_m   = roll_height_m,
        roll_width_m    = roll_width_m,
        roll_dimensions = roll_dimensions,
    )
