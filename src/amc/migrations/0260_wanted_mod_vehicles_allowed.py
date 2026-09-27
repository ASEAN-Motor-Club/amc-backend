from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0259_wanted_origin"),
    ]

    operations = [
        migrations.AddField(
            model_name="wanted",
            name="mod_vehicles_allowed",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When True, the wanted-tick modded-vehicle despawn pass "
                    "skips this record (Yuuka 2026-09-27): the race "
                    "enforcement must not despawn players' modded vehicles. "
                    "Anchored to the event-race origin but kept as its own "
                    "field so other future origins can opt in per record."
                ),
            ),
        ),
    ]
