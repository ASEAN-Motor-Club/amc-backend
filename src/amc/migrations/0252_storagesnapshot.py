from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0251_pending_wallet_payout"),
    ]

    operations = [
        migrations.CreateModel(
            name="StorageSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("kind", models.CharField(choices=[("IN", "Input"), ("OU", "Output")], max_length=2)),
                ("cargo_key", models.CharField(db_index=True, max_length=200)),
                ("amount", models.PositiveIntegerField()),
                ("capacity", models.PositiveIntegerField(blank=True, null=True)),
                ("captured_at", models.DateTimeField(db_index=True)),
                (
                    "delivery_point",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="storage_snapshots",
                        to="amc.deliverypoint",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["delivery_point", "cargo_key", "captured_at"],
                        name="idx_storagesnap_point_cargo_ts",
                    ),
                ]
            },
        ),
    ]
