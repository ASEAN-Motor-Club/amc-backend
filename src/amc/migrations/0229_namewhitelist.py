from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0228_namemoderationlog_reason"),
    ]

    operations = [
        migrations.CreateModel(
            name="NameWhitelist",
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
                ("name", models.CharField(max_length=64)),
                (
                    "added_by",
                    models.PositiveBigIntegerField(null=True, blank=True),
                ),
                ("reason", models.CharField(blank=True, max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="name_whitelists",
                        to="amc.player",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="namewhitelist",
            constraint=models.UniqueConstraint(
                fields=("player", "name"),
                name="unique_name_whitelist_per_player",
            ),
        ),
    ]
