from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc_finance", "0009_bankpolicy"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankpolicy",
            name="gov_salary_multiplier",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("2.000"),
                help_text="UBI multiplier applied to Government Salary / Police Salary (default 2 = gov employees and on-duty police earn 2x UBI).",
                max_digits=6,
            ),
        ),
    ]
