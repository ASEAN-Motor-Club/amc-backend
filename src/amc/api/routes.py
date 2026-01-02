import asyncio
import aiohttp
import json
from typing import Optional, Any, cast
from pydantic import AwareDatetime
from datetime import datetime, timedelta
from ninja_extra.security.session import AsyncSessionAuth
from django.core.cache import cache
from django.db.models import Count, Q, F, Window, Prefetch, Max
from django.db.models.functions import Ntile
from django.shortcuts import aget_object_or_404
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
  DeliveryJobSchema,
  LapSectionTimeSchema,
  # Phase 1
  CargoSchema,
  SubsidyRulePublicSchema,
  MinistryTermPublicSchema,
  ChampionshipSchema,
  DeliveryStatsSchema,
  # Phase 2
  CompanyPublicSchema,
  MinistryElectionPublicSchema,
  RaceSetupListSchema,
  # Phase 3
  SubsidyAreaSchema,
  PassengerStatsSchema,
  VehicleDecalPublicSchema,
  VehicleDealershipSchema,
  # Commands
  ServerCommandSchema,
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
  Championship,
  Delivery,
  DeliveryPoint,
  LapSectionTime,
  DeliveryJob,
  # Phase 1
  Cargo,
  SubsidyRule,
  MinistryTerm,
  # Phase 2
  Company,
  MinistryElection,
  SubsidyArea,
  ServerPassengerArrivedLog,
  VehicleDecal,
  VehicleDealership,
)
from amc.utils import lowercase_first_char_in_keys
from amc.save_file import get_world, get_character as get_save_character, get_housings, DATA_PATH
import os

POSITION_UPDATE_RATE = 1
POSITION_UPDATE_SLEEP = 1.0 / POSITION_UPDATE_RATE

app_router = Router()

@app_router.get('/world/', response=dict)
def world(request):
    return {
      **get_world(),
      'character': get_save_character(),
    }

@app_router.get('/housing/', response=dict)
def housing(request):
    return {
      **get_housings(get_world()),
    }

@app_router.get('/subsidies/', response=dict)
async def list_subsidies(request):
    """Returns current active subsidy rules as formatted text."""
    from amc.subsidies import get_subsidies_text
    text = await get_subsidies_text()
    return {'subsidies_text': text}

@app_router.get('/active_events', response=dict)
def list_active_events(request):
    events = []
    # Use DATA_PATH for /srv/www content
    event_infos_path = os.path.join(DATA_PATH, 'event_infos')
    if not os.path.exists(event_infos_path):
        return {'active_events': []}

    for filename in os.listdir(event_infos_path):
        filepath = os.path.join(event_infos_path, filename)
        if not os.path.isfile(filepath):
            continue
        file_modified_time = datetime.fromtimestamp(os.path.getmtime(filepath))
        if file_modified_time < datetime.now() - timedelta(seconds=30):
            continue

        with open(filepath) as f:
          event = json.load(f)
        events.append(event['event'])

    return {
      'active_events': events,
    }

def load_jsonl(file_path):
    data = []
    try:
        with open(file_path, 'r') as f:
            for line in f:
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    return data

@app_router.get('/route_info/{route_hash}/laps/{laps}', response=dict)
def route_info(request, route_hash: str, laps: int):
    laps = int(laps)
    with open(os.path.join(DATA_PATH, 'routes', f"{route_hash}.json")) as f:
      route = json.load(f)
    results = load_jsonl(os.path.join(DATA_PATH, 'route_infos', f"{route_hash}-{laps}.json"))

    best_results_per_player = {}
    for result in results:
      participants = result['participants']
      participant_times = result['participant_times']
      for participant in participants:
        participant['last_modified'] = result['last_modified']
        # event_hash, _ = result['filename'].split('.') # Filename not present in stored logic?
        # participant['event_hash'] = event_hash 
        if participant['disqualified'] or not participant['finished']:
          continue
        unique_id = participant['unique_id']
        starting_time = participant_times.get(unique_id, [[0.0]])[0][0]
        end_time = participant['last_section_time']
        if end_time > starting_time:
          participant['net_time'] = end_time - starting_time
        else:
          participant['net_time'] = end_time

        if unique_id not in best_results_per_player or best_results_per_player[unique_id]['net_time'] > participant['net_time']:
          best_results_per_player[unique_id] = participant

    return {
      'route': route,
      'best_times': sorted(best_results_per_player.values(), key=lambda p: p['net_time'])
    }


