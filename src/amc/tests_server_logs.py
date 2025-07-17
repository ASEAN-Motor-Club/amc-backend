from datetime import datetime, timedelta
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from amc.server_logs import (
    parse_log_line,
    PlayerChatMessageLogEvent,
    PlayerLoginLogEvent,
    LegacyPlayerLogoutLogEvent,
    PlayerLogoutLogEvent,
    PlayerEnteredVehicleLogEvent,
    PlayerExitedVehicleLogEvent,
    PlayerRestockedDepotLogEvent,
    PlayerLevelChangedLogEvent,
    CompanyAddedLogEvent,
    AnnouncementLogEvent,
    UnknownLogEntry,
)
from amc.tasks import process_log_event
from amc.models import (
  Player,
  Character,
  Company,
  ServerLog,
  BotInvocationLog,
  SongRequestLog,
  PlayerStatusLog,
  PlayerChatLog,
  PlayerVehicleLog,
  PlayerRestockDepotLog,
)
from zoneinfo import ZoneInfo

class LogParserTestCase(SimpleTestCase):
    """
    Test suite for the log parsing logic.
    """

    async def test_parse_player_chat_message(self):
        """
        Verifies that a standard player chat message is parsed correctly.
        """
        log_line = "2024-07-08T10:00:00.123Z hostname tag filename [2025.03.22-08.13.07] [CHAT] TestPlayer (123): Hello world!"
        expected_timestamp = datetime.fromisoformat("2025-03-22T08:13:07Z").replace(tzinfo=ZoneInfo('UTC'))

        # Await the async function call
        _log, result = parse_log_line(log_line)

        # Assert the type is correct
        self.assertIsInstance(result, PlayerChatMessageLogEvent)

        # Assert the content is correct
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.player_name, "TestPlayer")
        self.assertEqual(result.player_id, 123)
        self.assertEqual(result.message, "Hello world!")

    async def test_parse_player_login(self):
        """
        Verifies that a player login event is parsed correctly.
        """
        log_line = "2024-07-08T10:01:00Z hostname tag filename [2025.03.22-08.13.07] Player Login: Admin (1)"
        expected_timestamp = datetime.fromisoformat("2025-03-22T08:13:07Z").replace(tzinfo=ZoneInfo('UTC'))

        _log, result = parse_log_line(log_line)

        self.assertIsInstance(result, PlayerLoginLogEvent)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.player_name, "Admin")
        self.assertEqual(result.player_id, 1)

    async def test_parse_player_logout_legacy(self):
        """
        Verifies that a player login event is parsed correctly.
        """
        log_line = "2024-07-08T10:01:00Z hostname tag filename [2025.03.22-08.13.07] Player Logout: Admin"
        expected_timestamp = datetime.fromisoformat("2025-03-22T08:13:07Z").replace(tzinfo=ZoneInfo('UTC'))

        _log, result = parse_log_line(log_line)

        self.assertIsInstance(result, LegacyPlayerLogoutLogEvent)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.player_name, "Admin")

    async def test_parse_company_added(self):
        """
        Verifies that a company creation event is parsed, including boolean conversion.
        """
        log_line = "2024-07-08T10:02:00Z hostname tag filename [2025.03.22-08.13.07] Company added. Name=MegaCorp(Corp?true) Owner=CEO(99)"
        expected_timestamp = datetime.fromisoformat("2025-03-22T08:13:07Z").replace(tzinfo=ZoneInfo('UTC'))

        _log, result = parse_log_line(log_line)

        self.assertIsInstance(result, CompanyAddedLogEvent)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.company_name, "MegaCorp")
        self.assertTrue(result.is_corp)
        self.assertEqual(result.owner_name, "CEO")
        self.assertEqual(result.owner_id, 99)

    async def test_parse_entered_vehicle(self):
        """
        Verifies that a vehicle entered event is parsed
        """
        log_line = "2024-07-08T10:02:00Z hostname tag filename [2025.03.22-08.13.07] Player entered vehicle. Player=Dr-P (76561198129501840) Vehicle=Atlas 6x4 Semi(854460) "
        expected_timestamp = datetime.fromisoformat("2025-03-22T08:13:07Z").replace(tzinfo=ZoneInfo('UTC'))

        _log, result = parse_log_line(log_line)

        self.assertIsInstance(result, PlayerEnteredVehicleLogEvent)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.player_name, "Dr-P")
        self.assertEqual(result.player_id, 76561198129501840)
        self.assertEqual(result.vehicle_name, "Atlas 6x4 Semi")
        self.assertEqual(result.vehicle_id, 854460)

    async def test_parse_generic_announcement(self):
        """
        Verifies that a generic chat message is correctly identified as an Announcement.
        This test is important to ensure the order of regex patterns is working correctly.
        """
        log_line = "2024-07-08T10:03:00Z hostname tag filename [2025.03.22-08.13.07] [CHAT] Server is restarting in 5 minutes."
        expected_timestamp = datetime.fromisoformat("2025-03-22T08:13:07Z").replace(tzinfo=ZoneInfo('UTC'))

        _log, result = parse_log_line(log_line)

        self.assertIsInstance(result, AnnouncementLogEvent)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.message, "Server is restarting in 5 minutes.")

    async def test_unknown_log_entry(self):
        """
        Verifies that an un-parsable log line returns an UnknownLogEntry.
        """
        original_content = "This is a weird and unexpected log format."
        log_line = f"2024-07-08T10:04:00Z hostname tag filename [2025.03.22-08.13.07] {original_content}"
        expected_timestamp = datetime.fromisoformat("2025-03-22T08:13:07Z").replace(tzinfo=ZoneInfo('UTC'))

        _log, result = parse_log_line(log_line)

        self.assertIsInstance(result, UnknownLogEntry)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.original_line, original_content)

    async def test_malformed_line_prefix(self):
        """
        Verifies that a line without the expected timestamp prefix is handled gracefully.
        """
        log_line = "Just some junk data without a timestamp"

        _log, result = parse_log_line(log_line)

        self.assertIsInstance(result, UnknownLogEntry)
        self.assertEqual(result.original_line, log_line)
        # The timestamp will be timezone.now(), so we just check it exists
        self.assertIsInstance(result.timestamp, datetime)


