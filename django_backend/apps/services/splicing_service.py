"""
apps/services/splicing_service.py — DB-backed splice length calculator.

Formula (IS 14206 Part I:1995):
    fabric_rating  = belt_rating_kn_m / num_plies
    step_length    = lookup from splice_step_lookup table (fallback: calculations.py)
    splice_length  = round(0.3 × belt_width_mm + step_length × (num_plies − 1) + buffer)
    buffer         = from splice_method_config table (hot=50mm | cold=75mm); hardcoded fallback
    total_extra_m  = splice_length × num_joints / 1000
"""

from dataclasses import dataclass

from apps.services.calculations import step_length_mm as _fallback_step, total_extra_length_m


@dataclass(frozen=True)
class SplicingResult:
    step_length_mm: int
    splice_length_mm: int
    total_extra_length_m: float


def _get_buffer_from_db(method: str) -> int:
    """
    Fetch splice buffer (mm) from splice_method_config.
    Falls back to hardcoded values (hot=50, cold=75) if the table is absent.
    """
    from apps.core.models import SpliceMethodConfig
    try:
        row = SpliceMethodConfig.objects.filter(
            vulcanization_method=method.lower()
        ).first()
        if row is not None:
            return row.buffer_mm
    except Exception:
        pass
    return 50 if method == "hot" else 75


def _get_step_from_db(fabric_rating: float):
    """
    Query splice_step_lookup for the first row where
    max_fabric_rating_kn_m >= fabric_rating (ordered ASC).

    Returns None if the table is empty or not yet seeded
    (caller falls back to the hardcoded table in calculations.py).
    """
    from apps.core.models import SpliceStepLookup
    try:
        row = SpliceStepLookup.objects.filter(
            max_fabric_rating_kn_m__gte=fabric_rating
        ).order_by('max_fabric_rating_kn_m').first()
        if row is not None:
            return row.step_length_mm
        # fabric_rating > max row → use the highest step in the table
        last_row = SpliceStepLookup.objects.order_by(
            '-max_fabric_rating_kn_m'
        ).first()
        return last_row.step_length_mm if last_row else None
    except Exception:
        return None  # table missing or not seeded → caller uses hardcoded fallback


def compute_splicing(
    belt_rating_kn_m: float,
    num_plies: int,
    belt_width_mm: int,
    num_joints: int,
    vulcanization_method: str,
) -> SplicingResult:
    """
    Compute all splicing fields for a TDS record.

    Args:
        belt_rating_kn_m      : Full belt rating in kN/m (e.g. 800 for EP 800/4).
        num_plies             : Number of plies (e.g. 4).
        belt_width_mm         : Belt width in mm (e.g. 1000).
        num_joints            : Number of splice joints (e.g. 2 for endless).
        vulcanization_method  : 'hot' or 'cold' (case-insensitive).

    Returns:
        SplicingResult with step_length_mm, splice_length_mm, total_extra_length_m.
    """
    if num_plies <= 0:
        raise ValueError("num_plies must be > 0")

    fabric_rating = belt_rating_kn_m / num_plies
    method = vulcanization_method.lower() if vulcanization_method else "hot"
    buffer = _get_buffer_from_db(method)

    # Try DB lookup; fall back to hardcoded table in calculations.py
    step = _get_step_from_db(fabric_rating)
    if step is None:
        step = _fallback_step(belt_rating_kn_m, num_plies)

    splice = round(0.3 * belt_width_mm + step * (num_plies - 1) + buffer)
    extra = total_extra_length_m(num_joints, splice)

    return SplicingResult(
        step_length_mm=step,
        splice_length_mm=splice,
        total_extra_length_m=extra,
    )
