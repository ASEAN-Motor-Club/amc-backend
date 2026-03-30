from amc.command_framework import registry, CommandContext
from amc.game_server import get_players
from amc.models import CriminalRecord, PoliceSession
from amc.special_cargo import calculate_criminal_level
from django.utils import timezone
from django.utils.translation import gettext_lazy


def _format_duration(td):
    """Format a timedelta as a compact human-readable string, e.g. '3d 12h', '23h 45m'."""
    total_seconds = int(td.total_seconds())
    if total_seconds <= 0:
        return "0m"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@registry.register(
    "/wanted",
    description=gettext_lazy("List wanted criminals"),
    category="Faction",
)
async def cmd_wanted(ctx: CommandContext):
    now = timezone.now()

    # Fetch active criminal records with character data
    records = [
        r
        async for r in CriminalRecord.objects.filter(expires_at__gt=now).select_related(
            "character"
        )
    ]

    if not records:
        await ctx.reply("No wanted criminals")
        return

    # Exclude characters with active police sessions
    active_cop_ids = set()
    async for session in PoliceSession.objects.filter(ended_at__isnull=True):
        active_cop_ids.add(session.character_id)
    records = [r for r in records if r.character_id not in active_cop_ids]

    if not records:
        await ctx.reply("No wanted criminals")
        return

    # Calculate criminal level for each record
    entries = []
    for record in records:
        laundered = record.character.criminal_laundered_total
        level = calculate_criminal_level(laundered)
        on_record = now - record.created_at
        entries.append(
            {
                "name": record.character.name,
                "guid": record.character.guid,
                "level": level,
                "laundered": laundered,
                "on_record": on_record,
            }
        )

    # Determine online character GUIDs
    online_guids = set()
    players = await get_players(ctx.http_client)
    if players:
        for _uid, pdata in players:
            guid = pdata.get("character_guid")
            if guid:
                online_guids.add(guid)

    # Split into online/offline, sort by criminal level desc, then longest on record first
    online = sorted(
        [e for e in entries if e["guid"] in online_guids],
        key=lambda e: (e["level"], e["on_record"]),
        reverse=True,
    )
    offline = sorted(
        [e for e in entries if e["guid"] not in online_guids],
        key=lambda e: (e["level"], e["on_record"]),
        reverse=True,
    )

    # Build message
    msg = "<Title>Wanted List</>\n\n"

    if online:
        msg += "<Title>Online</>\n<Secondary></>\n"
        for e in online:
            msg += f"<Highlight>C{e['level']}</> - {e['name']} <Secondary>${e['laundered']:,}</> ({_format_duration(e['on_record'])})\n"
        msg += "\n"

    if offline:
        msg += "<Title>Offline</>\n<Secondary></>\n"
        for e in offline:
            msg += f"<Highlight>C{e['level']}</> - {e['name']} <Secondary>${e['laundered']:,}</> ({_format_duration(e['on_record'])})\n"

    await ctx.reply(msg.rstrip())