class ProcessLogEventTestCase(TestCase):
  def setUp(self):
    self.server_log = ServerLog.objects.create(
      timestamp=timezone.now(),
      log_path="path",
      text="test",
    )
    self.player = Player.objects.create(
      unique_id=1234,
    )
    self.character = Character.objects.create(
      name="test",
      player=self.player,
    )

  async def test_player_chat_message(self):
    event = PlayerChatMessageLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=1234,
      player_name='freeman',
      message='test'
    )
    await process_log_event(event)
    self.assertTrue(
      await PlayerChatLog.objects.filter(
        character__name=event.player_name,
        character__player__unique_id=event.player_id,
        text=event.message
      ).aexists()
    )

  async def test_player_bot_invocation(self):
    event = PlayerChatMessageLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=1234,
      player_name='freeman',
      message='/bot test'
    )
    await process_log_event(event)
    self.assertTrue(
      await PlayerChatLog.objects.filter(
        character__name=event.player_name,
        character__player__unique_id=event.player_id,
        text=event.message
      ).aexists()
    )
    self.assertTrue(
      await BotInvocationLog.objects.filter(
        character__name=event.player_name,
        character__player__unique_id=event.player_id,
        prompt="test"
      ).aexists()
    )

  async def test_player_song_request(self):
    event = PlayerChatMessageLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=1234,
      player_name='freeman',
      message='/song_request test'
    )
    await process_log_event(event)
    self.assertTrue(
      await PlayerChatLog.objects.filter(
        character__name=event.player_name,
        character__player__unique_id=event.player_id,
        text=event.message
      ).aexists()
    )
    self.assertTrue(
      await SongRequestLog.objects.filter(
        character__name=event.player_name,
        character__player__unique_id=event.player_id,
        song="test"
      ).aexists()
    )

  async def test_player_entered_vehicle(self):
    event = PlayerEnteredVehicleLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=1234,
      player_name='freeman',
      vehicle_id=2345,
      vehicle_name='Dabo',
    )
    await process_log_event(event)
    self.assertTrue(
      await PlayerVehicleLog.objects.filter(
        character__name=event.player_name,
        character__player__unique_id=event.player_id,
        vehicle_name=event.vehicle_name,
        vehicle_game_id=event.vehicle_id,
        action=PlayerVehicleLog.Action.ENTERED
      ).aexists()
    )

  async def test_player_exited_vehicle(self):
    event = PlayerExitedVehicleLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=1234,
      player_name='freeman',
      vehicle_id=2345,
      vehicle_name='Dabo',
    )
    await process_log_event(event)
    self.assertTrue(
      await PlayerVehicleLog.objects.filter(
        character__name=event.player_name,
        character__player__unique_id=event.player_id,
        vehicle_name=event.vehicle_name,
        vehicle_game_id=event.vehicle_id,
        action=PlayerVehicleLog.Action.EXITED
      ).aexists()
    )

  async def test_player_login(self):
    event = PlayerLoginLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=1234,
      player_name='freeman',
    )
    await process_log_event(event)
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character__name=event.player_name,
        character__player__unique_id=event.player_id,
        timespan=(event.timestamp, None)
      ).aexists()
    )

  async def test_player_login_out_of_order_1(self):
    await PlayerStatusLog.objects.acreate(
      character=self.character,
      timespan=(self.server_log.timestamp - timedelta(hours=1), None)
    )

    event = PlayerLoginLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=self.player.unique_id,
      player_name=self.character.name,
    )
    await process_log_event(event)
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(event.timestamp - timedelta(hours=1), None)
      ).aexists(),
      "original log stays the same"
    )
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan__startswith=event.timestamp,
        timespan__upper_inf=True
      ).aexists(),
      "new log with new login time"
    )

  async def test_player_login_out_of_order_2(self):
    await PlayerStatusLog.objects.acreate(
      character=self.character,
      timespan=(self.server_log.timestamp - timedelta(hours=1), self.server_log.timestamp + timedelta(hours=1))
    )

    event = PlayerLoginLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=self.player.unique_id,
      player_name=self.character.name,
    )
    await process_log_event(event)
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(event.timestamp, self.server_log.timestamp + timedelta(hours=1))
      ).aexists()
    )
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(self.server_log.timestamp - timedelta(hours=1), None)
      ).aexists()
    )

  async def test_player_logout(self):
    event = PlayerLogoutLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=self.player.unique_id,
      player_name=self.character.name,
    )

    # Use DjangoModelFactory
    await PlayerStatusLog.objects.acreate(
      character=self.character,
      timespan=(event.timestamp - timedelta(hours=1), None)
    )

    await process_log_event(event)
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(event.timestamp - timedelta(hours=1), event.timestamp),
      ).aexists()
    )
    self.assertEqual(
      await PlayerStatusLog.objects.acount(),
      1
    )

  async def test_player_logout_out_of_step_between(self):
    event = PlayerLogoutLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=self.player.unique_id,
      player_name=self.character.name,
    )

    # Use DjangoModelFactory
    await PlayerStatusLog.objects.acreate(
      character=self.character,
      timespan=(event.timestamp - timedelta(hours=1), event.timestamp + timedelta(hours=1))
    )

    await process_log_event(event)
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(event.timestamp - timedelta(hours=1), event.timestamp),
      ).aexists()
    )
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(None, event.timestamp + timedelta(hours=1)),
      ).aexists()
    )
    self.assertEqual(
      await PlayerStatusLog.objects.acount(),
      2
    )

  async def test_player_logout_out_of_step_after(self):
    event = PlayerLogoutLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=self.player.unique_id,
      player_name=self.character.name,
    )

    # Use DjangoModelFactory
    await PlayerStatusLog.objects.acreate(
      character=self.character,
      timespan=(None, event.timestamp - timedelta(hours=1))
    )

    await process_log_event(event)
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(None, event.timestamp),
      ).aexists()
    )
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(None, event.timestamp - timedelta(hours=1)),
      ).aexists()
    )
    self.assertEqual(
      await PlayerStatusLog.objects.acount(),
      2
    )

  async def test_player_logout_out_of_step_multi(self):
    event = PlayerLogoutLogEvent(
      timestamp=self.server_log.timestamp,
      player_id=self.player.unique_id,
      player_name=self.character.name,
    )

    # Use DjangoModelFactory
    await PlayerStatusLog.objects.acreate(
      character=self.character,
      timespan=(event.timestamp - timedelta(hours=1), None)
    )
    await PlayerStatusLog.objects.acreate(
      character=self.character,
      timespan=(event.timestamp - timedelta(hours=2), None)
    )

    await process_log_event(event)
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(event.timestamp - timedelta(hours=1), event.timestamp),
      ).aexists()
    )
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(event.timestamp - timedelta(hours=2), None),
      ).aexists()
    )
    self.assertEqual(
      await PlayerStatusLog.objects.acount(),
      2
    )

  async def test_player_logout_legacy(self):
    event = LegacyPlayerLogoutLogEvent(
      timestamp=self.server_log.timestamp,
      player_name=self.character.name,
    )

    # Use DjangoModelFactory
    await PlayerStatusLog.objects.acreate(
      character=self.character,
      timespan=(event.timestamp - timedelta(hours=1), None)
    )

    await process_log_event(event)
    self.assertEqual(
      await PlayerStatusLog.objects.acount(),
      1
    )
    self.assertTrue(
      await PlayerStatusLog.objects.filter(
        character=self.character,
        timespan=(event.timestamp - timedelta(hours=1), event.timestamp),
      ).aexists()
    )

  async def test_company_added(self):
    event = CompanyAddedLogEvent(
      timestamp=self.server_log.timestamp,
      company_name='ASEAN',
      is_corp=True,
      owner_id=1234,
      owner_name='freeman',
    )
    await process_log_event(event)
    self.assertTrue(
      await Company.objects.filter(
        owner__name=event.owner_name,
        owner__player__unique_id=event.owner_id,
        name=event.company_name,
        first_seen_at=event.timestamp,
        is_corp=event.is_corp,
      ).aexists()
    )

  async def test_player_restocked_depot(self):
    event = PlayerRestockedDepotLogEvent(
      timestamp=self.server_log.timestamp,
      player_name=self.character.name,
      depot_name="test",
    )
    await process_log_event(event)
    self.assertTrue(
      await PlayerRestockDepotLog.objects.filter(
        character=self.character,
        depot_name=event.depot_name,
      ).aexists()
    )

  async def test_player_level_changed(self):
    event = PlayerLevelChangedLogEvent(
      timestamp=self.server_log.timestamp,
      player_name=self.character.name,
      player_id=self.player.unique_id,
      level_type='CL_Driver',
      level_value=2,
    )
    await process_log_event(event)
    self.assertTrue(
      await Character.objects.filter(
        id=self.character.id,
        driver_level=event.level_value
      ).aexists()
    )

