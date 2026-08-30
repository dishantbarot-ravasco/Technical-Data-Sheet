"""
apps/core/models.py — Django ORM models for TDS Automation App.

These 31 tables were originally created and seeded by the FastAPI backend,
so they started life as managed = False (Django reads/writes them but never
alters or drops them). They are now managed = True: every field below was
verified against the live Postgres schema (information_schema.columns) and
the adoption migration (apps/core/migrations/0013_*) was applied with
--fake, since the tables already exist and already match what it describes
— no DDL ran, no data touched. From here on, Django owns their schema the
normal way: change a field, run makemigrations + migrate, and it generates
and actually executes real ALTER TABLE statements against Postgres, same as
any other Django-managed table.

Table count: 31
  purpose, belt_type, indus_brand
  purpose_belt_type, brand_belt_type         (composite-PK junctions)
  standards, tds_parameters, brand_parameters (composite-PK junction)
  standard_test_methods
  cover_grades, cover_grade_values
  fabric_types, fabric_type_parameter_values
  fabric_styles, fabric_style_parameter_values
  belt_ratings, belt_rating_values
  reel_types, packing_types, container_types, region_container_weight_limits
  dimensional_parameter_specs
  splice_step_lookup, hot_splice_curing_lookup
  construction_types, splice_method_config, sampling_plan_lookup
  tds_sequence
  users, customers, tds_inputs
"""

from django.db import models


# ─────────────────────────────────────────────────────────────────────────────
# 1. TOP-LEVEL LOOKUP TABLES
# ─────────────────────────────────────────────────────────────────────────────

class Purpose(models.Model):
    """
    Commercial purpose: Domestic or International.
    International orders require shipping_region + container_type on TDSInput.
    """
    purpose_id   = models.AutoField(primary_key=True)
    purpose_type = models.TextField()

    class Meta:
        db_table = 'purposes'
        managed  = True

    def __str__(self):
        return self.purpose_type


class BeltType(models.Model):
    """Belt construction category (Flat Open-End, Endless, etc.)."""
    belt_id   = models.AutoField(primary_key=True)
    belt_type = models.TextField()

    class Meta:
        db_table = 'belt_types'
        managed  = True

    def __str__(self):
        return self.belt_type


class IndusBrand(models.Model):
    """Product brand within Ravasco (e.g. INDUS SUPER BRUTE)."""
    brand_id   = models.AutoField(primary_key=True)
    brand_name = models.TextField()

    class Meta:
        db_table = 'brands'
        managed  = True

    def __str__(self):
        return self.brand_name


# ─────────────────────────────────────────────────────────────────────────────
# 2. M2M JUNCTION TABLES (composite PKs in PostgreSQL)
#    Django limitation: pre-5.2 Django has no native composite PK field.
#    Solution: one FK declared primary_key=True (harmless for queries since
#    we always filter, never look up by .pk directly) — independent of
#    managed status; these tables are managed=True like everything else here.
# ─────────────────────────────────────────────────────────────────────────────

class PurposeBeltType(models.Model):
    """
    Which belt type is valid for a given purpose.
    NOTE: the underlying (unmanaged) table's real primary key is purpose_id
    alone (see migrations/0001_initial.py), so this is a true 1:1 at the
    database level -- each Purpose maps to exactly one BeltType, not a M2M.
    OneToOneField is therefore correct here; do not change to ForeignKey
    without first altering the real DB schema to add a surrogate PK.
    """
    purpose   = models.OneToOneField('Purpose',  on_delete=models.CASCADE,
                                     related_name='belt_type_links', primary_key=True)
    # Actual column is 'belt_id', not the Django default 'belt_type_id'
    belt_type = models.ForeignKey('BeltType', on_delete=models.CASCADE,
                                  db_column='belt_id', related_name='purpose_links')

    class Meta:
        db_table        = 'purpose_belt_type'
        managed         = True
        unique_together = [('purpose', 'belt_type')]

    def __str__(self):
        return f"{self.purpose} — {self.belt_type}"


class BrandBeltType(models.Model):
    """
    Which belt type a brand manufactures.
    NOTE: same as PurposeBeltType above -- the underlying (unmanaged) table's
    real primary key is brand_id alone, so this is a true 1:1 at the database
    level. OneToOneField is correct; do not change without a real schema change.
    """
    brand     = models.OneToOneField('IndusBrand', on_delete=models.CASCADE,
                                     related_name='belt_type_links', primary_key=True)
    belt_type = models.ForeignKey('BeltType',   on_delete=models.CASCADE,
                                  db_column='belt_id', related_name='brand_links')

    class Meta:
        db_table        = 'brand_belt_type'
        managed         = True
        unique_together = [('brand', 'belt_type')]

    def __str__(self):
        return f"{self.brand} — {self.belt_type}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. STANDARDS & PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────

class Standard(models.Model):
    """
    Quality/testing standard (IS 1891, ISO 14890, DIN 22102, AS 1332,
    SANS 1173, ASTM D378). Each standard belongs to one brand.
    """
    standard_id          = models.AutoField(primary_key=True)
    standard_name        = models.TextField()
    standard_edition     = models.TextField(null=True, blank=True)
    standard_description = models.TextField(null=True, blank=True)
    standard_country     = models.TextField(null=True, blank=True)
    brand                = models.ForeignKey('IndusBrand', on_delete=models.PROTECT,
                                             related_name='standards')

    class Meta:
        db_table = 'standards'
        managed  = True

    def __str__(self):
        return self.standard_name


