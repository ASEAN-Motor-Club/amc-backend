from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0229_namewhitelist"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpostingconfig",
            name="min_active_jobs",
            field=models.PositiveIntegerField(
                default=3,
                help_text=(
                    "Floor on the target number of active jobs. Even with very "
                    "few players, the board keeps at least this many jobs to "
                    "choose from."
                ),
            ),
        ),
    ]