"""Move daily login streak state from character to Steam account.

A Steam account normally owns several character rows, and a login can resolve
to a different one of them (stale GUID, character switch). Streak state on the
character therefore produced spurious "day 1" resets for the same player.
The live state now lives on ``amc_player`` (keyed by SteamID64). Existing
per-character values are backfilled from the character with the most recent
claim (tie-break: highest streak). The legacy character columns stay in place
for rollback safety but are no longer read or written.
"""

from django.db import migrations, models


def backfill_player_streaks(apps, schema_editor):
    Character = apps.get_model("amc", "Character")
    Player = apps.get_model("amc", "Player")

    best_by_player = {}
    for row in (
        Character.objects.filter(last_login_reward_date__isnull=False)
        .order_by("player_id", "-last_login_reward_date", "-login_streak")
        .values("player_id", "login_streak", "last_login_reward_date")
    ):
        best_by_player.setdefault(row["player_id"], row)

    for unique_id, row in best_by_player.items():
        Player.objects.filter(unique_id=unique_id).update(
            login_streak=row["login_streak"],
            last_login_reward_date=row["last_login_reward_date"],
        )


def clear_player_streaks(apps, schema_editor):
    Player = apps.get_model("amc", "Player")
    Player.objects.all().update(login_streak=0, last_login_reward_date=None)


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0271_remove_character_cargo_ignore_until"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="login_streak",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="player",
            name="last_login_reward_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_player_streaks, clear_player_streaks),
    ]
