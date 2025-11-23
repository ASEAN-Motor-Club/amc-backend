import asyncio
import discord
from django.conf import settings
from necesse.server_logs import (
  parse_log_line,
  LogEvent,
  PlayerChatMessageLogEvent,
  PlayerLoginLogEvent,
  PlayerLogoutLogEvent,
  PrintLogEvent,
  UnknownLogEntry,
)
from necesse.models import (
  NCServerLog as ServerLog
)

async def forward_to_discord(client, channel_id, content, escape_mentions=True, **kwargs):
  if not client.is_ready():
    await client.wait_until_ready()

  allowed_mentions = discord.AllowedMentions.all()
  if escape_mentions:
    content = discord.utils.escape_mentions(content)
    allowed_mentions = discord.AllowedMentions.none()

  channel = client.get_channel(int(channel_id))
  if channel:
    return await channel.send(content, allowed_mentions=allowed_mentions, **kwargs)

async def process_log_event(event: LogEvent, ctx = {}, hostname=''):
  discord_client = ctx.get('discord_client')
  timestamp = event.timestamp

  forward_message = None

  match event:
    case PlayerChatMessageLogEvent(timestamp, player_name, message):
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_NECESSE_GAME_CHAT_CHANNEL_ID,
          f"**{player_name}:** {message}"
        )
    case PlayerLoginLogEvent(timestamp, player_name):
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_NECESSE_GAME_CHAT_CHANNEL_ID,
          f"**🟢 Player Login:** {player_name}"
        )
    case PlayerLogoutLogEvent(timestamp, player_name):
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_NECESSE_GAME_CHAT_CHANNEL_ID,
          f"**🔴 Player Logout:** {player_name}"
        )

    case PrintLogEvent(timestamp, message):
      if discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time'):
        forward_message = (
          settings.DISCORD_NECESSE_GAME_CHAT_CHANNEL_ID,
          message
        )

    case UnknownLogEntry(timestamp, _original_line):
      pass

    case _:
      pass

  if forward_message and discord_client and ctx.get('startup_time') and timestamp > ctx.get('startup_time') and hostname == "asean-mt-server":
    forward_message_channel_id, forward_message_content = forward_message
    asyncio.run_coroutine_threadsafe(
      forward_to_discord(
        discord_client,
        forward_message_channel_id,
        forward_message_content[:240]
      ),
      discord_client.loop
    )

async def process_necesse_log(ctx, line):
  log, event = parse_log_line(line)
  server_log, server_log_created = await ServerLog.objects.aget_or_create(
    timestamp=log.timestamp,
    hostname=log.hostname,
    tag=log.tag,
    text=log.content,
    log_path=log.log_path,
  )
  if not server_log_created and server_log.event_processed:
    return {'status': 'duplicate', 'timestamp': event.timestamp}

  await process_log_event(
    event,
    ctx=ctx,
    hostname=log.hostname
  )

  server_log.event_processed = True
  await server_log.asave(update_fields=['event_processed'])

  return {'status': 'created', 'timestamp': event.timestamp}

