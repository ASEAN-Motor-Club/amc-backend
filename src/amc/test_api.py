from datetime import timedelta
from urllib.parse import quote
from typing import cast, Any
from django.utils import timezone
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from django.test import TestCase
from ninja.testing import TestAsyncClient
from unittest.mock import patch
from amc.api.routes import (
    players_router,
    characters_router,
    player_locations_router,
    stats_router,
    teams_router,
    scheduled_events_router,
    championships_router,
    results_router,
    deliveryjobs_router,
    deliverypoints_router,
    app_router,
)
from amc.factories import (
    PlayerFactory,
    CharacterFactory,
    TeamFactory,
    GameEventFactory,
    GameEventCharacterFactory,
    ChampionshipFactory,
    ChampionshipPointFactory,
    DeliveryJobFactory,
    CargoFactory,
    DeliveryPointFactory,
)
from amc.models import (
    Character,
    PlayerStatusLog,
    PlayerRestockDepotLog,
    CharacterLocation,
    LapSectionTime,
    DeliveryPoint,
    DeliveryPointStorage,
)


class PlayersAPITest(TestCase):
    def setUp(self):
        self.maxDiff = None
        self.api_client = TestAsyncClient(players_router)

    async def test_get_player(self):
        player = await sync_to_async(PlayerFactory)()
        character = (
            await Character.objects.with_total_session_time()
            .filter(player=player)
            .order_by("-total_session_time", "id")
            .afirst()
        )
        response = await cast(Any, self.api_client.get(f"/{player.unique_id}/"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "discord_user_id": player.discord_user_id,
                "unique_id": str(player.unique_id),
                "total_session_time": "P0DT00H00M00S",
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
            },
        )

    async def test_get_player_logged_in(self):
        player = await sync_to_async(PlayerFactory)()
        character = (
            await Character.objects.with_total_session_time()
            .filter(player=player)
            .order_by("-total_session_time", "id")
            .afirst()
        )
        now = timezone.now()
        now = now.replace(microsecond=0)
        await PlayerStatusLog.objects.acreate(
            character=character,
            timespan=(now - timedelta(days=1), now - timedelta(hours=1)),
        )
        response = await cast(Any, self.api_client.get(f"/{player.unique_id}/"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
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
                "total_session_time": "P0DT23H00M00S",
                "last_login": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )

    async def test_get_player_characters(self):
        player = await sync_to_async(PlayerFactory)()
        response = await cast(
            Any, self.api_client.get(f"/{player.unique_id}/characters/")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
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
            ],
        )


class CharactersAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(characters_router)

    async def test_get_character(self):
        character = await sync_to_async(CharacterFactory)()
        response = await cast(Any, self.api_client.get(f"/{character.id}/"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
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
            },
        )


class LeaderboardsAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(stats_router)

    async def test_get_character(self):
        character = await sync_to_async(CharacterFactory)()
        await PlayerRestockDepotLog.objects.acreate(
            character=character, timestamp=timezone.now(), depot_name="test"
        )
        response = await cast(
            Any, self.api_client.get("/depots_restocked_leaderboard/")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["depots_restocked"], 1)


class PlayerLocationsAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(player_locations_router)

    async def test_list_positions(self):
        character = await sync_to_async(CharacterFactory)()
        await CharacterLocation.objects.acreate(
            character=character,
            timestamp=timezone.now() - timedelta(hours=3),
            location=Point(1, 1, 1),
        )
        start_time = timezone.now() - timedelta(days=3)
        start_time_str = quote(start_time.isoformat())
        end_time = timezone.now()
        end_time_str = quote(end_time.isoformat())
        response = await cast(
            Any,
            self.api_client.get(
                f"/?start_time={start_time_str}&end_time={end_time_str}"
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["location"]["x"], 1.0)

    async def test_list_positions_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await player.characters.afirst()

        await CharacterLocation.objects.acreate(
            character=character,
            timestamp=timezone.now() - timedelta(hours=5),
            location=Point(1, 1, 1),
        )
        await CharacterLocation.objects.acreate(
            character=character,
            timestamp=timezone.now() - timedelta(hours=4),
            location=Point(1, 1, 1),
        )
        await CharacterLocation.objects.acreate(
            character=character,
            timestamp=timezone.now() - timedelta(hours=3),
            location=Point(1, 1, 1),
        )
        await CharacterLocation.objects.acreate(
            character=character,
            timestamp=timezone.now() - timedelta(hours=2),
            location=Point(1, 1, 1),
        )
        start_time = timezone.now() - timedelta(days=3)
        start_time_str = quote(start_time.isoformat())
        end_time = timezone.now()
        end_time_str = quote(end_time.isoformat())
        response = await cast(
            Any,
            self.api_client.get(
                f"/?start_time={start_time_str}&end_time={end_time_str}&player_id={player.unique_id}&num_samples=2"
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["location"]["x"], 1.0)


class TeamsAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(teams_router)

    async def test_list_teams(self):
        team = await sync_to_async(TeamFactory)()
        player = await sync_to_async(PlayerFactory)()
        await team.players.aadd(player)
        response = await cast(Any, self.api_client.get("/"))
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], team.id)

    async def test_get_team(self):
        team = await sync_to_async(TeamFactory)()
        player = await sync_to_async(PlayerFactory)()
        await team.players.aadd(player)
        response = await cast(Any, self.api_client.get(f"/{team.id}/"))
        data = response.json()
        self.assertEqual(data["id"], team.id)


class ScheduledEventAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(scheduled_events_router)

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

        response = await cast(
            Any, self.api_client.get(f"/{game_event.scheduled_event_id}/results/")
        )
        data = response.json()
        self.assertEqual(len(data), 2)

    async def test_results_time_trial(self):
        game_event = await sync_to_async(GameEventFactory)(
            state=3,
            scheduled_event__time_trial=True,
        )
        game_event.start_time = game_event.scheduled_event.start_time + timedelta(
            hours=1
        )
        await game_event.asave()
        await sync_to_async(GameEventCharacterFactory)(
            game_event=game_event,
            finished=True,
        )
        await sync_to_async(GameEventCharacterFactory)(
            game_event=game_event,
            finished=False,
        )

        response = await cast(
            Any, self.api_client.get(f"/{game_event.scheduled_event_id}/results/")
        )
        data = response.json()
        self.assertEqual(len(data), 2)


