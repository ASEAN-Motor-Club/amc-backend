import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0244_criminal_record_removal"),
    ]

    operations = [
        migrations.CreateModel(
            name="PendingWanted",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("apply_at", models.DateTimeField()),
                (
                    "trigger_amount",
                    models.BigIntegerField(
                        default=0,
                        help_text=(
                            "Accumulated illicit delivery amount that fired the "
                            "trigger (used for the laundered announce when the "
                            "wanted applies)."
                        ),
                    ),
                ),
                (
                    "character",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pending_wanted_records",
                        to="amc.character",
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "pending wants",
            },
        ),
    ]
