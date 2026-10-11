import json
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone
from amc.models import GameEvent, GameEventCharacter, RaceSetup, ScheduledEvent
from amc.events import process_event


class EventsTests(TestCase):
    async def test_event_empty_route(self):
        data = json.loads(
            """{"data":[{"Players":[{"LastSectionTotalTimeSeconds":0.0, "SectionIndex":-1, "BestLapTime":0.0, "bFinished":false, "Reward_Money":{"BaseValue":0, "ShadowedValue":521312}, "bWrongVehicle":false, "LapTimes":{}, "Reward_RacingExp":0, "PlayerName":"freeman", "bWrongEngine":false, "bDisqualified":false, "Rank":0, "Laps":0, "CharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}}], "OwnerCharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}, "EventType":1, "EventGuid":"5B11926A45D1869C3AA6309F3F564829", "EventName":"freeman's Event", "RaceSetup":{"VehicleKeys":{}, "Route":{"Waypoints":{}, "RouteName":""}, "EngineKeys":{}, "NumLaps":0}, "State":1, "bInCountdown":false}]}"""
        )
        event = data["data"][0]
        await process_event(event)
        self.assertTrue(await GameEvent.objects.aexists())

    async def test_event_ready(self):
        data = json.loads(
            """{"data":[{"Players":[{"LastSectionTotalTimeSeconds":0.0, "SectionIndex":-1, "BestLapTime":0.0, "bFinished":false, "Reward_Money":{"BaseValue":0, "ShadowedValue":521312}, "bWrongVehicle":false, "LapTimes":{}, "Reward_RacingExp":0, "PlayerName":"freeman", "bWrongEngine":false, "bDisqualified":false, "Rank":0, "Laps":0, "CharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}}], "OwnerCharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}, "EventType":1, "EventGuid":"5B11926A45D1869C3AA6309F3F564829", "EventName":"freeman's Event", "RaceSetup":{"VehicleKeys":{}, "Route":{"Waypoints":[{"Rotation":{"Z":0.0, "W":1.0, "X":0.0, "Y":-0.0}, "Scale3D":{"Z":10.0, "X":1.0, "Y":20.0}, "Location":{"Z":-19609.658969609, "X":-254858.28075295, "Y":118884.42245999}}, {"Rotation":{"Z":0.0, "W":1.0, "X":0.0, "Y":-0.0}, "Scale3D":{"Z":10.0, "X":1.0, "Y":20.0}, "Location":{"Z":-19115.643333376, "X":-240477.2487217, "Y":99544.413866238}}], "RouteName":"My Event Route"}, "EngineKeys":{}, "NumLaps":0}, "State":1, "bInCountdown":false}]}"""
        )
        event = data["data"][0]
        await process_event(event)
        self.assertTrue(await GameEvent.objects.aexists())
        game_event_character = await GameEventCharacter.objects.afirst()
        self.assertEqual(await game_event_character.lap_section_times.acount(), 0)

        data = json.loads(
            """{"data":[{"Players":[{"LastSectionTotalTimeSeconds":0.0, "SectionIndex":-1, "BestLapTime":0.0, "bFinished":false, "Reward_Money":{"BaseValue":0, "ShadowedValue":521312}, "bWrongVehicle":false, "LapTimes":{}, "Reward_RacingExp":0, "PlayerName":"freeman", "bWrongEngine":false, "bDisqualified":false, "Rank":0, "Laps":0, "CharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}}], "OwnerCharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}, "EventType":1, "EventGuid":"5B11926A45D1869C3AA6309F3F564829", "EventName":"freeman's Event", "RaceSetup":{"VehicleKeys":{}, "Route":{"Waypoints":[{"Rotation":{"Z":0.0, "W":1.0, "X":0.0, "Y":-0.0}, "Scale3D":{"Z":10.0, "X":1.0, "Y":20.0}, "Location":{"Z":-19609.658969609, "X":-254858.28075295, "Y":118884.42245999}}, {"Rotation":{"Z":0.0, "W":1.0, "X":0.0, "Y":-0.0}, "Scale3D":{"Z":10.0, "X":1.0, "Y":20.0}, "Location":{"Z":-19115.643333376, "X":-240477.2487217, "Y":99544.413866238}}, {"Rotation":{"Z":-0.98809630079603, "W":0.15068189790301, "X":0.030640382976089, "Y":0.0046725719503176}, "Scale3D":{"Z":10.0, "X":1.0, "Y":11.520000732422}, "Location":{"Z":-16764.790649464, "X":-242392.21107517, "Y":43289.42309418}}], "RouteName":"My Event Route"}, "EngineKeys":{}, "NumLaps":0}, "State":1, "bInCountdown":false}]}"""
        )
        event = data["data"][0]
        await process_event(event)
        game_event_character = await GameEventCharacter.objects.afirst()
        self.assertEqual(await game_event_character.lap_section_times.acount(), 0)

    async def test_event_in_progress(self):
        data = json.loads(
            """{"data":[{"Players":[{"LastSectionTotalTimeSeconds":0.0, "SectionIndex":-1, "BestLapTime":0.0, "bFinished":false, "Reward_Money":{"BaseValue":0, "ShadowedValue":521312}, "bWrongVehicle":false, "LapTimes":{}, "Reward_RacingExp":0, "PlayerName":"freeman", "bWrongEngine":false, "bDisqualified":false, "Rank":1, "Laps":0, "CharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}}], "OwnerCharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}, "EventType":1, "EventGuid":"5B11926A45D1869C3AA6309F3F564829", "EventName":"freeman's Event", "RaceSetup":{"VehicleKeys":{}, "Route":{"Waypoints":[{"Rotation":{"Z":0.98488250115999, "W":0.17322372501724, "X":-1.4767306650533e-11, "Y":2.5973127387929e-12}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999556, "X":-186309.94382771, "Y":-2238.2620422257}}, {"Rotation":{"Z":0.98500605282663, "W":0.17251978406809, "X":3.0069591686612e-10, "Y":-5.266566078354e-11}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999722, "X":-188908.25843948, "Y":-1332.4194582572}}], "RouteName":"My Event Route"}, "EngineKeys":{}, "NumLaps":0}, "State":2, "bInCountdown":false}]}"""
        )
        event = data["data"][0]
        await process_event(event)
        self.assertTrue(await GameEvent.objects.aexists())
        game_event_character = await GameEventCharacter.objects.afirst()
        self.assertEqual(await game_event_character.lap_section_times.acount(), 0)

        data = json.loads(
            """{"data":[{"Players":[{"LastSectionTotalTimeSeconds":69.733757019043, "SectionIndex":0, "BestLapTime":0.0, "bFinished":false, "Reward_Money":{"BaseValue":0, "ShadowedValue":521312}, "bWrongVehicle":false, "LapTimes":{}, "Reward_RacingExp":0, "PlayerName":"freeman", "bWrongEngine":false, "bDisqualified":false, "Rank":1, "Laps":1, "CharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}}], "OwnerCharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}, "EventType":1, "EventGuid":"5B11926A45D1869C3AA6309F3F564829", "EventName":"freeman's Event", "RaceSetup":{"VehicleKeys":{}, "Route":{"Waypoints":[{"Rotation":{"Z":0.98488250115999, "W":0.17322372501724, "X":-1.4767306650533e-11, "Y":2.5973127387929e-12}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999556, "X":-186309.94382771, "Y":-2238.2620422257}}, {"Rotation":{"Z":0.98500605282663, "W":0.17251978406809, "X":3.0069591686612e-10, "Y":-5.266566078354e-11}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999722, "X":-188908.25843948, "Y":-1332.4194582572}}], "RouteName":"My Event Route"}, "EngineKeys":{}, "NumLaps":0}, "State":2, "bInCountdown":false}]}"""
        )
        event = data["data"][0]
        await process_event(event)
        game_event_character = await GameEventCharacter.objects.afirst()
        self.assertEqual(await game_event_character.lap_section_times.acount(), 1)

        data = json.loads(
            """{"data":[{"Players":[{"LastSectionTotalTimeSeconds":1216.6295166016, "SectionIndex":1, "BestLapTime":0.0, "bFinished":true, "Reward_Money":{"BaseValue":0, "ShadowedValue":521312}, "bWrongVehicle":false, "LapTimes":{}, "Reward_RacingExp":0, "PlayerName":"freeman", "bWrongEngine":false, "bDisqualified":false, "Rank":1, "Laps":1, "CharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}}], "OwnerCharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}, "EventType":1, "EventGuid":"5B11926A45D1869C3AA6309F3F564829", "EventName":"freeman's Event", "RaceSetup":{"VehicleKeys":{}, "Route":{"Waypoints":[{"Rotation":{"Z":0.98488250115999, "W":0.17322372501724, "X":-1.4767306650533e-11, "Y":2.5973127387929e-12}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999556, "X":-186309.94382771, "Y":-2238.2620422257}}, {"Rotation":{"Z":0.98500605282663, "W":0.17251978406809, "X":3.0069591686612e-10, "Y":-5.266566078354e-11}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999722, "X":-188908.25843948, "Y":-1332.4194582572}}], "RouteName":"My Event Route"}, "EngineKeys":{}, "NumLaps":0}, "State":3, "bInCountdown":false}]}"""
        )
        event = data["data"][0]
        await process_event(event)
        game_event_character = await GameEventCharacter.objects.afirst()
        self.assertEqual(await game_event_character.lap_section_times.acount(), 2)

    async def test_event_state_back_to_ready(self):
        data = json.loads(
            """{"data":[{"Players":[{"LastSectionTotalTimeSeconds":0.0, "SectionIndex":-1, "BestLapTime":0.0, "bFinished":false, "Reward_Money":{"BaseValue":0, "ShadowedValue":521312}, "bWrongVehicle":false, "LapTimes":{}, "Reward_RacingExp":0, "PlayerName":"freeman", "bWrongEngine":false, "bDisqualified":false, "Rank":1, "Laps":0, "CharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}}], "OwnerCharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}, "EventType":1, "EventGuid":"5B11926A45D1869C3AA6309F3F564829", "EventName":"freeman's Event", "RaceSetup":{"VehicleKeys":{}, "Route":{"Waypoints":[{"Rotation":{"Z":0.98488250115999, "W":0.17322372501724, "X":-1.4767306650533e-11, "Y":2.5973127387929e-12}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999556, "X":-186309.94382771, "Y":-2238.2620422257}}, {"Rotation":{"Z":0.98500605282663, "W":0.17251978406809, "X":3.0069591686612e-10, "Y":-5.266566078354e-11}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999722, "X":-188908.25843948, "Y":-1332.4194582572}}], "RouteName":"My Event Route"}, "EngineKeys":{}, "NumLaps":0}, "State":2, "bInCountdown":false}]}"""
        )
        event = data["data"][0]
        await process_event(event)
        self.assertTrue(await GameEvent.objects.aexists())
        await GameEventCharacter.objects.afirst()

        data = json.loads(
            """{"data":[{"Players":[{"LastSectionTotalTimeSeconds":0.0, "SectionIndex":-1, "BestLapTime":0.0, "bFinished":false, "Reward_Money":{"BaseValue":0, "ShadowedValue":521312}, "bWrongVehicle":false, "LapTimes":{}, "Reward_RacingExp":0, "PlayerName":"freeman", "bWrongEngine":false, "bDisqualified":false, "Rank":0, "Laps":0, "CharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}}], "OwnerCharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}, "EventType":1, "EventGuid":"5B11926A45D1869C3AA6309F3F564829", "EventName":"freeman's Event", "RaceSetup":{"VehicleKeys":{}, "Route":{"Waypoints":[{"Rotation":{"Z":0.0, "W":1.0, "X":0.0, "Y":-0.0}, "Scale3D":{"Z":10.0, "X":1.0, "Y":20.0}, "Location":{"Z":-19609.658969609, "X":-254858.28075295, "Y":118884.42245999}}, {"Rotation":{"Z":0.0, "W":1.0, "X":0.0, "Y":-0.0}, "Scale3D":{"Z":10.0, "X":1.0, "Y":20.0}, "Location":{"Z":-19115.643333376, "X":-240477.2487217, "Y":99544.413866238}}], "RouteName":"My Event Route"}, "EngineKeys":{}, "NumLaps":0}, "State":1, "bInCountdown":false}]}"""
        )
        event = data["data"][0]
        await process_event(event)
        await GameEventCharacter.objects.afirst()
        self.assertEqual(
            [game_event.state async for game_event in GameEvent.objects.all()], [2, 1]
        )

        data = json.loads(
            """{"data":[{"Players":[{"LastSectionTotalTimeSeconds":0.0, "SectionIndex":-1, "BestLapTime":0.0, "bFinished":false, "Reward_Money":{"BaseValue":0, "ShadowedValue":521312}, "bWrongVehicle":false, "LapTimes":{}, "Reward_RacingExp":0, "PlayerName":"freeman", "bWrongEngine":false, "bDisqualified":false, "Rank":1, "Laps":0, "CharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}}], "OwnerCharacterId":{"UniqueNetId":"76561198378447512", "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181"}, "EventType":1, "EventGuid":"5B11926A45D1869C3AA6309F3F564829", "EventName":"freeman's Event", "RaceSetup":{"VehicleKeys":{}, "Route":{"Waypoints":[{"Rotation":{"Z":0.98488250115999, "W":0.17322372501724, "X":-1.4767306650533e-11, "Y":2.5973127387929e-12}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999556, "X":-186309.94382771, "Y":-2238.2620422257}}, {"Rotation":{"Z":0.98500605282663, "W":0.17251978406809, "X":3.0069591686612e-10, "Y":-5.266566078354e-11}, "Scale3D":{"Z":10.0, "X":1.0, "Y":25.0}, "Location":{"Z":-13849.999999722, "X":-188908.25843948, "Y":-1332.4194582572}}], "RouteName":"My Event Route"}, "EngineKeys":{}, "NumLaps":0}, "State":2, "bInCountdown":false}]}"""
        )
        event = data["data"][0]
        await process_event(event)
        await GameEventCharacter.objects.afirst()
        self.assertEqual(
            [game_event.state async for game_event in GameEvent.objects.all()], [2, 2]
        )


