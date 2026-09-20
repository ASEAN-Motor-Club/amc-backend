from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0241_questionnaire"),
    ]

    operations = [
        migrations.AddField(
            model_name="character",
            name="criminal_score",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="character",
            name="last_illicit_delivery_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Decay clock anchor — set on every illicit cargo delivery.",
            ),
        ),
    ]
