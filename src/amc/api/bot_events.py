"""SSE endpoint for bot-relevant game events using Redis pub/sub."""

import asyncio
import json
from datetime import datetime
from ninja import Router
from django.http import StreamingHttpResponse
from django.conf import settings
import redis.asyncio as aioredis

router = Router()

# Redis channel name for bot events
BOT_EVENTS_CHANNEL = "bot_events"


def _get_redis_url() -> str:
    """Build Redis URL from settings, falling back to localhost."""
    redis_settings = getattr(settings, 'REDIS_SETTINGS', {})
    host = redis_settings.get('host', 'localhost')
    port = redis_settings.get('port', 6379)
    return f"redis://{host}:{port}"


async def emit_bot_event(event: dict):
    """Called from tasks.py to emit events to the bot via Redis pub/sub."""
    redis_client = aioredis.from_url(_get_redis_url())
    try:
        await redis_client.publish(BOT_EVENTS_CHANNEL, json.dumps(event))
    finally:
        await redis_client.aclose()


@router.get('/')
async def bot_events_stream(request):
    """SSE stream for bot-relevant game events.
    
    Events include:
    - chat_message: In-game chat with full player context
    - heartbeat: Periodic heartbeat for connection verification
    """
    
    async def event_stream():
        redis_client = aioredis.from_url(_get_redis_url())
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(BOT_EVENTS_CHANNEL)
        
        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=10.0),
                        timeout=15.0
                    )
                    if message and message['type'] == 'message':
                        data = message['data']
                        if isinstance(data, bytes):
                            data = data.decode()
                        yield f"data: {data}\n\n"
                    else:
                        # Send heartbeat event for connection verification
                        heartbeat = {
                            "type": "heartbeat",
                            "timestamp": datetime.now().isoformat(),
                        }
                        yield f"data: {json.dumps(heartbeat)}\n\n"
                except asyncio.TimeoutError:
                    # Send heartbeat on timeout
                    heartbeat = {
                        "type": "heartbeat",
                        "timestamp": datetime.now().isoformat(),
                    }
                    yield f"data: {json.dumps(heartbeat)}\n\n"
        finally:
            await pubsub.unsubscribe(BOT_EVENTS_CHANNEL)
            await redis_client.aclose()
    
    return StreamingHttpResponse(
        event_stream(), 
        content_type="text/event-stream"
    )
