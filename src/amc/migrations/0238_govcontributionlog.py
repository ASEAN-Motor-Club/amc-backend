import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0237_alter_namemoderationlog_action"),
    ]

    operations = [
        migrations.CreateModel(
            name="GovContributionLog",
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
                    "contribution",
                    models.PositiveBigIntegerField(),
                ),
                (
                    "timestamp",
                    models.DateTimeField(db_index=True, default=django.utils.timezone.now),
                ),
                (
                    "character",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="gov_contribution_logs",
                        to="amc.character",
                    ),
                ),
            ],
        ),
    ]
