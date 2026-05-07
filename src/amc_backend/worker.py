import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
from arq.connections import RedisSettings
from arq import cron
import django

django.setup()
from django.conf import settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from amc.tasks import process_log_line  # noqa: E402
import amc.tasks as tasks_module  # noqa: E402
from necesse.tasks import process_necesse_log  # noqa: E402
from amc.events import monitor_events, send_event_embeds, post_random_events  # noqa: E402
from amc.locations import monitor_locations, run_location_listener  # noqa: E402
from amc.characterlocation_stats import refresh_all_vehicle_stats  # noqa: E402
from amc.webhook import monitor_webhook, WEBHOOK_SSE_ENABLED  # noqa: E402
from amc.sse_client import run_sse_listener  # noqa: E402
from amc.ubi import handout_ubi, TASK_FREQUENCY as UBI_TASK_FREQUENCY  # noqa: E402
from amc.deliverypoints import monitor_deliverypoints  # noqa: E402
from amc.criminals import (  # noqa: E402
    refresh_suspect_tags,
    tick_criminal_record_decay,
    tick_police_suspect_locations,
    tick_wanted_countdown,
)

from amc.jobs import monitor_jobs  # noqa: E402
from amc.status import monitor_server_status  # noqa: E402
from amc.gov_employee import expire_gov_employees  # noqa: E402
from amc.supply_chain import monitor_supply_chain_events  # noqa: E402
from amc.rescue_reminder import send_rescue_reminders  # noqa: E402
import discord  # noqa: E402
from amc.discord_client import bot as discord_client  # noqa: E402
from amc_finance.services import (  # noqa: E402
    apply_interest_to_bank_accounts,
    apply_wealth_tax,
    transfer_nirc,
)
from amc_finance.loans import evaluate_credit_scores  # noqa: E402

REDIS_SETTINGS = RedisSettings(**settings.REDIS_SETTINGS)

# Global timeout for all game/mod server API calls (prevents 5-min default)
GAME_SERVER_TIMEOUT = aiohttp.ClientTimeout(total=10)

bot_task_handle = None
sse_task_handle = None
location_sse_task_handle = None
# pyrefly: ignore [unknown-name]
loop = None


def run_blocking_bot():
    discord.utils.setup_logging(root=False)
    try:
        discord_client.run(settings.DISCORD_TOKEN)
    except Exception as e:
        print(f"Error in bot thread: {e}")
    except asyncio.CancelledError:
        # pyrefly: ignore [unused-coroutine]
        discord_client.close()


async def run_discord():
    global loop
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(ThreadPoolExecutor(max_workers=1), run_blocking_bot)


async def startup(ctx):
    global bot_task_handle, sse_task_handle, location_sse_task_handle
    ctx["startup_time"] = timezone.now()
    ctx["http_client"] = aiohttp.ClientSession(
        base_url=settings.GAME_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )
    ctx["http_client_mod"] = aiohttp.ClientSession(
        base_url=settings.MOD_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )
    ctx["http_client_webhook"] = aiohttp.ClientSession(
        base_url=settings.WEBHOOK_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )
    ctx["http_client_mgmt"] = aiohttp.ClientSession(
        base_url=settings.MOD_MANAGEMENT_API_URL, timeout=GAME_SERVER_TIMEOUT
    )
    ctx["http_client_event"] = aiohttp.ClientSession(
        base_url=settings.EVENT_GAME_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )
    ctx["http_client_event_mod"] = aiohttp.ClientSession(
        base_url=settings.EVENT_MOD_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )

    if settings.DISCORD_TOKEN:
        ctx["discord_client"] = discord_client
        bot_task_handle = asyncio.create_task(run_discord())
        # Set Discord client reference for the message queue
        tasks_module._discord_client_ref = discord_client

    if WEBHOOK_SSE_ENABLED:
        sse_task_handle = asyncio.create_task(run_sse_listener(ctx))

    location_sse_task_handle = asyncio.create_task(run_location_listener(ctx))

    # Bootstrap webhook dedup high-water marks from DB / mod buffer if Redis is cold.
    # Only bootstrap LAST_SEQ from DB IDs when using polling mode — SSE uses
    # its own monotonic ring buffer seq (1, 2, 3...) which is independent of
    # DB auto-increment IDs.
    from django.core.cache import cache
    from amc.webhook import LAST_SEQ_CACHE_KEY, LAST_TS_CACHE_KEY
    from amc.models import ServerCargoArrivedLog
    from amc.mod_server import get_webhook_events2

    if not WEBHOOK_SSE_ENABLED:
        current_seq = cache.get(LAST_SEQ_CACHE_KEY, 0)
        if not current_seq:
            latest_id = (
                await ServerCargoArrivedLog.objects.order_by("-id")
                .values_list("id", flat=True)
                .afirst()
            )
            if latest_id:
                cache.set(LAST_SEQ_CACHE_KEY, latest_id, timeout=None)
                print(f"Bootstrapped {LAST_SEQ_CACHE_KEY} from DB: {latest_id}")

    # Timestamp floor: drain current mod buffer to prevent replay of old events
    ts_floor = cache.get(LAST_TS_CACHE_KEY, 0)
    if not ts_floor:
        try:
            events = await get_webhook_events2(ctx["http_client_webhook"])
            if events:
                max_ts = max(e["timestamp"] for e in events)
                cache.set(LAST_TS_CACHE_KEY, max_ts, timeout=None)
                print(
                    f"Bootstrapped {LAST_TS_CACHE_KEY} from mod buffer: {max_ts} ({len(events)} events)"
                )
        except Exception as e:
            print(f"Failed to bootstrap timestamp floor: {e}")


