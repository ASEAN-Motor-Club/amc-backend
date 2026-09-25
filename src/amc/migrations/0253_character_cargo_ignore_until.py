from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0252_storagesnapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="character",
            name="cargo_ignore_until",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "Cargo arrivals are ignored until this timestamp (set by a "
                    "vehicle reset near a delivery point; expires on its own)."
                ),
                null=True,
            ),
        ),
    ]
