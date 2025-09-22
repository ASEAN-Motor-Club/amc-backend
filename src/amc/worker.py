import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
from arq.connections import RedisSettings
from arq import cron
import django
django.setup()
from django.conf import settings
from django.utils import timezone
from amc.tasks import process_log_line
from amc.events import monitor_events, send_event_embeds
from amc.locations import monitor_locations
from amc.webhook import monitor_webhook
from amc.ubi import handout_ubi, TASK_FREQUENCY as UBI_TASK_FREQUENCY
from amc.deliverypoints import monitor_deliverypoints
import discord
from amc.discord_client import bot as discord_client
from amc_finance.services import apply_interest_to_bank_accounts

REDIS_SETTINGS = RedisSettings(**settings.REDIS_SETTINGS)

bot_task_handle = None
global loop

def run_blocking_bot():
  try:
    discord_client.run(settings.DISCORD_TOKEN)
  except Exception as e:
    print(f"Error in bot thread: {e}")
  except asyncio.CancelledError:
    discord_client.close()

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
  ctx['http_client_event'] = aiohttp.ClientSession(base_url=settings.EVENT_GAME_SERVER_API_URL)
  ctx['http_client_event_mod'] = aiohttp.ClientSession(base_url=settings.EVENT_MOD_SERVER_API_URL)

  if settings.DISCORD_TOKEN:
    ctx['discord_client'] = discord_client
    bot_task_handle = asyncio.create_task(run_discord())


async def shutdown(ctx):
  if http_client := ctx.get('http_client'):
    await http_client.close()

  if http_client_mod := ctx.get('http_client_mod'):
    await http_client_mod.close()

  if http_client := ctx.get('http_client_event'):
    await http_client.close()

  if http_client := ctx.get('http_client_event_mod'):
    await http_client.close()

  if bot_task_handle and (discord_client := ctx.get('discord_client')):
    asyncio.run_coroutine_threadsafe(discord_client.close(), discord_client.loop)
    await bot_task_handle

async def monitor_event_locations(ctx):
  await monitor_locations({'http_client_mod': ctx['http_client_event_mod']})

class WorkerSettings:
    functions = [process_log_line]
    cron_jobs = [
        cron(monitor_webhook, second=set(range(0, 60, 4))),
        cron(monitor_locations, second=None),
        cron(handout_ubi, minute=set(range(0, 60, UBI_TASK_FREQUENCY)), second=37),
        cron(apply_interest_to_bank_accounts, hour=None, minute=0, second=0),
        # cron(monitor_events, second=None),
        # cron(send_event_embeds, second=set(range(0, 60, 10))),
        # cron(monitor_event_locations, second=None),
        cron(monitor_deliverypoints, second=set(range(0, 60, 7))),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = REDIS_SETTINGS
    max_jobs = 30

