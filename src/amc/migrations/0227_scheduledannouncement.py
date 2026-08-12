from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0226_forcednamelog"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScheduledAnnouncement",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "message",
                    models.TextField(
                        help_text="Text shown as the server's pinned announcement (`/ap`)."
                    ),
                ),
                (
                    "scheduled_at",
                    models.DateTimeField(
                        help_text="First time this announcement goes live."
                    ),
                ),
                (
                    "repeat",
                    models.CharField(
                        choices=[
                            ("none", "None"),
                            ("hourly", "Hourly"),
                            ("daily", "Daily"),
                            ("weekly", "Weekly"),
                            ("monthly", "Monthly"),
                        ],
                        default="none",
                        help_text="How often the announcement re-occurs after the first fire.",
                        max_length=10,
                    ),
                ),
                (
                    "active_minutes",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text=(
                            "How long each occurrence stays live (minutes). Blank = stays "
                            "live until the next occurrence."
                        ),
                        null=True,
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(default=True),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True),
                ),
            ],
            options={
                "ordering": ["scheduled_at"],
                "verbose_name": "Scheduled Announcement",
                "verbose_name_plural": "Scheduled Announcements",
            },
        ),
    ]
