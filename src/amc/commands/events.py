import asyncio
from typing import Optional
from amc.command_framework import registry, CommandContext
from amc.models import GameEvent, GameEventCharacter, ScheduledEvent, BotInvocationLog
from amc.events import (
    staggered_start, auto_starting_grid, show_scheduled_event_results_popup,
    setup_event
)
from amc.utils import format_timedelta, format_in_local_tz, countdown
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.translation import gettext_lazy

@registry.register("/staggered_start", description=gettext_lazy("Start event with staggered delay"), category="Events")
async def cmd_staggered_start(ctx: CommandContext, delay: int):
    active_event = await (GameEvent.objects
        .filter(Exists(GameEventCharacter.objects.filter(game_event=OuterRef('pk'), character=ctx.character)))
        .select_related('race_setup').alatest('last_updated'))
    
    if not active_event:
        await ctx.reply("No active events")
        return
    await staggered_start(ctx.http_client, ctx.http_client_mod, active_event, player_id=ctx.player.unique_id, delay=float(delay))

@registry.register("/auto_grid", description=gettext_lazy("Automatically grid players for event"), category="Events")
async def cmd_auto_grid(ctx: CommandContext):
    active_event = await (GameEvent.objects
        .filter(Exists(GameEventCharacter.objects.filter(game_event=OuterRef('pk'), character=ctx.character)))
        .select_related('race_setup').alatest('last_updated'))
    
    if not active_event:
        await ctx.reply("No active events")
        return
    await auto_starting_grid(ctx.http_client_mod, active_event)

@registry.register("/results", description=gettext_lazy("See the results of active events"), category="Events")
async def cmd_results(ctx: CommandContext):
    active_event = await ScheduledEvent.objects.filter_active_at(ctx.timestamp).select_related('race_setup').afirst()
    if not active_event:
        await ctx.reply("No active events")
        return
    await show_scheduled_event_results_popup(ctx.http_client_mod, active_event, character_guid=ctx.character.guid, player_id=str(ctx.player.unique_id))

@registry.register("/setup_event", description=gettext_lazy("Creates an event properly"), category="Events")
async def cmd_setup_event(ctx: CommandContext, event_id: Optional[int] = None):
    try:
        if event_id:
            scheduled_event = await ScheduledEvent.objects.select_related('race_setup').filter(race_setup__isnull=False).aget(pk=event_id)
        else:
            scheduled_event = await ScheduledEvent.objects.filter_active_at(ctx.timestamp).select_related('race_setup').filter(race_setup__isnull=False).afirst()
            if not scheduled_event:
                await ctx.reply("There does not seem to be an active event.")
                return
        
        event_setup = await setup_event(ctx.timestamp, ctx.player.unique_id, scheduled_event, ctx.http_client_mod)
        if not event_setup:
             await ctx.reply("There does not seem to be an active event.")
    except Exception as e:
        await ctx.reply(f"Failed to setup event: {e}")
        raise e
    
    await BotInvocationLog.objects.acreate(timestamp=ctx.timestamp, character=ctx.character, prompt="/setup_event")

@registry.register("/events", description=gettext_lazy("List current and upcoming scheduled events"), category="Events")
async def cmd_events_list(ctx: CommandContext):
    events: list[str] = []
    async for event in ScheduledEvent.objects.filter(end_time__gte=timezone.now()).order_by('start_time'):
        start_msg = f"{format_timedelta(event.start_time - timezone.now())} from now" if event.start_time > timezone.now() else 'In progress'
        events.append(f"""<Title>{event.name}</>
Use <Highlight>/setup_event {event.id}</>
<Secondary>{format_in_local_tz(event.start_time)} - {format_in_local_tz(event.end_time)} ({start_msg})</>
{event.description_in_game or event.description}""")
    
    await ctx.reply(f"[EVENTS]\n\n{'\n\n'.join(events)}")

@registry.register("/countdown", description=gettext_lazy("Initiate a 5 second countdown"), category="Events")
async def cmd_countdown(ctx: CommandContext):
    asyncio.create_task(countdown(ctx.http_client))
