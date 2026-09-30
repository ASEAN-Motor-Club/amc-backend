import asyncio
import math
import uuid
from datetime import timedelta
from urllib.parse import quote

import aiohttp
import discord
from django.conf import settings
from django.db.models import Exists, F, OuterRef, Prefetch, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from amc.config import (
    RESPECT_PER_CHECKPOINT,
    UNDERGROUND_CHAMPIONSHIP_NAME,
    underground_blood_money,
)
from amc.game_server import announce
from amc.mod_server import (
    remove_event,
    send_message_as_player,
    show_popup,
    teleport_player,
    transfer_money,
)
from amc.models import (
    Championship,
    Character,
    GameEvent,
    GameEventCharacter,
    LapSectionTime,
    RaceSetup,
    ScheduledEvent,
    TTClass,
)
from amc.utils import skip_if_running
from amc_finance.services import check_treasury_floor, send_fund_to_player_wallet


def generate_guid():
    """
    Generates a random GUID (UUID version 4).

    Returns:
        str: The generated GUID as a string.
    """
    return str(uuid.uuid4()).replace("-", "").upper()


async def setup_event(timestamp, player_id, scheduled_event, http_client_mod):
    async with http_client_mod.get("/events") as resp:
        events = (await resp.json()).get("data", [])
        for event in events:
            if event["OwnerCharacterId"]["UniqueNetId"] == str(player_id):
                raise Exception("You already have an active event")

    async with http_client_mod.get(f"/players/{player_id}") as resp:
        players = (await resp.json()).get("data", [])
        if not players:
            raise Exception("Player not found")
        player = players[0]

    event_type = getattr(scheduled_event, "event_type", 1) or 1

    # Same treatment as the rotation posts (Yuuka 2026-09-26): unique
    # per-instance name so the game client can't match the event to a
    # native template (whose popup would override ours), and no setup
    # restrictions — the popup shows none and DQ/enforcement stays ours.
    instance = await _next_tt_instance_number()
    event_name = f"{scheduled_event.name} ({instance:03d})"
    # Pinned TT class (illegal-TT twin): the [TT-xxx] name tag is what the
    # SSE hook parses into GameEvent.tt_class, which arms the start-line
    # DQ / wanted / police flow. Classless SEs (championships etc.) post
    # with no tag and are never criminalized.
    if getattr(scheduled_event, "tt_class_id", None):
        event_name = f"{event_name} [{scheduled_event.tt_class.name}]"

    data = {
        "EventGuid": generate_guid(),
        "EventName": event_name,
        "EventType": event_type,
        "OwnerCharacterId": {
            "CharacterGuid": player["CharacterGuid"].rjust(32, "0"),
            "UniqueNetId": str(player_id),
        },
    }

    if event_type == 1:
        race_setup = scheduled_event.race_setup.config
        race_setup["Route"]["Waypoints"] = [
            {
                "Translation": waypoint["Location"],
                "Scale3D": waypoint["Scale3D"],
                "Rotation": waypoint["Rotation"],
            }
            for waypoint in race_setup["Route"]["Waypoints"]
        ]
        race_setup["VehicleKeys"] = []
        race_setup["EngineKeys"] = []
        data["RaceSetup"] = race_setup
    elif event_type == 2:
        data["CaptureTheFlagSetup"] = scheduled_event.capture_the_flag_setup.config

    async with http_client_mod.post("/events", json=data) as response:
        if response.status >= 400:
            error_body = await response.json()
            raise Exception(
                f"API Error: Received status {response.status} instead of 201. Body: {error_body}"
            )
        return True


