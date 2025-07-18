import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
import threading
from arq.connections import RedisSettings
from arq import cron
import django
django.setup()
from django.conf import settings
from django.utils import timezone
from amc.tasks import process_log_line
from amc.events import monitor_events
from amc.locations import monitor_locations
import discord

REDIS_SETTINGS = RedisSettings(**settings.REDIS_SETTINGS)

intents = discord.Intents.default()
intents.messages = True
intents.members = True
intents.message_content = True
client = discord.Client(intents=intents)

bot_task_handle = None
global loop


def run_blocking_bot():
  try:
    client.run(settings.DISCORD_TOKEN)
  except Exception as e:
    print(f"Error in bot thread: {e}")
  except asyncio.CancelledError:
    client.close()

async def run_discord():
  global loop
  loop = asyncio.get_running_loop()
  await loop.run_in_executor(
    ThreadPoolExecutor(max_workers=1),
    run_blocking_bot
  )


async def startup(ctx):
  global bot_task_handle
  ctx['startup_time'] = timezone.now()
  ctx['http_client'] = aiohttp.ClientSession(base_url=settings.GAME_SERVER_API_URL)
  ctx['http_client_mod'] = aiohttp.ClientSession(base_url=settings.MOD_SERVER_API_URL)
  if settings.DISCORD_TOKEN:
    ctx['discord_client'] = client
    bot_task_handle = asyncio.create_task(run_discord())


async def shutdown(ctx):
  if http_client := ctx.get('http_client'):
    await http_client.close()

  if http_client_mod := ctx.get('http_client_mod'):
    await http_client_mod.close()

  if bot_task_handle:
    asyncio.run_coroutine_threadsafe(client.close(), client.loop)
    await bot_task_handle


class WorkerSettings:
    functions = [process_log_line]
    cron_jobs = [
        cron(monitor_events, second=None),
        cron(monitor_locations, second=None),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = REDIS_SETTINGS
    max_jobs = 30

