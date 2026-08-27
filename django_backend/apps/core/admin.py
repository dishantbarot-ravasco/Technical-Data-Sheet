"""
apps/core/admin.py — Django Admin registration for all 31 TDS models.

Every model below is registered with a ModelAdmin subclass that configures
its list view (list_display/list_filter/search_fields), sane default
ordering, and — where a model has many FKs or sensitive fields — raw_id
lookups, read-only fields, or field exclusions. This file only configures
Django Admin's UI; it never contains business logic (that lives in
apps/services/ and is called from apps/api/routers/*_views.py instead).

Schema ownership note: most models are managed=True (Django owns and can
migrate their schema) except three composite-PK junction tables
(PurposeBeltType, BrandBeltType, BrandParameter — see the comments above each
of their class definitions in models.py for why). Admin works identically
either way; managed only affects whether `manage.py migrate` may alter these
tables' DDL, not whether Django Admin can read/edit their rows.

Groups:
  1. Reference data     — Purpose, BeltType, IndusBrand, Standard
  2. Parameters         — TDSParameter, BrandParameter, StandardTestMethod
  3. Cover Grades + EAV — CoverGrade, CoverGradeValue
  4. Fabric             — FabricType, FabricStyle, FabricTypeParameterValue,
                          FabricStyleParameterValue
  5. Belt Ratings + EAV — BeltRating, BeltRatingValue
  6. Packing / Logistics — ReelType, PackingType, ContainerType,
                           RegionContainerWeightLimit
  7. Dimensional Specs  — DimensionalParameterSpec
  8. Lookup tables      — SpliceStepLookup, HotSpliceCuringLookup,
                          ConstructionType, SpliceMethodConfig, SamplingPlanLookup
  9. Sequence           — TDSSequence
 10. Users              — TDSUser
 11. Customers          — Customer
 12. TDS Records        — TDSInput
 13. M2M junction       — PurposeBeltType, BrandBeltType
"""
from django.contrib import admin

from .audit_log import TDSAuditLog
from .models import (
    # Reference data
    Purpose, BeltType, IndusBrand, Standard,
    # Parameters
    TDSParameter, BrandParameter, StandardTestMethod,
    # Cover Grades
    CoverGrade, CoverGradeValue,
    # Fabric
    FabricType, FabricStyle, FabricTypeParameterValue, FabricStyleParameterValue,
    # Belt Ratings
    BeltRating, BeltRatingValue,
    # Packing / Logistics
    ReelType, PackingType, ContainerType, RegionContainerWeightLimit,
    # Dimensional Specs
    DimensionalParameterSpec,
    # Lookup tables
    SpliceStepLookup, HotSpliceCuringLookup, ConstructionType,
    SpliceMethodConfig, SamplingPlanLookup,
    # Sequence
    TDSSequence,
    # Users
    TDSUser,
    # Customers
    Customer,
    # TDS records
    TDSInput,
    # M2M junctions
    PurposeBeltType, BrandBeltType,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Reference data
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Purpose)
class PurposeAdmin(admin.ModelAdmin):
    list_display  = ('purpose_id', 'purpose_type')
    search_fields = ('purpose_type',)
    ordering      = ('purpose_id',)


@admin.register(BeltType)
class BeltTypeAdmin(admin.ModelAdmin):
    list_display  = ('belt_id', 'belt_type')
    search_fields = ('belt_type',)
    ordering      = ('belt_id',)


@admin.register(IndusBrand)
class IndusBrandAdmin(admin.ModelAdmin):
    list_display  = ('brand_id', 'brand_name')
    search_fields = ('brand_name',)
    ordering      = ('brand_id',)


