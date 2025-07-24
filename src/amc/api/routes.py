import asyncio
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
  TeamSchema,
  ScheduledEventSchema
)
from django.conf import settings
from amc.models import (
  Player,
  Character,
  CharacterLocation,
  RaceSetup,
  Team,
  ScheduledEvent,
)
from amc.utils import lowercase_first_char_in_keys

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


race_setups_router = Router()

@race_setups_router.get('/{hash}/')
async def get_race_setup_by_hash(request, hash):
  race_setup = await RaceSetup.objects.aget(hash=hash)
  route = lowercase_first_char_in_keys(race_setup.config['Route'])
  route['waypoints'] = [
    { **waypoint, 'translation': waypoint['location'] }
    for waypoint in route['waypoints']
  ]
  return route


teams_router = Router()

@teams_router.get('/', response=list[TeamSchema])
async def list_teams(request):
  return [
    team
    async for team in Team.objects.filter(racing=True)
  ]


scheduled_events_router = Router()

@scheduled_events_router.get('/', response=list[ScheduledEventSchema])
async def list_scheduled_events(request):
  return [
    scheduled_event
    async for scheduled_event in ScheduledEvent.objects.all()
  ]

