from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc_finance", "0007_fix_reserves_wealth_tax_entries"),
    ]

    operations = [
        migrations.CreateModel(
            name="BankPolicy",
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
                    "daily_interest_rate",
                    models.DecimalField(
                        decimal_places=6,
                        default=Decimal("0.022"),
                        help_text="Nominal daily interest rate applied to bank balances (e.g. 0.022 = 2.2%/day). Fraction, not percent.",
                        max_digits=8,
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "Bank policy",
            },
        ),
    ]
