"""SSE client for real-time event streaming from the C++ mod webserver.

Replaces the polling-based monitor_webhook cron when WEBHOOK_SSE_ENABLED=1.
Connects to /events/stream on the management port, parses SSE frames,
buffers events, and feeds them to process_events() in batches.
"""

import asyncio
import json
import logging

import aiohttp
from django.conf import settings

logger = logging.getLogger("amc.sse")


def parse_epoch_seq(event_id: str) -> tuple[int | None, int]:
    """Parse an SSE event ID that may be epoch-prefixed.

    Formats:
        "1711400000:42" -> (1711400000, 42)   # epoch:seq
        "42"           -> (None, 42)           # plain seq (backward compat)

    Returns (epoch, seq). Raises ValueError on unparseable input.
    """
    if ":" in event_id:
        epoch_str, seq_str = event_id.split(":", 1)
        return int(epoch_str), int(seq_str)
    return None, int(event_id)

# Debounce: flush buffered events after this much silence
FLUSH_DEBOUNCE_SECONDS = 0.5

# Force-flush even if events keep arriving (prevents indefinite delay during bursts)
FLUSH_MAX_WAIT_SECONDS = 2.0

# Force-flush when buffer reaches this many events
FLUSH_MAX_BATCH_SIZE = 50

# Reconnect backoff bounds
INITIAL_BACKOFF = 1
MAX_BACKOFF = 30


def parse_sse_event(raw_lines: list[str]) -> tuple[str | None, str | None]:
    """Parse a single SSE event block (lines between blank lines).

    Returns (event_id, data_string) or (None, None) if no data.
    """
    event_id = None
    data_parts = []

    for line in raw_lines:
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].strip())
        elif line.startswith(":"):
            # SSE comment — ignore (but could log for debugging)
            pass

    if data_parts:
        return event_id, "\n".join(data_parts)
    return None, None


async def _flush_loop(event_buffer, event_signal, http_client, http_client_mod, discord_client):
    """Dedicated flush loop with debounce, max-wait ceiling, and batch cap.

    Waits for events to arrive (via event_signal), then enters a
    debounce+ceiling wait before flushing the buffer to process_events().
    """
    from amc.webhook import process_events

    loop = asyncio.get_event_loop()

    while True:
        # Wait until at least one event arrives
        await event_signal.wait()
        event_signal.clear()

        first_event_time = loop.time()

        # Debounce loop: wait for silence or hit ceiling/cap
        while True:
            elapsed = loop.time() - first_event_time
            remaining_ceiling = FLUSH_MAX_WAIT_SECONDS - elapsed

            # Force-flush: ceiling or batch cap hit
            if remaining_ceiling <= 0 or len(event_buffer) >= FLUSH_MAX_BATCH_SIZE:
                break

            wait_time = min(FLUSH_DEBOUNCE_SECONDS, remaining_ceiling)
            try:
                await asyncio.wait_for(event_signal.wait(), timeout=wait_time)
                event_signal.clear()  # New event arrived — loop again for debounce
            except asyncio.TimeoutError:
                break  # Silence — flush now

        if not event_buffer:
            continue

        # Atomic drain
        events = list(event_buffer)
        event_buffer.clear()

        logger.info("SSE flushing %d events", len(events))
        try:
            await process_events(events, http_client, http_client_mod, discord_client)
        except Exception:
            logger.exception("SSE: error processing %d events", len(events))


