from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc_finance", "0010_govsalarymultiplier"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankpolicy",
            name="wealth_tax_multiplier",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("1.000"),
                help_text="Global multiplier applied to the computed wealth tax "
                "(default 1 = brackets unchanged; 0 disables the wealth tax).",
                max_digits=6,
            ),
        ),
    ]
