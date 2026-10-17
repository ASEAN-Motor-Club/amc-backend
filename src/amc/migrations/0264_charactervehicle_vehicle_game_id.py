from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0263_underground_racing"),
    ]

    operations = [
        migrations.AddField(
            model_name="charactervehicle",
            name="vehicle_game_id",
            field=models.BigIntegerField(
                blank=True,
                db_index=True,
                help_text=(
                    "7-digit game-assigned vehicle id from the bought/entered "
                    "vehicle log lines. Unlike vehicle_id (a recycled runtime "
                    "counter) this is the closest thing to a durable "
                    "per-vehicle identity; still stamped by name, so it follows "
                    "the most recent bought/entered match."
                ),
                null=True,
            ),
        ),
    ]