@admin.register(Standard)
class StandardAdmin(admin.ModelAdmin):
    list_display  = ('standard_id', 'standard_name', 'standard_edition',
                     'standard_country', 'brand')
    list_filter   = ('brand', 'standard_country')
    search_fields = ('standard_name', 'standard_edition')
    ordering      = ('standard_id',)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Parameters
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TDSParameter)
class TDSParameterAdmin(admin.ModelAdmin):
    # brand FK removed from TDSParameter model (no brand_id column in tds_parameters table;
    # brand association is through the brand_parameters junction table)
    list_display  = ('parameter_id', 'parameter_group', 'parameter_name',
                     'display_order', 'is_user_input', 'visibility_condition',
                     'spec_equals_indus')
    list_filter   = ('parameter_group', 'is_user_input',
                     'visibility_condition', 'spec_equals_indus')
    search_fields = ('parameter_name', 'parameter_group')
    ordering      = ('parameter_group', 'display_order')


@admin.register(BrandParameter)
class BrandParameterAdmin(admin.ModelAdmin):
    list_display  = ('brand', 'parameter', 'display_order', 'is_user_input',
                     'visibility_condition', 'spec_equals_indus')
    list_filter   = ('brand', 'is_user_input', 'spec_equals_indus')
    search_fields = ('parameter__parameter_name',)
    ordering      = ('brand', 'display_order')
    raw_id_fields = ('parameter',)


@admin.register(StandardTestMethod)
class StandardTestMethodAdmin(admin.ModelAdmin):
    list_display  = ('standard', 'parameter', 'section', 'test_method', 'reference')
    list_filter   = ('standard',)
    search_fields = ('parameter__parameter_name', 'section', 'test_method')
    ordering      = ('standard', 'parameter')
    raw_id_fields = ('parameter',)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Cover Grades + EAV
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(CoverGrade)
class CoverGradeAdmin(admin.ModelAdmin):
    list_display  = ('id', 'grade_code', 'grade_description', 'standard',
                     'specific_gravity')
    list_filter   = ('standard',)
    search_fields = ('grade_code', 'grade_description')
    ordering      = ('standard', 'grade_code')


@admin.register(CoverGradeValue)
class CoverGradeValueAdmin(admin.ModelAdmin):
    list_display  = ('cover_grade', 'parameter', 'spec_value', 'indus_value')
    list_filter   = ('cover_grade',)
    search_fields = ('parameter__parameter_name',)
    ordering      = ('cover_grade', 'parameter')
    raw_id_fields = ('parameter',)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fabric
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(FabricType)
class FabricTypeAdmin(admin.ModelAdmin):
    list_display  = ('id', 'fabric_code', 'description', 'manufacturer')
    search_fields = ('fabric_code', 'description')
    ordering      = ('fabric_code',)


@admin.register(FabricStyle)
class FabricStyleAdmin(admin.ModelAdmin):
    list_display  = ('id', 'fabric_type', 'style_name')
    list_filter   = ('fabric_type',)
    search_fields = ('style_name',)
    ordering      = ('fabric_type', 'style_name')


@admin.register(FabricTypeParameterValue)
class FabricTypeParameterValueAdmin(admin.ModelAdmin):
    list_display  = ('fabric_type', 'parameter', 'spec_value', 'indus_value')
    list_filter   = ('fabric_type',)
    search_fields = ('parameter__parameter_name',)
    ordering      = ('fabric_type', 'parameter')
    raw_id_fields = ('parameter',)


@admin.register(FabricStyleParameterValue)
class FabricStyleParameterValueAdmin(admin.ModelAdmin):
    list_display  = ('fabric_style', 'parameter', 'spec_value', 'indus_value')
    list_filter   = ('fabric_style',)
    search_fields = ('parameter__parameter_name',)
    ordering      = ('fabric_style', 'parameter')
    raw_id_fields = ('parameter',)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Belt Ratings + EAV
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(BeltRating)
class BeltRatingAdmin(admin.ModelAdmin):
    list_display  = ('id', 'rating_name', 'fabric_type')
    list_filter   = ('fabric_type',)
    search_fields = ('rating_name',)
    ordering      = ('fabric_type', 'rating_name')


@admin.register(BeltRatingValue)
class BeltRatingValueAdmin(admin.ModelAdmin):
    list_display  = ('belt_rating', 'parameter', 'spec_value', 'indus_value')
    list_filter   = ('belt_rating',)
    search_fields = ('parameter__parameter_name',)
    ordering      = ('belt_rating', 'parameter')
    raw_id_fields = ('parameter',)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Packing / Logistics
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(ReelType)
class ReelTypeAdmin(admin.ModelAdmin):
    list_display  = ('id', 'reel_name', 'formula_key', 'num_rolls_base',
                     'max_roll_diameter_m')
    search_fields = ('reel_name',)
    ordering      = ('reel_name',)


