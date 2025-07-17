import asyncio
import aiohttp
import json
from datetime import timedelta
from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone
from ninja import Router
from django.http import StreamingHttpResponse
from .schema import (
  ActivePlayerSchema,
  PlayerSchema,
  CharacterSchema,
  LeaderboardsRestockDepotCharacterSchema,
)
from django.conf import settings
from amc.models import (
  Player,
  Character,
)

players_router = Router()

async def get_players_mod(
  session,
  cache_key: str = "mod_online_players_list",
  cache_ttl: int = 1
):
  cached_data = cache.get(cache_key)
  if cached_data:
    return cached_data

  async with session.get('/players') as resp:
    players = (await resp.json()).get('data', [])
  cache.set(cache_key, players, timeout=cache_ttl)
  return players

async def get_players(
  session,
  cache_key: str = "online_players_list",
  cache_ttl: int = 1
):
  cached_data = cache.get(cache_key)
  if cached_data:
    return cached_data

  async with session.get('/player/list', params={'password': ''}) as resp:
    players = list((await resp.json()).get('data', {}).values())
  cache.set(cache_key, players, timeout=cache_ttl)
  return players


@players_router.get('/', response=list[ActivePlayerSchema])
async def list_players(request):
  """List all the players"""
  async with aiohttp.ClientSession(base_url=settings.GAME_SERVER_API_URL) as session:
    players = await get_players(session)
  return players


@players_router.get('/{unique_id}/', response=PlayerSchema)
async def get_player(request, unique_id):
  """Retrieve a single player"""
  player = await (Player.objects
    .with_total_session_time()
    .with_last_login()
    .aget(unique_id=unique_id)
  )
  return player


@players_router.get('/{unique_id}/characters/', response=list[CharacterSchema])
async def get_player_characters(request, unique_id):
  """Retrieve a single player"""
  return [
    character
    async for character in Character.objects.filter(player__unique_id=unique_id)
  ]


characters_router = Router()
@characters_router.get('/{id}/', response=CharacterSchema)
async def get_character(request, id):
  """Retrieve a single character"""
  character = await Character.objects.aget(id=id)
  return character


player_positions_router = Router()

@player_positions_router.get('/')
async def streaming_player_positions(request):
  session = request.state["aiohttp_client"]

  async def event_stream():
    while True:
      players = await get_players_mod(session)
      player_positions = {
        player['PlayerName']: {
          **{
            axis.lower(): value
            for axis, value in player['Location'].items()
          },
          'vehicle_key': player['VehicleKey'],
          'unique_id': player['UniqueID'],
        }
        for player in players
      }

      yield f"data: {json.dumps(player_positions)}\n\n"
      await asyncio.sleep(1)

  return StreamingHttpResponse(event_stream(), content_type="text/event-stream")


stats_router = Router()

@stats_router.get('/depots_restocked_leaderboard/', response=list[LeaderboardsRestockDepotCharacterSchema])
async def depots_restocked_leaderboard(request, limit=10, now=timezone.now(), days=7):
  qs = Character.objects.annotate(
    depots_restocked=Count(
      'restock_depot_logs',
      distinct=True,
      filter=Q(restock_depot_logs__timestamp__gte=now - timedelta(days=days))
    ),
  )

  return [char async for char in qs.order_by('-depots_restocked')[:limit]]


