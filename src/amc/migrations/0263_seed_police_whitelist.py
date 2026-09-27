from django.db import migrations


def seed_whitelist(apps, schema_editor):
    """Seed one PoliceWhitelist row per player who has ever served.

    Seed criteria: any PoliceSession on any of the player's characters, or any
    character with a non-zero confiscation total. The total is the SUM across
    the player's characters (freeman, 2026-09-27) — lifetime confiscations.
    """
    Player = apps.get_model("amc", "Player")
    Character = apps.get_model("amc", "Character")
    PoliceSession = apps.get_model("amc", "PoliceSession")
    PoliceWhitelist = apps.get_model("amc", "PoliceWhitelist")

    session_players = set(
        PoliceSession.objects.values_list("character__player_id", flat=True)
    )
    confiscating_players = set(
        Character.objects.filter(police_confiscated_total__gt=0)
        .values_list("player_id", flat=True)
    )
    for player_id in sorted(session_players | confiscating_players):
        total = sum(
            Character.objects.filter(player_id=player_id)
            .exclude(police_confiscated_total__isnull=True)
            .values_list("police_confiscated_total", flat=True)
        )
        PoliceWhitelist.objects.get_or_create(
            player_id=player_id,
            defaults={"police_confiscated_total": total},
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
