import django.contrib.postgres.fields
import django.db.models.deletion
from django.db import migrations, models


def _default_underground_vehicle_types() -> list[str]:
    return ["Small", "Pickup"]


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0263_underground_racing"),
    ]

    operations = [
        migrations.AddField(
            model_name="ttclass",
            name="allowed_vehicle_types",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=32),
                default=_default_underground_vehicle_types,
                blank=True,
                help_text=(
                    "Vehicle types allowed in races of this class. Values are "
                    "gamedata vehicle_type strings (Small, Pickup, Truck, "
                    "SemiTractor, SemiTrailer, Bus, SmallTrailer, Bike, Kart, "
                    "HeavyMachinery, Racecar, Motorhome). Fail-closed: an "
                    "unmapped vehicle type is always a violation."
                ),
                size=None,
            ),
        ),
        migrations.AlterField(
            model_name="gameevent",
            name="tt_class",
            field=models.ForeignKey(
                blank=True,
                help_text="TT power class for this event instance (parsed from the [TT-…] name tag)",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="game_events",
                to="amc.ttclass",
                verbose_name="HP class",
            ),
        ),
        migrations.AlterField(
            model_name="scheduledevent",
            name="tt_class",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional pinned TT power class; auto-posted events override per instance",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="scheduled_events",
                to="amc.ttclass",
                verbose_name="HP class",
            ),
        ),
    ]
