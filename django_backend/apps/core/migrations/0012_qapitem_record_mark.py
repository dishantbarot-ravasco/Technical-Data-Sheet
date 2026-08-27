from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_qap_item_textfields'),
    ]

    operations = [
        migrations.AddField(
            model_name='qapitem',
            name='record_mark',
            field=models.CharField(max_length=10, blank=True, default=''),
        ),
    ]
