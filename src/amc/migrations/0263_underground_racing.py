from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0262_alter_wanted_origin"),
    ]

    operations = [
        migrations.AddField(
            model_name="character",
            name="respect",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="scheduledevent",
            name="is_rotation_instance",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    "True = SE row mirrored from an auto-posted event instance "
                    "(never a rotation candidate); False = hand-made template."
                ),
            ),
        ),
        migrations.AddField(
            model_name="gameevent",
            name="rewards_paid",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    "True once the underground rotation-end payout settled this "
                    "event (idempotency marker for the Blood Money/Respect payout)."
                ),
            ),
        ),
    ]
