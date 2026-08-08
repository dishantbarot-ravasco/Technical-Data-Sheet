# Generated manually — replaces mandatory TOTP with device-aware email OTP.
# Depends on 0003_usertotp (which created user_totp).
#
# Operations:
#   1. Create trusted_devices table (TrustedDevice model)
#   2. Drop user_totp table (UserTOTP model — superseded)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_usertotp'),
    ]

    operations = [
        migrations.CreateModel(
            name='TrustedDevice',
            fields=[
                ('id',           models.AutoField(primary_key=True, serialize=False)),
                ('user_id',      models.IntegerField(db_index=True)),
                ('device_token', models.CharField(max_length=64, unique=True)),
                ('device_name',  models.TextField(blank=True, default='')),
                ('ip_address',   models.GenericIPAddressField(blank=True, null=True)),
                ('created_at',   models.DateTimeField(auto_now_add=True)),
                ('last_used_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'trusted_devices',
                'managed': True,
            },
        ),
        migrations.DeleteModel(
            name='UserTOTP',
        ),
    ]
