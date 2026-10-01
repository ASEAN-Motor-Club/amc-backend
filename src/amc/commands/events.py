import asyncio
from typing import Optional

from django.db.models import Exists, OuterRef
from django.utils.translation import gettext_lazy

from amc.command_framework import CommandContext, registry
from amc.events import (
    UNDERGROUND_CHAMPIONSHIP_NAME,
    auto_starting_grid,
    setup_event,
    show_scheduled_event_results_popup,
    staggered_start,
)
from amc.models import BotInvocationLog, GameEvent, GameEventCharacter, ScheduledEvent
from amc.utils import countdown, format_in_local_tz

STAGGERED_START_DEFAULT_DELAY = 20.0  # matches amc.events.staggered_start's own default


async def resolve_stagger_delay(active_event, delay_arg) -> float:
    """Manual argument > scheduled event's configured delay > 20s default."""
    if delay_arg is not None:
        return float(delay_arg)
    scheduled_event = getattr(active_event, "scheduled_event", None)
    if scheduled_event is not None and scheduled_event.staggered_start_delay > 0:
        return float(scheduled_event.staggered_start_delay)
    return STAGGERED_START_DEFAULT_DELAY


@registry.register(
    "/staggered_start",
    description=gettext_lazy("Start event with staggered delay"),
    category="Events",
)
async def cmd_staggered_start(ctx: CommandContext, delay: int | None = None):
    active_event = await (
        GameEvent.objects.filter(
            Exists(
                GameEventCharacter.objects.filter(
                    game_event=OuterRef("pk"), character=ctx.character
                )
            )
        )
        .select_related("race_setup", "scheduled_event")
        .alatest("last_updated")
    )

    if not active_event:
        await ctx.reply("No active events")
        return
    await staggered_start(
        ctx.http_client,
        ctx.http_client_mod,
        active_event,
        player_id=ctx.player.unique_id,
        delay=await resolve_stagger_delay(active_event, delay),
    )


@registry.register(
    "/auto_grid",
    description=gettext_lazy("Automatically grid players for event"),
    category="Events",
)
async def cmd_auto_grid(ctx: CommandContext):
    active_event = await (
        GameEvent.objects.filter(
            Exists(
                GameEventCharacter.objects.filter(
                    game_event=OuterRef("pk"), character=ctx.character
                )
            )
        )
        .select_related("race_setup")
        .alatest("last_updated")
    )

    if not active_event:
        await ctx.reply("No active events")
        return
    await auto_starting_grid(ctx.http_client_mod, active_event)


@registry.register(
    "/results",
    description=gettext_lazy("See the results of active events"),
    category="Events",
)
async def cmd_results(ctx: CommandContext):
    active_event = (
        await ScheduledEvent.objects.filter_active_at(ctx.timestamp)
        .select_related("race_setup")
        .afirst()
    )
    if not active_event:
        await ctx.reply("No active events")
        return
    await show_scheduled_event_results_popup(
        ctx.http_client_mod,
        active_event,
        character_guid=ctx.character.guid,
        player_id=str(ctx.player.unique_id),
    )


