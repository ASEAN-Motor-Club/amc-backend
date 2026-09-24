from django.utils.translation import gettext_lazy

from amc.command_framework import CommandContext, registry
from amc.criminals import _compute_stars
from amc.game_server import get_players
from amc.models import Character, PoliceSession, Wanted
from amc.special_cargo import (
    calculate_boss_cut_ratio,
    calculate_criminal_level,
    wanted_trigger_chance,
)


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

    # --- Section 2: Criminal scores without an active Wanted ---
    other_criminals = [
        c
        async for c in Character.objects.filter(criminal_score__gt=0)
        .order_by("-criminal_score")
        .exclude(pk__in=active_character_ids)
        .exclude(pk__in=active_cop_ids)
    ]

    if not active_bounties and not other_criminals:
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
        if i == 1:
            # Boss gets his own deferred layout: title-sized name line.
            msg += (
                f"<Title>BOSS {name}</>\n"
                f"<Money>${score:,}</> <Secondary>(C{level})</>\n"
            )
        else:
            msg += (
                f"{i}. {name} — <Money>${score:,}</> <Secondary>(C{level})</>\n"
            )

    # The caller's own standing in the criminal world.
    me = ctx.character
    if me is not None:
        my_score = (
            await Character.objects.filter(pk=me.pk)
            .values_list("criminal_score", flat=True)
            .aget()
        )
        my_level = calculate_criminal_level(my_score)
        boss = (
            await Character.objects.filter(criminal_score__gt=0)
            .order_by("-criminal_score", "pk")  # matches collect_boss_tax
            .afirst()
        )
        msg += "\n<Title>Your Criminal Record</>\n"
        if my_score > 0:
            ahead = await Character.objects.filter(
                criminal_score__gt=my_score
            ).acount()
            # Same-score ties rank by name (matches the board's ordering).
            ties = await Character.objects.filter(
                criminal_score=my_score, name__lt=me.name
            ).acount()
            rank = ahead + ties + 1
            msg += (
                f"#{rank} {me.name} — <Money>${my_score:,}</>"
                f" <Secondary>(C{my_level})</>\n"
            )
            if boss is not None and boss.pk == me.pk:
                msg += (
                    "<EffectGood>You are the BOSS — you collect the boss "
                    "cut on criminal deliveries.</>\n"
                )
            elif boss is not None:
                boss_level = calculate_criminal_level(boss.criminal_score)
                cut = calculate_boss_cut_ratio(my_level, boss_level)
                msg += (
                    f"<Secondary>Boss cut on criminal deliveries: "
                    f"{cut * 100:.0f}%</>\n"
                )
        else:
            msg += "<Secondary>You have no criminal score.</>\n"

        # Wanted-trigger risk table: the chance that ONE illicit delivery of
        # each size triggers a Wanted level, for the caller's current score
        # (no cop nearby — attenuation is unknown at /criminals time).
        msg += "\n<Title>Wanted Risk</>\n"
        msg += (
            "<Secondary>Chance per illicit delivery, no cop nearby"
            " (within 1km):</>\n"
        )
        for pay, label in (
            (100_000, "$100k"),
            (250_000, "$250k"),
            (500_000, "$500k"),
            (750_000, "$750k"),
            (1_000_000, "$1M"),
        ):
            chance = wanted_trigger_chance(pay, my_score, None)
            risk = (
                "<Warning>guaranteed</>"
                if chance >= 1.0
                else f"{chance * 100:.0f}%"
            )
            msg += f"{label} — <Highlight>{risk}</>\n"

    await ctx.reply(msg.rstrip())
