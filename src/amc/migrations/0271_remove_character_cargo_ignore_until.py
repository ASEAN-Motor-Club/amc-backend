from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0270_deliveryactivitylog"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="character",
            name="cargo_ignore_until",
        ),
    ]
