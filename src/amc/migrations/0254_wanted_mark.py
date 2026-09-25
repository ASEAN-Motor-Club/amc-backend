from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0253_character_cargo_ignore_until"),
    ]

    operations = [
        migrations.AddField(
            model_name="character",
            name="marked_wanted_until",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "Wanted-mark flag: the next illicit delivery before this "
                    "timestamp triggers a Wanted with certainty (cop-proximity "
                    "attenuation still applies). Set by /markwanted; expires on "
                    "its own and is cleared once the marked delivery triggers."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="wantedsystemconfig",
            name="markwanted_ttl_minutes",
            field=models.PositiveIntegerField(
                default=60,
                help_text=(
                    "How long (minutes) a /markwanted flag stays armed on a "
                    "character before expiring on its own."
                ),
            ),
        ),
    ]
