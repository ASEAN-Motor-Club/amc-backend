from datetime import timedelta
from django.utils import timezone
from asgiref.sync import sync_to_async
from django.test import TestCase
from ninja.testing import TestAsyncClient
from amc.api.routes import (
  players_router,
  characters_router,
  stats_router,
)
from amc.factories import (
  PlayerFactory,
  CharacterFactory
)
from amc.models import (
  PlayerStatusLog,
  PlayerRestockDepotLog
)

class PlayersAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(players_router)

  async def test_get_player(self):
    player = await sync_to_async(PlayerFactory)()
    response = await self.client.get(f"/{player.unique_id}/")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {
      "discord_user_id": player.discord_user_id,
      "unique_id": str(player.unique_id),
      "total_session_time": 'P0DT00H00M00S',
      "last_login": None,
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
    response = await self.client.get("/depots_restocked/")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()[0]['depots_restocked'], 1)