@admin.register(PackingType)
class PackingTypeAdmin(admin.ModelAdmin):
    list_display  = ('id', 'packing_name', 'is_available')
    list_filter   = ('is_available',)
    search_fields = ('packing_name',)
    ordering      = ('packing_name',)


@admin.register(ContainerType)
class ContainerTypeAdmin(admin.ModelAdmin):
    list_display  = ('id', 'name', 'max_height_m', 'max_width_m')
    search_fields = ('name',)
    ordering      = ('name',)


@admin.register(RegionContainerWeightLimit)
class RegionContainerWeightLimitAdmin(admin.ModelAdmin):
    list_display  = ('id', 'region', 'container_type', 'max_gross_weight_kg')
    list_filter   = ('region', 'container_type')
    ordering      = ('region', 'container_type')


# ─────────────────────────────────────────────────────────────────────────────
# 7. Dimensional Specs
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(DimensionalParameterSpec)
class DimensionalParameterSpecAdmin(admin.ModelAdmin):
    list_display  = ('id', 'standard', 'parameter', 'min_value', 'max_value',
                     'tolerance_value')
    list_filter   = ('standard', 'parameter')
    search_fields = ('tolerance_value',)
    ordering      = ('standard', 'parameter', 'min_value')
    raw_id_fields = ('parameter',)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Lookup tables
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(SpliceStepLookup)
class SpliceStepLookupAdmin(admin.ModelAdmin):
    list_display = ('max_fabric_rating_kn_m', 'step_length_mm', 'standard_ref')
    ordering     = ('max_fabric_rating_kn_m',)


@admin.register(HotSpliceCuringLookup)
class HotSpliceCuringLookupAdmin(admin.ModelAdmin):
    list_display = ('total_belt_thickness_mm', 'specific_pressure', 'curing_temp',
                    'curing_time_min', 'cooling_temp_c')
    ordering     = ('total_belt_thickness_mm',)


@admin.register(ConstructionType)
class ConstructionTypeAdmin(admin.ModelAdmin):
    list_display  = ('id', 'construction_name', 'qty_label', 'max_belt_length_m',
                     'description')
    search_fields = ('construction_name',)
    ordering      = ('construction_name',)


@admin.register(SpliceMethodConfig)
class SpliceMethodConfigAdmin(admin.ModelAdmin):
    list_display = ('vulcanization_method', 'buffer_mm', 'standard_ref')
    ordering     = ('vulcanization_method',)


@admin.register(SamplingPlanLookup)
class SamplingPlanLookupAdmin(admin.ModelAdmin):
    list_display = ('max_belt_length_m', 'sample_count', 'standard_ref')
    ordering     = ('max_belt_length_m',)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Sequence
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TDSSequence)
class TDSSequenceAdmin(admin.ModelAdmin):
    list_display = ('year', 'last_number')
    # Read-only — mutating this breaks the TDS numbering sequence
    readonly_fields = ('year', 'last_number')


# ─────────────────────────────────────────────────────────────────────────────
# 10. Users
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TDSUser)
class TDSUserAdmin(admin.ModelAdmin):
    list_display   = ('user_id', 'email', 'full_name', 'role', 'designation',
                      'is_active', 'created_at', 'last_login_at')
    list_filter    = ('role', 'is_active')
    search_fields  = ('email', 'full_name')
    ordering       = ('user_id',)
    # Never expose the password hash in the admin UI
    exclude        = ('password_hash',)
    readonly_fields = ('created_at', 'last_login_at')


# ─────────────────────────────────────────────────────────────────────────────
# 11. Customers
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display  = ('customer_id', 'customer_name', 'contact_person',
                     'application', 'plant_location')
    search_fields = ('customer_name', 'contact_person', 'application',
                     'plant_location')
    ordering      = ('customer_name',)


