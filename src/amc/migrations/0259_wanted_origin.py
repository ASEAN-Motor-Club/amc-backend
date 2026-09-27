"""Wanted.origin — trigger-source marker, fugitive-passenger amnesty carve-out.

Guild fugitive-passenger triggers (Hamster 2026-09-27) fire regardless of
police presence; their Wanted rows carry origin='fugitive_passenger' and are
exempt from the dormant amnesty so they aren't wiped the next tick when zero
cops are on duty.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0258_wanted_chase_quality"),
    ]

    operations = [
        migrations.AddField(
            model_name="wanted",
            name="origin",
            field=models.CharField(
                blank=True,
                help_text=(
                    "What triggered this wanted: NULL = illicit cargo, "
                    "'fugitive_passenger' = guild fugitive passenger. "
                    "Fugitive-origin records are exempt from the dormant "
                    "amnesty — they persist through dormant ticks and resume "
                    "normal decay once a cop is back on duty (Hamster "
                    "2026-09-27)."
                ),
                max_length=32,
                null=True,
            ),
        ),
    ]