players_router = Router()

async def get_players_mod(
  session,
  cache_key: str = "mod_online_players_list",
  cache_ttl: int = int(POSITION_UPDATE_SLEEP / 2)
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
      queryset=Character.objects.with_total_session_time().order_by('-total_session_time', 'id')[:1],
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
  player_id: Optional[str] = None,
  num_samples: int = 50,
):
  """Returns the locations of players between the specified times"""
  filters: dict[str, Any] = {
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
async def depots_restocked_leaderboard(request, limit: int = 10, now: Optional[AwareDatetime] = None, days: int = 7):
  if now is None:
    now = timezone.now()

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
  if race_setup.config is None:
      return {}
  route = lowercase_first_char_in_keys(race_setup.config.get('Route', {}))
  route_data = cast(dict[str, Any], route)
  route_data['waypoints'] = [
    { **waypoint, 'translation': waypoint['location'] }
    for waypoint in route_data.get('waypoints', [])
  ]
  return route_data


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
  async def authenticate(self, request, key):
    token = await super().authenticate(request, key)
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

tracks_router = Router()
@tracks_router.get('/{hash}/results/', response=list[ParticipantSchema])
async def list_track_results(request, hash):
  track = await aget_object_or_404(RaceSetup, hash__startswith=hash)

  qs = GameEventCharacter.objects.results_for_track(track)
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

@deliverypoints_router.get('/{guid}/', response=DeliveryPointSchema)
async def get_deliverypoint(request, guid):
  return await DeliveryPoint.objects.aget(guid=guid)


deliveryjobs_router = Router()

@deliveryjobs_router.get('/', response=list[DeliveryJobSchema])
async def list_deliveryjobs(request):
  return [
    dp async for dp in (DeliveryJob.objects
      .prefetch_related(
        'cargos',
        'source_points',
        'destination_points',
        Prefetch('deliveries', queryset=Delivery.objects.select_related('character'))
      )
      .filter_active()
    )
  ]


# Phase 1: Public API Routers

cargos_router = Router()

@cargos_router.get('/', response=list[CargoSchema])
async def list_cargos(request):
  """List all cargo types"""
  return [cargo async for cargo in Cargo.objects.all()]


subsidies_rules_router = Router()

@subsidies_rules_router.get('/', response=list[SubsidyRulePublicSchema])
async def list_subsidy_rules(request):
  """List all active subsidy rules (public information only)"""
  
  rules = SubsidyRule.objects.filter(active=True).prefetch_related(
    'cargos',
    'source_areas',
    'destination_areas',
    'source_delivery_points',
    'destination_delivery_points',
  )
  
  return [
    {
      'id': rule.id,
      'name': rule.name,
      'active': rule.active,
      'priority': rule.priority,
      'reward_type': rule.reward_type,
      'reward_value': float(rule.reward_value),
      'cargo_keys': [c.key async for c in rule.cargos.all()],
      'source_area_names': [a.name async for a in rule.source_areas.all()],
      'destination_area_names': [a.name async for a in rule.destination_areas.all()],
      'requires_on_time': rule.requires_on_time,
    }
    async for rule in rules
  ]


ministry_router = Router()

@ministry_router.get('/current/', response=Optional[MinistryTermPublicSchema])
async def get_current_ministry_term(request):
  """Get current active ministry term"""
  try:
    term = await (MinistryTerm.objects
      .select_related('minister')
      .aget(is_active=True)
    )
    return {
      'id': term.id,
      'minister_name': term.minister.discord_name or str(term.minister.unique_id),
      'minister_id': str(term.minister.unique_id),
      'start_date': term.start_date,
      'end_date': term.end_date,
      'initial_budget': float(term.initial_budget),
      'current_budget': float(term.current_budget),
      'total_spent': float(term.total_spent),
      'is_active': term.is_active,
      'created_jobs_count': term.created_jobs_count,
      'expired_jobs_count': term.expired_jobs_count,
    }
  except MinistryTerm.DoesNotExist:
    return None


championships_list_router = Router()

@championships_list_router.get('/', response=list[ChampionshipSchema])
async def list_championships(request, offset: int = 0, limit: int = 50):
  """List all championships with pagination"""
  return [champ async for champ in Championship.objects.all()[offset:offset + limit]]


deliveries_stats_router = Router()

@deliveries_stats_router.get('/', response=list[DeliveryStatsSchema])
async def list_delivery_stats(request, limit: int = 10, days: int = 7):
  """Get delivery statistics leaderboard"""
  from django.db.models import Sum
  
  cutoff_date = timezone.now() - timedelta(days=days)
  
  stats = (Delivery.objects
    .filter(timestamp__gte=cutoff_date, character__isnull=False)
    .values('character__id', 'character__name', 'character__player__unique_id')
    .annotate(
      total_deliveries=Count('id'),
      total_payment=Sum('payment'),
      total_subsidy=Sum('subsidy'),
      total_quantity=Sum('quantity'),
    )
    .order_by('-total_deliveries')[:limit]
  )
  
  return [
    {
      'character_id': stat['character__id'],
      'character_name': stat['character__name'],
      'player_id': str(stat['character__player__unique_id']),
      'total_deliveries': stat['total_deliveries'],
      'total_payment': stat['total_payment'] or 0,
      'total_subsidy': stat['total_subsidy'] or 0,
      'total_quantity': stat['total_quantity'] or 0,
    }
    async for stat in stats
  ]


# Phase 2: Community Features Routers

companies_router = Router()

@companies_router.get('/', response=list[CompanyPublicSchema])
async def list_companies(request, offset: int = 0, limit: int = 50):
  """List all companies with pagination (public information only)"""
  companies = Company.objects.select_related('owner').all()[offset:offset + limit]
  
  return [
    {
      'id': company.id,
      'name': company.name,
      'description': company.description or '',
      'owner_name': company.owner.name if company.owner else 'Unknown',
      'is_corp': company.is_corp,
      'first_seen_at': company.first_seen_at,
    }
    async for company in companies
  ]


ministry_elections_router = Router()

@ministry_elections_router.get('/', response=list[MinistryElectionPublicSchema])
async def list_ministry_elections(request, offset: int = 0, limit: int = 20):
  """List all ministry elections with pagination"""
  
  elections = (MinistryElection.objects
    .prefetch_related('candidates__candidate', 'candidates__votes', 'winner')
    .all()
    .order_by('-created_at')
  )[offset:offset + limit]
  
  return [
    {
      'id': election.id,
      'phase': election.phase,
      'created_at': election.created_at,
      'candidacy_end_at': election.candidacy_end_at,
      'poll_end_at': election.poll_end_at,
      'winner_name': election.winner.discord_name if election.winner else None,
      'candidates': [
        {
          'candidate_name': candidacy.candidate.discord_name or str(candidacy.candidate.unique_id),
          'candidate_id': str(candidacy.candidate.unique_id),
          'manifesto': candidacy.manifesto,
          'created_at': candidacy.created_at,
          'vote_count': await candidacy.votes.acount(),
        }
        async for candidacy in election.candidates.all()
      ],
    }
    async for election in elections
  ]


@ministry_elections_router.get('/{id}/', response=MinistryElectionPublicSchema)
async def get_ministry_election(request, id: int):
  """Get a specific ministry election"""
  
  election = await (MinistryElection.objects
    .prefetch_related('candidates__candidate', 'candidates__votes', 'winner')
    .aget(id=id)
  )
  
  return {
    'id': election.id,
    'phase': election.phase,
    'created_at': election.created_at,
    'candidacy_end_at': election.candidacy_end_at,
    'poll_end_at': election.poll_end_at,
    'winner_name': election.winner.discord_name if election.winner else None,
    'candidates': [
      {
        'candidate_name': candidacy.candidate.discord_name or str(candidacy.candidate.unique_id),
        'candidate_id': str(candidacy.candidate.unique_id),
        'manifesto': candidacy.manifesto,
        'created_at': candidacy.created_at,
        'vote_count': await candidacy.votes.acount(),
      }
      async for candidacy in election.candidates.all()
    ],
  }


race_setups_list_router = Router()

@race_setups_list_router.get('/', response=list[RaceSetupListSchema])
async def list_race_setups(request):
  """List all race setups (tracks)"""
  setups = RaceSetup.objects.filter(name__isnull=False).all()
  
  return [
    {
      'hash': setup.hash,
      'route_name': setup.route_name,
      'num_laps': setup.num_laps,
      'num_sections': setup.num_sections,
    }
    async for setup in setups
  ]

# Phase 3: Extended Data Routers

subsidy_areas_router = Router()

@subsidy_areas_router.get('/', response=list[SubsidyAreaSchema])
async def list_subsidy_areas(request):
  """List all subsidy geographic areas"""
  areas = SubsidyArea.objects.all()
  
  return [
    {
      'id': area.id,
      'name': area.name,
      'description': area.description or '',
      # Note: polygon serialization would need GeoJSON format
    }
    async for area in areas
  ]


passenger_stats_router = Router()

@passenger_stats_router.get('/', response=list[PassengerStatsSchema])
async def list_passenger_stats(request, limit: int = 10, days: int = 7):
  """Get passenger transport statistics leaderboard"""
  from django.db.models import Sum, Count, Q
  
  cutoff_date = timezone.now() - timedelta(days=days)
  
  # Aggregate by character with passenger type breakdown
  stats = (ServerPassengerArrivedLog.objects
    .filter(timestamp__gte=cutoff_date, player__isnull=False)
    .values('player__unique_id')
    .annotate(
      player_id_str=F('player__unique_id'),
      # Get first character name for display
      character_id=Max('player__characters__id'),
      character_name=Max('player__characters__name'),
      total_passengers=Count('id'),
      total_payment=Sum('payment'),
      # Count by passenger type
      hitchhiker_count=Count('id', filter=Q(passenger_type=1)),
      taxi_count=Count('id', filter=Q(passenger_type=2)),
      ambulance_count=Count('id', filter=Q(passenger_type=3)),
      bus_count=Count('id', filter=Q(passenger_type=4)),
    )
    .order_by('-total_passengers')[:limit]
  )
  
  return [
    {
      'character_id': stat['character_id'],
      'character_name': stat['character_name'],
      'player_id': str(stat['player_id_str']),
      'total_passengers': stat['total_passengers'],
      'total_payment': stat['total_payment'] or 0,
      'passenger_type_counts': {
        'hitchhiker': stat['hitchhiker_count'],
        'taxi': stat['taxi_count'],
        'ambulance': stat['ambulance_count'],
        'bus': stat['bus_count'],
      },
    }
    async for stat in stats
  ]


decals_router = Router()

@decals_router.get('/', response=list[VehicleDecalPublicSchema])
async def list_public_decals(request, offset: int = 0, limit: int = 50):
  """List all public vehicle decals with pagination"""
  decals = VehicleDecal.objects.filter(private=False).select_related('player').all()[offset:offset + limit]
  
  return [
    {
      'id': decal.id,
      'name': decal.name,
      'vehicle_key': decal.vehicle_key,
      'hash': decal.hash,
      'price': decal.price,
      'player_name': decal.player.discord_name if decal.player else None,
    }
    async for decal in decals
  ]


dealerships_router = Router()

@dealerships_router.get('/', response=list[VehicleDealershipSchema])
async def list_dealerships(request):
  """List all vehicle dealership locations"""
  dealerships = VehicleDealership.objects.all()
  
  return [
    {
      'id': dealership.id,
      'vehicle_key': dealership.vehicle_key,
      'location': {
        'x': dealership.location.x,
        'y': dealership.location.y,
        'z': dealership.location.z,
      },
      'notes': dealership.notes or '',
    }
    async for dealership in dealerships
  ]

# Server Commands Router

commands_list_router = Router()

@commands_list_router.get('/', response=list[ServerCommandSchema])
async def list_server_commands(request):
  """List all available server-side commands"""
  from amc.command_framework import registry
  from django.utils.encoding import force_str
  
  commands = []
  for cmd_data in registry.commands:
    # Get the primary command name
    command_name = cmd_data['name']
    aliases = cmd_data['aliases']
    
    # Determine shorthand (second alias if exists)
    shorthand = aliases[1] if len(aliases) > 1 else None
    
    # Get description - convert lazy translation to string
    description = cmd_data.get('description', '')
    description = force_str(description)
    
    commands.append({
      'command': command_name,
      'aliases': aliases,
      'shorthand': shorthand,
      'description': description,
      'category': cmd_data.get('category', 'General'),
      'deprecated': cmd_data.get('deprecated', False),
    })
  
  # Sort by category, then by command name
  commands.sort(key=lambda x: (x['category'], x['command']))
  
  return commands