class TDSParameter(models.Model):
    """
    Named measurable property (e.g. "Tensile Strength", "Belt Width (mm)").
    parameter_group drives PDF section grouping.
    display_order controls row order within each section.
    visibility_condition: NULL=always | 'international_only' | 'hot_splice_only'.
    spec_equals_indus: True for Belt Construction params 9–12.

    NOTE: brand association is through the brand_parameters junction table (BrandParameter),
    NOT a direct FK on this table. The tds_parameters DB table has no brand_id column.
    """
    parameter_id         = models.AutoField(primary_key=True)
    parameter_group      = models.TextField()
    parameter_name       = models.TextField()
    display_order        = models.IntegerField()
    is_user_input        = models.BooleanField(default=False)
    visibility_condition = models.TextField(null=True, blank=True)
    spec_equals_indus    = models.BooleanField(default=False)

    class Meta:
        db_table        = 'tds_parameters'
        managed         = True
        unique_together = [('parameter_group', 'parameter_name')]

    def __str__(self):
        return f"{self.parameter_group} — {self.parameter_name}"


class BrandParameter(models.Model):
    """
    Junction: brand × parameter with per-brand overrides (display_order,
    is_user_input, visibility_condition, spec_equals_indus).
    Composite PK: (brand_id, parameter_id).
    """
    brand                = models.OneToOneField('IndusBrand',   on_delete=models.CASCADE,
                                                related_name='brand_parameters', primary_key=True)
    parameter            = models.ForeignKey('TDSParameter', on_delete=models.CASCADE,
                                             related_name='brand_parameters')
    display_order        = models.IntegerField()
    is_user_input        = models.BooleanField(default=False)
    visibility_condition = models.TextField(null=True, blank=True)
    spec_equals_indus    = models.BooleanField(default=False)

    class Meta:
        db_table        = 'brand_parameters'
        managed         = True
        unique_together = [('brand', 'parameter')]

    def __str__(self):
        return f"{self.brand} × {self.parameter}"


class StandardTestMethod(models.Model):
    """
    Maps a TDSParameter to the test method clause for a specific Standard.
    Populates the "Test Method / Reference" column in the PDF.
    """
    standard    = models.ForeignKey('Standard',     on_delete=models.CASCADE,
                                    related_name='test_methods')
    parameter   = models.ForeignKey('TDSParameter', on_delete=models.CASCADE,
                                    related_name='test_methods')
    section     = models.TextField(null=True, blank=True)
    test_method = models.TextField(null=True, blank=True)
    reference   = models.TextField(null=True, blank=True)
    notes       = models.TextField(null=True, blank=True)

    class Meta:
        db_table        = 'standard_test_methods'
        managed         = True
        unique_together = [('standard', 'parameter')]

    def __str__(self):
        return f"{self.standard} / {self.parameter}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. COVER GRADES + EAV VALUES
# ─────────────────────────────────────────────────────────────────────────────

class CoverGrade(models.Model):
    """
    Rubber compound grade per Standard (M24, N17, HR, FR, OA, etc.).
    specific_gravity drives belt weight calculation.
    """
    standard          = models.ForeignKey('Standard', on_delete=models.PROTECT,
                                          related_name='cover_grades')
    grade_code        = models.TextField()
    grade_description = models.TextField(null=True, blank=True)
    specific_gravity  = models.FloatField()

    class Meta:
        db_table        = 'cover_grades'
        managed         = True
        unique_together = [('standard', 'grade_code')]

    def __str__(self):
        return f"{self.grade_code} ({self.standard})"


class CoverGradeValue(models.Model):
    """
    EAV: one spec/actual value per (CoverGrade × TDSParameter).
    spec_value  = minimum per the standard (e.g. "≥ 24 MPa").
    indus_value = Ravasco's actual achieved value printed on the TDS.
    """
    cover_grade = models.ForeignKey('CoverGrade',   on_delete=models.CASCADE,
                                    related_name='values')
    parameter   = models.ForeignKey('TDSParameter', on_delete=models.CASCADE,
                                    related_name='cover_grade_values')
    spec_value  = models.TextField(null=True, blank=True)
    indus_value = models.TextField()

    class Meta:
        db_table        = 'cover_grade_values'
        managed         = True
        unique_together = [('cover_grade', 'parameter')]

    def __str__(self):
        return f"CGV(grade={self.cover_grade_id}, param={self.parameter_id})"


# ─────────────────────────────────────────────────────────────────────────────
# 5. FABRIC TYPES, STYLES, RATINGS + EAV VALUES
# ─────────────────────────────────────────────────────────────────────────────

class FabricType(models.Model):
    """
    Carcass fabric material (EP, NN, etc.).
    Has multiple BeltRatings (tension classes) and FabricStyles (weave variants).
    """
    fabric_code  = models.TextField(unique=True)
    description  = models.TextField(null=True, blank=True)
    manufacturer = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'fabric_types'
        managed  = True

    def __str__(self):
        return self.fabric_code


class FabricTypeParameterValue(models.Model):
    """EAV: fabric-level spec value (constant across all ratings of a fabric)."""
    fabric_type = models.ForeignKey('FabricType',   on_delete=models.CASCADE,
                                    related_name='values')
    parameter   = models.ForeignKey('TDSParameter', on_delete=models.CASCADE,
                                    related_name='fabric_type_values')
    spec_value  = models.TextField(null=True, blank=True)
    indus_value = models.TextField()

    class Meta:
        db_table        = 'fabric_type_parameter_values'
        managed         = True
        unique_together = [('fabric_type', 'parameter')]

    def __str__(self):
        return f"FTPV(type={self.fabric_type_id}, param={self.parameter_id})"


class FabricStyle(models.Model):
    """
    Weave variant within a FabricType (Straight Warp, 2×2 Twill, etc.).
    Optional on TDS form; overrides FabricTypeParameterValue when present.
    """
    fabric_type = models.ForeignKey('FabricType', on_delete=models.CASCADE,
                                    related_name='styles')
    style_name  = models.TextField()

    class Meta:
        db_table        = 'fabric_styles'
        managed         = True
        unique_together = [('fabric_type', 'style_name')]

    def __str__(self):
        return f"{self.fabric_type} / {self.style_name}"


