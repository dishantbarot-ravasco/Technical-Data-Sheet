from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_qap_models'),
    ]

    operations = [
        migrations.AlterField(
            model_name='qapitem',
            name='component',
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name='qapitem',
            name='characteristic',
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name='qapitem',
            name='type_of_check',
            field=models.TextField(blank=True),
        ),
    ]