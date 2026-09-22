# Generated for the wanted-system police-required toggle (WantedSystemConfig)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('amc', '0247_compasstuningconfig'),
    ]

    operations = [
        migrations.CreateModel(
            name='WantedSystemConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('police_required', models.BooleanField(default=True)),
            ],
            options={
                'verbose_name': 'Wanted System Configuration',
                'verbose_name_plural': 'Wanted System Configuration',
            },
        ),
    ]