class FabricStyleParameterValue(models.Model):
    """EAV: style-level override values (take precedence over FabricTypeParameterValue)."""
    fabric_style = models.ForeignKey('FabricStyle',  on_delete=models.CASCADE,
                                     related_name='values')
    parameter    = models.ForeignKey('TDSParameter', on_delete=models.CASCADE,
                                     related_name='fabric_style_values')
    spec_value   = models.TextField(null=True, blank=True)
    indus_value  = models.TextField()

    class Meta:
        db_table        = 'fabric_style_parameter_values'
        managed         = True
        unique_together = [('fabric_style', 'parameter')]

    def __str__(self):
        return f"FSPV(style={self.fabric_style_id}, param={self.parameter_id})"


class BeltRating(models.Model):
    """
    Tension class for a FabricType (EP 315/3, EP 630/4, NN 200/3, etc.).
    rating_name format: '<fabric_code> <kN/m>/<plies>'.
    EAV values include carcass_thickness_mm (param_id=4) and interply_skim_mm (param_id=5).
    """
    fabric_type = models.ForeignKey('FabricType', on_delete=models.PROTECT,
                                    related_name='belt_ratings')
    rating_name = models.TextField()

    class Meta:
        db_table        = 'belt_ratings'
        managed         = True
        unique_together = [('fabric_type', 'rating_name')]

    def __str__(self):
        return self.rating_name


class BeltRatingValue(models.Model):
    """
    EAV: carcass parameter values per BeltRating.
    param_id=4 → carcass_thickness_mm (user may override).
    param_id=5 → interply_skim_mm (server-fetched, no override).
    """
    belt_rating = models.ForeignKey('BeltRating',   on_delete=models.CASCADE,
                                    related_name='values')
    parameter   = models.ForeignKey('TDSParameter', on_delete=models.CASCADE,
                                    related_name='belt_rating_values')
    spec_value  = models.TextField(null=True, blank=True)
    indus_value = models.TextField()

    class Meta:
        db_table        = 'belt_rating_values'
        managed         = True
        unique_together = [('belt_rating', 'parameter')]

    def __str__(self):
        return f"BRV(rating={self.belt_rating_id}, param={self.parameter_id})"


# ─────────────────────────────────────────────────────────────────────────────
# 6. PACKING & LOGISTICS
# ─────────────────────────────────────────────────────────────────────────────

class ReelType(models.Model):
    """
    Physical reel geometry.
    formula_key: 'circular' | 'twin' | 'elliptical'
    max_roll_diameter_m caps computed outer diameter.
    """
    reel_name           = models.TextField(unique=True)
    formula_key         = models.TextField()
    num_rolls_base      = models.IntegerField()
    core_diameter_m     = models.DecimalField(max_digits=6, decimal_places=4)
    center_to_center_m  = models.DecimalField(max_digits=6, decimal_places=4,
                                              null=True, blank=True)
    max_roll_diameter_m = models.DecimalField(max_digits=4, decimal_places=2,
                                              default='2.50')

    class Meta:
        db_table = 'reel_types'
        managed  = True

    def __str__(self):
        return self.reel_name


class PackingType(models.Model):
    """Outer packaging method (HDPE Wrapping, Wooden Crate, etc.)."""
    packing_name = models.TextField(unique=True)
    is_available = models.BooleanField(default=False)

    class Meta:
        db_table = 'packing_types'
        managed  = True

    def __str__(self):
        return self.packing_name


class ContainerType(models.Model):
    """Shipping container physical limits (height/width caps reel dimensions)."""
    name         = models.TextField(unique=True)
    max_height_m = models.DecimalField(max_digits=5, decimal_places=3)
    max_width_m  = models.DecimalField(max_digits=5, decimal_places=3)

    class Meta:
        db_table = 'container_types'
        managed  = True

    def __str__(self):
        return self.name


class RegionContainerWeightLimit(models.Model):
    """Max gross weight (kg) per shipping region + container type."""
    region              = models.TextField()
    container_type      = models.ForeignKey('ContainerType', on_delete=models.CASCADE,
                                            related_name='weight_limits')
    max_gross_weight_kg = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        db_table        = 'region_container_weight_limits'
        managed         = True
        unique_together = [('region', 'container_type')]

    def __str__(self):
        return f"{self.region} / {self.container_type}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. DIMENSIONAL SPECS & LOOKUP TABLES
# ─────────────────────────────────────────────────────────────────────────────

class DimensionalParameterSpec(models.Model):
    """
    Tolerance spec for dimensional parameters (width, covers, carcass, total thickness).
    Queried by pdf_service to resolve spec_value for params 1, 2, 3, 4, 6.
    """
    standard        = models.ForeignKey('Standard',     on_delete=models.CASCADE,
                                        related_name='dimensional_specs')
    parameter       = models.ForeignKey('TDSParameter', on_delete=models.CASCADE,
                                        related_name='dimensional_specs')
    min_value       = models.FloatField(null=True, blank=True)
    max_value       = models.FloatField(null=True, blank=True)
    tolerance_value = models.TextField()

    class Meta:
        db_table        = 'dimensional_parameter_specs'
        managed         = True
        unique_together = [('standard', 'parameter', 'min_value', 'max_value')]

    def __str__(self):
        return f"DimSpec(std={self.standard_id}, param={self.parameter_id})"


class SpliceStepLookup(models.Model):
    """
    IS 14206: fabric rating (kN/m/ply) → step_length_mm.
    Query: first row where max_fabric_rating_kn_m >= user value, ORDER BY ASC.
    """
    max_fabric_rating_kn_m = models.IntegerField(unique=True)
    step_length_mm         = models.IntegerField()
    standard_ref           = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'splice_step_lookup'
        managed  = True

    def __str__(self):
        return f"≤{self.max_fabric_rating_kn_m} kN/m → {self.step_length_mm} mm step"


class HotSpliceCuringLookup(models.Model):
    """
    Hot-vulcanisation curing parameters by total belt thickness.
    Query: first row where total_belt_thickness_mm >= tds.total_thickness_mm, ORDER BY ASC.
    """
    total_belt_thickness_mm = models.IntegerField(unique=True)
    specific_pressure       = models.TextField()
    curing_temp             = models.TextField()
    curing_time_min         = models.IntegerField()
    cooling_temp_c          = models.IntegerField()

    class Meta:
        db_table = 'hot_splice_curing_lookup'
        managed  = True

    def __str__(self):
        return f"Cure @ ≤{self.total_belt_thickness_mm} mm"


