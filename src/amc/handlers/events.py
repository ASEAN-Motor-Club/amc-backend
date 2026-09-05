"""Event system handlers — race events via SSE.

Handles: ServerAddEvent, ServerChangeEventState, ServerPassedRaceSection,
ServerRemoveEvent, ServerJoinEvent, ServerLeaveEvent.

These hooks arrive through the SSE pipeline (replacing the old polling-based
monitor_events cron).  The C++ mod extracts the full FMTEvent struct from
Unreal and sends it as JSON with PascalCase keys.

The handler mirrors the logic from ``amc.events.process_event`` but operates
on individual SSE events rather than a polled snapshot.
"""

from __future__ import annotations

import asyncio
import logging
import time

import discord
from django.conf import settings
from django.db.models import Exists, OuterRef, Prefetch
from django.utils import timezone

from amc.events import create_event_embed, show_results_popup
from amc.handlers import register
from amc.mod_server import transfer_exp
from amc.models import (
    Character,
    GameEvent,
    GameEventCharacter,
    LapSectionTime,
    RaceSetup,
    ScheduledEvent,
)
from amc.utils import delay

logger = logging.getLogger("amc.webhook.handlers.events")

# Throttle embed updates: at most one per event every 5 seconds.
# Prevents Discord rate-limit issues during busy races with many section passes.
_embed_update_times: dict[int, float] = {}
_EMBED_UPDATE_COOLDOWN = 5.0  # seconds


