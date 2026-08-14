from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0224_add_0_7_19_vehicles"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="forced_name",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Admin-imposed display name that overrides the player's chosen "
                    "name across all their characters. Cleared by /clear_forced_name."
                ),
                max_length=200,
                null=True,
            ),
        ),
    ]
