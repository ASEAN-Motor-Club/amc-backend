from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0225_player_forced_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="ForcedNameLog",
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
                (
                    "action",
                    models.CharField(
                        choices=[("set", "Set"), ("clear", "Clear")], max_length=10
                    ),
                ),
                (
                    "old_name",
                    models.CharField(max_length=200, null=True, blank=True),
                ),
                (
                    "new_name",
                    models.CharField(max_length=200, null=True, blank=True),
                ),
                (
                    "actor_discord_id",
                    models.PositiveBigIntegerField(null=True, blank=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor_character",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.SET_NULL,
                        related_name="+",
                        to="amc.character",
                    ),
                ),
                (
                    "actor_player",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.SET_NULL,
                        related_name="+",
                        to="amc.player",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        related_name="forced_name_logs",
                        to="amc.player",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]