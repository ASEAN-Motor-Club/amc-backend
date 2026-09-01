from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0232_player_muted_until"),
    ]

    operations = [
        migrations.AddField(
            model_name="character",
            name="exclusive_progression",
            field=models.BooleanField(
                blank=True,
                help_text=(
                    "True = every level gain observed in play on this server "
                    "(armed when a character's entire level table is all-1); "
                    "False = the client showed levels above what observed play "
                    "left behind, i.e. the player leveled outside; "
                    "null = not tracked."
                ),
                null=True,
            ),
        ),
    ]