async def _throttled_update_embed(game_event_id: int, discord_client, force: bool = False):
    """Update embed with per-event rate limiting. force=True bypasses cooldown."""
    now = time.monotonic()
    last = _embed_update_times.get(game_event_id, 0)
    if not force and (now - last) < _EMBED_UPDATE_COOLDOWN:
        return
    _embed_update_times[game_event_id] = now
    await _update_discord_event_embed(game_event_id, discord_client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_race_setup(race_setup_raw: dict) -> dict:
    """Convert PascalCase SSE RaceSetup to the dict format expected by
    ``RaceSetup.calculate_hash`` / ``RaceSetup.config``.

    The existing ``process_event`` stores the config exactly as received, so
    we must preserve the same key casing.  The C++ extractor already emits
    PascalCase keys (``Route``, ``NumLaps``, ``VehicleKeys``, …) that match
    what the Lua hooks previously sent, so this is largely a passthrough.
    """
    return race_setup_raw


async def _upsert_game_event(event_data: dict):
    """Create or update a ``GameEvent`` from SSE event data.

    *event_data* is the ``Event`` dict emitted by the C++ ``ServerAddEvent``
    or ``ServerChangeEventState`` hooks (PascalCase keys).

    Returns ``(game_event, transition)`` where *transition* is
    ``(old_state, new_state)`` or *None*.
    """
    event_guid = event_data.get("EventGuid", "")
    event_name = event_data.get("EventName", "")
    state = event_data.get("State", 0)
    race_setup_raw = event_data.get("RaceSetup", {})

    # --- RaceSetup ---
    race_setup = None
    if race_setup_raw:
        race_setup_hash = RaceSetup.calculate_hash(race_setup_raw)
        race_setup, _ = await RaceSetup.objects.aget_or_create(
            hash=race_setup_hash,
            defaults={
                "config": race_setup_raw,
                "name": race_setup_raw.get("Route", {}).get("RouteName"),
            },
        )

    # --- Owner ---
    owner = None
    owner_data = event_data.get("OwnerCharacterId", {})
    if owner_data:
        owner = await Character.objects.filter(
            player__unique_id=owner_data.get("UniqueNetId"),
            guid=owner_data.get("CharacterGuid"),
        ).afirst()

    # --- ScheduledEvent association ---
    scheduled_event = None
    if race_setup:
        scheduled_event = await (
            ScheduledEvent.objects.filter(
                race_setup=race_setup,
                start_time__lte=timezone.now(),
                end_time__gte=timezone.now(),
            )
            .order_by("start_time")  # deterministic pick if several match
            .afirst()
        )

    # --- GameEvent upsert ---
    transition = None
    try:
        game_event = await (
            GameEvent.objects.filter(
                guid=event_guid,
                state__lte=state,
            )
            .select_related("scheduled_event")
            .alatest("start_time")
        )
        if game_event.state != state:
            transition = (game_event.state, state)
        game_event.state = state
        game_event.owner = owner
        if race_setup:
            game_event.race_setup = race_setup
        if not game_event.scheduled_event and scheduled_event:
            game_event.scheduled_event = scheduled_event
        await game_event.asave()
    except GameEvent.DoesNotExist:
        try:
            existing_event = await (
                GameEvent.objects.filter(
                    guid=event_guid,
                    discord_message_id__isnull=False,
                )
                .exclude(
                    Exists(
                        GameEventCharacter.objects.filter(
                            game_event=OuterRef("pk"), finished=True
                        )
                    )
                )
                .alatest("last_updated")
            )
            discord_message_id = existing_event.discord_message_id
        except GameEvent.DoesNotExist:
            discord_message_id = None

        game_event = await GameEvent.objects.acreate(
            guid=event_guid,
            name=event_name,
            state=state,
            race_setup=race_setup,
            discord_message_id=discord_message_id,
            owner=owner,
            scheduled_event=scheduled_event,
            auto_created=(owner is None),
        )

    return game_event, transition


async def _upsert_game_event_character(game_event, player_info: dict):
    """Create or update a ``GameEventCharacter`` from SSE player data."""
    character_id = player_info.get("CharacterId", {})
    player_name = player_info.get("PlayerName", "")
    unique_net_id = character_id.get("UniqueNetId", "")
    character_guid = character_id.get("CharacterGuid", "")

    if not unique_net_id:
        return None

    character, *_ = await Character.objects.aget_or_create_character_player(
        player_name,
        int(unique_net_id),
        character_guid=character_guid,
    )

    player_finished = await GameEventCharacter.objects.filter(
        character=character, game_event=game_event, finished=True
    ).aexists()
    if player_finished:
        return None

    defaults = {
        "last_section_total_time_seconds": player_info.get(
            "LastSectionTotalTimeSeconds", 0
        ),
        "section_index": player_info.get("SectionIndex", -1),
        "best_lap_time": player_info.get("BestLapTime", 0),
        "rank": player_info.get("Rank", 0),
        "laps": player_info.get("Laps", 0),
        "finished": player_info.get("bFinished", False),
        "disqualified": player_info.get("bDisqualified", False),
        "lap_times": list(player_info.get("LapTimes", [])),
    }
    if game_event.state < 2:
        defaults.update(
            {
                "wrong_vehicle": player_info.get("bWrongVehicle", False),
                "wrong_engine": player_info.get("bWrongEngine", False),
            }
        )

    game_event_character, _ = await GameEventCharacter.objects.aupdate_or_create(
        character=character,
        game_event=game_event,
        defaults=defaults,
        create_defaults={
            **defaults,
            "wrong_vehicle": player_info.get("bWrongVehicle", False),
            "wrong_engine": player_info.get("bWrongEngine", False),
        },
    )

    # Record lap section times
    if (
        game_event.state >= 2
        and game_event_character.section_index >= 0
        and game_event_character.laps >= 1
    ):
        laps = game_event_character.laps - 1
        section_index = game_event_character.section_index
        await LapSectionTime.objects.aupdate_or_create(
            game_event_character=game_event_character,
            section_index=section_index,
            lap=laps,
            defaults={
                "total_time_seconds": game_event_character.last_section_total_time_seconds,
                "rank": game_event_character.rank,
            },
        )

    # First section time tracking
    if (
        game_event.state == 2
        and player_info.get("SectionIndex", -1) == 0
        and player_info.get("Laps", 0) == 1
    ):
        total_time = player_info.get("LastSectionTotalTimeSeconds", 0)
        if total_time < 10_000_000:
            await GameEventCharacter.objects.filter(pk=game_event_character.pk).aupdate(
                first_section_total_time_seconds=total_time
            )
        else:
            await GameEventCharacter.objects.filter(pk=game_event_character.pk).aupdate(
                first_section_total_time_seconds=0
            )

    return game_event_character


async def _reward_event_exp(game_event_id: int, http_client_mod):
    participants = GameEventCharacter.objects.filter(
        game_event_id=game_event_id,
    ).select_related("character__player")

    async for p in participants:
        if not p.character.player:
            continue
        player_id = p.character.player.unique_id
        try:
            await transfer_exp(
                http_client_mod,
                player_id,
                level_type=4,
                exp=1000,
                message="Racing EXP reward",
            )
        except Exception:
            logger.exception(
                "Failed to grant EXP to player %s for event %s",
                player_id,
                game_event_id,
            )


async def _show_finish_results(game_event_id: int, http_client_mod):
    """Push the final results popup to every participant on event finish.

    Restores the legacy monitor_events behaviour (2→3 transition) that was
    dropped in the SSE migration. Popup content is built by
    amc.events.print_results (rank / net time / DNF-ENGINE-VEHICLE flags).
    """
    participants = [
        p
        async for p in GameEventCharacter.objects.select_related(
            "character", "character__player"
        ).filter(game_event_id=game_event_id)
    ]
    if not participants:
        return
    try:
        await show_results_popup(http_client_mod, participants)
    except Exception:
        logger.exception(
            "Failed to show finish results popup for event %s", game_event_id
        )


async def _update_discord_event_embed(game_event_id: int, discord_client):
    if discord_client is None:
        logger.debug("_update_discord_event_embed: discord_client is None, skipping")
        return

    channel = discord_client.get_channel(settings.DISCORD_EVENTS_CHANNEL_ID)
    if channel is None:
        logger.debug("_update_discord_event_embed: channel not found, skipping")
        return

    try:
        game_event = await (
            GameEvent.objects.select_related("race_setup", "scheduled_event")
            .prefetch_related(
                Prefetch(
                    "participants",
                    queryset=GameEventCharacter.objects.select_related("character"),
                )
            )
            .aget(pk=game_event_id)
        )
    except GameEvent.DoesNotExist:
        logger.warning("_update_discord_event_embed: GameEvent %s not found", game_event_id)
        return

    if game_event.discord_message_id is None:
        return

    embed = create_event_embed(game_event)

    async def _edit_embed():
        try:
            message = await channel.fetch_message(game_event.discord_message_id)
            await message.edit(content="", embed=embed)
            logger.info(
                "Updated Discord embed for event %s (state=%s)",
                game_event.name, game_event.state,
            )
        except discord.NotFound:
            logger.warning(
                "Discord message %s not found for event %s",
                game_event.discord_message_id, game_event.name,
            )
        except Exception:
            logger.exception(
                "Failed to edit Discord embed for event %s (msg=%s)",
                game_event.name, game_event.discord_message_id,
            )

    asyncio.run_coroutine_threadsafe(_edit_embed(), discord_client.loop)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@register("ServerAddEvent")
async def handle_add_event(event, player, character, ctx):
    """Handle ServerAddEvent: create GameEvent + GameEventCharacters."""
    event_data = event["data"].get("Event", {})
    if not event_data or not event_data.get("EventGuid"):
        return 0, 0, 0, 0

    game_event, _ = await _upsert_game_event(event_data)

    # Process all players
    for player_info in event_data.get("Players", []):
        await _upsert_game_event_character(game_event, player_info)

    # Update Discord embed if one already exists for this event
    if game_event.discord_message_id:
        asyncio.create_task(
            _throttled_update_embed(game_event.pk, ctx.discord_client, force=True)
        )

    return 0, 0, 0, 0


@register("ServerChangeEventState")
async def handle_change_event_state(event, player, character, ctx):
    """Handle ServerChangeEventState: update GameEvent state + player data."""
    event_data = event["data"].get("Event", {})
    if not event_data or not event_data.get("EventGuid"):
        return 0, 0, 0, 0

    game_event, transition = await _upsert_game_event(event_data)

    logger.info(
        "ServerChangeEventState: guid=%s state=%s transition=%s",
        event_data.get("EventGuid"), game_event.state, transition,
    )

    # Process all players
    for player_info in event_data.get("Players", []):
        await _upsert_game_event_character(game_event, player_info)

    if transition and transition[1] == 3:
        asyncio.create_task(
            delay(_show_finish_results(game_event.pk, ctx.http_client_mod), 5)
        )
        asyncio.create_task(
            delay(_reward_event_exp(game_event.pk, ctx.http_client_mod), 10)
        )

    # Update Discord embed on any state transition (force=True to bypass throttle)
    if transition:
        asyncio.create_task(
            _throttled_update_embed(game_event.pk, ctx.discord_client, force=True)
        )

    return 0, 0, 0, 0


@register("ServerPassedRaceSection")
async def handle_passed_race_section(event, player, character, ctx):
    """Handle ServerPassedRaceSection: record section time for a player."""
    data = event["data"]
    event_guid = data.get("EventGuid", "")
    section_index = data.get("SectionIndex", -1)
    total_time_seconds = data.get("TotalTimeSeconds", 0)

    if not event_guid:
        return 0, 0, 0, 0

    game_event = await GameEvent.objects.filter(guid=event_guid).afirst()
    if not game_event:
        logger.warning("ServerPassedRaceSection: GameEvent %s not found", event_guid)
        return 0, 0, 0, 0

    # The CharacterGuid is in the base event data
    character_guid = data.get("CharacterGuid", "")
    if not character_guid:
        return 0, 0, 0, 0

    game_event_char = await GameEventCharacter.objects.filter(
        game_event=game_event, character__guid=character_guid
    ).select_related("character").afirst()

    if not game_event_char:
        logger.warning(
            "ServerPassedRaceSection: GameEventCharacter not found for event %s, character %s",
            event_guid, character_guid,
        )
        return 0, 0, 0, 0

    laptime_seconds = data.get("LaptimeSeconds", 0)
    # A section-0 crossing AFTER the first one (the start line, recorded in
    # first_section_total_time_seconds) completes a lap, and LaptimeSeconds is
    # then the just-completed lap's time (verified live 2026-09-05: S0 splits
    # 19.88/16.83 matched TotalTime deltas exactly). The SSE stream never
    # carries the cumulative LapTimes/BestLapTime snapshot mid-race — that was
    # the polling-era data source — so laps are reconstructed here.
    #
    # Sentinel guard: the game sends LaptimeSeconds = seconds-since-server-boot
    # (~6300s observed) on start-line crossings. A real lap is always a subset
    # of TotalTimeSeconds; the sentinel is orders of magnitude larger than the
    # fresh TotalTime of the crossing that carries it (verified live: 6329 vs
    # 6.75). Subset check rejects it; the old 10M ceiling did NOT (sentinel is
    # boot time, not a huge constant — it grows every boot).
    completed_lap = (
        section_index == 0
        and game_event_char.first_section_total_time_seconds is not None
        and 0 < laptime_seconds <= total_time_seconds
    )

    # Update section index and total time
    game_event_char.section_index = section_index
    game_event_char.last_section_total_time_seconds = total_time_seconds
    just_finished = False
    if completed_lap:
        game_event_char.lap_times = list(game_event_char.lap_times or []) + [
            laptime_seconds
        ]
        best = game_event_char.best_lap_time or 0
        if best <= 0 or laptime_seconds < best:
            game_event_char.best_lap_time = laptime_seconds
        game_event_char.laps += 1

        # Rule B — N-lap natural-finish detection (NumLaps>=1).  Natural
        # completion is a server-internal transition — ChangeEventState(3)
        # never reached SSE in the observed runs (verified live 2026-09-05:
        # a 2-lap kart event recorded both laps in LapTimes {11.32, 9.54}
        # yet the run stayed finished=False forever).  In NumLaps>=1 routes
        # the finish checkpoint is the FIRST waypoint (freeman 2026-09-05):
        # a 1-lap run finishes on the first W0 lap crossing, an N-lap run on
        # the section-0 crossing that completes the final lap — exactly the
        # crossing reconstructed above.  (NumLaps==0 routes finish at the
        # LAST waypoint instead — that rule lives in PR #83.)
        # NOTE: ``laps`` is 1 + completed-lap count (the initial 1 is the
        # in-progress marker set on the first section crossing), so the
        # final-lap condition is laps - 1 >= num_laps, not laps >= num_laps.
        num_laps = None
        if game_event.race_setup_id:
            race_setup = await RaceSetup.objects.filter(
                pk=game_event.race_setup_id
            ).afirst()
            num_laps = race_setup.num_laps if race_setup else None
        if (
            num_laps is not None
            and num_laps >= 1
            and game_event_char.laps - 1 >= num_laps
        ):
            game_event_char.finished = True
            just_finished = True
    elif game_event_char.laps == 0:
        game_event_char.laps = 1
    update_fields = ["section_index", "last_section_total_time_seconds", "laps"]
    if completed_lap:
        update_fields += ["lap_times", "best_lap_time"]
        if just_finished:
            update_fields.append("finished")
    await game_event_char.asave(update_fields=update_fields)

    # Record lap section time
    if section_index >= 0 and game_event_char.laps >= 1:
        lap = game_event_char.laps - 1
        await LapSectionTime.objects.aupdate_or_create(
            game_event_character=game_event_char,
            section_index=section_index,
            lap=lap,
            defaults={
                "total_time_seconds": total_time_seconds,
                "rank": game_event_char.rank,
            },
        )

    # First section time tracking — set once on the very first crossing of
    # section 0 so that net_time = last_section - first_section is the full
    # race duration.  We guard on ``is None`` rather than ``laps == 1``
    # because the SSE handler never receives lap count updates; the ``laps``
    # field in the DB would stay at 1 throughout the race, causing every
    # subsequent section-0 crossing to overwrite the value.
    if section_index == 0 and game_event_char.first_section_total_time_seconds is None:
        if total_time_seconds < 10_000_000:
            await GameEventCharacter.objects.filter(pk=game_event_char.pk).aupdate(
                first_section_total_time_seconds=total_time_seconds
            )
        else:
            await GameEventCharacter.objects.filter(pk=game_event_char.pk).aupdate(
                first_section_total_time_seconds=0
            )

    # Update Discord embed to reflect section progress (throttled)
    asyncio.create_task(
        _throttled_update_embed(game_event.pk, ctx.discord_client)
    )

    return 0, 0, 0, 0


@register("ServerRemoveEvent")
async def handle_remove_event(event, player, character, ctx):
    """Handle ServerRemoveEvent: no-op (events are managed by game state)."""
    return 0, 0, 0, 0


@register("ServerJoinEvent")
async def handle_join_event(event, player, character, ctx):
    """Handle ServerJoinEvent: trigger Discord embed update for the event."""
    event_guid = event["data"].get("EventGuid", "")
    if not event_guid:
        return 0, 0, 0, 0

    game_event = await GameEvent.objects.filter(guid=event_guid).afirst()
    if game_event is None:
        return 0, 0, 0, 0

    asyncio.create_task(
        _update_discord_event_embed(game_event.pk, ctx.discord_client)
    )

    return 0, 0, 0, 0


@register("ServerLeaveEvent")
async def handle_leave_event(event, player, character, ctx):
    """Handle ServerLeaveEvent: trigger Discord embed update for the event."""
    event_guid = event["data"].get("EventGuid", "")
    if not event_guid:
        return 0, 0, 0, 0

    game_event = await GameEvent.objects.filter(guid=event_guid).afirst()
    if game_event is None:
        return 0, 0, 0, 0

    asyncio.create_task(
        _update_discord_event_embed(game_event.pk, ctx.discord_client)
    )

    return 0, 0, 0, 0
