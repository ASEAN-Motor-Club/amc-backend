import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0264_ttclass_allowed_vehicle_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="scheduledevent",
            name="allowed_vehicle_types",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=32),
                blank=True,
                help_text=(
                    "Per-event override of allowed vehicle types; empty = "
                    "inherit the pinned HP class's list"
                ),
                null=True,
                size=None,
            ),
        ),
    ]
