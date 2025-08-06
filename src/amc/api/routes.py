import asyncio
import aiohttp
import json
from typing import Optional
from pydantic import AwareDatetime
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from ninja_extra.security.session import AsyncSessionAuth
from django.core.cache import cache
from django.db.models import Count, Q, F, Window, Prefetch, Max
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
  ScheduledEventSchema,
  ParticipantSchema,
  PersonalStandingSchema,
  TeamStandingSchema,
  DeliveryPointSchema,
  LapSectionTimeSchema,
  WebhookPayloadSchema,
)
from django.conf import settings
from amc.models import (
  Player,
  Character,
  CharacterLocation,
  RaceSetup,
  Team,
  ScheduledEvent,
  GameEventCharacter,
  ChampionshipPoint,
  DeliveryPoint,
  LapSectionTime,
  ServerCargoArrivedLog,
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


players_qs = (Player.objects
  .with_total_session_time()
  .with_last_login()
  .prefetch_related(
    Prefetch(
      'characters',
      queryset=Character.objects.with_total_session_time().order_by('-total_session_time')[:1],
      to_attr='main_characters'
    )
  )
)

@players_router.get('/me/', auth=AsyncSessionAuth(), response=PlayerSchema)
async def get_player_me(request):
  """Retrieve a single player"""
  player = await players_qs.aget(user=request.auth)
  return player

@players_router.get('/{unique_id}/', response=PlayerSchema)
async def get_player(request, unique_id):
  """Retrieve a single player"""
  player = await players_qs.aget(unique_id=unique_id)
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
teams_qs = (Team.objects
  .prefetch_related(
    Prefetch(
      'players',
      queryset=players_qs.alias(
        max_racer_level=Max('characters__racer_level')
      ).order_by('-max_racer_level')
    )
  )
  .filter(racing=True)
)

@teams_router.get('/', response=list[TeamSchema])
async def list_teams(request):
  return [
    team
    async for team in teams_qs 
  ]

@teams_router.get('/{id}/', response=TeamSchema)
async def get_team(request, id):
  team = await teams_qs.aget(id=id)
  return team

class TeamOwnerSessionAuth(AsyncSessionAuth):
  async def authenticate(self, request, token):
    token = await super().authenticate(request, token)
    if not token:
      return
    print(request)
    return True

# @teams_router.patch('/{team_id}/', response=TeamSchema, auth=AsyncSessionAuth())
# async def update_team(request, team_id: int, payload: PatchTeamSchema):
#   team = await Team.objects.aget(id=team_id)
#   updated_fields = payload.dict(exclude_unset=True)
#   for attr, value in updated_fields.items():
#     setattr(team, attr, value)
#   await team.asave()
#   return team


scheduled_events_router = Router()

@scheduled_events_router.get('/', response=list[ScheduledEventSchema])
async def list_scheduled_events(request):
  return [
    scheduled_event
    async for scheduled_event in ScheduledEvent.objects.all()
  ]

@scheduled_events_router.get('/{id}/', response=ScheduledEventSchema)
async def get_scheduled_event(request, id):
  return await ScheduledEvent.objects.select_related('race_setup').aget(id=id)

@scheduled_events_router.get('/{id}/results/', response=list[ParticipantSchema])
async def list_scheduled_event_results(request, id):
  scheduled_event = await ScheduledEvent.objects.select_related('race_setup').aget(id=id)

  qs = GameEventCharacter.objects.results_for_scheduled_event(scheduled_event)
  return [
    participant
    async for participant in qs
  ]

@players_router.get('/{player_id}/results/', response=list[ParticipantSchema])
async def list_player_results(request, player_id, route_hash: Optional[str]=None, scheduled_event_id: Optional[int]=None):
  qs = GameEventCharacter.objects.select_related(
    'character',
    'character__player',
    'championship_point',
    'championship_point__team',
  ).filter(
    character__player__unique_id=int(player_id),
  ).order_by('-game_event__start_time')

  if route_hash is not None:
    qs = qs.filter(game_event__race_setup__hash=route_hash)

  if scheduled_event_id is not None:
    qs = qs.filter(game_event__scheduled_event=scheduled_event_id)

  return [
    participant
    async for participant in qs
  ]

results_router = Router()

@results_router.get('/{participant_id}/lap_section_times/', response=list[LapSectionTimeSchema])
async def list_player_results_times(request, participant_id):
  qs = (LapSectionTime.objects
    .select_related( 'game_event_character')
    .annotate_deltas()
    .annotate_net_time()
    .filter(
      game_event_character=int(participant_id),
    )
    .order_by('lap', 'section_index')
  )

  return [
    participant
    async for participant in qs
  ]

championships_router = Router()

@championships_router.get('/{id}/personal_standings/', response=list[PersonalStandingSchema])
async def list_championship_personal_standings(request, id):
  return [
    standing
    async for standing in ChampionshipPoint.objects.personal_standings(id)
  ]

@championships_router.get('/{id}/team_standings/', response=list[TeamStandingSchema])
async def list_championship_team_standings(request, id):
  return [
    standing
    async for standing in ChampionshipPoint.objects.team_standings(id)
  ]

deliverypoints_router = Router()

@deliverypoints_router.get('/', response=list[DeliveryPointSchema])
async def list_deliverypoints(request):
  return [
    dp async for dp in DeliveryPoint.objects.all()
  ]

@deliverypoints_router.get('/{guid}/', response=list[DeliveryPointSchema])
async def get_deliverypoint(request, guid):
  return await DeliveryPoint.objects.aget(guid=guid)


webhook_router = Router()

@webhook_router.post('/')
async def webhook(request, payload: WebhookPayloadSchema):
  match payload.hook:
    case "/Script/MotorTown.MotorTownPlayerController:ServerCargoArrived":
      player_id = payload.data['PlayerId']
      player = await Player.objects.aget(unique_id=player_id)
      logs = [
        ServerCargoArrivedLog(
          timestamp=datetime.utcfromtimestamp(payload.timestamp / 1000),
          player=player,
          cargo_key=cargo['Net_CargoKey'],
          payment=cargo['Net_Payment']['BaseValue'],
          weight=cargo['Net_Weight'],
          damage=cargo['Net_Damage'],
        )
        for cargo in payload.data['Cargos']
      ]
      await ServerCargoArrivedLog.objects.abulk_create(logs)

