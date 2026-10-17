import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0232_player_muted_until"),
    ]

    operations = [
        migrations.CreateModel(
            name="ServerVehicleDespawnLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("timestamp", models.DateTimeField()),
                ("hook", models.CharField(max_length=50)),
                ("vehicle_game_id", models.BigIntegerField(blank=True, null=True)),
                (
                    "vehicle_name",
                    models.CharField(blank=True, max_length=100, null=True),
                ),
                ("data", models.JSONField(blank=True, null=True)),
                (
                    "character",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="vehicle_despawn_logs",
                        to="amc.character",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="vehicle_despawn_logs",
                        to="amc.player",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["character", "-timestamp"],
                        name="amc_serverve_charact_9f2ab1_idx",
                    ),
                    models.Index(
                        fields=["vehicle_game_id"],
                        name="amc_serverve_vehicl_3c81de_idx",
                    ),
                ],
            },
        ),
    ]
