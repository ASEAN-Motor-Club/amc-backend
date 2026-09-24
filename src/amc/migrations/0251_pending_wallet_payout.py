import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0250_character_no_teleport"),
    ]

    operations = [
        migrations.CreateModel(
            name="PendingWalletPayout",
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
                    "amount",
                    models.PositiveBigIntegerField(
                        help_text="Bank -> wallet amount in game currency."
                    ),
                ),
                (
                    "reason",
                    models.CharField(
                        max_length=200,
                        help_text="Short audit label shown in the ledger description.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "booked_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        help_text="Set once the ledger withdrawal leg is booked.",
                    ),
                ),
                (
                    "paid_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        help_text="Set once the wallet transfer succeeded.",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pending_wallet_payouts",
                        to="amc.player",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
    ]
