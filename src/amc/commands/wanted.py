from datetime import timedelta

from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import Character, PoliceSession, Wanted
from amc.special_cargo import calculate_criminal_level
from amc.criminals import _compute_stars
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy

SETWANTED_COOLDOWN = timedelta(minutes=settings.SETWANTED_COOLDOWN_MINUTES)



def _stars(n: int) -> str:
    """Return n filled stars + (5-n) empty stars."""
    return "★" * n + "☆" * (5 - n)


@registry.register(
    "/wanted",
    description=gettext_lazy("List wanted criminals"),
    category="Faction",
    deprecated=True,
)
async def cmd_wanted(ctx: CommandContext):
    timezone.now()

    # --- Online player GUIDs ---
    online_guids: set[str] = set()
    players = await get_players(ctx.http_client)
    if players:
        for _uid, pdata in players:
            guid = pdata.get("character_guid")
            if guid:
                online_guids.add(guid)

    # --- Active cops (excluded from all lists) ---
    active_cop_ids: set[int] = set()
    async for session in PoliceSession.objects.filter(ended_at__isnull=True):
        active_cop_ids.add(session.character_id)

    # --- Section 1: Active Wanted records (have a live bounty) ---
    # Bounty = Wanted.amount, set at creation to 10% of the criminal score
    # and frozen for the chase.
    active_bounties: list[dict] = []
    active_character_ids: set[int] = set()
    async for wanted in (
        Wanted.objects.filter(expired_at__isnull=True, wanted_remaining__gt=0)
        .select_related("character")
    ):
        if wanted.character_id in active_cop_ids:
            continue
        active_character_ids.add(wanted.character_id)
        stars = _compute_stars(wanted.wanted_remaining)
        is_online = wanted.character.guid in online_guids
        active_bounties.append(
            {
                "name": wanted.character.name,
                "stars": stars,
                "bounty": wanted.amount,
                "online": is_online,
            }
        )

    # Sort active bounties: online first, then by bounty desc within each group
    active_bounties.sort(key=lambda e: (not e["online"], -e["bounty"]))

    # --- Section 1.5: Recently expired wanteds (on cooldown) ---
    cooldown_entries: list[dict] = []
    cooldown_character_ids: set[int] = set()
    cutoff = timezone.now() - SETWANTED_COOLDOWN
    async for wanted in (
        Wanted.objects.filter(expired_at__isnull=False, expired_at__gte=cutoff)
        .select_related("character", "set_by")
        .order_by("-expired_at")
    ):
        if wanted.character_id in active_character_ids or wanted.character_id in active_cop_ids:
            continue
        if wanted.character_id in cooldown_character_ids:
            continue
        cooldown_character_ids.add(wanted.character_id)
        remaining = (wanted.expired_at + SETWANTED_COOLDOWN) - timezone.now()
        remaining_mins = int(remaining.total_seconds() / 60)
        remaining_secs = int(remaining.total_seconds()) % 60
        cooldown_entries.append(
            {
                "name": wanted.character.name,
                "online": wanted.character.guid in online_guids,
                "remaining_mins": remaining_mins,
                "remaining_secs": remaining_secs,
                "set_by_name": wanted.set_by.name if wanted.set_by else None,
            }
        )
    # Sort: online first, then by remaining cooldown (ascending)
    cooldown_entries.sort(
        key=lambda e: (not e["online"], e["remaining_mins"], e["remaining_secs"])
    )

    # --- Section 2: Criminal scores without an active Wanted ---
    other_criminals = [
        c
        async for c in Character.objects.filter(criminal_score__gt=0)
        .order_by("-criminal_score")
        .exclude(pk__in=active_character_ids)
        .exclude(pk__in=active_cop_ids)
        .exclude(pk__in=cooldown_character_ids)
    ]

    if not active_bounties and not cooldown_entries and not other_criminals:
        await ctx.reply("No wanted criminals")
        return

    other_entries = []
    for char in other_criminals:
        guid = char.guid
        other_entries.append(
            {
                "name": char.name,
                "guid": guid,
                "level": calculate_criminal_level(char.criminal_score),
                "score": char.criminal_score,
                "online": guid in online_guids,
            }
        )
    other_online = sorted(
        [e for e in other_entries if e["online"]],
        key=lambda e: e["score"],
        reverse=True,
    )
    other_offline = sorted(
        [e for e in other_entries if not e["online"]],
        key=lambda e: e["score"],
        reverse=True,
    )

    # --- Build message ---
    msg = "<Title>Wanted List</>\n\n"

    def _row_bounty(e: dict) -> str:
        amount_str = f"${e['bounty']:,}" if e["bounty"] > 0 else "no bounty"
        return f"{_stars(e['stars'])} {e['name']} <Secondary>{amount_str}</>\n"

    def _row_record(e: dict) -> str:
        score_str = f"${e['score']:,}" if e["score"] > 0 else "$0"
        return (
            f"<Highlight>C{e['level']}</> {e['name']}"
            f" <Secondary>{score_str}</>\n"
        )

    def _row_cooldown(e: dict) -> str:
        return (
            f"{e['name']} <Secondary>"
            f"Cooldown: {e['remaining_mins']}m {e['remaining_secs']}s"
            f"</>\n"
        )

    if active_bounties:
        bounties_online = [e for e in active_bounties if e["online"]]
        bounties_offline = [e for e in active_bounties if not e["online"]]
        msg += "<Title>Active Bounties</>\n"
        if bounties_online:
            msg += "<EffectGood>Online</>\n"
            for e in bounties_online:
                msg += _row_bounty(e)
        if bounties_offline:
            msg += "<Warning>Offline</>\n"
            for e in bounties_offline:
                msg += _row_bounty(e)
        msg += "\n"

    if cooldown_entries:
        cooldown_online = [e for e in cooldown_entries if e["online"]]
        cooldown_offline = [e for e in cooldown_entries if not e["online"]]
        msg += "<Title>Recently Wanted</>\n"
        if cooldown_online:
            msg += "<EffectGood>Online</>\n"
            for e in cooldown_online:
                msg += _row_cooldown(e)
        if cooldown_offline:
            msg += "<Warning>Offline</>\n"
            for e in cooldown_offline:
                msg += _row_cooldown(e)
        msg += "\n"

    if other_online or other_offline:
        msg += "<Title>Criminal Record</>\n"
        if other_online:
            msg += "<EffectGood>Online</>\n"
            for e in other_online:
                msg += _row_record(e)
        if other_offline:
            msg += "<Warning>Offline</>\n"
            for e in other_offline:
                msg += _row_record(e)
        msg += (
            "<Secondary>Criminal score decays (7-day half-life) after 48h "
            "without illicit deliveries. Arrests negate the bounty from it.</>\n"
        )

    await ctx.reply(msg.rstrip())


@registry.register(
    "/criminals",
    description=gettext_lazy("Criminal leaderboard — top 10 by criminal score"),
    category="Faction",
)
async def cmd_criminals(ctx: CommandContext):
    """Criminal leaderboard (plan §6.1): top 10 by criminal score.

    Levels are derived live from the score, so the board reflects decay and
    arrest negation automatically. Rank 1 is the boss — the character the
    boss tax pays into.
    """
    rows = [
        (char.name, char.criminal_score)
        async for char in (
            Character.objects.filter(criminal_score__gt=0)
            .order_by("-criminal_score", "name")[:10]
        )
    ]

    if not rows:
        await ctx.reply("No criminals yet")
        return

    msg = "<Title>Criminal Leaderboard</>\n\n"
    for i, (name, score) in enumerate(rows, start=1):
        level = calculate_criminal_level(score)
        marker = "👑 <Highlight>BOSS</> " if i == 1 else ""
        msg += (
            f"{i}. {marker}{name} — <Money>${score:,}</> <Secondary>(C{level})</>\n"
        )
    await ctx.reply(msg.rstrip())