@registry.register(
    "/setup_event",
    description=gettext_lazy("Creates an event properly"),
    category="Events",
)
async def cmd_setup_event(ctx: CommandContext, event_id: Optional[int] = None):
    try:
        if event_id:
            scheduled_event = (
                await ScheduledEvent.objects.select_related("race_setup")
                .filter(race_setup__isnull=False)
                .aget(pk=event_id)
            )
        else:
            # No id: start the CURRENT ACTIVE event (window live right now).
            # Originals and illegal-TT twins are distinct SE rows (Yuuka
            # 2026-09-27: "accurately separate them") — prefer the classed
            # twin outright; only fall back to a classless SE when no twin
            # window is live.
            # Rotation instances (the daily post, is_rotation_instance=True)
            # are never /setup_event targets — they are already live
            # in-game; re-setting them up would double-post the same setup.
            # Candidates are the underground TEMPLATES (classless, windowed
            # now+14d to match the rotation instance convention) plus any
            # other windowed race SEs.
            base = ScheduledEvent.objects.filter(
                race_setup__isnull=False,
                is_rotation_instance=False,
            ).filter_active_at(ctx.timestamp)
            # Prefer an underground template (rolls a class in setup_event);
            # else the newest classed twin; else any windowed race SE.
            underground = base.filter(
                championship__name=UNDERGROUND_CHAMPIONSHIP_NAME,
                tt_class__isnull=True,
            )
            scheduled_event = (
                await underground.select_related("race_setup")
                .order_by("-start_time")
                .afirst()
            )
            if scheduled_event is None:
                scheduled_event = (
                    await base.filter(tt_class__isnull=False)
                    .select_related("race_setup", "tt_class")
                    .order_by("-start_time")
                    .afirst()
                )
            if scheduled_event is None:
                scheduled_event = (
                    await base.select_related("race_setup")
                    .order_by("-start_time")
                    .afirst()
                )
            if scheduled_event is None:
                await ctx.reply("No active events right now.")
                return

        event_setup = await setup_event(
            ctx.timestamp, ctx.player.unique_id, scheduled_event, ctx.http_client_mod
        )
        if not event_setup:
            await ctx.reply("There does not seem to be an active event.")
    except Exception as e:
        await ctx.reply(f"Failed to setup event: {e}")
        raise e

    await BotInvocationLog.objects.acreate(
        timestamp=ctx.timestamp, character=ctx.character, prompt="/setup_event"
    )


@registry.register(
    "/events",
    description=gettext_lazy("List current and upcoming scheduled events"),
    category="Events",
    featured=True,
)
async def cmd_events_list(ctx: CommandContext):
    # Only events whose window is live RIGHT NOW — the old version listed
    # everything with end_time in the future, a long stale list.
    events: list[str] = []
    async for event in ScheduledEvent.objects.filter_active_at(ctx.timestamp).order_by(
        "start_time"
    ):
        start_txt = format_in_local_tz(event.start_time)
        end_txt = format_in_local_tz(event.end_time)
        events.append(f"""<Title>{event.name}</>
Use <Highlight>/setup_event</> to start it
<Secondary>{start_txt} - {end_txt}</>
{event.description_in_game or event.description}""")

    if not events:
        await ctx.reply("No active events right now.")
        return

    await ctx.reply(f"[EVENTS]\n\n{'\n\n'.join(events)}")


@registry.register(
    "/countdown",
    description=gettext_lazy("Initiate a 5 second countdown"),
    category="Events",
)
async def cmd_countdown(ctx: CommandContext):
    asyncio.create_task(
        countdown(ctx.http_client_mod, str(ctx.player.unique_id))
    )


@registry.register(
    "/racelegality",
    description=gettext_lazy(
        "Toggle the event you are in between legal and illegal "
        "(illegal races get start-line DQ, the 60s announcement and "
        "the star Wanted)"
    ),
    category="Events",
)
async def cmd_race_legality(ctx: CommandContext):
    # Available to everyone (Yuuka 2026-09-27: "make it available for
    # no admins too") — but only the event OWNER (or an admin) may
    # flip it, so random players can't sabotage other people's races.
    is_admin = bool(ctx.player_info and ctx.player_info.get("bIsAdmin"))

    event = await (
        GameEvent.objects.filter(
            Exists(
                GameEventCharacter.objects.filter(
                    game_event=OuterRef("pk"), character=ctx.character
                )
            ),
            state__lt=3,
        )
        .alatest("start_time")
    )

    if not event:
        await ctx.reply(
            "<Title>No event</>\nYou are not inside an active event."
        )
        return

    if not is_admin and event.owner_id != ctx.character.id:
        await ctx.reply(
            "<Title>Not your event</>\nOnly the event owner can change its legality."
        )
        return

    event.race_legality = "illegal" if event.race_legality != "illegal" else "legal"
    await event.asave(update_fields=["race_legality"])
    label = "ILLEGAL" if event.race_legality == "illegal" else "LEGAL"
    await ctx.reply(
        f"<Title>Race legality updated</>\n"
        f"{event.name} is now <Highlight>{label}</>."
    )
