import asyncio
import uuid
import aiohttp
import json
from pydantic import AwareDatetime
from datetime import timedelta
from ninja_extra.security.session import AsyncSessionAuth
from django.core.cache import cache
from django.db.models import Count, Q, F, Window
from django.db.models.functions import Ntile
from django.utils import timezone
from ninja import Router
from django.http import StreamingHttpResponse
from .schema import (
  ActivePlayerSchema,
  PlayerSchema,
  CharacterSchema,
  CharacterLocationSchema,
  LeaderboardsRestockDepotCharacterSchema,
)
from django.conf import settings
from amc.models import (
  Player,
  Character,
  CharacterLocation,
)

POSITION_UPDATE_RATE = 10
POSITION_UPDATE_SLEEP = 1.0 / POSITION_UPDATE_RATE

players_router = Router()

async def get_players_mod(
  session,
  cache_key: str = "mod_online_players_list",
  cache_ttl: int = POSITION_UPDATE_SLEEP / 2
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



@players_router.get('/me/', auth=AsyncSessionAuth(), response=PlayerSchema)
async def get_player_me(request):
  """Retrieve a single player"""
  player = await (Player.objects
    .with_total_session_time()
    .with_last_login()
    .aget(user=request.auth)
  )
  return player

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

  q = Q(player__unique_id=unique_id)
  if unique_id == 'me':
    user = await request.auser()
    q = Q(player__user=user)

  return [
    character
    async for character in Character.objects.filter(q)
  ]


characters_router = Router()
@characters_router.get('/{id}/', response=CharacterSchema)
async def get_character(request, id):
  """Retrieve a single character"""
  character = await Character.objects.aget(id=id)
  return character


player_locations_router = Router()

@player_locations_router.get('/', response=list[CharacterLocationSchema])
async def player_locations(
  request,
  start_time: AwareDatetime,
  end_time: AwareDatetime,
  player_id: str=None,
  num_samples: int=50,
):
  """Returns the locations of players between the specified times"""
  filters = {
    'timestamp__gte': start_time,
    'timestamp__lt': end_time,
  }
  if player_id is not None:
    filters['character__player__unique_id'] = player_id

  qs = (CharacterLocation.objects
    .filter(**filters)
    .prefetch_related('character__player')
    .order_by('character')
    .annotate(
      bucket=Window(
        expression=Ntile(num_samples),
        partition_by=F('character'),
        order_by=F('timestamp').asc()
      )
    )
    .order_by('character', 'bucket')
    .distinct('character', 'bucket')
  )
  return [cl async for cl in qs]


def diff_player_positions(players, previous_players):
  current_players = {
    p['UniqueID']: {
      'location': {axis: round(value) for axis, value in p['Location'].items()},
      'vehicle_key': p['VehicleKey'],
      'name': p['PlayerName']
    }
    for p in players
  }
  
  if not previous_players:
    player_positions = {
      uid: {
        **{axis.lower(): value for axis, value in data['location'].items()},
        'vehicle_key': data['vehicle_key'],
        'name': data['name']
      }
      for uid, data in current_players.items()
    }
    return player_positions, current_players.copy()
  
  player_positions = {}
  
  for uid, current_data in current_players.items():
    if uid in previous_players:
      cached_data = previous_players[uid]
      changes = {}
      
      location_changed = False
      for axis, value in current_data['location'].items():
        if cached_data['location'].get(axis) != value:
          changes[axis.lower()] = value
          location_changed = True
      
      vehicle_changed = cached_data['vehicle_key'] != current_data['vehicle_key']
      if vehicle_changed:
        changes['vehicle_key'] = current_data['vehicle_key']
      
      name_changed = cached_data['name'] != current_data['name']
      if name_changed:
        changes['name'] = current_data['name']
      
      if location_changed and vehicle_changed and not name_changed:
        changes['name'] = current_data['name']
      
      if changes:
        player_positions[uid] = changes
    else:
      player_positions[uid] = {
        **{axis.lower(): value for axis, value in current_data['location'].items()},
        'vehicle_key': current_data['vehicle_key'],
        'name': current_data['name']
      }
  
  for uid in previous_players:
    if uid not in current_players:
      player_positions[uid] = None
  
  if player_positions:
    return player_positions, current_players.copy()
  
  return {}, current_players.copy()


player_positions_router = Router()
@player_positions_router.get('/')
async def streaming_player_positions(request, diff=False):
  session = request.state["aiohttp_client"]
  
  async def event_stream():
    previous_players = {}
    while True:
      players = await get_players_mod(session)
      if diff:
        player_positions, previous_players = diff_player_positions(players, previous_players)
      else:
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

      yield f"data: {json.dumps(player_positions, separators=(',', ':'))}\n\n"
      await asyncio.sleep(POSITION_UPDATE_SLEEP)

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


