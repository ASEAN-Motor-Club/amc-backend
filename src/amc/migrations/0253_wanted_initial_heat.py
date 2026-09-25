"""Wanted.initial_heat — the heat value a chase was issued at.

Scale-with-delivery (freeman 2026-09-25): a chase is issued at
max(5, delivery // 100_000) stars; initial_heat records the issued heat
(stars × LEVEL_PER_STAR) so the mid-chase running-growth cap matches the
issued chase instead of the 5★ floor (600).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0252_storagesnapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="wanted",
            name="initial_heat",
            field=models.IntegerField(
                default=600,
                help_text=(
                    "Heat value this chase was issued at (stars × "
                    "LEVEL_PER_STAR). Caps mid-chase running-heat regrowth; "
                    "never re-priced by decay."
                ),
            ),
        ),
    ]