class ConstructionType(models.Model):
    """
    Open-End (Rolls, unlimited length) or Endless (Nos, max 100 m).
    qty_label: 'Rolls' | 'Nos' — printed on the TDS PDF.
    """
    construction_name = models.TextField(unique=True)
    max_belt_length_m = models.DecimalField(max_digits=8, decimal_places=2,
                                            null=True, blank=True)
    qty_label         = models.TextField(default='Rolls')
    description       = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'construction_types'
        managed  = True

    def __str__(self):
        return self.construction_name


class SpliceMethodConfig(models.Model):
    """
    Splice buffer per vulcanisation method (IS 14206).
    hot → 50 mm | cold → 75 mm.
    """
    vulcanization_method = models.TextField(unique=True)
    buffer_mm            = models.IntegerField()
    standard_ref         = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'splice_method_config'
        managed  = True

    def __str__(self):
        return f"{self.vulcanization_method}: {self.buffer_mm} mm"


class SamplingPlanLookup(models.Model):
    """
    IS 1891 sampling plan: total belt length → sample count.
    Query: first row where max_belt_length_m >= total_length, ORDER BY ASC.
    """
    max_belt_length_m = models.DecimalField(max_digits=12, decimal_places=2, unique=True)
    sample_count      = models.IntegerField()
    standard_ref      = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'sampling_plan_lookup'
        managed  = True

    def __str__(self):
        return f"≤{self.max_belt_length_m} m → {self.sample_count} samples"


# ─────────────────────────────────────────────────────────────────────────────
# 8. TDS SEQUENCE (atomic counter)
# ─────────────────────────────────────────────────────────────────────────────

class TDSSequence(models.Model):
    """
    Global TDS serial number counter. One row: year=0 (sentinel).
    next_tds_number() uses SELECT FOR UPDATE to generate collision-free
    4-digit numbers: '0001', '0002', ...
    Phase 4 note: use select_for_update() in a transaction.atomic() block.
    """
    year        = models.IntegerField(primary_key=True)
    last_number = models.IntegerField(default=0)

    class Meta:
        db_table = 'tds_sequence'
        managed  = True

    def __str__(self):
        return f"TDSSequence year={self.year} last={self.last_number}"


# ─────────────────────────────────────────────────────────────────────────────
# 9. USERS
# ─────────────────────────────────────────────────────────────────────────────

