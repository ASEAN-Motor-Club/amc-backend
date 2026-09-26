"""Wanted.chase_quality — chase-quality meter for the evasion bonus.

Replaces the flat +10% criminal-score evasion bonus (freeman 2026-09-20)
with a metered one (freeman 2026-09-26): the meter accrues each tick from
cop proximity and suspect speed while an organic wanted is active, and the
evasion pays EVASION_MAX_BONUS (10%) × the meter, 0–10%.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0257_merge_ttclass_master"),
    ]

    operations = [
        migrations.AddField(
            model_name="wanted",
            name="chase_quality",
            field=models.FloatField(
                default=0.0,
                help_text=(
                    "Chase-quality meter in [0, 1] (freeman 2026-09-26): "
                    "accrues each tick from cop proximity + speed while an "
                    "organic wanted is active. On evasion the criminal-score "
                    "bonus is EVASION_MAX_BONUS × this meter instead of a "
                    "flat 10%."
                ),
            ),
        ),
    ]
