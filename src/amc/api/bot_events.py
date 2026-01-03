"""SSE endpoint for bot-relevant game events."""

import asyncio
import json
from datetime import datetime
from ninja import Router
from django.http import StreamingHttpResponse

router = Router()

# In-memory async queue for bot events
_bot_event_queue: asyncio.Queue = asyncio.Queue()


async def emit_bot_event(event: dict):
    """Called from tasks.py to emit events to the bot."""
    await _bot_event_queue.put(event)


@router.get('/')
async def bot_events_stream(request):
    """SSE stream for bot-relevant game events.
    
    Events include:
    - chat_message: In-game chat with full player context
    - heartbeat: Periodic heartbeat for connection verification
    """
    
    async def event_stream():
        while True:
            try:
                event = await asyncio.wait_for(
                    _bot_event_queue.get(), 
                    timeout=10.0
                )
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                # Send heartbeat event for connection verification
                heartbeat = {
                    "type": "heartbeat",
                    "timestamp": datetime.now().isoformat(),
                }
                yield f"data: {json.dumps(heartbeat)}\n\n"
    
    return StreamingHttpResponse(
        event_stream(), 
        content_type="text/event-stream"
    )