def _payload(event_name, race_setup_config, state=1):
    return {
        "Players": [
            {
                "LastSectionTotalTimeSeconds": 0.0,
                "SectionIndex": -1,
                "BestLapTime": 0.0,
                "bFinished": False,
                "Reward_Money": {"BaseValue": 0, "ShadowedValue": 521312},
                "bWrongVehicle": False,
                "LapTimes": {},
                "Reward_RacingExp": 0,
                "PlayerName": "freeman",
                "bWrongEngine": False,
                "bDisqualified": False,
                "Rank": 0,
                "Laps": 0,
                "CharacterId": {
                    "UniqueNetId": "76561198378447512",
                    "CharacterGuid": "E603C74946EFF3F8834C9AAB3D0E3181",
                },
            }
        ],
        "OwnerCharacterId": {
            "UniqueNetId": "76561198378447512",
            "CharacterGuid": "E603C74946EFF3F8834C9AAB3D0E3181",
        },
        "EventType": 1,
        "EventGuid": "5B11926A45D1869C3AA6309F3F564829",
        "EventName": event_name,
        "RaceSetup": race_setup_config,
        "State": state,
        "bInCountdown": False,
    }


_ROUTE_A = {
    "VehicleKeys": {},
    "Route": {
        "Waypoints": [
            {
                "Rotation": {"Z": 0.0, "W": 1.0, "X": 0.0, "Y": -0.0},
                "Scale3D": {"Z": 10.0, "X": 1.0, "Y": 20.0},
                "Location": {
                    "Z": -19609.658969609,
                    "X": -254858.28075295,
                    "Y": 118884.42245999,
                },
            }
        ],
        "RouteName": "My Event Route",
    },
    "EngineKeys": {},
    "NumLaps": 0,
}

