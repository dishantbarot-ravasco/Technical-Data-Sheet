# Generated manually on 2026-08-29 — data migration, no schema change.
#
# Fixes a data bug found while validating the FR_ISO QAP template against a
# reference PDF: item "1.10 Textile Fabric" is stored with sn='1.1' instead
# of '1.10' in all three seeded QAP templates (GP, HR, FR_ISO), colliding
# with item "1.1 Raw Rubber". Root cause: the source SAMPLE_QAP.xlsx cell for
# that SN is stored as a numeric value (1.1) rather than text, so
# seed_qap_templates.py's str(cell.value) faithfully read an already-lossy
# "1.10" -> 1.1 -> "1.1". This migration corrects the three existing rows
# directly rather than re-running the seed command (which would wipe and
# re-import every section/item for all three templates via --replace) --
# every other field on these rows is already correct, only the sn is wrong.
#
# The source spreadsheet itself should still be fixed (that cell re-typed as
# text "1.10") before anyone runs seed_qap_templates again, or this bug will
# reappear on the next --replace.

from django.db import migrations


def fix_textile_fabric_sn(apps, schema_editor):
    QAPItem = apps.get_model('core', 'QAPItem')
    QAPItem.objects.filter(sn='1.1', component='Textile Fabric').update(sn='1.10')


def noop_reverse(apps, schema_editor):
    # Deliberately not reversible to the buggy state -- reversing this
    # migration would just reintroduce the SN collision.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_tdsinput_current_revision_tdsrevision'),
    ]

    operations = [
        migrations.RunPython(fix_textile_fabric_sn, noop_reverse),
    ]
