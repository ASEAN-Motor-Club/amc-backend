from datetime import timedelta
from asgiref.sync import sync_to_async
from django.test import TestCase
from ninja.testing import TestAsyncClient
from amc.api.routes import (
  players_router,
  characters_router,
)
from amc.factories import (
  PlayerFactory,
  CharacterFactory
)

class PlayersAPITest(TestCase):
  def setUp(self):
    self.client = TestAsyncClient(players_router)

  async def test_get_player(self):
    player = await sync_to_async(PlayerFactory)()
    response = await self.client.get(f"/{player.unique_id}")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {
      "discord_user_id": player.discord_user_id,
      "unique_id": player.unique_id,
      "total_session_time": str(timedelta(0)),
    })

  async def test_get_player_characters(self):
    player = await sync_to_async(PlayerFactory)()
    response = await self.client.get(f"/{player.unique_id}/characters")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), [
      {
        "id": character.id,
        "name": character.name,
        "player_id": player.unique_id,
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
    response = await self.client.get(f"/{character.id}")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {
      "id": character.id,
      "name": character.name,
      "player_id": character.player.unique_id,
      "driver_level": None,
      "bus_level": None,
      "taxi_level": None,
      "police_level": None,
      "truck_level": None,
      "wrecker_level": None,
      "racer_level": None,
    })