async def shutdown(ctx):
    global sse_task_handle, location_sse_task_handle

    if location_sse_task_handle:
        location_sse_task_handle.cancel()
        try:
            await location_sse_task_handle
        except asyncio.CancelledError:
            pass
        location_sse_task_handle = None

    if sse_task_handle:
        sse_task_handle.cancel()
        try:
            await sse_task_handle
        except asyncio.CancelledError:
            pass
        sse_task_handle = None

    if http_client := ctx.get("http_client"):
        await http_client.close()

    if http_client_mod := ctx.get("http_client_mod"):
        await http_client_mod.close()

    if http_client_mod := ctx.get("http_client_webhook"):
        await http_client_mod.close()

    if http_client_mgmt := ctx.get("http_client_mgmt"):
        await http_client_mgmt.close()

    if http_client := ctx.get("http_client_event"):
        await http_client.close()

    if http_client := ctx.get("http_client_event_mod"):
        await http_client.close()

    if bot_task_handle and (discord_client := ctx.get("discord_client")):
        asyncio.run_coroutine_threadsafe(discord_client.close(), discord_client.loop)
        await bot_task_handle


async def monitor_event_locations(ctx):
    await monitor_locations({
        "http_client": ctx["http_client_event"],
        "http_client_mod": ctx["http_client_event_mod"],
        "http_client_mgmt": ctx.get("http_client_mgmt"),
    })


async def monitor_events_main(ctx):
    await monitor_events(ctx, ctx["http_client_mod"])


async def monitor_events_event(ctx):
    await monitor_events(ctx, ctx["http_client_event_mod"])


async def wanted_countdown_tick(ctx):
    await tick_wanted_countdown(ctx["http_client"], ctx["http_client_mod"])


async def suspect_tag_refresh_tick(ctx):
    await refresh_suspect_tags(ctx["http_client_mod"])


async def police_suspect_locations_tick(ctx):
    await tick_police_suspect_locations(ctx["http_client"], ctx["http_client_mod"], ctx["http_client_mgmt"])


async def criminal_record_decay_tick(ctx):
    await tick_criminal_record_decay(ctx["http_client_mod"])


class WorkerSettings:
    functions = [
        process_log_line,
        process_necesse_log,
    ]
    cron_jobs = [
        # Polling cron — only active when SSE is disabled (default)
        *(
            [
                # pyrefly: ignore [bad-argument-type]
                cron(monitor_webhook, second=set(range(0, 60, 4))),
            ]
            if not WEBHOOK_SSE_ENABLED
            else []
        ),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_locations, second=None),
        # pyrefly: ignore [bad-argument-type]
        cron(wanted_countdown_tick, second=None),
        # pyrefly: ignore [bad-argument-type]
        cron(suspect_tag_refresh_tick, second=set(range(0, 60, 30))),
        # pyrefly: ignore [bad-argument-type]
        cron(police_suspect_locations_tick, second=set(range(5, 60, 5))),
        # pyrefly: ignore [bad-argument-type]
        cron(criminal_record_decay_tick, minute=None, second=30),  # every minute at :30s
        # pyrefly: ignore [bad-argument-type]
        cron(handout_ubi, minute=set(range(0, 60, UBI_TASK_FREQUENCY)), second=37),
        # pyrefly: ignore [bad-argument-type]
        cron(apply_interest_to_bank_accounts, hour=None, minute=0, second=0),
        # pyrefly: ignore [bad-argument-type]
        cron(apply_wealth_tax, hour=None, minute=0, second=30),
        # pyrefly: ignore [bad-argument-type]
        cron(evaluate_credit_scores, hour=0, minute=15, second=0),
        # pyrefly: ignore [bad-argument-type]
        cron(transfer_nirc, hour=0, minute=5, second=0),  # daily NIRC drip
        # cron(monitor_events_main, second=None),
        # cron(monitor_events_event, second=None),  # Replaced by SSE event handlers
        # pyrefly: ignore [bad-argument-type]
        cron(send_event_embeds, second=set(range(0, 60, 10))),
        # cron(monitor_event_locations, second=None),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_deliverypoints, second=set(range(0, 60, 30))),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_jobs, minute=set(range(0, 60, 5)), second=37),
        # cron(monitor_corporations, second=23),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_server_status, second=set(range(3, 60, 10))),
        # pyrefly: ignore [bad-argument-type]
        cron(expire_gov_employees, minute=set(range(0, 60, 5)), second=47),
        # pyrefly: ignore [bad-argument-type]
        cron(refresh_all_vehicle_stats, hour=None, minute=30, second=0),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_supply_chain_events, second=47),
        # pyrefly: ignore [bad-argument-type]
        cron(send_rescue_reminders, second=set(range(0, 60, 15))),
        # pyrefly: ignore [bad-argument-type]
        cron(post_random_events, hour={0, 3, 6, 9, 12, 15, 18, 21}, minute=0, second=15),
        # cron(monitor_server_condition, minute=set(range(3, 60, 5))),
        # cron(monitor_rp_mode, second=set(range(7, 60, 13))),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = REDIS_SETTINGS
    max_jobs = 100