class TDSUser(models.Model):
    """
    Application user. Completely independent of Django's auth.User.
    Named TDSUser to avoid Python import clashes; maps to the 'users' table.

    Roles: 'admin' (full access) | 'tds_creator' (create/edit TDS) | 'viewer'
    (search + view + download TDS only, no create/edit/delete, no admin panel).
    password_hash: bcrypt (python-jose, rounds=12). Never returned by API.
    Phase 3 note: Phase 3 will wire this to simplejwt via a custom backend.
    """
    user_id       = models.AutoField(primary_key=True)
    email         = models.TextField(unique=True)
    password_hash = models.TextField()
    full_name     = models.TextField(null=True, blank=True)
    role          = models.TextField(default='tds_creator')
    designation   = models.TextField(null=True, blank=True)
    is_active     = models.BooleanField(default=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    # ── Django/DRF auth protocol ──────────────────────────────────────────────
    # TDSUser does NOT inherit from AbstractBaseUser, so we must explicitly
    # declare these attributes.  DRF's IsAuthenticated permission calls
    # request.user.is_authenticated; without this it raises AttributeError.
    is_authenticated = True
    is_anonymous     = False

    class Meta:
        db_table = 'users'
        managed  = True

    def __str__(self):
        return f"{self.email} ({self.role})"


# ─────────────────────────────────────────────────────────────────────────────
# 10. CUSTOMERS
# ─────────────────────────────────────────────────────────────────────────────

class Customer(models.Model):
    """Company or individual who receives a TDS document."""
    customer_id    = models.AutoField(primary_key=True)
    customer_name  = models.TextField()
    contact_person = models.TextField(null=True, blank=True)
    application    = models.TextField(null=True, blank=True)
    plant_location = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'customers'
        managed  = True

    def __str__(self):
        return self.customer_name


# ─────────────────────────────────────────────────────────────────────────────
# 11. TDS INPUT — core business record
# ─────────────────────────────────────────────────────────────────────────────

class TDSInput(models.Model):
    """
    One row per Technical Data Sheet.

    Server-computed fields (set by API on create, not by user):
      total_thickness_mm    = top + bottom + carcass
      interply_skim_mm      = from belt_rating_values (param_id=5)
      belt_weight_per_m_kg  = SG × T × (W/1000)
      step/splice/extra     = computed by splicing_service

    FK column mapping note:
      construction_type_fk → db_column='construction_type_id'
      created_by  → db_column='created_by'  (not 'created_by_id')
      approved_by → db_column='approved_by' (not 'approved_by_id')
    """
    tds_id             = models.AutoField(primary_key=True)
    tds_number         = models.TextField(unique=True)
    tds_doc_number     = models.TextField(null=True, blank=True)
    tds_date           = models.DateField()
    status             = models.TextField(default='draft')
    construction_type  = models.TextField(default='Open-End')

    # ── Foreign Keys ──────────────────────────────────────────────────────────
    purpose   = models.ForeignKey('Purpose',   on_delete=models.PROTECT,
                                  related_name='tds_inputs')
    belt_type = models.ForeignKey('BeltType',  on_delete=models.PROTECT,
                                  related_name='tds_inputs')
    brand     = models.ForeignKey('IndusBrand', on_delete=models.PROTECT,
                                  related_name='tds_inputs')
    standard  = models.ForeignKey('Standard',  on_delete=models.PROTECT,
                                  related_name='tds_inputs')
    customer  = models.ForeignKey('Customer',  on_delete=models.SET_NULL,
                                  related_name='tds_inputs',
                                  null=True, blank=True)
    cover_grade  = models.ForeignKey('CoverGrade',  on_delete=models.PROTECT,
                                     related_name='tds_inputs')
    fabric_type  = models.ForeignKey('FabricType',  on_delete=models.PROTECT,
                                     related_name='tds_inputs')
    fabric_style = models.ForeignKey('FabricStyle', on_delete=models.SET_NULL,
                                     related_name='tds_inputs',
                                     null=True, blank=True)
    belt_rating  = models.ForeignKey('BeltRating',  on_delete=models.PROTECT,
                                     related_name='tds_inputs')
    reel_type    = models.ForeignKey('ReelType',    on_delete=models.SET_NULL,
                                     related_name='tds_inputs',
                                     null=True, blank=True)
    packing_type = models.ForeignKey('PackingType', on_delete=models.SET_NULL,
                                     related_name='tds_inputs',
                                     null=True, blank=True)
    container_type = models.ForeignKey('ContainerType', on_delete=models.SET_NULL,
                                       related_name='tds_inputs',
                                       null=True, blank=True)
    # FK column is 'construction_type_id' — need explicit db_column because field name
    # would auto-generate 'construction_type_fk_id' which doesn't match the DB column.
    construction_type_fk = models.ForeignKey('ConstructionType', on_delete=models.SET_NULL,
                                             db_column='construction_type_id',
                                             related_name='tds_inputs',
                                             null=True, blank=True)
    # DB column is 'created_by' (not 'created_by_id') — explicit db_column required.
    created_by  = models.ForeignKey('TDSUser', on_delete=models.PROTECT,
                                    db_column='created_by',
                                    related_name='created_tds')
    # DB column is 'approved_by' (not 'approved_by_id') — explicit db_column required.
    approved_by = models.ForeignKey('TDSUser', on_delete=models.SET_NULL,
                                    db_column='approved_by',
                                    related_name='approved_tds',
                                    null=True, blank=True)

    # ── Belt Identity ─────────────────────────────────────────────────────────
    belt_description     = models.TextField(null=True, blank=True)
    # decimal_places verified against the live tds_inputs column (information_schema:
    # numeric_precision=10, numeric_scale=2) — was previously declared as (10, 3),
    # which doesn't match the DB and could give a false impression of 3-decimal
    # precision that Postgres silently rounds away on every save.
    belt_length_m        = models.DecimalField(max_digits=10, decimal_places=2)
    # verified: numeric_precision=8, numeric_scale=3 (was incorrectly (8, 4))
    belt_weight_per_m_kg = models.DecimalField(max_digits=8, decimal_places=3,
                                               null=True, blank=True)
    make_of_fabric       = models.TextField(default='MIT')
    belt_width_mm        = models.IntegerField()

    # ── Construction Dimensions ───────────────────────────────────────────────
    num_plies            = models.IntegerField()
    top_cover_mm         = models.DecimalField(max_digits=5, decimal_places=2)
    bottom_cover_mm      = models.DecimalField(max_digits=5, decimal_places=2)
    carcass_from_rating  = models.DecimalField(max_digits=5, decimal_places=2)
    carcass_thickness_mm = models.DecimalField(max_digits=5, decimal_places=2)
    interply_skim_mm     = models.DecimalField(max_digits=5, decimal_places=2,
                                               null=True, blank=True)
    total_thickness_mm   = models.DecimalField(max_digits=6, decimal_places=2)
    breaker_top          = models.BooleanField(default=False)
    breaker_top_plies    = models.IntegerField(null=True, blank=True)
    breaker_bottom       = models.BooleanField(default=False)
    breaker_bottom_plies = models.IntegerField(null=True, blank=True)
    edge_construction    = models.TextField()

    # ── Packing ───────────────────────────────────────────────────────────────
    num_rolls                = models.IntegerField(null=True, blank=True)
    # verified: numeric_precision=10, numeric_scale=2 (was incorrectly (8, 3))
    length_per_roll_m        = models.DecimalField(max_digits=10, decimal_places=2,
                                                   null=True, blank=True)
    # Optional list[float] of individual roll lengths (m), only set when the
    # user manually overrides with UNEQUAL rolls (e.g. [200, 100] instead of
    # an even 150/150 split). Null for auto-calc and uniform-override records
    # — every existing flow is unaffected. When set: num_rolls == len(...)
    # and length_per_roll_m == average(...), kept for backward compat with
    # any code still reading the scalar.
    roll_lengths_m            = models.JSONField(null=True, blank=True)
    roll_dimensions          = models.TextField(null=True, blank=True)
    net_weight_kg            = models.DecimalField(max_digits=10, decimal_places=2,
                                                   null=True, blank=True)
    gross_weight_kg          = models.DecimalField(max_digits=10, decimal_places=2,
                                                   null=True, blank=True)
    gross_weight_per_roll_kg = models.DecimalField(max_digits=10, decimal_places=2,
                                                   null=True, blank=True)

    # ── International Logistics ───────────────────────────────────────────────
    shipping_region = models.TextField(null=True, blank=True)

    # ── Splicing ──────────────────────────────────────────────────────────────
    splicing_required    = models.BooleanField(default=False)
    vulcanization_method = models.TextField(null=True, blank=True)
    num_joints           = models.IntegerField(null=True, blank=True)
    step_length_mm       = models.IntegerField(null=True, blank=True)
    splice_length_mm     = models.IntegerField(null=True, blank=True)
    # verified: numeric_precision=8, numeric_scale=2 (was incorrectly (8, 3))
    total_extra_length_m = models.DecimalField(max_digits=8, decimal_places=2,
                                               null=True, blank=True)

    # ── Batch Link (nullable — NULL means created via single-belt form) ────────
    # db_column='batch_id' matches the column added by migration 0006's RunSQL
    # step, back when tds_inputs was still managed=False and Django couldn't
    # add the column declaratively. Now that tds_inputs is managed=True, a
    # future column addition here would go through a normal AddField
    # migration instead — no RunSQL needed.
    batch = models.ForeignKey(
        'TDSBatch',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        db_column='batch_id',
        related_name='tds_records',
    )

    # ── Version History ──────────────────────────────────────────────────────
    # Starts at 0 ("Rev 00") for a freshly created record. Bumped by 1 in
    # tds_views.py::_update_tds() each time an edit actually changes a field
    # value, right after the pre-edit state is snapshotted into a TDSRevision
    # row (see that model below) — so revision N's snapshot always holds what
    # this record looked like immediately before it became revision N+1.
    current_revision = models.PositiveIntegerField(default=0)

    # ── Audit Timestamps ──────────────────────────────────────────────────────
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tds_inputs'
        managed  = True

    def __str__(self):
        return self.tds_number


# ─────────────────────────────────────────────────────────────────────────────
# DJANGO-MANAGED TABLES  (managed = True — Django creates + owns these)
# ─────────────────────────────────────────────────────────────────────────────

class TDSBatch(models.Model):
    """
    Groups TDS records created together via the Bulk TDS entry flow.

    Stores the shared configuration that applies to every belt in the batch:
      - make_of_fabric   : MIT or SRF (same supplier for all belts in one order)
      - splicing_*       : one splice method for the whole batch; individual
                           TDSInput rows store their own num_joints + computed values
      - reel / packing   : same reel geometry and outer packaging for all belts
      - shipping_region  : one destination (USA or Rest of World); container_type
                           can differ per belt and stays on TDSInput

    Single-belt TDS records have tds_inputs.batch_id = NULL — nothing changes
    for the existing create_tds workflow.

    created_by is a real ForeignKey to TDSUser (on_delete=CASCADE — deleting
    a user removes their batch-metadata rows; the individual TDSInput rows
    in that batch are unaffected since TDSInput.batch is already SET_NULL).
    db_column stays 'created_by_id' so every existing `.created_by_id` /
    `created_by_id=` access pattern in batch_views.py keeps working unchanged.
    """
    batch_id             = models.AutoField(primary_key=True)
    make_of_fabric       = models.TextField(default='MIT')
    splicing_required    = models.BooleanField(default=False)
    vulcanization_method = models.TextField(null=True, blank=True)   # 'Hot' | 'Cold'
    reel_type            = models.ForeignKey(
                               'ReelType', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='batches',
                           )
    packing_type         = models.ForeignKey(
                               'PackingType', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='batches',
                           )
    shipping_region      = models.TextField(null=True, blank=True)   # shared for intl orders
    created_by           = models.ForeignKey('TDSUser', on_delete=models.CASCADE,
                                             related_name='batches')
    created_at           = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tds_batch'
        managed  = True   # Django owns this table via migrations

    def __str__(self):
        return f"TDSBatch #{self.batch_id} ({self.created_at.date() if self.created_at else '—'})"


class BatchExportJob(models.Model):
    """
    Tracks one async batch PDF export (ZIP / merged ZIP / print-all) run on a
    background thread instead of inline in the request.

    Measured WeasyPrint render cost is ~2s per PDF on dev hardware (likely
    2-4x slower on Render's free-tier shared CPU), and a ZIP export renders
    up to 2 PDFs (TDS + QAP) per belt — a ~15-belt batch can already exceed
    gunicorn's 120s request timeout. There is no task queue / broker
    (Celery, Redis) provisioned on the current Render free-tier deployment,
    so this deliberately uses a plain Python thread (apps/api/routers/
    batch_export_views.py's _run_export_job) plus this DB row as the shared
    job-status/result store, the same reasoning CLAUDE.md documents for using
    DatabaseCache over LocMemCache: gunicorn runs multiple worker *processes*,
    so an in-memory dict wouldn't be visible across the request that starts
    the job and the request that later polls/downloads it.

    file_bytes holds the finished export in Postgres (bytea) rather than on
    local disk — Render's disk is not guaranteed to persist or be shared
    across worker processes/restarts, whereas the DB already is the shared
    state store for this deployment. Rows are swept on a TTL (see
    start_export()) rather than via a scheduled task, since no cron
    infrastructure beyond the existing daily-report endpoint exists yet.
    """
    EXPORT_TYPES = (
        ('zip',        'ZIP (per-belt + merged)'),
        ('merged_zip', 'Merged ZIP'),
        ('print_all',  'Print-all merged PDF'),
    )
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('done',    'Done'),
        ('failed',  'Failed'),
    )

    job_id        = models.AutoField(primary_key=True)
    batch         = models.ForeignKey('TDSBatch', on_delete=models.CASCADE, related_name='export_jobs')
    export_type   = models.CharField(max_length=20, choices=EXPORT_TYPES)
    status        = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    # Per-belt progress, updated by the export builder as it renders each
    # record — lets the frontend show "Generating... (3 / 12)" instead of
    # just a spinner for the whole (potentially multi-minute) job. Both stay
    # 0 until the job is running; progress_total is set once the builder
    # knows the record count.
    progress_current = models.PositiveIntegerField(default=0)
    progress_total   = models.PositiveIntegerField(default=0)
    params        = models.JSONField(default=dict, blank=True)
    filename      = models.CharField(max_length=255, blank=True)
    content_type  = models.CharField(max_length=100, blank=True)
    file_bytes    = models.BinaryField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_by    = models.ForeignKey('TDSUser', on_delete=models.SET_NULL, null=True, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    completed_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'batch_export_jobs'
        managed  = True

    def __str__(self):
        return f"BatchExportJob #{self.job_id} ({self.export_type}, {self.status})"


class TDSRevision(models.Model):
    """
    Version history for TDSInput. One row per edit that actually changed a
    field — created in tds_views.py::_update_tds() right before the record is
    overwritten, so `snapshot` holds the record's editable-field values as
    they were immediately before this edit (i.e. "what TDSInput.tds_id looked
    like as of TDSInput.current_revision == revision_number").

    snapshot only stores the fields an edit can actually change (belt spec,
    packing, splicing, etc.) — identity fields that never change after
    creation (tds_number, tds_date, status, created_by) are read from the
    live TDSInput row when displaying a past version, since they're the same
    across every revision of that record.
    """
    tds             = models.ForeignKey('TDSInput', on_delete=models.CASCADE, related_name='revisions')
    revision_number = models.PositiveIntegerField()
    snapshot        = models.JSONField()
    edited_by       = models.ForeignKey('TDSUser', on_delete=models.SET_NULL, null=True, blank=True)
    edited_at       = models.DateTimeField(auto_now_add=True)
    change_summary  = models.TextField(blank=True)

    class Meta:
        db_table        = 'tds_revisions'
        managed         = True
        unique_together = [('tds', 'revision_number')]
        ordering        = ['-revision_number']

    def __str__(self):
        return f"TDS {self.tds_id} Rev {self.revision_number:02d}"


class OTPCode(models.Model):
    """
    Stores one active password-reset OTP per email address.

    Security design:
    - `code_hash` holds the bcrypt hash of the 6-digit code — never plaintext.
      Even if someone reads the DB directly they cannot recover a valid code.
    - `expires_at` enforces a 10-minute TTL enforced in DB time, not process time.
      Safe across multi-worker Gunicorn deployments (unlike the old in-memory dict).
    - `attempts` increments on each wrong guess; the row is deleted at 5 failures.
    - `used` is set True on a successful verify and checked before accepting the code,
      preventing replay attacks within the expiry window.
    - Only one active OTP per email: generate_otp() deletes any existing row first.

    This model is managed = True — Django runs CREATE TABLE via migrations.
    Run after adding this model:
        python manage.py makemigrations core
        python manage.py migrate
    """

    email      = models.EmailField(db_index=True)
    code_hash  = models.CharField(max_length=128)       # bcrypt hash of the 6-digit code
    expires_at = models.DateTimeField()
    attempts   = models.PositiveSmallIntegerField(default=0)
    used       = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'otp_codes'
        managed  = True                                 # Django owns this table
        indexes  = [models.Index(fields=['email'])]     # fast lookup by email

    def __str__(self):
        return f"OTP({self.email}, expires={self.expires_at}, used={self.used})"


class TrustedDevice(models.Model):
    """
    Stores a verified device token for each TDSUser (Instagram-style device trust).

    Design decisions
    ----------------
    - user is a real ForeignKey to TDSUser (on_delete=CASCADE — deleting a
      user removes their trusted-device records too). Deliberately a plain
      IntegerField before 'users' became managed=True, since Django couldn't
      then safely declare a real FK constraint against it; the underlying
      db_column is still 'user_id' so every existing `.user_id` / `user_id=`
      access pattern in device_service.py keeps working unchanged (Django
      auto-exposes `<fk_name>_id` for the raw pk on every ForeignKey field).
    - device_token is a 64-char hex string (secrets.token_hex(32), 256-bit entropy)
      stored as-is; it is set in an httpOnly SameSite=Lax cookie on the browser.
    - last_used_at (auto_now) is bumped on every successful is_trusted_device()
      check so stale-device cleanup jobs can use it accurately.
    - created_at (auto_now_add) is immutable and records when the device was first
      verified via email OTP.

    This model is managed = True — Django creates + owns the table via migrations.
    After adding this model run:
        python manage.py makemigrations core
        python manage.py migrate
    """

    user         = models.ForeignKey('TDSUser', on_delete=models.CASCADE,
                                     related_name='trusted_devices')
    device_token = models.CharField(max_length=64, unique=True)
    device_name  = models.TextField()                         # User-Agent, max 512 chars
    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'trusted_devices'
        managed  = True

    def __str__(self):
        return f"TrustedDevice(user_id={self.user_id}, name={self.device_name[:40]})"


# ─────────────────────────────────────────────────────────────────────────────
# QAP MODELS  (Quality Assurance Plan — managed = True)
# ─────────────────────────────────────────────────────────────────────────────

class QAPTemplate(models.Model):
    """
    One template per belt category (General Purpose, Heat Resistant, Fire Resistant ISO).
    The template that applies to a given TDS is determined by resolving the TDS's
    standard_id against STANDARD_TO_QAP_CATEGORY in qap_service.py.
    """
    CATEGORY_CHOICES = [
        ('GP',     'General Purpose'),
        ('HR',     'Heat Resistant'),
        ('FR_ISO', 'Fire Resistant (ISO)'),
        ('OR',     'Oil Resistant'),
        ('FR_CAN', 'Fire Resistant (CAN/NTPC)'),
    ]
    # Explicit AutoField (not the project-wide BigAutoField default) — these
    # tables were created by migration 0010 with an int4 'id' column; without
    # pinning this, makemigrations sees a mismatch against DEFAULT_AUTO_FIELD
    # and wants to ALTER the live column (and every FK pointing at it) to
    # bigint for no functional benefit on a table this small.
    id           = models.AutoField(primary_key=True)
    category     = models.CharField(max_length=20, choices=CATEGORY_CHOICES, unique=True)
    display_name = models.CharField(max_length=100)
    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'qap_templates'
        managed  = True

    def __str__(self):
        return f"QAPTemplate({self.category}: {self.display_name})"


class QAPSection(models.Model):
    """
    A section heading row within a QAP template.
    Examples: '1.0 Raw Material', '2.0 In-Process Inspection', '3.0 Finished Product'
    """
    # See QAPTemplate.id above — same reasoning applies to every QAP model.
    id           = models.AutoField(primary_key=True)
    template     = models.ForeignKey(QAPTemplate, on_delete=models.CASCADE,
                                     related_name='sections')
    section_code = models.CharField(max_length=10)    # '1.0', '2.0', '3.0'
    section_name = models.CharField(max_length=200)
    sort_order   = models.PositiveIntegerField()

    class Meta:
        db_table        = 'qap_sections'
        managed         = True
        ordering        = ['sort_order']
        unique_together = [('template', 'section_code')]

    def __str__(self):
        return f"{self.section_code} {self.section_name} [{self.template.category}]"


class QAPItem(models.Model):
    """
    One data row inside a QAP section.
    Each row maps to a single line in the QAP table
    (SN, Component, Characteristic, Type of Check, etc.)

    is_static=True marks raw-material rows (section 1.0) that are identical
    across all templates — stored per template for independent editability.

    (QAPRecord below uses a plain IntegerField for tds_id instead of a FK —
    same deliberate simplification as TrustedDevice.user_id.)
    """
    # See QAPTemplate.id above — same reasoning applies to every QAP model.
    id                 = models.AutoField(primary_key=True)
    section            = models.ForeignKey(QAPSection, on_delete=models.CASCADE,
                                           related_name='items')
    sn                 = models.CharField(max_length=20)        # '1.1', '2.1a', etc.
    component          = models.CharField()
    characteristic     = models.CharField(blank=True)
    check_class        = models.CharField(max_length=50,  blank=True)   # Critical/Major/Minor
    type_of_check      = models.CharField(blank=True)
    quantum_m          = models.CharField(max_length=200, blank=True)   # Manufacturer col
    quantum_sc         = models.CharField(max_length=200, blank=True)   # S/C col
    reference_docs     = models.TextField(blank=True)
    acceptance_norms   = models.TextField(blank=True)
    format_of_records  = models.CharField(max_length=200, blank=True)
    agency             = models.CharField(max_length=100, blank=True)   # M / S / C
    record_mark        = models.CharField(max_length=10, blank=True)    # 'D' column — record required (e.g. a tick mark)
    remarks            = models.TextField(blank=True)
    is_static          = models.BooleanField(default=False)  # True = raw material row
    sort_order         = models.PositiveIntegerField()

    class Meta:
        db_table = 'qap_items'
        managed  = True
        ordering = ['sort_order']

    def __str__(self):
        return f"{self.sn} {self.component[:50]}"


class QAPItemSubRow(models.Model):
    """
    One additional physical spreadsheet row within a QAPItem's group (the
    "b) Ash Content", "c) Mooney Viscosity", ... lines under an item like
    "1.1 Raw Rubber"). QAPItem itself represents the group's first physical
    row; every row after that is one of these.

    Each column here is stored EXACTLY as it appeared in that row of the
    source spreadsheet: blank ('') means the source cell was blank, i.e. that
    column is visually merged with the nearest non-blank cell above it in the
    same group (this is literally how the Excel source represents "same
    class/quantum/reference/... as the row above" - a merged cell, not a
    repeated value). A non-blank value means this row starts a NEW value for
    that column from here down, e.g. item 1.1's characteristic often switches
    Type of Check from "Physical" to "Chemical" partway through the group,
    and item 3.5 switches Reference Documents/Acceptance Norms partway
    through for "Angular Tear Strength"/"Abrasion Loss"/"Shore Hardness".

    build_qap_context() in qap_service.py walks the combined [item, *subrows]
    sequence per column and computes the actual rowspan/merge structure from
    this blank-vs-non-blank pattern, so the rendered PDF reproduces the same
    merged-cell layout as the source Excel instead of the previous behaviour
    of losing every non-first-row value except the sub-row's own bullet text.
    """
    id                 = models.AutoField(primary_key=True)
    item               = models.ForeignKey(QAPItem, on_delete=models.CASCADE,
                                           related_name='sub_rows_data')
    characteristic     = models.CharField(blank=True)
    check_class        = models.CharField(max_length=50,  blank=True)
    type_of_check      = models.CharField(blank=True)
    quantum_m          = models.CharField(max_length=200, blank=True)
    quantum_sc         = models.CharField(max_length=200, blank=True)
    reference_docs     = models.TextField(blank=True)
    acceptance_norms   = models.TextField(blank=True)
    format_of_records  = models.CharField(max_length=200, blank=True)
    agency             = models.CharField(max_length=100, blank=True)
    record_mark        = models.CharField(max_length=10, blank=True)
    remarks            = models.TextField(blank=True)
    sort_order         = models.PositiveIntegerField()   # 1-based position within the group

    class Meta:
        db_table = 'qap_item_sub_rows'
        managed  = True
        ordering = ['sort_order']

    def __str__(self):
        return f"{self.item.sn}#{self.sort_order} {self.characteristic[:50]}"


class QAPRecord(models.Model):
    """
    A generated QAP linked to one TDS record.

    tds is a real OneToOneField to TDSInput (on_delete=CASCADE — deleting a
    TDS deletes its generated QAP record too, matching what delete_tds()
    already effectively leaves behind as an orphan today). OneToOneField
    implies the same unique=True the old IntegerField declared explicitly;
    db_column stays 'tds_id' so the existing `tds_id=` filter kwarg in
    qap_service.py and the `self.tds_id` accessor keep working unchanged.

    PO No / PO Date are intentionally absent — those fields render as blank lines
    in the PDF for manual fill-in before dispatch. Revision defaults to '00'.
    doc_number is auto-set to 'QAP-{tds_number}' at generation time.
    """
    # See QAPTemplate.id above — same reasoning applies to every QAP model.
    id           = models.AutoField(primary_key=True)
    tds          = models.OneToOneField('TDSInput', on_delete=models.CASCADE,
                                        related_name='qap_record')
    template     = models.ForeignKey(QAPTemplate, on_delete=models.SET_NULL,
                                     null=True, related_name='records')
    doc_number   = models.CharField(max_length=100, blank=True)   # QAP-0042
    revision     = models.CharField(max_length=10,  default='00')
    generated_at = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'qap_records'
        managed  = True

    def __str__(self):
        return f"QAPRecord(tds_id={self.tds_id}, doc={self.doc_number})"