async def process_event(event):
    transition = None
    race_setup = None
    if "RaceSetup" in event:
        race_setup_hash = RaceSetup.calculate_hash(event["RaceSetup"])
        race_setup, _ = await RaceSetup.objects.aget_or_create(
            hash=race_setup_hash,
            defaults={
                "config": event["RaceSetup"],
                "name": event["RaceSetup"].get("Route", {}).get("RouteName"),
            },
        )
    owner = await Character.objects.filter(
        player__unique_id=event["OwnerCharacterId"]["UniqueNetId"],
        guid=event["OwnerCharacterId"]["CharacterGuid"],
    ).afirst()

    scheduled_event = await (
        ScheduledEvent.objects.filter(
            race_setup=race_setup,
            start_time__lte=timezone.now(),
            end_time__gte=timezone.now(),
        )
        .order_by("start_time")  # deterministic pick if several match
        .afirst()
    )

    try:
        game_event = await (
            GameEvent.objects.filter(
                guid=event["EventGuid"],
                state__lte=event["State"],
            )
            .select_related("scheduled_event")
            .alatest("start_time")
        )

        if game_event.state != event["State"]:
            transition = (game_event.state, event["State"])

        game_event.state = event["State"]
        game_event.owner = owner
        game_event.race_setup = race_setup
        if not game_event.scheduled_event:
            game_event.scheduled_event = scheduled_event
        await game_event.asave()
    except GameEvent.DoesNotExist:
        try:
            # TODO: Refactor, use the above query as the existing_event
            existing_event = await (
                GameEvent.objects.filter(
                    guid=event["EventGuid"],
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
            guid=event["EventGuid"],
            name=event["EventName"],
            state=event["State"],
            race_setup=race_setup,
            discord_message_id=discord_message_id,
            owner=owner,
            scheduled_event=scheduled_event,
        )

    async def process_player(player_info):
        character, *_ = await Character.objects.aget_or_create_character_player(
            player_info["PlayerName"],
            int(player_info["CharacterId"]["UniqueNetId"]),
            character_guid=player_info["CharacterId"]["CharacterGuid"],
        )
        player_finished = await GameEventCharacter.objects.filter(
            character=character, game_event=game_event, finished=True
        ).aexists()
        if player_finished:
            # Do not update finished players
            return

        defaults = {
            "last_section_total_time_seconds": player_info[
                "LastSectionTotalTimeSeconds"
            ],
            "section_index": player_info["SectionIndex"],
            "best_lap_time": player_info["BestLapTime"],
            "rank": player_info["Rank"],
            "laps": player_info["Laps"],
            "finished": player_info["bFinished"],
            "disqualified": player_info["bDisqualified"],
            "lap_times": list(player_info["LapTimes"]),
        }
        if game_event.state < 2:
            defaults = {
                **defaults,
                "wrong_vehicle": player_info["bWrongVehicle"],
                "wrong_engine": player_info["bWrongEngine"],
            }
        if (
            game_event.state == 2
            and player_info["SectionIndex"] == 0
            and player_info["Laps"] == 1
        ):
            # There's a bug where the first section is a big number
            if player_info["LastSectionTotalTimeSeconds"] < 10_000_000:
                defaults["first_section_total_time_seconds"] = player_info[
                    "LastSectionTotalTimeSeconds"
                ]
            else:
                defaults["first_section_total_time_seconds"] = 0

        game_event_character, _ = await GameEventCharacter.objects.aupdate_or_create(
            character=character,
            game_event=game_event,
            defaults=defaults,
            create_defaults={
                **defaults,
                "wrong_vehicle": player_info["bWrongVehicle"],
                "wrong_engine": player_info["bWrongEngine"],
            },
        )

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

        return game_event_character

    await asyncio.gather(
        *[process_player(player_info) for player_info in event["Players"]]
    )

    return game_event, transition, scheduled_event


def format_time(total_seconds: float) -> str:
    if total_seconds is None or total_seconds < 0:
        return "-"
    """Converts seconds (float) into MM:SS.sss format.

  Args:
    total_seconds: The total number of seconds as a float.

  Returns:
    A string representing the time in MM:SS.sss format.
  """
    if total_seconds is None:
        # Participants without any section crossing have a NULL net time —
        # render a dash instead of crashing the popup build.
        return "-"
    if not isinstance(total_seconds, (int, float)):
        raise TypeError("Input must be a number (int or float).")
    if total_seconds < 0:
        raise ValueError("Input seconds cannot be negative.")

    minutes = int(total_seconds // 60)
    seconds = total_seconds % 60

    # Format minutes to always have two digits
    formatted_minutes = f"{minutes:02d}"

    # Format seconds to have two digits for the integer part
    # and three digits for the fractional part
    formatted_seconds = f"{seconds:06.3f}"  # 06.3f ensures XX.YYY format

    return f"{formatted_minutes}:{formatted_seconds}"


def format_lap(seconds):
    """Lap time for display; '-' when no lap was recorded."""
    if seconds is None or seconds <= 0:
        return "-"
    return format_time(seconds)


def participant_lap_segment(participant):
    """' BL <best> LL <last>' suffix for Discord participant lines.

    Empty string when the participant recorded no laps, so sprint and
    time-trial lines stay byte-identical to the legacy format.
    """
    laps = participant.lap_times or []
    best = format_lap(participant.best_lap_time)
    last = format_lap(laps[-1]) if laps else "-"
    if best == "-" and last == "-":
        return ""
    return f" BL {best} LL {last}"


def _lap_breakdown_lines(participants):
    """Per-lap performance breakdown rendered under the results table.

    One row per recorded lap per participant: lap number, lap time, delta
    to the participant's own best lap ("BEST" on the fastest), and the
    position that lap achieved among all participants' same-index laps
    (so a driver can see which lap lost them the race).  The section is
    omitted entirely when nobody recorded a lap, so sprint / time-trial
    popups stay byte-identical to the legacy layout.

    Display-side sentinel filter: boot-age values can leak into
    ``lap_times`` via the game's own start snapshots (live evidence
    2026-09-05: 6329.98 as a first entry) and are never real laps — a
    single lap above 600s is not renderable on any current route.
    """
    def display_laps(participant):
        return [t for t in (participant.lap_times or []) if 0 < t <= 600]

    blocks = []  # (participant, laps) for everyone with at least one lap
    per_lap_laps = []  # per_lap_laps[i] = lap-i times across participants
    for participant in participants:
        laps = display_laps(participant)
        if not laps:
            continue
        blocks.append((participant, laps))
        for i, t in enumerate(laps):
            if len(per_lap_laps) <= i:
                per_lap_laps.append([])
            per_lap_laps[i].append(t)

    if not blocks:
        return []

    def lap_position(i, t):
        if len(per_lap_laps[i]) <= 1:
            return ""
        faster = sum(1 for other in per_lap_laps[i] if other < t)
        return f"P{faster + 1}"

    lines = ["", "<Title>Lap breakdown</>"]
    for participant, laps in blocks:
        best = min(laps)
        lines.append(f"<Bold>{participant.character.name.ljust(16)}</>")
        for i, t in enumerate(laps):
            marker = "BEST" if t == best else f"+{t - best:.3f}"
            position = lap_position(i, t)
            suffix = f"  {position}" if position else ""
            lines.append(f"  L{i + 1}  {t:>9.3f}s  {marker:>9}{suffix}")
    return lines


def print_results(participants):
    def best_lap(participant):
        return format_lap(participant.best_lap_time)

    def last_lap(participant):
        laps = participant.lap_times or []
        return format_lap(laps[-1]) if laps else "-"

    # Lap columns only appear when at least one participant recorded a lap
    # (multi-lap events). Sprint / time-trial popups keep the legacy layout.
    show_laps = any(
        (p.best_lap_time or 0) > 0 or p.lap_times for p in participants
    )

    def print_result(participant, rank):
        flags = []
        if not participant.finished:
            flags.append("DNF")
        if participant.wrong_engine:
            flags.append("ENGINE")
        if participant.wrong_vehicle:
            flags.append("VEHICLE")

        flags = ", ".join(flags)
        line = f"#{str(rank).zfill(2)}: <Bold>{participant.character.name.ljust(16)}</> {format_time(participant.net_time).ljust(14)}"
        if show_laps:
            line += f" BL {best_lap(participant).ljust(9)} LL {last_lap(participant).ljust(9)}"
        line += f" <Warning>{flags}</>"
        return line

    lines = [
        print_result(participant, rank)
        for rank, participant in enumerate(participants, start=1)
    ]
    lines += _lap_breakdown_lines(participants)
    return "\n".join(lines)


async def show_results_popup(
    http_client, participants, player_id=None, character_guid=None
):
    message = f"<Title>Results</>\n\n{print_results(participants)}"
    if player_id is not None or character_guid is not None:
        await show_popup(
            http_client, message, player_id=player_id, character_guid=character_guid
        )
        return

    for participant in participants:
        await show_popup(
            http_client,
            message,
            character_guid=participant.character.guid,
        )


async def show_scheduled_event_results_popup(
    http_client, scheduled_event, player_id=None, character_guid=None
):
    participants = [
        p
        async for p in GameEventCharacter.objects.results_for_scheduled_event(
            scheduled_event
        )
    ]
    await show_results_popup(
        http_client, participants, player_id=player_id, character_guid=character_guid
    )


@skip_if_running
async def monitor_events(ctx, http_client):
    discord_client = ctx.get("discord_client")
    events_cog = discord_client.get_cog("EventsCog") if discord_client else None

    try:
        async with http_client.get("/events") as resp:
            events = (await resp.json()).get("data", [])
            results = await asyncio.gather(*[process_event(event) for event in events])

            for game_event, transition, scheduled_event in results:
                if transition == (2, 3):  # Finished
                    participants = [
                        p
                        async for p in (
                            GameEventCharacter.objects.select_related(
                                "character", "character__player"
                            ).filter(
                                game_event=game_event,
                            )
                        )
                    ]
                    await show_results_popup(http_client, participants)
                    try:
                        if (
                            scheduled_event
                            and events_cog
                            and hasattr(events_cog, "update_scheduled_event_embed")
                        ):
                            loop = asyncio.get_running_loop()
                            loop.run_in_executor(
                                None,
                                lambda: asyncio.run_coroutine_threadsafe(
                                    events_cog.update_scheduled_event_embed(
                                        scheduled_event.id
                                    ),
                                    discord_client.loop,
                                ),
                            )
                    except Exception as e:
                        print(f"Failed to update scheduled event embed: {e}")

    except Exception:
        pass


def create_event_embed(game_event):
    """Displays the event information in an embed."""

    race_setup = game_event.race_setup
    url = f"https://api.aseanmotorclub.com/race_setups/{race_setup.hash}/"
    track_editor_link = (
        f"https://www.aseanmotorclub.com/track?uri={quote(url, safe='')}"
    )
    embed = discord.Embed(
        title=f"🏁 Event: {game_event.name}",
        color=discord.Color.blue(),  # You can choose any color
        url=track_editor_link,
    )

    embed.add_field(name="🔀 Route", value=str(race_setup), inline=False)

    if game_event.scheduled_event is not None:
        embed.add_field(
            name="🕒 Results",
            value=f"https://www.aseanmotorclub.com/championship?event={game_event.scheduled_event.id}",
            inline=False,
        )

    if race_setup.vehicles:
        embed.add_field(
            name="Vehicles", value=", ".join(race_setup.vehicles), inline=False
        )
    if race_setup.engines:
        embed.add_field(
            name="Engines", value=", ".join(race_setup.engines), inline=False
        )

    participant_list_str = ""
    for rank, participant in enumerate(game_event.participants.all(), start=1):
        try:
            if participant.finished:
                progress_str = format_time(participant.net_time)
            else:
                total_laps = max(race_setup.num_laps, 1)
                total_waypoints = race_setup.num_sections

                if race_setup.num_laps == 0:
                    total_waypoints = total_waypoints - 1

                progress_percentage = 0.0
                if total_waypoints > 0:
                    progress_percentage = (
                        100.0 * max(participant.laps - 1, 0) / total_laps
                    )
                    progress_percentage += (
                        100.0
                        * max(participant.section_index, 0)
                        / float(total_waypoints)
                        / total_laps
                    )
                if race_setup.num_laps > 0:
                    progress_str = f"{participant.laps}/{race_setup.num_laps} Laps - {progress_percentage:.1f}%"
                else:
                    progress_str = f"{progress_percentage:.1f}%"

            participant_line = f"{rank}. {participant.character.name} ({progress_str})"
            participant_line += participant_lap_segment(participant)

            if participant.wrong_vehicle:
                participant_line += " [Wrong Vehicle]"
            if participant.wrong_engine:
                participant_line += " [Wrong Engine]"

            participant_list_str += f"{participant_line}\n"
        except Exception as e:
            print(f"Failed to display participant: {e}")
            pass

    embed.add_field(
        name="👥 Participants", value=participant_list_str.strip(), inline=False
    )

    # You can add more fields from the 'event' dictionary if needed
    match game_event.state:
        case 1:
            state_str = "Ready"
        case 2:
            state_str = "In Progress"
        case 3:
            state_str = "Finished"
        case 0:
            state_str = "Not Ready"
        case _:
            state_str = "Unknown"
    embed.set_footer(text=f"Status: {state_str}")

    return embed


async def send_event_embed(game_event, channel):
    embed = create_event_embed(game_event)

    ## Create embed
    if game_event.discord_message_id is None:
        message = await channel.send("", embed=embed)
        game_event.discord_message_id = message.id
        await game_event.asave(update_fields=["discord_message_id"])
    else:
        try:
            message = await channel.fetch_message(game_event.discord_message_id)
            await message.edit(content="", embed=embed)
        except discord.NotFound:
            message = await channel.send("", embed=embed)
            game_event.discord_message_id = message.id
            await game_event.asave(update_fields=["discord_message_id"])


@skip_if_running
async def send_event_embeds(ctx):
    http_client = ctx.get("http_client_mod")
    discord_client = ctx.get("discord_client")
    if not discord_client:
        return
    if not isinstance(discord_client.loop, asyncio.AbstractEventLoop):
        return
    if not discord_client.is_ready():
        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(
                discord_client.wait_until_ready(), discord_client.loop
            )
        )
    channel = discord_client.get_channel(settings.DISCORD_EVENTS_CHANNEL_ID)

    try:
        async with http_client.get("/events") as resp:
            if resp.status != 200:
                return
            events = (await resp.json()).get("data", [])
    except aiohttp.ClientConnectorError:
        return

    event_guids = [event["EventGuid"] for event in events]
    qs = (
        GameEvent.objects.select_related("race_setup", "scheduled_event")
        .prefetch_related(
            Prefetch(
                "participants",
                queryset=GameEventCharacter.objects.select_related("character"),
            )
        )
        .annotate(
            rank=Window(
                expression=RowNumber(),
                partition_by=[F("guid")],
                order_by=[F("last_updated").desc()],
            )
        )
        .filter(rank=1, guid__in=event_guids)
    )

    # Also include recently-finished events that still have an embed to refresh.
    # Once the mod server removes a finished event from /events, the main query
    # above won't pick it up.  Grab any event with a discord_message_id that was
    # updated in the last 10 minutes so the embed shows final results.
    recently_finished_qs = (
        GameEvent.objects.select_related("race_setup", "scheduled_event")
        .prefetch_related(
            Prefetch(
                "participants",
                queryset=GameEventCharacter.objects.select_related("character"),
            )
        )
        .annotate(
            rank=Window(
                expression=RowNumber(),
                partition_by=[F("guid")],
                order_by=[F("last_updated").desc()],
            )
        )
        .filter(
            rank=1,
            discord_message_id__isnull=False,
            state=3,
            last_updated__gte=timezone.now() - timedelta(minutes=10),
        )
        .exclude(guid__in=event_guids)
    )

    async for game_event in qs:
        asyncio.run_coroutine_threadsafe(
            send_event_embed(game_event, channel), discord_client.loop
        )

    async for game_event in recently_finished_qs:
        asyncio.run_coroutine_threadsafe(
            send_event_embed(game_event, channel), discord_client.loop
        )

    # Remove expired embeds

    expired_discord_message_ids = list(
        set(
            [
                discord_message_id
                async for discord_message_id in (
                    GameEvent.objects.filter(
                        discord_message_id__isnull=False,
                        last_updated__gte=timezone.now() - timedelta(days=7),
                    )
                    .exclude(
                        Exists(
                            GameEventCharacter.objects.filter(
                                game_event=OuterRef("pk"), finished=True
                            )
                        )
                    )
                    .difference(qs)
                    .order_by("-last_updated")
                    .values_list("discord_message_id", flat=True)
                )[:50]
            ]
        )
    )

    async def delete_expired_messages(mIds):
        expired_discord_messages = [discord.Object(id=str(mId)) for mId in mIds]
        if expired_discord_messages:
            try:
                await GameEvent.objects.filter(discord_message_id__in=mIds).aupdate(
                    discord_message_id=None
                )
                await channel.delete_messages(expired_discord_messages)
            except Exception as e:
                print(f"Failed to delete {mIds}: {e}", flush=True)

    async def delete_unattached_embeds():
        to_delete = []
        async for m in channel.history(limit=20):
            if not (await GameEvent.objects.filter(discord_message_id=m.id).aexists()):
                to_delete.append(m)
        await channel.delete_messages(to_delete)

    asyncio.run_coroutine_threadsafe(
        delete_expired_messages(expired_discord_message_ids), discord_client.loop
    )
    asyncio.run_coroutine_threadsafe(delete_unattached_embeds(), discord_client.loop)


async def staggered_start(
    http_client_game, http_client_mod, game_event, player_id=None, delay=20.0
):
    async with http_client_mod.get(f"/events/{game_event.guid}") as resp:
        events = (await resp.json()).get("data", [])

    if not events:
        raise Exception("Event not found")
    event = events[0]

    if event["State"] != 1:
        raise Exception("Event is not in Ready state")

    participants = [player_info for player_info in event["Players"]]
    line_up_message = f"<Title>Staggered Start Line Up</>\n\nThe event will start in 30 seconds!\nYour time will only be counted when you cross the starting line\n<Secondary>Delay between participants = {delay} seconds</>\n<Announce>ONLY start when your name is called!!</>\n\n"
    line_up_message += "\n".join(
        [
            f"{idx}. {player_info['PlayerName']}"
            for idx, player_info in enumerate(participants, start=1)
        ]
    )
    for player_info in participants:
        await show_popup(
            http_client_mod,
            line_up_message,
            player_id=player_info["CharacterId"]["UniqueNetId"],
        )

    await announce(
        "The event is starting in 30 seconds!",
        http_client_game,
    )
    await asyncio.sleep(30.0)  # in-game countdown

    await announce(
        "The event is starting starting!",
        http_client_game,
    )

    await http_client_mod.post(
        f"/events/{event['EventGuid']}/state",
        json={
            "State": 2,
        },
    )

    await asyncio.sleep(5.0)  # in-game countdown

    for player_info in participants:
        await asyncio.sleep(delay)
        await asyncio.gather(
            announce(
                f"{player_info['PlayerName']} GO!!!",
                http_client_game,
            ),
            send_message_as_player(
                http_client_mod,
                "GO!!!",
                player_info["CharacterId"]["UniqueNetId"],
                category=7,
            ),
        )


def _rotate_vector_by_quaternion(vector, quat):
    """
    Rotates a 3D vector by a quaternion.

    Args:
        vector (dict): The vector to rotate {'x': float, 'y': float, 'z': float}.
        quat (dict): The quaternion for rotation {'w': float, 'x': float, 'y': float, 'z': float}.

    Returns:
        dict: The rotated vector.
    """
    # Normalize the quaternion to be safe
    q_mag = math.sqrt(quat["W"] ** 2 + quat["X"] ** 2 + quat["Y"] ** 2 + quat["Z"] ** 2)
    if q_mag == 0:
        return vector  # Avoid division by zero
    qw, qx, qy, qz = (
        quat["W"] / q_mag,
        quat["X"] / q_mag,
        quat["Y"] / q_mag,
        quat["Z"] / q_mag,
    )

    # Hamilton product: q * v * q_conjugate
    # First, q * v
    w_res = -qx * vector["X"] - qy * vector["Y"] - qz * vector["Z"]
    x_res = qw * vector["X"] + qy * vector["Z"] - qz * vector["Y"]
    y_res = qw * vector["Y"] - qx * vector["Z"] + qz * vector["X"]
    z_res = qw * vector["Z"] + qx * vector["Y"] - qy * vector["X"]

    # Then, (q * v) * q_conjugate
    final_x = w_res * -qx + x_res * qw + y_res * -qz - z_res * -qy
    final_y = w_res * -qy - x_res * -qz + y_res * qw + z_res * -qx
    final_z = w_res * -qz + x_res * -qy - y_res * -qx + z_res * qw

    return {"X": final_x, "Y": final_y, "Z": final_z}


async def auto_starting_grid(http_client_mod, game_event):
    async with http_client_mod.get(f"/events/{game_event.guid}") as resp:
        events = (await resp.json()).get("data", [])

    if not events:
        raise Exception("Event not found")
    event = events[0]

    if event["State"] != 1:
        raise Exception("Event is not in Ready state")

    participants = [player_info for player_info in event["Players"]]

    config = {
        "lateral_spacing": game_event.race_setup.lateral_spacing,
        "longitudinal_spacing": game_event.race_setup.longitudinal_spacing,
        "initial_offset": game_event.race_setup.initial_offset,
        "pole_side": "right" if game_event.race_setup.pole_side_right else "left",
        "reverse_starting_direction": game_event.race_setup.reverse_starting_direction,
    }
    starting_point = game_event.race_setup.waypoints[0]
    start_pos = starting_point["Location"]
    start_quat = starting_point["Rotation"]
    lateral_spacing = int(config.get("lateral_spacing", 600))
    longitudinal_spacing = int(config.get("longitudinal_spacing", 1000))
    initial_offset = int(config.get("initial_offset", 1000))
    pole_side = str(config.get("pole_side", "right"))

    # --- 2. Vector Calculations from Quaternion ---
    # Define base vectors in a standard coordinate system (e.g., X-Forward, Y-Left, Z-Up)
    base_forward = {"X": -1, "Y": 0, "Z": 0}
    base_right = {"X": 0, "Y": -1, "Z": 0}  # Negative Y is right if positive Y is left

    # Rotate these base vectors by the start line's quaternion to get world-space directions
    forward_vec = _rotate_vector_by_quaternion(base_forward, start_quat)
    right_vec = _rotate_vector_by_quaternion(base_right, start_quat)

    # For the output, calculate the effective yaw from the new forward vector
    yaw_deg = math.degrees(math.atan2(forward_vec["Y"], forward_vec["X"]))
    if config.get("reverse_starting_direction", False):
        yaw_deg += 180

    pole_side_multiplier = 1 if pole_side == "right" else -1

    for i, player_info in enumerate(participants):
        row = i // 2
        side = 1 if i % 2 == 0 else -1

        # a) Longitudinal offset (how far back from the line)
        total_longitudinal_offset = initial_offset + (row * longitudinal_spacing)
        longitudinal_displacement = {
            "X": -total_longitudinal_offset * forward_vec["X"],
            "Y": -total_longitudinal_offset * forward_vec["Y"],
            "Z": -total_longitudinal_offset * forward_vec["Z"],
        }

        # b) Lateral offset (how far to the side of the line)
        total_lateral_offset = side * pole_side_multiplier * (lateral_spacing / 2)
        lateral_displacement = {
            "X": total_lateral_offset * right_vec["X"],
            "Y": total_lateral_offset * right_vec["Y"],
            "Z": total_lateral_offset * right_vec["Z"],  # Account for roll
        }

        # --- 4. Final Position Calculation ---
        final_x = (
            start_pos["X"] + longitudinal_displacement["X"] + lateral_displacement["X"]
        )
        final_y = (
            start_pos["Y"] + longitudinal_displacement["Y"] + lateral_displacement["Y"]
        )
        final_z = (
            start_pos["Z"] + longitudinal_displacement["Z"] + lateral_displacement["Z"]
        )

        player_location = {
            "X": final_x,
            "Y": final_y,
            "Z": final_z + 20,
        }
        player_rotation = {"Roll": 0, "Pitch": 0, "Yaw": yaw_deg}
        await asyncio.sleep(0.2)
        await teleport_player(
            http_client_mod,
            player_info["CharacterId"]["UniqueNetId"],
            player_location,
            player_rotation,
        )


_TT_INSTANCE: int | None = None


def _underground_description(tt_class, checkpoints: int) -> str:
    """Requirements text the auto-poster writes on the mirrored SE."""
    from amc.config import BLOOD_MONEY_PER_CHECKPOINT, UNDERGROUND_VEHICLE_TYPES

    first = checkpoints * BLOOD_MONEY_PER_CHECKPOINT
    flat = underground_blood_money(checkpoints, 5)
    lines = [
        f"Underground street race — {tt_class.name}: max {tt_class.max_hp} HP, "
        f"vanilla tires only, {' & '.join(UNDERGROUND_VEHICLE_TYPES)} vehicles only."
    ]
    if BLOOD_MONEY_PER_CHECKPOINT > 0:
        lines.append(
            f"Blood Money: 1st {first} → halves each place → flat {flat} from 5th on. "
            f"Respect: {checkpoints * RESPECT_PER_CHECKPOINT}."
        )
    else:
        # Rate 0 = payouts disabled pending community discussion.
        lines.append(f"Respect: {checkpoints * RESPECT_PER_CHECKPOINT}.")
    return "\n".join(lines)


@skip_if_running
async def _mirror_posted_event(
    scheduled_event, tt_class, event_name: str, config: dict
) -> ScheduledEvent:
    """Mirror a posted underground instance as its own ScheduledEvent row.

    The posted setup is the RE-SERIALIZED config (Location→Translation
    waypoint rewrite), which hashes differently from the template's
    original RaceSetup — mirror the setup row the hook will actually
    resolve (same calculate_hash), then the SE link lands on this mirror
    (newest start_time among SEs on that setup). Carries the rolled
    class, the requirements text, and is_rotation_instance=True so it
    can never become a rotation candidate itself.
    """
    from amc.models import RaceSetup

    race_setup, _ = await RaceSetup.objects.aget_or_create(
        hash=RaceSetup.calculate_hash(config),
        defaults={
            "config": config,
            "name": config.get("Route", {}).get("RouteName"),
        },
    )
    checkpoints = len((config.get("Route", {}).get("Waypoints")) or [])
    description = _underground_description(tt_class, checkpoints)
    return await ScheduledEvent.objects.acreate(
        name=event_name,
        start_time=timezone.now(),
        end_time=timezone.now() + timedelta(days=14),
        race_setup=race_setup,
        championship=await _underground_championship(),
        description=description,
        description_in_game=description,
        time_trial=scheduled_event.time_trial,
        tt_class=tt_class,
        is_rotation_instance=True,
    )


@skip_if_running
async def _underground_championship() -> Championship:
    champs, _ = await Championship.objects.aget_or_create(
        name=UNDERGROUND_CHAMPIONSHIP_NAME,
        defaults={"description": ""},
    )
    return champs


@skip_if_running
async def pay_underground_rotation_rewards(ctx, live_guids: set[str] | None):
    """Rotation-end Blood Money / Respect payout (Yuuka 2026-09-29).

    An underground event that is no longer live in-game and not yet paid
    settles exactly once: every participant who actually raced (laps > 0)
    gets Blood Money by finish position (halving ladder, flattened from
    5th) plus Respect (rate 0 today). Idempotent via GameEvent.rewards_paid.
    """
    http_client_mod = ctx["http_client_mod"]
    unpaid = GameEvent.objects.filter(
        rewards_paid=False,
        auto_created=True,
        scheduled_event__championship__name=UNDERGROUND_CHAMPIONSHIP_NAME,
        scheduled_event__is_rotation_instance=True,
    ).select_related("scheduled_event", "scheduled_event__tt_class", "race_setup")
    async for game_event in unpaid:
        if live_guids is not None and game_event.guid in live_guids:
            continue
        # Claim the row FIRST — a single writer settles the payout.
        claimed = await GameEvent.objects.filter(
            pk=game_event.pk, rewards_paid=False
        ).aupdate(rewards_paid=True)
        if not claimed:
            continue
        checkpoints = len(
            (game_event.race_setup.config.get("Route", {}).get("Waypoints")) or []
        ) if game_event.race_setup else 0
        racers = (
            game_event.participants.filter(laps__gt=0)
            .select_related("character")
            .order_by(
                "-finished",
                F("net_time").asc(nulls_last=True),
                "-laps",
                "-section_index",
            )
        )
        position = 0
        paid, failed = [], []
        async for row in racers:
            position += 1
            amount = underground_blood_money(checkpoints, position)
            character = row.character
            try:
                if amount > 0:
                    # Treasury-funded: the government loses the money. Skip
                    # BOTH the game transfer and the ledger entry when the
                    # Treasury Fund would breach its floor (same pattern as
                    # subsidise_player) — no payout from thin air.
                    if not await check_treasury_floor(amount):
                        failed.append(
                            f"{character.name}=P{position}:{amount}:treasury"
                        )
                        print(
                            f"Underground payout: treasury at floor, skipping "
                            f"{character.name} ({game_event.name} P{position})"
                        )
                    else:
                        await transfer_money(
                            http_client_mod,
                            amount,
                            f"Blood Money — {game_event.name} (P{position})",
                            str(character.player_id),
                        )
                        # Ledger: Dr Treasury Expenses / Cr Treasury Fund.
                        await send_fund_to_player_wallet(
                            amount,
                            character,
                            f"Blood Money — {game_event.name} (P{position})",
                        )
                if RESPECT_PER_CHECKPOINT > 0:
                    respect_amount = checkpoints * RESPECT_PER_CHECKPOINT
                    character.respect = (character.respect or 0) + respect_amount
                    # Criminal-level pipe (Yuuka 2026-09-30): underground
                    # respect feeds the criminal score too — the rap sheet
                    # counts the race, the level derives live from the
                    # score (same F-expression accrual as illicit
                    # deliveries in special_cargo).
                    character.criminal_score = F("criminal_score") + respect_amount
                    await character.asave(update_fields=["respect", "criminal_score"])
                paid.append(f"{character.name}=P{position}:{amount}")
            except Exception as e:
                # Per-racer containment: one failed transfer never blocks
                # the rest. The event stays marked paid (no double-pay on
                # retries) and the miss is logged loudly.
                failed.append(f"{character.name}=P{position}:{amount}")
                print(
                    f"Underground payout: transfer FAILED for {character.name} "
                    f"({game_event.name} P{position}, {amount}): {e}"
                )
        print(
            f"Underground payout: {game_event.name} ({game_event.guid}) "
            f"C={checkpoints} paid=[{', '.join(paid)}] failed=[{', '.join(failed)}]"
        )


@skip_if_running
async def _next_tt_instance_number() -> int:
    """Monotonic counter for posted TT event instance names (001, 002, ...).

    Derived from the highest "(NNN)" already present on posted GameEvent
    rows (the DB event history), cached in-process and incremented per
    post. Single worker process, so no locking concern.
    """
    global _TT_INSTANCE
    if _TT_INSTANCE is None:
        import re

        from amc.models import GameEvent

        highest = 0
        async for name in GameEvent.objects.values_list("name", flat=True):
            m = re.search(r"\((\d{3})\)", name or "")
            if m:
                highest = max(highest, int(m.group(1)))
        _TT_INSTANCE = highest
    _TT_INSTANCE += 1
    return _TT_INSTANCE


@skip_if_running
async def post_random_events(ctx):
    http_client_mod = ctx["http_client_mod"]

    stale_events = GameEvent.objects.filter(
        auto_created=True,
        state=0,
    )
    async for game_event in stale_events:
        try:
            await remove_event(http_client_mod, game_event.guid)
        except Exception as e:
            # RemoveEvent needs at least one player online (mod-side
            # PlayerArray guard) — on an empty server the delete 400s and
            # the in-game event lingers. Log instead of passing silently.
            print(
                f"Auto-TT: failed to remove stale event {game_event.guid} "
                f"(state=0): {e}"
            )
        game_event.state = 3
        await game_event.asave(update_fields=["state"])

    # Live list snapshot — drives both reconcile steps below. If the fetch
    # fails, skip the reconcile entirely: an empty/absent live list must
    # NEVER read as "no events live" (it would close every tracked row).
    live_guids: set[str] | None = None
    try:
        async with http_client_mod.get("/events") as resp:
            if resp.status < 400:
                payload = (await resp.json()).get("data", [])
                events = payload.values() if isinstance(payload, dict) else payload
                live_guids = {
                    ev.get("EventGuid", "") for ev in events if ev.get("EventGuid")
                }
    except Exception as e:
        print(f"Auto-TT: failed to fetch live events: {e}")

    if live_guids is not None:
        # 0) Underground rotation-end payout: settle every classed
        #    underground event that left the live list and was never paid
        #    (Yuuka 2026-09-29 — rewards fire at rotation end, not per run).
        await pay_underground_rotation_rewards(ctx, live_guids)

        # 1) Vanished events: the game silently deletes unclaimed owner-less
        #    events a few minutes after creation (observed 2026-09-22 on the
        #    test server — no RemoveEvent hook fires). Close their Ready,
        #    never-raced rows so the slots and setups free up again.
        async for game_event in GameEvent.objects.filter(
            auto_created=True, state=1
        ):
            if game_event.guid in live_guids:
                continue
            if await game_event.participants.aexists():
                continue  # raced/joined rows are never closed by the cron
            print(
                f"Auto-TT: event {game_event.guid} ({game_event.name}) is no "
                f"longer live in-game; closing stale row {game_event.id}"
            )
            await GameEvent.objects.filter(pk=game_event.pk).aupdate(state=3)

        # 2) Rotation: each tick replaces owner-less Ready events that nobody
        #    joined during their window (Yuuka 2026-09-22: events should get
        #    replaced when the time is up). Joined/raced events are left
        #    alone — a player who joined mid-window keeps their event.
        live_rows = [
            ge
            async for ge in GameEvent.objects.filter(
                auto_created=True, state=1, guid__in=live_guids
            )
        ]
        for game_event in live_rows:
            if await game_event.participants.aexists():
                continue
            try:
                await remove_event(http_client_mod, game_event.guid)
            except Exception as e:
                print(
                    f"Auto-TT: failed to rotate event {game_event.guid} "
                    f"({game_event.name}): {e}"
                )
                continue
            print(
                f"Auto-TT: rotated out unclaimed event {game_event.guid} "
                f"({game_event.name})"
            )
            await GameEvent.objects.filter(pk=game_event.pk).aupdate(state=3)

    active_auto = await GameEvent.objects.filter(
        auto_created=True,
        state__gte=0,
        state__lt=3,
    ).acount()

    TARGET_EVENTS = 1  # one event per reset (Yuuka 2026-09-26: "only 1 every reset")
    slots_to_fill = TARGET_EVENTS - active_auto
    if slots_to_fill <= 0:
        return

    active_race_setup_ids = (
        GameEvent.objects.filter(auto_created=True, state__lt=3)
        .values_list("race_setup_id", flat=True)
    )

    # Pool = Jeju Underground Street Racing TEMPLATES (Yuuka 2026-09-29):
    # hand-made duplicates of the original SEs living in the underground
    # championship — time trials AND sprints (Yuuka 2026-09-30: "rework
    # them", sprint templates join the pool) — with NO pinned class; the
    # class ROLLS per post (it is the HP cap that arms DQ/enforcement).
    # The old pinned-class twin filter is gone; mirrored instance rows
    # (is_rotation_instance=True) are never candidates. No window gate
    # (#301): the daily rotation owns each auto event's lifetime, and
    # handlers/events.py links the SE via the out-of-window fallback.
    candidate_qs = (
        ScheduledEvent.objects.filter(
            race_setup__isnull=False,
            tt_class__isnull=True,
            is_rotation_instance=False,
            championship__name=UNDERGROUND_CHAMPIONSHIP_NAME,
        )
        .exclude(race_setup_id__in=active_race_setup_ids)
        .select_related("race_setup")
        .order_by("?")
    )
    # 0-lap events only for rotation (Yuuka 2026-09-26: "only rotate events
    # that have 0 laps for now") — NumLaps is a JSON config key that may be
    # absent (= 0 per RaceSetup.num_laps), so filter in Python rather than
    # risk a JSON-path lookup dropping absent-key setups.
    candidates = [
        se async for se in candidate_qs if (se.race_setup.num_laps or 0) == 0
    ][:slots_to_fill]

    if not candidates:
        return

    posted_names = []
    for scheduled_event in candidates:
        race_setup = scheduled_event.race_setup
        config = dict(race_setup.config)
        config["Route"]["Waypoints"] = [
            {
                "Translation": wp["Location"],
                "Scale3D": wp["Scale3D"],
                "Rotation": wp["Rotation"],
            }
            for wp in config["Route"]["Waypoints"]
        ]
        if not config.get("VehicleKeys"):
            config["VehicleKeys"] = []
        if not config.get("EngineKeys"):
            config["EngineKeys"] = []

        # Class ROLLS per post (Yuuka 2026-09-29, back to the #190 behavior
        # with the new 10-tier ladder — templates carry no pin). The class
        # rides in the event-name tag ([TT-480]) — that tag is the only
        # reliable per-instance channel: the DB GameEvent row is created
        # later by the SSE hook, which parses the tag back into
        # GameEvent.tt_class.
        tt_class = await TTClass.objects.order_by("?").afirst()
        if not tt_class:
            print("Auto-TT: no TTClass rows exist — cannot post underground event")
            continue
        # Per-instance counter so every posted event is uniquely named.
        # The game client matches posted events to its native event
        # templates by name (a native name like "Get The Priest! - Time
        # Trial" gets the template's popup requirements regardless of our
        # setup) — a unique suffix defeats that match (Yuuka 2026-09-26).
        instance = await _next_tt_instance_number()
        event_name = f"{scheduled_event.name} ({instance:03d})"
        if tt_class:
            event_name = f"{event_name} [{tt_class.name}]"
            # Clear the setup's stale, class-conflicting restrictions
            # (Yuuka 2026-09-26: "the override should be to NONE, we rely
            # on our DQ checks (total hp)") — the game popup then shows no
            # requirements and the start-line DQ (total-hp + vanilla-tires +
            # vehicle-type check) is the single source of enforcement.
            config["EngineKeys"] = []
            config["VehicleKeys"] = []

        data = {
            "EventGuid": generate_guid(),
            "EventName": event_name,
            "EventType": 1,
            "RaceSetup": config,
        }

        try:
            async with http_client_mod.post("/events", json=data) as resp:
                if resp.status >= 400:
                    error_body = await resp.text()
                    print(
                        f"Auto-TT: failed to post event "
                        f"{scheduled_event.name}: {resp.status} {error_body}"
                    )
                else:
                    # Mirror the instance as its own SE row (admin panel:
                    # rolled class, requirements text, reward totals).
                    await _mirror_posted_event(
                        scheduled_event, tt_class, event_name, config
                    )
                    posted_names.append(scheduled_event.name)
        except Exception as e:
            print(f"Auto-TT: failed to post event {scheduled_event.name}: {e}")

    # Announce ONLY what actually reached the game server. The old code
    # announced unconditionally — even when every POST failed, players saw
    # "New time trial events available!" with no events existing.
    if posted_names:
        await announce(
            f"New underground racing events available: "
            f"{', '.join(posted_names)}! Use /events to see them.",
            ctx["http_client"],
        )
