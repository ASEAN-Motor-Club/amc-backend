from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0216_guild_passenger_fugitive_chance"),
    ]

    operations = [
        migrations.AddField(
            model_name="serverstatus",
            name="fd_total",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Total open file descriptors across motortown wineserver + GameThread",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="serverstatus",
            name="fd_max_num",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Highest FD number across motortown processes (FD_SETSIZE limit = 1024)",
                null=True,
            ),
        ),
    ]
