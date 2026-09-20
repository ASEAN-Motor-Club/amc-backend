from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0243_criminal_score_carryforward"),
    ]

    operations = [
        migrations.RemoveField(model_name="delivery", name="criminal_record"),
        migrations.RemoveField(
            model_name="character", name="criminal_laundered_total"
        ),
        migrations.DeleteModel(name="CriminalRecord"),
    ]
