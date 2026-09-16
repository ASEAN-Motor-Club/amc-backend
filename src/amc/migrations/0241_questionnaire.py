import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0240_merge_0239_leafs"),
    ]

    operations = [
        migrations.CreateModel(
            name="Questionnaire",
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
                ("title", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("questions", models.JSONField()),
                (
                    "response_mode",
                    models.CharField(
                        choices=[
                            ("single", "One response per user"),
                            ("multiple", "Multiple responses allowed"),
                        ],
                        default="single",
                        max_length=16,
                    ),
                ),
                ("created_by_discord_id", models.CharField(max_length=32)),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                ("closed", models.BooleanField(default=False)),
                ("channel_id", models.CharField(blank=True, max_length=32)),
                ("message_id", models.CharField(blank=True, max_length=32)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="QuestionnaireResponse",
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
                ("discord_user_id", models.CharField(max_length=32)),
                ("discord_username", models.CharField(max_length=100)),
                (
                    "answers",
                    models.JSONField(
                        help_text="List of chosen option strings, per question."
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "questionnaire",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="responses",
                        to="amc.questionnaire",
                    ),
                ),
            ],
        ),
    ]
