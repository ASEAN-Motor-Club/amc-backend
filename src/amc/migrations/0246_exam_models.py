import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0245_pending_wanted"),
    ]

    operations = [
        migrations.CreateModel(
            name="Exam",
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
                    "pass_mark",
                    models.PositiveSmallIntegerField(default=80),
                ),
                (
                    "max_attempts",
                    models.PositiveSmallIntegerField(default=1),
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
            name="ExamAttempt",
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
                        help_text="List of submitted answers, per question."
                    ),
                ),
                ("correct", models.PositiveSmallIntegerField()),
                ("graded", models.PositiveSmallIntegerField()),
                (
                    "score",
                    models.PositiveSmallIntegerField(help_text="Percent 0-100."),
                ),
                ("passed", models.BooleanField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "exam",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attempts",
                        to="amc.exam",
                    ),
                ),
            ],
        ),
    ]
