# Migration 0005 — fix Django W342 warnings on junction table models.
#
# Changes ForeignKey(primary_key=True) → OneToOneField(primary_key=True) for
# three managed=False junction models:
#   - PurposeBeltType.purpose
#   - BrandBeltType.brand
#   - BrandParameter.brand
#
# Because all three models are managed=False, Django generates NO SQL for this
# migration — it only updates the internal migration state so system checks
# stop emitting W342 warnings.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_trusted_device'),
    ]

    operations = [
        # PurposeBeltType.purpose: ForeignKey → OneToOneField
        migrations.AlterField(
            model_name='purposebelttype',
            name='purpose',
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                primary_key=True,
                related_name='belt_type_links',
                serialize=False,
                to='core.purpose',
            ),
        ),
        # BrandBeltType.brand: ForeignKey → OneToOneField
        migrations.AlterField(
            model_name='brandbelttype',
            name='brand',
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                primary_key=True,
                related_name='belt_type_links',
                serialize=False,
                to='core.indusbrand',
            ),
        ),
        # BrandParameter.brand: ForeignKey → OneToOneField
        migrations.AlterField(
            model_name='brandparameter',
            name='brand',
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                primary_key=True,
                related_name='brand_parameters',
                serialize=False,
                to='core.indusbrand',
            ),
        ),
    ]
