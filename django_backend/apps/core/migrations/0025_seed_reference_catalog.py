"""
apps/core/migrations/0025_seed_reference_catalog.py

Seeds the ~30 reference/lookup tables every TDS is actually built from
(standards, cover grades, belt ratings, fabric styles, test methods,
splice/sampling lookups, QAP templates, ...) — see fixtures/0025_reference_catalog.json.

WHY THIS EXISTS: 0001_initial.py creates these tables' *schema* (see its own
comment on why several of them were flipped managed=False -> True across
migrations 0008/0009/0014), but never their *data*. That data has existed,
until now, only in whichever Postgres instance happened to be provisioned
outside Django when this app was first stood up — there was no reproducible
way to populate a brand-new database (a fresh Render deploy, a new
developer's local DB, CI's from-scratch Postgres container) with anything
beyond empty tables. Every dropdown in generate-tds.html would render empty
and no TDS could ever be created. This migration closes that gap by loading
a one-time export of the current (authoritative) catalog as a normal,
version-controlled part of migration history — it runs automatically inside
build.sh's existing `migrate` step, same as every other migration here.

WHY bulk_create() INSTEAD OF loaddata/save(): PurposeBeltType, BrandBeltType,
and BrandParameter each declare their brand/purpose FK as
`OneToOneField(primary_key=True)` in models.py, which loaddata's Model.save()
would treat as the WHOLE identity and silently overwrite same-brand/purpose
rows instead of inserting them alongside each other (this is exactly the
schema/model mismatch 0024_fix_junction_table_composite_pks.py fixes at the
database level — see that migration's docstring for the full story).
bulk_create() issues plain multi-row INSERTs with no existence check, so it
round-trips every row correctly regardless of that mismatch, and works
correctly for every other table here too.

IDEMPOTENT BY DESIGN: guarded on `Purpose.objects.exists()` so it's a safe
no-op if ever re-run against a database that already has this data (Django's
migration-state tracking already prevents that under normal operation, since
a migration only runs once per database — this guard is defense-in-depth).
Safe to run for real everywhere, including this project's own database
(where it correctly detects existing data and does nothing).

Insertion order follows the FK dependency graph (Purpose/BeltType/IndusBrand
before the join tables that reference them, TDSParameter before every
*Value/*ParameterValue table that references it, etc.) because, unlike
loaddata, bulk_create does NOT disable constraint checking — Postgres
enforces each FK the moment a row is inserted, so parents must exist first.
"""
import os

from django.core.serializers import deserialize
from django.db import migrations, models


FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), 'fixtures', '0025_reference_catalog.json',
)

# FK-dependency order — a model must appear after every model it references.
MODEL_ORDER = [
    'core.purpose', 'core.belttype', 'core.indusbrand',
    'core.purposebelttype', 'core.brandbelttype',
    'core.standard', 'core.tdsparameter', 'core.brandparameter',
    'core.standardtestmethod',
    'core.covergrade', 'core.covergradevalue',
    'core.fabrictype', 'core.fabrictypeparametervalue',
    'core.fabricstyle', 'core.fabricstyleparametervalue',
    'core.beltrating', 'core.beltratingvalue',
    'core.reeltype', 'core.packingtype', 'core.containertype',
    'core.regioncontainerweightlimit',
    'core.dimensionalparameterspec',
    'core.splicesteplookup', 'core.hotsplicecuringlookup',
    'core.constructiontype', 'core.splicemethodconfig', 'core.samplingplanlookup',
    'core.qaptemplate', 'core.qapsection', 'core.qapitem', 'core.qapitemsubrow',
]


def load_reference_catalog(apps, schema_editor):
    Purpose = apps.get_model('core', 'Purpose')
    if Purpose.objects.exists():
        # Already seeded (e.g. this is the DB the fixture was exported
        # from) — nothing to do. See module docstring.
        return

    with open(FIXTURE_PATH, encoding='utf-8') as f:
        raw = f.read()

    # django.core.serializers.deserialize resolves "model": "core.x" against
    # the CURRENT (non-historical) app registry, so this bypasses the `apps`
    # argument RunPython normally uses. That's fine here: this is the newest
    # migration in the app, so "current" and "historical" shapes for these
    # models are identical at this point in history.
    by_model = {name: [] for name in MODEL_ORDER}
    for deserialized in deserialize('json', raw):
        key = deserialized.object._meta.label_lower
        by_model[key].append(deserialized.object)

    for name in MODEL_ORDER:
        objs = by_model[name]
        if objs:
            objs[0].__class__.objects.bulk_create(objs)

    # RELIABILITY: bulk_create() above inserts explicit PK values straight
    # from the fixture (e.g. BeltRatingValue.id=1..1626) without touching
    # each table's own Postgres identity sequence, which still starts at 1.
    # The very next ORM-driven insert into one of these tables (a test
    # factory, or later real app usage of an AutoField-keyed model) would
    # collide with a seeded row instead of getting a fresh id. Business-key
    # tables (Purpose, Standard, TDSParameter, ...) and the pure-composite
    # junction tables (PurposeBeltType, BrandBeltType, BrandParameter) have
    # no DB-side auto-increment at all (see CLAUDE.md) and are skipped here
    # since there's no sequence to advance.
    with schema_editor.connection.cursor() as cursor:
        for name in MODEL_ORDER:
            objs = by_model[name]
            if not objs:
                continue
            model = objs[0].__class__
            pk_field = model._meta.pk
            if not isinstance(pk_field, (models.AutoField, models.BigAutoField, models.SmallAutoField)):
                continue
            table = model._meta.db_table
            column = pk_field.column
            quoted_table = schema_editor.connection.ops.quote_name(table)
            quoted_column = schema_editor.connection.ops.quote_name(column)
            cursor.execute(
                f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                f"(SELECT MAX({quoted_column}) FROM {quoted_table}), true)",
                [table, column],
            )


def noop_reverse(apps, schema_editor):
    # Deliberately not reversible — this is reference/catalog data that
    # real TDS records may already point to by the time anyone considers
    # rolling this migration back; blowing it away is far more dangerous
    # than leaving it in place. Matches 0020_fix_qap_textile_fabric_sn's
    # same noop_reverse choice for the same reason.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_fix_junction_table_composite_pks'),
    ]

    operations = [
        migrations.RunPython(load_reference_catalog, noop_reverse),
    ]
