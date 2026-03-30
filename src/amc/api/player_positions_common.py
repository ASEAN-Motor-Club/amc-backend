from django.core.cache import cache

POSITION_UPDATE_RATE = 1
POSITION_UPDATE_SLEEP = 1.0 / POSITION_UPDATE_RATE
HEARTBEAT_INTERVAL = 15


async def get_players_mod(
    session,
    cache_key: str = "mod_online_players_list",
    cache_ttl: int = int(POSITION_UPDATE_SLEEP / 2),
):
    cached_data = cache.get(cache_key)
    if cached_data:
        return cached_data

    async with session.get("/players") as resp:
        players = (await resp.json()).get("data", [])
    cache.set(cache_key, players, timeout=cache_ttl)
    return players
