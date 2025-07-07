import asyncio
import aiohttp
import json
from django.core.cache import cache
from ninja import Router
from django.http import StreamingHttpResponse
from .schema import PlayerSchema
from django.conf import settings

players_router = Router()

async def get_players(
  session,
  cache_key: str = "online_players_list",
  cache_ttl: int = 1
):
  cached_data = cache.get(cache_key)
  if cached_data:
    return cached_data

  async with session.get('/players') as resp:
    players = (await resp.json()).get('data', [])
  cache.set(cache_key, players, timeout=cache_ttl)
  return players


@players_router.get('', response=list[PlayerSchema])
async def list_players(request):
  """List all the players"""
  async with aiohttp.ClientSession(base_url=settings.MOD_SERVER_API_URL) as session:
    players = await get_players(session)
  return players


player_positions_router = Router()

@player_positions_router.get('')
async def streaming_player_positions(request):
  session = request.state["aiohttp_client"]

  async def event_stream():
    while True:
      players = await get_players(session)
      player_positions = {
        player['PlayerName']: {
          **{
            axis.lower(): value
            for axis, value in player['Location'].items()
          },
          'vehicleKey': player['VehicleKey'],
        }
        for player in players
      }

      yield f"data: {json.dumps(player_positions)}\n\n"
      await asyncio.sleep(1)

  return StreamingHttpResponse(event_stream(), content_type="text/event-stream")