# ─────────────────────────────────────────────────────────────────────────────
# 12. TDS Records
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TDSInput)
class TDSInputAdmin(admin.ModelAdmin):
    list_display   = ('tds_id', 'tds_number', 'tds_doc_number', 'tds_date',
                      'status', 'customer', 'brand', 'standard',
                      'belt_width_mm', 'belt_length_m', 'construction_type',
                      'created_by', 'created_at')
    # PERF (fixed): list_display references 4 FK columns (customer, brand,
    # standard, created_by) with no list_select_related, so the admin list
    # page issued one extra query per FK per row (N+1) — noticeable once TDS
    # volume grows. This joins them all in the single list query instead.
    list_select_related = ('customer', 'brand', 'standard', 'created_by')
    list_filter    = ('status', 'brand', 'standard', 'construction_type',
                      'splicing_required')
    search_fields  = ('tds_number', 'tds_doc_number', 'customer__customer_name',
                      'brand__brand_name')
    ordering       = ('-tds_id',)
    readonly_fields = ('tds_id', 'created_at', 'updated_at',
                       'belt_weight_per_m_kg', 'total_thickness_mm',
                       'interply_skim_mm', 'step_length_mm', 'splice_length_mm',
                       'total_extra_length_m')
    raw_id_fields  = ('customer', 'created_by', 'approved_by')
    fieldsets = (
        ('Identity', {
            'fields': ('tds_id', 'tds_number', 'tds_doc_number', 'tds_date',
                       'status', 'brand', 'standard', 'purpose', 'belt_type',
                       'customer', 'belt_description'),
        }),
        ('Construction', {
            'fields': ('construction_type', 'construction_type_fk',
                       'fabric_type', 'fabric_style', 'belt_rating', 'cover_grade',
                       'make_of_fabric', 'num_plies',
                       'belt_width_mm', 'belt_length_m',
                       'top_cover_mm', 'bottom_cover_mm',
                       'carcass_from_rating', 'carcass_thickness_mm',
                       'interply_skim_mm', 'total_thickness_mm',
                       'belt_weight_per_m_kg',
                       'breaker_top', 'breaker_top_plies',
                       'breaker_bottom', 'breaker_bottom_plies',
                       'edge_construction'),
        }),
        ('Packing', {
            'fields': ('reel_type', 'packing_type',
                       'num_rolls', 'length_per_roll_m', 'roll_dimensions',
                       'net_weight_kg', 'gross_weight_kg', 'gross_weight_per_roll_kg'),
        }),
        ('International Logistics', {
            'fields': ('shipping_region', 'container_type'),
            'classes': ('collapse',),
        }),
        ('Splicing', {
            'fields': ('splicing_required', 'vulcanization_method', 'num_joints',
                       'step_length_mm', 'splice_length_mm', 'total_extra_length_m'),
            'classes': ('collapse',),
        }),
        ('Audit', {
            'fields': ('created_by', 'created_at', 'approved_by', 'approved_at',
                       'updated_at'),
            'classes': ('collapse',),
        }),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 13. M2M junction tables
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(PurposeBeltType)
class PurposeBeltTypeAdmin(admin.ModelAdmin):
    list_display = ('purpose', 'belt_type')
    list_filter  = ('purpose', 'belt_type')
    ordering     = ('purpose', 'belt_type')


@admin.register(BrandBeltType)
class BrandBeltTypeAdmin(admin.ModelAdmin):
    list_display = ('brand', 'belt_type')
    list_filter  = ('brand', 'belt_type')
    ordering     = ('brand', 'belt_type')


# ─────────────────────────────────────────────────────────────────────────────
# 14. Audit log — append-only; read-only in Admin (see audit_log.py)
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TDSAuditLog)
class TDSAuditLogAdmin(admin.ModelAdmin):
    list_display  = ('timestamp', 'action', 'tds_number', 'actor_email', 'ip_address', 'detail')
    list_filter   = ('action',)
    search_fields = ('tds_number', 'actor_email', 'ip_address', 'detail')
    ordering      = ('-timestamp',)
    readonly_fields = [f.name for f in TDSAuditLog._meta.get_fields()]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