# Same route in the game's re-emitted key shape (Location→Translation) —
# the hash over the whole config differs even though the route is identical.
_ROUTE_A_TRANSLATED = {
    "VehicleKeys": [],
    "Route": {
        "Waypoints": [
            {
                "Rotation": {"Z": 0.0, "W": 1.0, "X": 0.0, "Y": -0.0},
                "Scale3D": {"Z": 10.0, "X": 1.0, "Y": 20.0},
                "Translation": {
                    "Z": -19609.658969609,
                    "X": -254858.28075295,
                    "Y": 118884.42245999,
                },
            }
        ],
        "RouteName": "My Event Route",
    },
    "EngineKeys": [],
    "NumLaps": 0,
}


class ScheduledEventMatchTests(TestCase):
    async def _make_scheduled_event(
        self, name, config, start_delta_hours, end_delta_hours
    ):
        def _create():
            rs = RaceSetup.objects.create(
                config=config, hash=RaceSetup.calculate_hash(config)
            )
            now = timezone.now()
            return ScheduledEvent.objects.create(
                name=name,
                race_setup=rs,
                start_time=now + timedelta(hours=start_delta_hours),
                end_time=now + timedelta(hours=end_delta_hours),
                time_trial=True,
                description="",
            )

        return await sync_to_async(_create)()

    async def test_linked_by_race_setup_hash(self):
        await self._make_scheduled_event(
            "Get the Priest", _ROUTE_A, start_delta_hours=-1, end_delta_hours=1
        )
        event = _payload("Get the Priest - IR - 620", _ROUTE_A)
        game_event, _, scheduled_event = await process_event(event)
        self.assertIsNotNone(scheduled_event)
        self.assertIsNotNone(game_event.scheduled_event)

    async def test_linked_by_name_prefix_when_hash_drifts(self):
        """The stored template config and the game's re-emission can differ in
        key shape (Location/Translation, VehicleKeys) — same route, different
        hash. The IR-suffix name match must rescue the link."""
        await self._make_scheduled_event(
            "Get the Priest", _ROUTE_A, start_delta_hours=-1, end_delta_hours=1
        )
        event = _payload("Get the Priest - IR - 620", _ROUTE_A_TRANSLATED)
        game_event, _, scheduled_event = await process_event(event)
        self.assertIsNotNone(scheduled_event)
        self.assertIsNotNone(game_event.scheduled_event)
        self.assertEqual(game_event.scheduled_event.name, "Get the Priest")

    async def test_player_event_stays_unlinked(self):
        """A player ad-hoc event whose name only coincidentally shares a prefix
        fragment must NOT match a scheduled event."""
        await self._make_scheduled_event(
            "Get the Priest", _ROUTE_A, start_delta_hours=-1, end_delta_hours=1
        )
        event = _payload("Get the Priests Private Run", _ROUTE_A_TRANSLATED)
        game_event, _, scheduled_event = await process_event(event)
        self.assertIsNone(scheduled_event)
        self.assertIsNone(game_event.scheduled_event)

    async def test_name_match_ignored_outside_window(self):
        await self._make_scheduled_event(
            "Get the Priest", _ROUTE_A, start_delta_hours=2, end_delta_hours=3
        )
        event = _payload("Get the Priest - IR - 620", _ROUTE_A_TRANSLATED)
        game_event, _, scheduled_event = await process_event(event)
        self.assertIsNone(scheduled_event)
        self.assertIsNone(game_event.scheduled_event)
