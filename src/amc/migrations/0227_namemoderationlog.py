from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0226_forcednamelog"),
    ]

    operations = [
        migrations.CreateModel(
            name="NameModerationLog",
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
                ("base_name", models.CharField(max_length=64)),
                (
                    "verdict_source",
                    models.CharField(
                        choices=[
                            ("blocklist", "Blocklist"),
                            ("llm", "Llm"),
                            ("cache", "Cache"),
                            ("error", "Error"),
                        ],
                        max_length=16,
                    ),
                ),
                ("is_violation", models.BooleanField()),
                ("confidence", models.FloatField(default=0.0)),
                (
                    "categories",
                    ArrayField(
                        models.CharField(max_length=32), blank=True, default=list
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("rename", "Rename"),
                            ("none", "None"),
                            ("manual_review", "Manual Review"),
                        ],
                        default="none",
                        max_length=16,
                    ),
                ),
                (
                    "suggested_name",
                    models.CharField(max_length=64, null=True, blank=True),
                ),
                ("llm_model", models.CharField(max_length=64, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "character",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.SET_NULL,
                        related_name="+",
                        to="amc.character",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        related_name="name_moderation_logs",
                        to="amc.player",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]