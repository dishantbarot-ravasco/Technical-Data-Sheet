"""
apps/services/pdf_service.py — Database assembly layer for TDS PDF generation.

Django port of FastAPI services/pdf_service.py.
Key changes from FastAPI version:
  - `db: Session` parameter removed — Django ORM accessed directly.
  - SQLAlchemy joinedload()   → select_related()
  - db.query(Model).filter()  → Model.objects.filter()
  - FK field names updated:
      tds.reel_type_rel        → tds.reel_type
      tds.packing_type_rel     → tds.packing_type
      tds.container_type_rel   → tds.container_type
      tds.construction_type_rel→ tds.construction_type_fk
      tds.created_by_user      → tds.created_by
  - BrandParameter+TDSParameter join: BrandParameter.objects.select_related('parameter')

Pipeline:
  pdf_views.py → build_tds_doc_data(tds_id) → TDSDocData → pdf_renderer.py → PDF bytes

Reading guide — top to bottom, this file is organized as:
  1. Data classes (ParameterRow, ParameterGroup, GIRow, TDSDocData) — the
     plain-data shape the Jinja2/WeasyPrint renderer (pdf_renderer.py) consumes.
     None of these have behaviour; they only carry already-resolved strings.
  2. _breaker_top / _breaker_bottom — tiny per-field formatters used by _DIRECT_MAP.
  3. _DIRECT_MAP — parameter_name → lambda(tds) for values that live directly
     on the TDSInput row (as opposed to the EAV tables below).
  4. TDS_NOTES — static footer text, unrelated to any one TDS.
  5. build_tds_doc_data() — the single entry point. Internally it:
       a. loads the TDSInput row with select_related() for every FK it needs
          (Django's answer to SQLAlchemy's joinedload()),
       b. builds an in-memory EAV lookup (parameter_id → {spec, indus}) by
          layering CoverGradeValue, then FabricTypeParameterValue, then
          FabricStyleParameterValue (later layers override earlier ones for
          the same parameter_id — see step b below for the exact order),
       c. builds the dimensional-tolerance lookup and the standard/test-method
          lookup for the TDS's selected Standard,
       d. loads this Brand's parameter list (BrandParameter, ordered by
          display_order) and groups it by parameter_group,
       e. assembles the General Information header rows via _GI_RESOLVER,
       f. resolves the Hot Splicing curing lookup (only if splicing_required
          and vulcanization_method == "hot"),
       g. walks PARAMETER_GROUP_ORDER (apps/services/sections.py) building one
          ParameterGroup per section, resolving each row's value with this
          precedence: EAV lookup → hot-splice curing resolver → _DIRECT_MAP →
          "—" (dash, meaning "not available"),
       h. applies two group-specific post-processing rules (Belt Construction
          Parameters' spec column is forced to "Not Specified"; Belt Breaking
          Strength computes "Weft % as per Warp" and rounds Elastic Modulus).

This is a straight data-assembly function, not a class, for the same reason
as batch_views.py's helpers: it has one job (TDSInput row → TDSDocData), no
per-call state to encapsulate, and every step depends on the previous step's
local variables — extracting it into a class would just turn locals into
self.attributes without changing behaviour or improving readability.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

from django.db.models import F

from apps.core.models import (
    BeltRatingValue,
    BrandParameter,
    CoverGradeValue,
    DimensionalParameterSpec,
    FabricStyleParameterValue,
    FabricTypeParameterValue,
    HotSpliceCuringLookup,
    StandardTestMethod,
    TDSInput,
    TDSParameter,
)
from apps.services.sections import PARAMETER_GROUP_ORDER


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParameterRow:
    parameter_id: int
    name: str
    section: Optional[str]
    test_method: Optional[str]
    reference: Optional[str]
    spec_value: Optional[str]
    indus_value: Optional[str]


@dataclass
class ParameterGroup:
    name: str
    rows: list[ParameterRow] = field(default_factory=list)
    has_test_columns: bool = False  # True if any row in the group has STM data


@dataclass
class GIRow:
    """One row in the General Information header table (DB-driven)."""
    label: str
    value: str
    bold: bool = False  # True for TDS Number and Brand Name rows


@dataclass
class TDSDocData:
    # ── General Information ────────────────────────────────────────────────
    tds_number: str
    tds_date: str
    customer_name: str
    contact_person: Optional[str]
    application: Optional[str]
    plant_location: Optional[str]
    brand_name: str
    standard_name: str
    belt_description: Optional[str]
    belt_length_m: str
    num_rolls: Optional[int]
    length_per_roll_m: Optional[str]
    belt_weight_per_m: Optional[str]
    construction_type: str
    status: str
    # ── General Information rows (DB-driven via tds_parameters GI group) ──
    gi_rows: list[GIRow] = field(default_factory=list)
    # ── Parameter Groups ───────────────────────────────────────────────────
    groups: list[ParameterGroup] = field(default_factory=list)
    # ── Footer ────────────────────────────────────────────────────────────
    prepared_by_name: str = "—"
    prepared_by_designation: str = ""
    # Set only when this doc was built from a past TDSRevision snapshot
    # (see `overrides` on build_tds_doc_data) — shown as a header notice.
    revision_banner: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Direct-field map: parameter_name → lambda(tds) → indus_value string
# ─────────────────────────────────────────────────────────────────────────────

def _breaker_top(t: TDSInput) -> str:
    if t.breaker_top:
        plies = f", {t.breaker_top_plies} Ply" if t.breaker_top_plies else ""
        return f"Yes{plies}"
    return "No"


def _breaker_bottom(t: TDSInput) -> str:
    if t.breaker_bottom:
        plies = f", {t.breaker_bottom_plies} Ply" if t.breaker_bottom_plies else ""
        return f"Yes{plies}"
    return "No"


# ─────────────────────────────────────────────────────────────────────────────
# Reference / test-method sanitiser
# ─────────────────────────────────────────────────────────────────────────────

def _clean_ref(value: str | None) -> str | None:
    """
    Strip verbose ARPM manual citations that overflow PDF table cells.
    "ARPM MANUAL FOR OPERATIONAL, SAFETY AND MAINTENANCE RECOMMENDATIONS, FIG. R"
    → None (cell shows —)
    Any other long string is returned as-is.
    """
    if not value:
        return value
    if re.search(r'manual\s+for\s+operational', value, re.IGNORECASE):
        return None          # renders as — in the template
    if re.search(r'\bFIG\.?\s*R\b', value, re.IGNORECASE):
        return None
    return value


# NOTE: Django FK accessors use the field name defined in models.py:
#   reel_type (not reel_type_rel), packing_type, container_type,
#   construction_type_fk (FK; construction_type is the TextField)
_DIRECT_MAP: dict[str, callable] = {
    # Dimensional Parameters
    "Belt Width (mm)":                       lambda t: str(t.belt_width_mm),
    "Top Cover Thickness (mm)":              lambda t: str(t.top_cover_mm),
    "Bottom Cover Thickness (mm)":           lambda t: str(t.bottom_cover_mm),
    "Carcass Thickness (mm)":                lambda t: str(t.carcass_thickness_mm),
    "Interply Skim Thickness (mm)":          lambda t: str(t.interply_skim_mm) if t.interply_skim_mm is not None else "—",
    "Total Belt Thickness (mm)":             lambda t: str(t.total_thickness_mm),
    # Belt Construction Parameters
    "Fabric Type":                           lambda t: t.fabric_type.fabric_code if t.fabric_type else "—",
    "Make of Fabric":                        lambda t: t.make_of_fabric,
    "Number of Plies":                       lambda t: str(t.num_plies),
    "Breaker on Top | Number of Plies":      _breaker_top,
    "Breaker on Bottom | Number of Plies":   _breaker_bottom,
    "Edge Construction":                     lambda t: t.edge_construction,
    # Fabric Parameters
    "Fabric Style":                          lambda t: t.fabric_style.style_name if t.fabric_style else "—",
    # Packing and Logistics (Django field names — no _rel suffix)
    "Reel Type":                             lambda t: t.reel_type.reel_name if t.reel_type else "—",
    "Packing Type":                          lambda t: t.packing_type.packing_name if t.packing_type else "—",
    "Number of Rolls":                       lambda t: str(t.num_rolls) if t.num_rolls is not None else "—",
    "Rolls Dimensions (H X W)":              lambda t: t.roll_dimensions if t.roll_dimensions else "—",
    "Total Order Net Weight (kg)":           lambda t: str(t.net_weight_kg) if t.net_weight_kg is not None else "—",
    "Total Order Gross Weight (kg)":         lambda t: str(t.gross_weight_kg) if t.gross_weight_kg is not None else "—",
    "Gross Weight per Roll (kg)":            lambda t: str(t.gross_weight_per_roll_kg) if t.gross_weight_per_roll_kg is not None else "—",
    # International logistics
    "Shipping Region":                       lambda t: t.shipping_region or "—",
    "Container Type":                        lambda t: t.container_type.name if t.container_type else "—",
    # Splicing Parameters
    "Splicing Method":                       lambda t: (t.vulcanization_method or "—").capitalize(),
    "Number of Splice Joints":               lambda t: str(t.num_joints) if t.num_joints is not None else "—",
    "Step Length (mm)":                      lambda t: str(t.step_length_mm) if t.step_length_mm is not None else "—",
    "Splice Length (mm)":                    lambda t: f"{float(t.splice_length_mm):.2f}" if t.splice_length_mm is not None else "—",
    "Total Extra Belt Length for Splicing (m)": lambda t: f"{float(t.total_extra_length_m):.2f}" if t.total_extra_length_m is not None else "—",
    "Total Belt Length with Splicing (m)":   lambda t: (
        f"{float(t.belt_length_m) + float(t.total_extra_length_m):.2f}"
        if t.total_extra_length_m is not None else "—"
    ),
    # Hot-splice curing — values injected by _curing_resolver; lambdas are safe fallback
    "Specific Pressure (Hot Splicing)":      lambda t: "—",
    "Curing Temperature (Hot Splicing)":     lambda t: "—",
    "Curing Time (Hot Splicing)":            lambda t: "—",
}


# ─────────────────────────────────────────────────────────────────────────────
# Notes that appear at the bottom of every TDS
# ─────────────────────────────────────────────────────────────────────────────

TDS_NOTES: list[str] = [
    (
        "We use Silicon Paper as press release agent. Sometimes the impressions of silicon papers "
        "on the belt surface creates paper marks on the belt surface. These are superficial marks "
        "and not cracks. They do not affect the belt life or performance. The paper marks are only "
        "a cosmetic issue and shall not be accepted as a cause of rejection."
    ),
    (
        "Please send stamped and signed copy of this datasheet along with the purchase order as a "
        "token of acceptance of all technical parameters mentioned in the datasheet."
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Historical-revision overlay
# ─────────────────────────────────────────────────────────────────────────────

# FK id-column → relation-accessor name, for every relation build_tds_doc_data
# reads off `tds.<relation>` rather than the raw `tds.<relation>_id` column.
# When a revision snapshot overrides one of these id columns, the cached
# related object select_related() already fetched (for the CURRENT id) must
# be dropped too, or `tds.<relation>` below would keep returning the current
# record's related row instead of re-querying for the snapshotted id.
_OVERRIDE_RELATION_MAP: dict[str, str] = {
    'customer_id':        'customer',
    'brand_id':            'brand',
    'standard_id':          'standard',
    'cover_grade_id':        'cover_grade',
    'fabric_type_id':         'fabric_type',
    'fabric_style_id':         'fabric_style',
    'belt_rating_id':           'belt_rating',
    'reel_type_id':               'reel_type',
    'packing_type_id':             'packing_type',
    'container_type_id':            'container_type',
}


def _apply_revision_overrides(tds: TDSInput, overrides: dict) -> None:
    """
    Overlay a TDSRevision.snapshot dict onto an in-memory (unsaved) TDSInput
    instance, so the rest of build_tds_doc_data resolves every direct field
    AND every EAV/spec lookup (which key off the FK id columns, e.g.
    cover_grade_id) against the values that were live at that revision,
    without ever writing back to the database.
    """
    for field_name, value in overrides.items():
        if not hasattr(tds, field_name):
            continue
        setattr(tds, field_name, value)
        rel_name = _OVERRIDE_RELATION_MAP.get(field_name)
        if rel_name:
            tds._state.fields_cache.pop(rel_name, None)


# ─────────────────────────────────────────────────────────────────────────────
# Main assembly function
# ─────────────────────────────────────────────────────────────────────────────

def build_tds_doc_data(
    tds_id: int,
    exclude_groups: list[str] | None = None,
    exclude_params: list[int] | None = None,
    show_section: bool = True,
    show_test_method: bool = True,
    show_reference: bool = True,
    overrides: dict | None = None,
    revision_banner: str | None = None,
) -> TDSDocData:
    """
    Assembles all data for TDS {tds_id} from the database.

    Args:
        tds_id:          Primary key of the TDSInput record.
        exclude_groups:  Group names to omit entirely.
        exclude_params:  parameter_id values to omit.
        show_section:    Include Section column in output rows.
        show_test_method: Include Test Method column in output rows.
        show_reference:  Include Reference column in output rows.
        overrides:       Optional field->value dict (typically a
                         TDSRevision.snapshot) overlaid onto the live record
                         before assembly, to render a past revision instead
                         of the current state. See _apply_revision_overrides.
        revision_banner: Optional header notice text, set on the returned
                         doc when `overrides` is used.

    Returns:
        TDSDocData ready for the renderer.

    Raises:
        ValueError: if TDS not found.
    """
    exclude_groups = set(exclude_groups or [])
    exclude_params = set(exclude_params or [])

    # ── Load TDS with all FK relationships ────────────────────────────────────
    # Django select_related() replaces SQLAlchemy joinedload() — single SQL JOIN query.
    # Field names follow the Django model (no _rel suffix; construction_type_fk for FK).
    tds: TDSInput | None = (
        TDSInput.objects
        .select_related(
            'customer', 'brand', 'standard',
            'cover_grade', 'fabric_type', 'fabric_style',
            'belt_rating', 'reel_type', 'packing_type',
            'container_type', 'construction_type_fk', 'created_by',
        )
        .filter(pk=tds_id)
        .first()
    )
    if tds is None:
        raise ValueError(f"TDS {tds_id} not found")

    if overrides:
        _apply_revision_overrides(tds, overrides)

    # ── Build EAV lookup: parameter_id → {spec, indus} ────────────────────────
    eav: dict[int, dict[str, str | None]] = {}

    # 1. Cover grade values
    for v in CoverGradeValue.objects.filter(cover_grade_id=tds.cover_grade_id):
        eav[v.parameter_id] = {"spec": v.spec_value, "indus": v.indus_value}

    # 2. Fabric type values (only if not already set by cover grade)
    for v in FabricTypeParameterValue.objects.filter(fabric_type_id=tds.fabric_type_id):
        if v.parameter_id not in eav:
            eav[v.parameter_id] = {"spec": v.spec_value, "indus": v.indus_value}

    # 3. Fabric style values (override fabric-type values for style-specific specs)
    if tds.fabric_style_id:
        for v in FabricStyleParameterValue.objects.filter(fabric_style_id=tds.fabric_style_id):
            eav[v.parameter_id] = {"spec": v.spec_value, "indus": v.indus_value}

    # param_id 4 (Carcass Thickness) skipped from EAV — _DIRECT_MAP uses tds_inputs column
    _RATING_EAV_SKIP = frozenset({4})

    # 4. Belt rating values
    for v in BeltRatingValue.objects.filter(belt_rating_id=tds.belt_rating_id):
        if v.parameter_id in _RATING_EAV_SKIP:
            continue
        eav[v.parameter_id] = {"spec": v.spec_value, "indus": v.indus_value}

    # ── Dimensional parameter spec lookup ─────────────────────────────────────
    _param_values: dict[int, float] = {
        1: float(tds.belt_width_mm),
        2: float(tds.top_cover_mm),
        3: float(tds.bottom_cover_mm),
        4: float(tds.carcass_thickness_mm),
        6: float(tds.total_thickness_mm),
    }
    _all_dim_rows = (
        DimensionalParameterSpec.objects
        .filter(standard_id=tds.standard_id)
        .order_by('parameter_id', F('min_value').asc(nulls_first=True))
    )
    dim_spec_map: dict[int, str] = {}
    for row in _all_dim_rows:
        if row.parameter_id in dim_spec_map:
            continue
        val = _param_values.get(row.parameter_id)
        if val is None:
            continue
        lo_ok = row.min_value is None or row.min_value <= val
        hi_ok = row.max_value is None or row.max_value >= val
        if lo_ok and hi_ok:
            dim_spec_map[row.parameter_id] = row.tolerance_value

    # ── Standard test method lookup ───────────────────────────────────────────
    stm_map: dict[int, StandardTestMethod] = {
        stm.parameter_id: stm
        for stm in StandardTestMethod.objects.filter(standard_id=tds.standard_id)
    }

    # ── Load & group all parameters for this brand ────────────────────────────
    # BrandParameter.objects.select_related('parameter') replaces the SQLAlchemy
    # db.query(TDSParameter, BrandParameter).join(...) — one SQL query with JOIN.
    brand_params = (
        BrandParameter.objects
        .filter(brand_id=tds.brand_id)
        .select_related('parameter')
        .order_by('display_order')
    )
    params = [
        SimpleNamespace(
            parameter_id=bp.parameter.parameter_id,
            parameter_group=bp.parameter.parameter_group,
            parameter_name=bp.parameter.parameter_name,
            display_order=bp.display_order,
            is_user_input=bp.is_user_input,
            visibility_condition=bp.visibility_condition,
            spec_equals_indus=bp.spec_equals_indus,
        )
        for bp in brand_params
    ]

    grouped: dict[str, list] = defaultdict(list)
    for p in params:
        grouped[p.parameter_group].append(p)

    # ── Build General Information header ──────────────────────────────────────
    std = tds.standard
    std_label = std.standard_name
    if std.standard_edition:
        std_label += f" : {std.standard_edition}"

    belt_len_f = float(tds.belt_length_m)
    is_endless = (tds.construction_type or "").strip().lower() == "endless"
    # Django field: construction_type_fk (FK to ConstructionType); construction_type is TextField
    _qty_label = (
        tds.construction_type_fk.qty_label
        if tds.construction_type_fk
        else ("Nos" if is_endless else "Rolls")
    )
    if is_endless:
        _qty = tds.num_rolls or 1
        belt_len_display = f"{belt_len_f:.2f} X {_qty} {_qty_label}"
    elif tds.roll_lengths_m and len(set(round(float(x), 2) for x in tds.roll_lengths_m)) > 1:
        # Manual override with UNEQUAL roll lengths (e.g. 200m + 100m instead
        # of an even split) — the frontend only sends this field when the
        # lengths genuinely differ, but the >1-distinct-value guard here is
        # defensive in case a record was ever saved another way.
        parts = " + ".join(f"{float(x):.2f}" for x in tds.roll_lengths_m)
        belt_len_display = f"{parts} m ({len(tds.roll_lengths_m)} {_qty_label})"
    elif tds.num_rolls and tds.length_per_roll_m:
        belt_len_display = f"{float(tds.length_per_roll_m):.2f} X {tds.num_rolls} {_qty_label}"
    else:
        belt_len_display = f"{belt_len_f:.2f} m"

    _GI_BOLD = frozenset({"TDS Number", "Indus Brand Name", "Customer Name"})
    _GI_RESOLVER: dict[str, object] = {
        # tds_doc_number is an optional full reference (e.g. "TDS-2024-0007").
        # If not set, fall back to the sequential tds_number (e.g. "0007").
        "TDS Number":                                   lambda t: t.tds_doc_number or t.tds_number or "—",
        "Date":                                         lambda t: t.tds_date.strftime("%d %b %Y"),
        "Customer Name":                                lambda t: t.customer.customer_name if t.customer else "—",
        "Contact Person":                               lambda t: (t.customer.contact_person or "—") if t.customer else "—",
        "Application":                                  lambda t: (t.customer.application or "—") if t.customer else "—",
        "Plant / Factory Location":                     lambda t: (t.customer.plant_location or "—") if t.customer else "—",
        "Indus Brand Name":                             lambda t: t.brand.brand_name,
        "Applicable Standard":                          lambda t: std_label,
        "Belt End Type":                                lambda t: t.construction_type,
        "Belt Description":                             lambda t: t.belt_description or "—",
        "Total Belt Length (with Roll Length Breakup)": lambda t: belt_len_display,
        "Number of Rolls":                              lambda t: str(t.num_rolls) if t.num_rolls is not None else "—",
        "Belt Weight per Meter (kg/m)":                 lambda t: f"{float(t.belt_weight_per_m_kg):.2f} kg/m" if t.belt_weight_per_m_kg is not None else "—",
    }

    # GI parameters — filtered by brand and group, ordered by brand-level display_order
    gi_bps = (
        BrandParameter.objects
        .filter(brand_id=tds.brand_id, parameter__parameter_group="General Information")
        .select_related('parameter')
        .order_by('display_order')
    )
    gi_rows: list[GIRow] = []
    for _bp in gi_bps:
        _gi_p = _bp.parameter
        if _gi_p.parameter_id in exclude_params:
            continue
        _resolver = _GI_RESOLVER.get(_gi_p.parameter_name)
        if _resolver is None:
            continue
        try:
            _val = _resolver(tds)
        except Exception:
            _val = "—"
        gi_rows.append(GIRow(
            label=_gi_p.parameter_name,
            value=_val,
            bold=_gi_p.parameter_name in _GI_BOLD,
        ))

    doc = TDSDocData(
        tds_number=tds.tds_number,
        tds_date=tds.tds_date.strftime("%d %b %Y"),
        customer_name=tds.customer.customer_name if tds.customer else "—",
        contact_person=tds.customer.contact_person if tds.customer else None,
        application=tds.customer.application if tds.customer else None,
        plant_location=tds.customer.plant_location if tds.customer else None,
        brand_name=tds.brand.brand_name,
        standard_name=std_label,
        belt_description=tds.belt_description,
        belt_length_m=belt_len_display,
        num_rolls=tds.num_rolls,
        length_per_roll_m=str(tds.length_per_roll_m) if tds.length_per_roll_m else None,
        belt_weight_per_m=f"{float(tds.belt_weight_per_m_kg):.2f} kg/m" if tds.belt_weight_per_m_kg else None,
        construction_type=tds.construction_type,
        status=tds.status,
        gi_rows=gi_rows,
        revision_banner=revision_banner,
    )

    # ── Footer: populate from the user who created this TDS ──────────────────
    # Django FK field: created_by (not created_by_user)
    creator = tds.created_by
    if creator:
        doc.prepared_by_name        = creator.full_name or "—"
        doc.prepared_by_designation = creator.designation or ""

    # ── Hot-splice curing lookup ──────────────────────────────────────────────
    hot_curing_row = None
    if tds.splicing_required and (tds.vulcanization_method or "").lower() == "hot" and tds.total_thickness_mm:
        thickness = float(tds.total_thickness_mm)
        hot_curing_row = (
            HotSpliceCuringLookup.objects
            .filter(total_belt_thickness_mm__gte=thickness)
            .order_by('total_belt_thickness_mm')
            .first()
        )
        if hot_curing_row is None:
            # Thickness exceeds all rows → use the highest tier
            hot_curing_row = (
                HotSpliceCuringLookup.objects
                .order_by('-total_belt_thickness_mm')
                .first()
            )

    _curing_resolver: dict[str, str] = {}
    if hot_curing_row is not None:
        _curing_time_str = (
            f"{hot_curing_row.curing_time_min} min"
            f" | Cool to ≤{hot_curing_row.cooling_temp_c}°C before removing pressure"
        )
        _curing_resolver = {
            "Specific Pressure (Hot Splicing)":  str(hot_curing_row.specific_pressure),
            "Curing Temperature (Hot Splicing)": str(hot_curing_row.curing_temp),
            "Curing Time (Hot Splicing)":        _curing_time_str,
        }

    # ── Build parameter groups ────────────────────────────────────────────────
    for group_name in PARAMETER_GROUP_ORDER:
        if group_name == "General Information":
            continue
        if group_name in exclude_groups:
            continue
        if group_name not in grouped:
            continue
        if group_name == "Splicing Parameters" and not tds.splicing_required:
            continue

        group = ParameterGroup(name=group_name)

        for param in grouped[group_name]:
            if param.parameter_id in exclude_params:
                continue

            vc = param.visibility_condition
            if vc == "international_only" and not tds.shipping_region:
                continue
            if vc == "hot_splice_only" and (tds.vulcanization_method or "").lower() != "hot":
                continue

            stm = stm_map.get(param.parameter_id)
            section     = stm.section                                  if (stm and show_section)      else None
            test_method = _clean_ref(stm.test_method if stm else None) if show_test_method            else None
            reference   = _clean_ref(stm.reference   if stm else None) if show_reference              else None

            # Value resolution: EAV → _curing_resolver → _DIRECT_MAP → dash
            if param.parameter_id in eav:
                spec_val  = eav[param.parameter_id]["spec"]
                indus_val = eav[param.parameter_id]["indus"]
            elif param.parameter_name in _curing_resolver:
                indus_val = _curing_resolver[param.parameter_name]
                spec_val  = None
            elif param.parameter_name in _DIRECT_MAP:
                try:
                    indus_val = _DIRECT_MAP[param.parameter_name](tds)
                except Exception:
                    indus_val = "—"
                if param.parameter_id in dim_spec_map:
                    spec_val = dim_spec_map[param.parameter_id]
                elif param.spec_equals_indus:
                    spec_val = indus_val
                else:
                    spec_val = None
            else:
                spec_val  = None
                indus_val = "—"

            if stm and any([section, test_method, reference]):
                group.has_test_columns = True

            group.rows.append(ParameterRow(
                parameter_id=param.parameter_id,
                name=param.parameter_name,
                section=section,
                test_method=test_method,
                reference=reference,
                spec_value=spec_val or "—",
                indus_value=indus_val or "—",
            ))

        # Belt Construction Parameters: spec = "Not Specified"
        if group_name == "Belt Construction Parameters":
            for row in group.rows:
                row.spec_value = "Not Specified"

        # Belt Breaking Strength: compute Weft % and round Elastic Modulus
        if group_name == "Belt Breaking Strength":
            _warp_indus = eav.get(37, {}).get("indus")
            _weft_indus = eav.get(38, {}).get("indus")
            for row in group.rows:
                if row.name == "Weft % as per Warp":
                    try:
                        _pct = (float(_weft_indus) / float(_warp_indus)) * 100
                        row.indus_value = f"{_pct:.1f}%"
                        row.spec_value = "Not Specified"
                    except (TypeError, ValueError, ZeroDivisionError):
                        row.indus_value = "—"
                elif "Elastic Modulus" in row.name and row.indus_value and row.indus_value != "—":
                    try:
                        row.indus_value = f"{float(row.indus_value):.2f}"
                    except (ValueError, TypeError):
                        pass

        if group.rows:
            doc.groups.append(group)

    return doc
