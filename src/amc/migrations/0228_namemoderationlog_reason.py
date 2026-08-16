from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0227_namemoderationlog"),
    ]

    operations = [
        migrations.AddField(
            model_name="namemoderationlog",
            name="reason",
            field=models.CharField(blank=True, max_length=1000),
        ),
    ]