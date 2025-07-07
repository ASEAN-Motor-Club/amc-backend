from contextlib import asynccontextmanager

import aiohttp

from django_asgi_lifespan.types import LifespanManager
from django.conf import settings

@asynccontextmanager
async def aiohttp_lifespan_manager() -> LifespanManager:
    state = {"aiohttp_client": aiohttp.ClientSession(base_url=settings.MOD_SERVER_API_URL)}

    try:
        yield state
    finally:
        await state["aiohttp_client"].close()
