# Generated manually — adds QAPTemplate, QAPSection, QAPItem, QAPRecord tables.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_alter_beltrating_options_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='QAPTemplate',
            fields=[
                ('id',           models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('category',     models.CharField(choices=[('GP','General Purpose'),('HR','Heat Resistant'),('FR_ISO','Fire Resistant (ISO)'),('OR','Oil Resistant'),('FR_CAN','Fire Resistant (CAN/NTPC)')], max_length=20, unique=True)),
                ('display_name', models.CharField(max_length=100)),
                ('is_active',    models.BooleanField(default=True)),
                ('created_at',   models.DateTimeField(auto_now_add=True)),
            ],
            options={'db_table': 'qap_templates', 'managed': True},
        ),
        migrations.CreateModel(
            name='QAPSection',
            fields=[
                ('id',           models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('section_code', models.CharField(max_length=10)),
                ('section_name', models.CharField(max_length=200)),
                ('sort_order',   models.PositiveIntegerField()),
                ('template',     models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sections', to='core.qaptemplate')),
            ],
            options={'db_table': 'qap_sections', 'managed': True, 'ordering': ['sort_order']},
        ),
        migrations.AddConstraint(
            model_name='qapsection',
            constraint=models.UniqueConstraint(fields=['template', 'section_code'], name='unique_template_section_code'),
        ),
        migrations.CreateModel(
            name='QAPItem',
            fields=[
                ('id',                models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sn',                models.CharField(max_length=20)),
                ('component',         models.CharField(max_length=300)),
                ('characteristic',    models.CharField(blank=True, max_length=300)),
                ('check_class',       models.CharField(blank=True, max_length=50)),
                ('type_of_check',     models.CharField(blank=True, max_length=200)),
                ('quantum_m',         models.CharField(blank=True, max_length=200)),
                ('quantum_sc',        models.CharField(blank=True, max_length=200)),
                ('reference_docs',    models.TextField(blank=True)),
                ('acceptance_norms',  models.TextField(blank=True)),
                ('format_of_records', models.CharField(blank=True, max_length=200)),
                ('agency',            models.CharField(blank=True, max_length=100)),
                ('remarks',           models.TextField(blank=True)),
                ('is_static',         models.BooleanField(default=False)),
                ('sort_order',        models.PositiveIntegerField()),
                ('section',           models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='core.qapsection')),
            ],
            options={'db_table': 'qap_items', 'managed': True, 'ordering': ['sort_order']},
        ),
        migrations.CreateModel(
            name='QAPRecord',
            fields=[
                ('id',           models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tds_id',       models.IntegerField(db_index=True, unique=True)),
                ('doc_number',   models.CharField(blank=True, max_length=100)),
                ('revision',     models.CharField(default='00', max_length=10)),
                ('generated_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at',   models.DateTimeField(auto_now=True)),
                ('template',     models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='records', to='core.qaptemplate')),
            ],
            options={'db_table': 'qap_records', 'managed': True},
        ),
    ]
