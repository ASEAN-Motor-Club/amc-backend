# Compass cadence rework (freeman 2026-09-27): the parked-suspect distance
# law changes from the reciprocal `1/(D·20·c)` (which collapsed to the
# min_interval floor when FAR away — the clamp inverted past ~1.1 km) to
# `max_interval * (1 - (1 - far_mult) * w)`: near+parked is now the SLOWEST
# case, distance only speeds updates up. `c` is retired; `far_mult` replaces
# it. Existing rows move to the new config-A defaults (far_mult 0.25,
# max_interval 20 s) so the live cadence follows the new law on deploy.

from django.db import migrations, models


def reseed_rows(apps, schema_editor):
    CompassTuningConfig = apps.get_model("amc", "CompassTuningConfig")
    CompassTuningConfig.objects.all().update(
        far_mult=0.25, max_interval=20.0
    )


class Migration(migrations.Migration):

    dependencies = [
        ('amc', '0261_gameevent_race_legality'),
    ]

    operations = [
        migrations.AddField(
            model_name='compasstuningconfig',
            name='far_mult',
            field=models.FloatField(
                default=0.25,
                help_text=(
                    "Parked-far base as a fraction of max_interval: base = "
                    "max_interval * (1 - (1 - far_mult) * w), w saturating "
                    "0→1 with distance past the 500 m near cap. Must be in "
                    "(0, 1]."
                ),
            ),
        ),
        migrations.AlterField(
            model_name='compasstuningconfig',
            name='max_interval',
            field=models.FloatField(
                default=20.0,
                help_text=(
                    "SOLO parked-near ceiling in seconds (also the <200m "
                    "close-ping interval)."
                ),
            ),
        ),
        migrations.RunPython(reseed_rows, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='compasstuningconfig',
            name='c',
        ),
    ]
