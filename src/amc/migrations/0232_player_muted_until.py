from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0231_alter_namemoderationlog_action_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="muted_until",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "Absolute time the player's mute expires, or the permanent "
                    "sentinel (year 9999) for a permanent mute. Re-applied to "
                    "the mod on login."
                ),
                null=True,
            ),
        ),
    ]
