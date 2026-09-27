from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0263_seed_police_whitelist"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="character",
            name="police_confiscated_total",
        ),
    ]
