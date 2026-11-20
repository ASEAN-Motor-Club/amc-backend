from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0268_playerstatuslog_host"),
    ]

    operations = [
        migrations.AddField(
            model_name="character",
            name="login_streak",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="character",
            name="last_login_reward_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
