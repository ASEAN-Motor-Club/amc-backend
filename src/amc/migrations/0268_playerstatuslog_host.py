from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0267_delivery_quote_cache"),
    ]

    operations = [
        migrations.AddField(
            model_name="playerstatuslog",
            name="host",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Game-server host that emitted the login event. Used so a "
                    "restart on one host closes only its own sessions. Empty "
                    "for legacy rows written before the column existed."
                ),
                max_length=100,
            ),
        ),
    ]
