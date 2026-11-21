from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0269_character_daily_login_reward"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeliveryActivityLog",
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
                ("timestamp", models.DateTimeField(db_index=True)),
                (
                    "kind",
                    models.CharField(
                        choices=[("accepted", "Accepted"), ("picked_up", "Picked up")],
                        db_index=True,
                        max_length=20,
                    ),
                ),
                (
                    "delivery_id",
                    models.BigIntegerField(blank=True, db_index=True, null=True),
                ),
                (
                    "cargo_key",
                    models.CharField(
                        blank=True, db_index=True, max_length=200, null=True
                    ),
                ),
                ("payment", models.PositiveBigIntegerField(blank=True, null=True)),
                ("weight", models.FloatField(blank=True, null=True)),
                ("data", models.JSONField(blank=True, null=True)),
                (
                    "character",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="delivery_activities",
                        to="amc.character",
                    ),
                ),
                (
                    "destination_point",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="delivery_activities_in",
                        to="amc.deliverypoint",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="activities",
                        to="amc.deliveryjob",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="delivery_activities",
                        to="amc.player",
                    ),
                ),
                (
                    "sender_point",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="delivery_activities_out",
                        to="amc.deliverypoint",
                    ),
                ),
            ],
            options={
                "ordering": ["-timestamp"],
                "indexes": [
                    models.Index(
                        fields=["delivery_id", "kind"],
                        name="del_activity_delivery_kind_idx",
                    ),
                    models.Index(
                        fields=["job", "timestamp"], name="del_activity_job_ts_idx"
                    ),
                ],
            },
        ),
    ]
