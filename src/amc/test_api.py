import time
from datetime import timedelta
from urllib.parse import quote
from django.utils import timezone
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from django.test import TestCase
from ninja.testing import TestAsyncClient
from amc.api.routes import (
  players_router,
  characters_router,
  player_locations_router,
  stats_router,
  teams_router,
  scheduled_events_router,
  championships_router,
  results_router,
  webhook_router,
)
from amc.factories import (
  PlayerFactory,
  CharacterFactory,
  TeamFactory,
  GameEventFactory,
  GameEventCharacterFactory,
  ChampionshipFactory,
  ChampionshipPointFactory,
)
from amc.models import (
  PlayerStatusLog,
  PlayerRestockDepotLog,
  CharacterLocation,
  LapSectionTime,
  ServerCargoArrivedLog,
)

class PlayersAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(players_router)

  async def test_get_player(self):
    player = await sync_to_async(PlayerFactory)()
    character = await player.characters.afirst()
    response = await self.client.get(f"/{player.unique_id}/")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {
      "discord_user_id": player.discord_user_id,
      "unique_id": str(player.unique_id),
      "total_session_time": 'P0DT00H00M00S',
      "last_login": None,
      "main_character": {
        "id": character.id,
        "name": character.name,
        "player_id": str(player.unique_id),
        "driver_level": None,
        "bus_level": None,
        "taxi_level": None,
        "police_level": None,
        "truck_level": None,
        "wrecker_level": None,
        "racer_level": None,
      },
    })

  async def test_get_player_logged_in(self):
    player = await sync_to_async(PlayerFactory)()
    character = await player.characters.afirst()
    now = timezone.now()
    now = now.replace(microsecond=0)
    await PlayerStatusLog.objects.acreate(
      character=character,
      timespan=(now - timedelta(days=1), now - timedelta(hours=1))
    )
    response = await self.client.get(f"/{player.unique_id}/")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {
      "discord_user_id": player.discord_user_id,
      "unique_id": str(player.unique_id),
      "main_character": {
        "id": character.id,
        "name": character.name,
        "player_id": str(player.unique_id),
        "driver_level": None,
        "bus_level": None,
        "taxi_level": None,
        "police_level": None,
        "truck_level": None,
        "wrecker_level": None,
        "racer_level": None,
      },
      "total_session_time": 'P0DT23H00M00S',
      "last_login": (now - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    })

  async def test_get_player_characters(self):
    player = await sync_to_async(PlayerFactory)()
    response = await self.client.get(f"/{player.unique_id}/characters/")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), [
      {
        "id": character.id,
        "name": character.name,
        "player_id": str(player.unique_id),
        "driver_level": None,
        "bus_level": None,
        "taxi_level": None,
        "police_level": None,
        "truck_level": None,
        "wrecker_level": None,
        "racer_level": None,
      }
      async for character in player.characters.all()
    ])


class CharactersAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(characters_router)

  async def test_get_character(self):
    character = await sync_to_async(CharacterFactory)()
    response = await self.client.get(f"/{character.id}/")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {
      "id": character.id,
      "name": character.name,
      "player_id": str(character.player.unique_id),
      "driver_level": None,
      "bus_level": None,
      "taxi_level": None,
      "police_level": None,
      "truck_level": None,
      "wrecker_level": None,
      "racer_level": None,
    })

class LeaderboardsAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(stats_router)

  async def test_get_character(self):
    character = await sync_to_async(CharacterFactory)()
    await PlayerRestockDepotLog.objects.acreate(
      character=character,
      timestamp=timezone.now(),
      depot_name='test'
    )
    response = await self.client.get("/depots_restocked_leaderboard/")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()[0]['depots_restocked'], 1)

class PlayerLocationsAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(player_locations_router)

  async def test_list_positions(self):
    character = await sync_to_async(CharacterFactory)()
    await CharacterLocation.objects.acreate(
      character=character,
      timestamp=timezone.now() - timedelta(hours=3),
      location=Point(1, 1, 1)
    )
    start_time = timezone.now() - timedelta(days=3)
    start_time_str = quote(start_time.isoformat())
    end_time = timezone.now()
    end_time_str = quote(end_time.isoformat())
    response = await self.client.get(f"/?start_time={start_time_str}&end_time={end_time_str}")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()[0]['location']['x'], 1.0)

  async def test_list_positions_character(self):
    player = await sync_to_async(PlayerFactory)()
    character = await player.characters.afirst()

    await CharacterLocation.objects.acreate(
      character=character,
      timestamp=timezone.now() - timedelta(hours=5),
      location=Point(1, 1, 1)
    )
    await CharacterLocation.objects.acreate(
      character=character,
      timestamp=timezone.now() - timedelta(hours=4),
      location=Point(1, 1, 1)
    )
    await CharacterLocation.objects.acreate(
      character=character,
      timestamp=timezone.now() - timedelta(hours=3),
      location=Point(1, 1, 1)
    )
    await CharacterLocation.objects.acreate(
      character=character,
      timestamp=timezone.now() - timedelta(hours=2),
      location=Point(1, 1, 1)
    )
    start_time = timezone.now() - timedelta(days=3)
    start_time_str = quote(start_time.isoformat())
    end_time = timezone.now()
    end_time_str = quote(end_time.isoformat())
    response = await self.client.get(f"/?start_time={start_time_str}&end_time={end_time_str}&player_id={player.unique_id}&num_samples=2")

    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertEqual(len(data), 2)
    self.assertEqual(data[0]['location']['x'], 1.0)


class TeamsAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(teams_router)

  async def test_list_teams(self):
    team = await sync_to_async(TeamFactory)()
    player = await sync_to_async(PlayerFactory)()
    await team.players.aadd(player)
    response = await self.client.get("/")
    data = response.json()
    self.assertEqual(len(data), 1)
    self.assertEqual(data[0]['id'], team.id)

  async def test_get_team(self):
    team = await sync_to_async(TeamFactory)()
    player = await sync_to_async(PlayerFactory)()
    await team.players.aadd(player)
    response = await self.client.get(f"/{team.id}/")
    data = response.json()
    self.assertEqual(data['id'], team.id)

class ScheduledEventAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(scheduled_events_router)

  async def test_results(self):
    game_event = await sync_to_async(GameEventFactory)(state=3)
    await sync_to_async(GameEventCharacterFactory)(
      game_event=game_event,
      finished=True,
    )
    await sync_to_async(GameEventCharacterFactory)(
      game_event=game_event,
      finished=False,
    )

    response = await self.client.get(f"/{game_event.scheduled_event_id}/results/")
    data = response.json()
    self.assertEqual(len(data), 2)

  async def test_results_time_trial(self):
    game_event = await sync_to_async(GameEventFactory)(
      state=3,
      scheduled_event__time_trial=True,
    )
    game_event.start_time = game_event.scheduled_event.start_time + timedelta(hours=1)
    await game_event.asave()
    await sync_to_async(GameEventCharacterFactory)(
      game_event=game_event,
      finished=True,
    )
    await sync_to_async(GameEventCharacterFactory)(
      game_event=game_event,
      finished=False,
    )

    response = await self.client.get(f"/{game_event.scheduled_event_id}/results/")
    data = response.json()
    self.assertEqual(len(data), 2)


class ResultsAPITestCase(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(results_router)

  async def test_lap_section_times(self):
    game_event = await sync_to_async(GameEventFactory)(
      state=3,
      scheduled_event__time_trial=True,
    )
    participant = await sync_to_async(GameEventCharacterFactory)(
      game_event=game_event,
      finished=True,
    )
    best_participant = await sync_to_async(GameEventCharacterFactory)(
      game_event=game_event,
      finished=True,
    )
    await LapSectionTime.objects.acreate(
      game_event_character=participant,
      lap=1,
      section_index=0,
      total_time_seconds=2,
      rank=1,
    )
    await LapSectionTime.objects.acreate(
      game_event_character=participant,
      lap=1,
      section_index=1,
      total_time_seconds=4,
      rank=1,
    )
    await LapSectionTime.objects.acreate(
      game_event_character=best_participant,
      lap=1,
      section_index=0,
      total_time_seconds=2,
      rank=1,
    )
    await LapSectionTime.objects.acreate(
      game_event_character=best_participant,
      lap=1,
      section_index=1,
      total_time_seconds=3,
      rank=1,
    )

    response = await self.client.get(
      f"/{participant.id}/lap_section_times/",
      query_params={'compare': best_participant.id}
    )
    data = response.json()
    self.assertEqual(len(data), 2)
    print(data)

class ChampionshipAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(championships_router)

  async def test_personal_standings(self):
    championship = await sync_to_async(ChampionshipFactory)()
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )

    response = await self.client.get(f"/{championship.id}/personal_standings/")
    data = response.json()
    self.assertEqual(len(data), 2)

  async def test_team_standings(self):
    championship = await sync_to_async(ChampionshipFactory)()
    cp = await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
    )
    cp2 = await sync_to_async(ChampionshipPointFactory)(
      championship=championship,
      team=cp.team,
      participant__game_event=cp.participant.game_event,
    )
    self.assertEqual(cp.championship, cp2.championship)
    self.assertEqual(cp.participant.game_event, cp2.participant.game_event)
    self.assertEqual(cp.team, cp2.team)

    response = await self.client.get(f"/{championship.id}/team_standings/")
    data = response.json()
    self.assertEqual(len(data), 1)


class WebhookAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(webhook_router)

  async def test_cargo_arrived(self):
    player = await sync_to_async(PlayerFactory)()
    response = await self.client.post('/', json={
      'hook': "/Script/MotorTown.MotorTownPlayerController:ServerCargoArrived",
      'timestamp': int(time.time() * 1000),
      'data': {
        'Cargos': [
          {
            'Net_CargoKey': 'oranges',
            'Net_Payment': {
              'BaseValue': 10_000,
              'ShadowedValue': 10_000,
            },
            'Net_Weight': 100.0,
            'Net_Damage': 0.0,
          }
        ],
        'PlayerId': str(player.unique_id),
      }
    })
    self.assertEqual(response.status_code, 200)
    self.assertEqual(
      await ServerCargoArrivedLog.objects.acount(),
      1
    )
    delivery = await ServerCargoArrivedLog.objects.select_related('player').afirst()
    self.assertEqual(delivery.payment, 10_000)
    self.assertEqual(delivery.cargo_key, 'oranges')
    self.assertEqual(delivery.weight, 100.0)
    self.assertEqual(delivery.damage, 0.0)
    self.assertEqual(delivery.player, player)