async def run_sse_listener(ctx):
    """Long-running SSE listener task.

    Connects to the mod management server's /events/stream endpoint,
    receives events in real-time, buffers them, and flushes to
    process_events() after a short debounce.

    On disconnect, reconnects with exponential backoff and sends
    Last-Event-ID to replay missed events.
    """

    base_url = settings.WEBHOOK_SERVER_API_URL
    http_client = ctx.get("http_client")
    http_client_mod = ctx.get("http_client_mod")
    discord_client = ctx.get("discord_client")

    from django.core.cache import cache
    from amc.webhook import LAST_SEQ_CACHE_KEY, LAST_EPOCH_CACHE_KEY
    current_epoch = await cache.aget(LAST_EPOCH_CACHE_KEY)
    if current_epoch is not None:
        # Build epoch-prefixed Last-Event-ID so the C++ server can
        # properly position in the ring buffer for this epoch.
        last_seq = await cache.aget(LAST_SEQ_CACHE_KEY, 0)
        last_event_id = f"{current_epoch}:{last_seq}" if last_seq else "0"
    else:
        # No epoch cached — SSE has never connected, or the cache was
        # cleared.  The LAST_SEQ is likely from the old polling system
        # and meaningless for the SSE ring buffer.  Start fresh.
        last_event_id = "0"
    backoff = INITIAL_BACKOFF

    # sock_read=90: detect dead connections if the mod server hangs after
    # accepting TCP but stops sending data/heartbeats.  The C++ mod should
    # send SSE comments as keepalives; 90s is generous enough to avoid
    # false positives during quiet periods.
    timeout = aiohttp.ClientTimeout(
        total=None,  # No total timeout — SSE is long-lived
        sock_connect=10,
        sock_read=90,
    )

    # Minimum connection duration (seconds) to consider a session "healthy".
    # If a connection lasted this long, the disconnect is likely a server
    # restart rather than a persistent error, so we reset backoff.
    HEALTHY_SESSION_SECONDS = 60

    loop = asyncio.get_event_loop()

    while True:
        connected_at = loop.time()
        try:
            async with aiohttp.ClientSession(
                base_url=base_url, timeout=timeout
            ) as session:
                headers = {}
                if last_event_id != "0":
                    headers["Last-Event-ID"] = last_event_id

                logger.info(
                    "SSE connecting to %s/events/stream (Last-Event-ID: %s)",
                    base_url, last_event_id,
                )

                async with session.get(
                    "/events/stream", headers=headers
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "SSE endpoint returned %s, retrying in %ss",
                            resp.status, backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        continue

                    logger.info("SSE connected")
                    connected_at = loop.time()
                    backoff = INITIAL_BACKOFF  # Reset on successful connect

                    event_buffer: list[dict] = []
                    current_lines: list[str] = []
                    event_signal = asyncio.Event()

                    flush_task = asyncio.create_task(
                        _flush_loop(
                            event_buffer, event_signal,
                            http_client, http_client_mod, discord_client,
                        )
                    )

                    try:
                        while True:
                            try:
                                raw_line = await asyncio.wait_for(
                                    resp.content.readline(), timeout=120.0
                                )
                            except asyncio.TimeoutError:
                                logger.warning("SSE read timeout (no events/heartbeats for 120s), forcing fresh reconnect")
                                await cache.aset(LAST_SEQ_CACHE_KEY, 0, timeout=None)
                                break  # Break loop to trigger reconnect

                            if not raw_line:
                                break  # EOF

                            line = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")

                            if line == "":
                                # Blank line = end of SSE event block
                                if current_lines:
                                    event_id, data = parse_sse_event(current_lines)
                                    current_lines = []
                                    if data:
                                        parsed_epoch = None
                                        parsed_seq = None
                                        if event_id:
                                            try:
                                                parsed_epoch, parsed_seq = parse_epoch_seq(event_id)
                                            except ValueError:
                                                logger.warning("SSE: invalid event ID: %s", event_id)
                                            last_event_id = event_id

                                            # Detect epoch change (server restart)
                                            if parsed_epoch is not None and current_epoch is not None and parsed_epoch != current_epoch:
                                                logger.warning(
                                                    "SSE epoch changed: %s -> %s (server restarted), resetting seq high-water mark",
                                                    current_epoch, parsed_epoch,
                                                )
                                                await cache.aset(LAST_SEQ_CACHE_KEY, 0, timeout=None)

                                            if parsed_epoch is not None:
                                                current_epoch = parsed_epoch
                                                await cache.aset(LAST_EPOCH_CACHE_KEY, current_epoch, timeout=None)

                                        try:
                                            event_obj = json.loads(data)
                                            if parsed_seq is not None:
                                                event_obj["_seq"] = parsed_seq
                                            if parsed_epoch is not None:
                                                event_obj["_epoch"] = parsed_epoch
                                            event_buffer.append(event_obj)
                                        except json.JSONDecodeError:
                                            logger.warning(
                                                "SSE: invalid JSON data: %s", data[:200]
                                            )

                                        # Signal the flush loop
                                        event_signal.set()
                            else:
                                current_lines.append(line)
                    finally:
                        flush_task.cancel()
                        try:
                            await flush_task
                        except asyncio.CancelledError:
                            pass

                        # Flush any remaining events before reconnecting
                        if event_buffer:
                            from amc.webhook import process_events
                            remaining = list(event_buffer)
                            event_buffer.clear()
                            logger.info("SSE flushing %d remaining events before reconnect", len(remaining))
                            try:
                                await process_events(remaining, http_client, http_client_mod, discord_client)
                            except Exception:
                                logger.exception("SSE: error flushing remaining events")

            # Stream ended cleanly (server closed the connection)
            session_duration = loop.time() - connected_at
            if session_duration >= HEALTHY_SESSION_SECONDS:
                # Long-lived session — likely a server restart, reconnect fast
                logger.info(
                    "SSE stream ended after %.0fs (healthy session), reconnecting immediately",
                    session_duration,
                )
                backoff = INITIAL_BACKOFF
                continue  # skip the sleep at the bottom
            else:
                logger.info("SSE stream ended after %.0fs, retrying in %ss", session_duration, backoff)

        except asyncio.CancelledError:
            logger.info("SSE listener shutting down")
            return

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            session_duration = loop.time() - connected_at
            if session_duration >= HEALTHY_SESSION_SECONDS:
                backoff = INITIAL_BACKOFF
            logger.warning("SSE connection error: %s, retrying in %ss", e, backoff)

        except Exception:
            logger.exception("SSE unexpected error, retrying in %ss", backoff)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)
