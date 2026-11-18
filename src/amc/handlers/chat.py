"""Chat event handlers.

Handles: ServerSendChat (from MTDediMod webhook events)

The log pipeline (PlayerChatMessageLogEvent in tasks.py) is the primary
processor for normal chat (category 0). This handler only processes
non-normal chat categories (Company/Proximity), which are produced when
muted players' messages are redirected by the mod.
"""

from __future__ import annotations

import asyncio
import logging

from django.utils import timezone

from amc.handlers import register
from amc.models import PlayerChatLog
from amc.game_server import get_player_info
from amc.mod_server import get_player as get_mod_player

logger = logging.getLogger("amc.webhook.handlers.chat")


@register("ServerSendChat")
async def handle_server_send_chat(event, player, character, ctx):
    data = event.get("data", {})
    message = data.get("Message", "")
    category = data.get("Category", 0)
    character_guid = data.get("CharacterGuid", "")
    unique_id = data.get("UniqueID", "")

    if not message or not character_guid:
        return 0, 0, 0, 0

    is_normal_chat = category == 0
    if is_normal_chat:
        return 0, 0, 0, 0

    is_bot_command = message.startswith("/bot ")

    # Muted players keep ordinary chat commands (song requests, news, tags,
    # etc.) — the mod already suppresses their visible chat by redirecting it to
    # a non-normal category. What a mute must NOT do is let the player drive the
    # AI bot invisibly: block both AI-bot channels (`/bot` and `@annie`) which
    # would otherwise still reach Annie from a muted player's hidden message.
    from amc.mute import is_muted

    bot_channels = is_bot_command or "@annie" in message.lower()
    if is_muted(player) and bot_channels:
        logger.info(
            "Suppressing AI-bot channel from muted player %s: %.80r",
            unique_id or "?",
            message,
        )
        return 0, 0, 0, 0

    try:
        await PlayerChatLog.objects.acreate(
            timestamp=timezone.now(),
            character=character,
            text=message,
        )
    except Exception:
        logger.exception("Failed to create PlayerChatLog for ServerSendChat webhook event")

    if player and character:
        from amc.command_framework import registry, CommandContext

        player_info = {}
        if ctx.http_client and unique_id:
            try:
                player_info = await get_player_info(ctx.http_client, unique_id) or {}
            except Exception:
                logger.debug("Failed to fetch player_info for chat command context")

        # The native Host Web API can come back empty (wrong/absent password is
        # fail-closed since Motor Town 0.7.20, and the endpoint also drops
        # players mid-load).  The mod's /players endpoint carries the fields
        # the commands need — Location, VehicleKey, bIsAdmin — so fall back to
        # it rather than handing the command framework a blank context.
        if not player_info.get("Location") and ctx.http_client_mod and unique_id:
            try:
                mod_info = await get_mod_player(ctx.http_client_mod, unique_id) or {}
            except Exception:  # noqa: BLE001 - mod server can be down mid-restart
                mod_info = {}
                logger.debug("Failed to fetch mod player_info for chat command context")
            if mod_info.get("Location"):
                player_info = {
                    **player_info,
                    "CharacterGuid": mod_info.get(
                        "CharacterGuid", player_info.get("CharacterGuid", "")
                    ),
                    "PlayerName": mod_info.get(
                        "PlayerName", player_info.get("PlayerName", "")
                    ),
                    "Location": mod_info["Location"],
                    "VehicleKey": mod_info.get(
                        "VehicleKey", player_info.get("VehicleKey", "None")
                    ),
                    "bIsAdmin": mod_info.get(
                        "bIsAdmin", player_info.get("bIsAdmin", False)
                    ),
                }
                logger.debug(
                    "Native player info unavailable for %s — using mod player info",
                    unique_id,
                )

        cmd_ctx = CommandContext(
            timestamp=timezone.now(),
            character=character,
            player=player,
            http_client=ctx.http_client,
            http_client_mod=ctx.http_client_mod,
            discord_client=ctx.discord_client,
            player_info=player_info,
            is_current_event=True,
        )

        asyncio.create_task(registry.execute(message, cmd_ctx))

    from amc.api.bot_events import emit_bot_event

    player_name = character.name if character else "Unknown"
    player_id = str(player.unique_id) if player else None
    discord_id = player.discord_user_id if player else None

    asyncio.create_task(
        emit_bot_event(
            {
                "type": "chat_message",
                "timestamp": timezone.now().isoformat(),
                "player_name": player_name,
                "player_id": player_id,
                "discord_id": discord_id,
                "character_guid": str(character.guid) if character and character.guid else None,
                "message": message[5:] if is_bot_command else message,
                "is_bot_command": is_bot_command,
            }
        )
    )

    return 0, 0, 0, 0