class ResultsAPITestCase(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(results_router)

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

        response = await cast(
            Any,
            self.api_client.get(
                f"/{participant.id}/lap_section_times/",
                query_params={"compare": best_participant.id},
            ),
        )
        data = response.json()
        self.assertEqual(len(data), 2)
        print(data)


class ChampionshipAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(championships_router)

    async def test_personal_standings(self):
        championship = await sync_to_async(ChampionshipFactory)()
        await sync_to_async(ChampionshipPointFactory)(
            championship=championship,
        )
        await sync_to_async(ChampionshipPointFactory)(
            championship=championship,
        )

        response = await cast(
            Any, self.api_client.get(f"/{championship.id}/personal_standings/")
        )
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

        response = await cast(
            Any, self.api_client.get(f"/{championship.id}/team_standings/")
        )
        data = response.json()
        self.assertEqual(len(data), 1)


class DeliveryJobsAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(deliveryjobs_router)

    async def test_list(self):
        job = await sync_to_async(DeliveryJobFactory)()
        await job.cargos.aadd(await sync_to_async(CargoFactory)())
        await job.source_points.aadd(await sync_to_async(DeliveryPointFactory)())
        await job.destination_points.aadd(await sync_to_async(DeliveryPointFactory)())

        response = await cast(Any, self.api_client.get("/"))
        data = response.json()
        self.assertEqual(len(data), 1)


class DeliveryPointsAPITest(TestCase):
    def setUp(self):
        self.api_client = TestAsyncClient(deliverypoints_router)

    async def test_list_deliverypoints(self):
        await sync_to_async(DeliveryPointFactory)()
        await sync_to_async(DeliveryPointFactory)()

        response = await cast(Any, self.api_client.get("/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        for dp in data:
            self.assertIn("guid", dp)
            self.assertIn("name", dp)
            self.assertIn("type", dp)
            self.assertIn("last_updated", dp)

    async def test_get_deliverypoint_detail_includes_data(self):
        dp = await DeliveryPoint.objects.acreate(
            guid="test-dp-001",
            name="Test Depot",
            type="Mine",
            coord=Point(100, 200, 300, srid=3857),
            data={
                "inputInventory": [
                    {"cargoKey": "C::Stone", "amount": 50, "cargo": {"name": "Stone"}},
                ],
                "outputInventory": [
                    {"cargoKey": "C::Iron", "amount": 30, "cargo": {"name": "Iron"}},
                ],
                "deliveries": [],
            },
        )

        response = await cast(Any, self.api_client.get(f"/{dp.guid}/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["guid"], "test-dp-001")
        self.assertEqual(data["name"], "Test Depot")
        self.assertIn(
            "data", data, "data field must be present for frontend compatibility"
        )
        self.assertIn("inputInventory", data["data"])
        self.assertIn("outputInventory", data["data"])
        self.assertEqual(len(data["data"]["inputInventory"]), 1)
        self.assertEqual(data["data"]["inputInventory"][0]["cargoKey"], "C::Stone")
        self.assertEqual(data["data"]["inputInventory"][0]["amount"], 50)
        self.assertEqual(len(data["data"]["outputInventory"]), 1)
        self.assertEqual(data["data"]["outputInventory"][0]["cargoKey"], "C::Iron")
        self.assertEqual(data["data"]["outputInventory"][0]["amount"], 30)

    async def test_get_deliverypoint_without_data(self):
        dp = await DeliveryPoint.objects.acreate(
            guid="test-dp-002",
            name="Empty Depot",
            type="Factory",
            coord=Point(400, 500, 600, srid=3857),
        )

        response = await cast(Any, self.api_client.get(f"/{dp.guid}/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn(
            "data", data, "data field must be present for frontend compatibility"
        )
        self.assertIn("inputInventory", data["data"])
        self.assertIn("outputInventory", data["data"])
        self.assertEqual(data["data"]["inputInventory"], [])
        self.assertEqual(data["data"]["outputInventory"], [])

    async def test_get_deliverypoint_null_data_falls_back_to_storage(self):
        dp = await DeliveryPoint.objects.acreate(
            guid="test-dp-003",
            name="Storage Depot",
            type="Factory",
            coord=Point(500, 600, 700, srid=3857),
        )
        cargo = await sync_to_async(CargoFactory)(key="C::Wood", label="Wood")
        await DeliveryPointStorage.objects.acreate(
            delivery_point=dp,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="C::Wood",
            cargo=cargo,
            amount=25,
            capacity=100,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=dp,
            kind=DeliveryPointStorage.Kind.OUTPUT,
            cargo_key="C::Planks",
            cargo=cargo,
            amount=10,
            capacity=50,
        )

        response = await cast(Any, self.api_client.get(f"/{dp.guid}/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("data", data)
        self.assertIn("inputInventory", data["data"])
        self.assertIn("outputInventory", data["data"])
        self.assertEqual(len(data["data"]["inputInventory"]), 1)
        self.assertEqual(data["data"]["inputInventory"][0]["cargoKey"], "C::Wood")
        self.assertEqual(data["data"]["inputInventory"][0]["amount"], 25)
        self.assertEqual(len(data["data"]["outputInventory"]), 1)
        self.assertEqual(data["data"]["outputInventory"][0]["cargoKey"], "C::Planks")
        self.assertEqual(data["data"]["outputInventory"][0]["amount"], 10)


class DepotsAPITest(TestCase):
    """Test the /depots/ endpoint"""

    def setUp(self):
        self.api_client = TestAsyncClient(app_router)

    @patch("amc.api.routes.get_world")
    async def test_list_depots_without_owner(self, mock_get_world):
        """Test GET /depots/ without owner query param"""
        mock_get_world.return_value = {
            "depot": [
                {"name": "Depot A", "storage": 100, "taxiDispatchLevel": 1, "buildingGuid": "guid-a"},
                {"name": "Depot B", "storage": 200, "taxiDispatchLevel": 2, "buildingGuid": "guid-b"},
            ],
        }

        response = await cast(Any, self.api_client.get("/depots/"))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["guid"], "guid-a")
        self.assertEqual(data[0]["name"], "Depot A")
        self.assertEqual(data[0]["storage"], 100)
        self.assertEqual(data[0]["taxiDispatchLevel"], 1)
        self.assertNotIn("owner", data[0])
        self.assertEqual(data[1]["guid"], "guid-b")
        self.assertEqual(data[1]["name"], "Depot B")
        self.assertEqual(data[1]["storage"], 200)
        self.assertEqual(data[1]["taxiDispatchLevel"], 2)
        self.assertNotIn("owner", data[1])

    @patch("amc.api.routes.get_world")
    async def test_list_depots_with_owner_false(self, mock_get_world):
        """Test GET /depots/?owner=false does not include owner"""
        mock_get_world.return_value = {
            "depot": [
                {"name": "Depot A", "storage": 100, "taxiDispatchLevel": 1, "buildingGuid": "guid-a"},
            ],
        }

        response = await cast(Any, self.api_client.get("/depots/?owner=false"))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["guid"], "guid-a")
        self.assertNotIn("owner", data[0])

    @patch("amc.api.routes.get_world")
    async def test_list_depots_with_owner_true(self, mock_get_world):
        """Test GET /depots/?owner=true includes housingKey as owner"""
        mock_get_world.return_value = {
            "depot": [
                {
                    "name": "Depot A",
                    "storage": 100,
                    "taxiDispatchLevel": 1,
                    "buildingGuid": "guid-1",
                },
                {
                    "name": "Depot B",
                    "storage": 200,
                    "taxiDispatchLevel": 2,
                    "buildingGuid": "guid-2",
                },
                {
                    "name": "Depot C",
                    "storage": 300,
                    "taxiDispatchLevel": 3,
                    "buildingGuid": "guid-missing",
                },
            ],
            "building": [
                {"guid": "guid-1", "housingKey": "House1"},
                {"guid": "guid-2", "housingKey": "House2"},
                {"guid": "guid-3", "housingKey": "House3"},
            ],
        }

        response = await cast(Any, self.api_client.get("/depots/?owner=true"))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["guid"], "guid-1")
        self.assertEqual(data[0]["name"], "Depot A")
        self.assertEqual(data[0]["owner"], "House1")
        self.assertEqual(data[1]["guid"], "guid-2")
        self.assertEqual(data[1]["name"], "Depot B")
        self.assertEqual(data[1]["owner"], "House2")
        self.assertEqual(data[2]["guid"], "guid-missing")
        self.assertEqual(data[2]["name"], "Depot C")
        self.assertIsNone(data[2]["owner"])

    @patch("amc.api.routes.get_world")
    async def test_list_depots_empty(self, mock_get_world):
        """Test GET /depots/ returns empty list when no depots"""
        mock_get_world.return_value = {"depot": []}

        response = await cast(Any, self.api_client.get("/depots/"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    @patch("amc.api.routes.get_world_last_modified")
    async def test_list_depots_unchanged_since_last_fetch(self, mock_get_mtime):
        """Test GET /depots/ with If-Modified-Since returns 304 when world unchanged"""
        mock_get_mtime.return_value = 946684800.0

        response = await cast(
            Any,
            self.api_client.get(
                "/depots/",
                headers={"If-Modified-Since": "Sat, 01 Jan 2000 00:00:00 GMT"},
            ),
        )

        self.assertEqual(response.status_code, 304)

    @patch("amc.api.routes.get_world_last_modified")
    @patch("amc.api.routes.get_world")
    async def test_list_depots_changed_since_last_fetch(
        self, mock_get_world, mock_get_mtime
    ):
        """Test GET /depots/ with If-Modified-Since returns data when world changed"""
        mock_get_mtime.return_value = 946684801.0
        mock_get_world.return_value = {
            "depot": [
                {"name": "Depot A", "storage": 100, "taxiDispatchLevel": 1, "buildingGuid": "guid-a"}
            ],
        }

        response = await cast(
            Any,
            self.api_client.get(
                "/depots/",
                headers={"If-Modified-Since": "Sat, 01 Jan 2000 00:00:00 GMT"},
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("Last-Modified"),
            "Sat, 01 Jan 2000 00:00:01 GMT",
        )
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["guid"], "guid-a")
        self.assertEqual(data[0]["name"], "Depot A")
