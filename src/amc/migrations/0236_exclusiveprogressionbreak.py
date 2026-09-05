from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0235_character_exclusive_progression"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExclusiveProgressionBreak",
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
                ("level_field", models.CharField(max_length=30)),
                ("stored_level", models.PositiveIntegerField(null=True)),
                ("seen_level", models.PositiveIntegerField()),
                ("detected_at", models.DateTimeField(auto_now_add=True)),
                (
                    "character",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="progression_breaks",
                        to="amc.character",
                    ),
                ),
            ],
            options={
                "verbose_name": "exclusive progression break",
                "verbose_name_plural": "exclusive progression breaks",
            },
        ),
    ]
