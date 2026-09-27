from django.db import migrations, models


def stamp_illegal(apps, schema_editor):
    """Existing enforcement anchor was tt_class: TT-classed events were the
    illegal races. Backfill them to illegal; everything else defaults legal.
    """
    GameEvent = apps.get_model("amc", "GameEvent")
    GameEvent.objects.filter(tt_class__isnull=False).update(race_legality="illegal")


def unstamp(apps, schema_editor):
    GameEvent = apps.get_model("amc", "GameEvent")
    GameEvent.objects.filter(race_legality="illegal").update(race_legality="legal")


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0260_wanted_mod_vehicles_allowed"),
    ]

    operations = [
        migrations.AddField(
            model_name="gameevent",
            name="race_legality",
            field=models.CharField(
                choices=[("legal", "Legal"), ("illegal", "Illegal")],
                default="legal",
                help_text="Illegal races get start-line DQ, the 60s announcement and the star Wanted. Defaults to legal — only events explicitly classed illegal (e.g. stamped with a TT class by the auto-poster) are enforced.",
                max_length=7,
            ),
        ),
        migrations.RunPython(stamp_illegal, unstamp),
    ]
