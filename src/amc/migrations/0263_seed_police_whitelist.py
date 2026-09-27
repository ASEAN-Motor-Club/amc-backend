from django.db import migrations
from django.db.models import Sum


def seed_whitelist(apps, schema_editor):
    """Seed one PoliceWhitelist row per player who has ever served.

    Seed criteria: any PoliceSession on any of the player's characters, or any
    character with a non-zero confiscation total. The total is the SUM across
    the player's characters (freeman, 2026-09-27) — lifetime confiscations.

    Three queries total: session players, per-player SUM aggregation, bulk
    insert (ignore_conflicts keeps any pre-existing rows intact).
    """
    Character = apps.get_model("amc", "Character")
    PoliceSession = apps.get_model("amc", "PoliceSession")
    PoliceWhitelist = apps.get_model("amc", "PoliceWhitelist")

    session_players = set(
        PoliceSession.objects.values_list("character__player_id", flat=True)
    )
    totals = (
        Character.objects.filter(player_id__in=session_players)
        .values("player_id")
        .annotate(total_sum=Sum("police_confiscated_total"))
    )
    PoliceWhitelist.objects.bulk_create(
        (
            PoliceWhitelist(
                player_id=row["player_id"],
                police_confiscated_total=row["total_sum"] or 0,
            )
            for row in totals
        ),
        ignore_conflicts=True,
    )


def unseed(apps, schema_editor):
    apps.get_model("amc", "PoliceWhitelist").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0262_police_whitelist"),
    ]

    operations = [
        migrations.RunPython(seed_whitelist, unseed),
    ]
