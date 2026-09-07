from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0237_alter_namemoderationlog_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="worldobject",
            name="tag",
            field=models.CharField(blank=True, max_length=256, null=True),
        ),
        migrations.AddField(
            model_name="worldobject",
            name="notes",
            field=models.TextField(blank=True),
        ),
    ]
