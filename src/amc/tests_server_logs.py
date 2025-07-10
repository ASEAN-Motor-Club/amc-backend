from datetime import datetime
from django.test import SimpleTestCase, TestCase
from amc.server_logs import (
    parse_log_line,
    PlayerChatMessageLogEvent,
    PlayerLoginLogEvent,
    PlayerEnteredVehicleLogEvent,
    CompanyAddedLogEvent,
    AnnouncementLogEvent,
    UnknownLogEntry,
)
from amc.tasks import process_log_event
from amc.models import (
  PlayerChatLog,
  PlayerVehicleLog,
)

class LogParserTestCase(SimpleTestCase):
    """
    Test suite for the log parsing logic.
    """

    async def test_parse_player_chat_message(self):
        """
        Verifies that a standard player chat message is parsed correctly.
        """
        log_line = "2024-07-08T10:00:00.123Z hostname tag game_ts [CHAT] TestPlayer (123): Hello world!"
        expected_timestamp = datetime.fromisoformat("2024-07-08T10:00:00.123Z")

        # Await the async function call
        result = parse_log_line(log_line)

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
        log_line = "2024-07-08T10:01:00Z hostname tag game_ts Player Login: Admin (1)"
        expected_timestamp = datetime.fromisoformat("2024-07-08T10:01:00Z")

        result = parse_log_line(log_line)

        self.assertIsInstance(result, PlayerLoginLogEvent)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.player_name, "Admin")
        self.assertEqual(result.player_id, 1)

    async def test_parse_company_added(self):
        """
        Verifies that a company creation event is parsed, including boolean conversion.
        """
        log_line = "2024-07-08T10:02:00Z hostname tag game_ts Company added. Name=MegaCorp(Corp?true) Owner=CEO(99)"
        expected_timestamp = datetime.fromisoformat("2024-07-08T10:02:00Z")

        result = parse_log_line(log_line)

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
        log_line = "2024-07-08T10:02:00Z hostname tag game_ts Player entered vehicle. Player=freeman (123) Vehicle=Dabo(1233)"
        expected_timestamp = datetime.fromisoformat("2024-07-08T10:02:00Z")

        result = parse_log_line(log_line)

        self.assertIsInstance(result, PlayerEnteredVehicleLogEvent)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.player_name, "freeman")
        self.assertEqual(result.player_id, 123)
        self.assertEqual(result.vehicle_name, "Dabo")
        self.assertEqual(result.vehicle_id, 1233)

    async def test_parse_generic_announcement(self):
        """
        Verifies that a generic chat message is correctly identified as an Announcement.
        This test is important to ensure the order of regex patterns is working correctly.
        """
        log_line = "2024-07-08T10:03:00Z hostname tag game_ts [CHAT] Server is restarting in 5 minutes."
        expected_timestamp = datetime.fromisoformat("2024-07-08T10:03:00Z")

        result = parse_log_line(log_line)

        self.assertIsInstance(result, AnnouncementLogEvent)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.message, "Server is restarting in 5 minutes.")

    async def test_unknown_log_entry(self):
        """
        Verifies that an un-parsable log line returns an UnknownLogEntry.
        """
        original_content = "This is a weird and unexpected log format."
        log_line = f"2024-07-08T10:04:00Z hostname tag game_ts {original_content}"
        expected_timestamp = datetime.fromisoformat("2024-07-08T10:04:00Z")

        result = parse_log_line(log_line)

        self.assertIsInstance(result, UnknownLogEntry)
        self.assertEqual(result.timestamp, expected_timestamp)
        self.assertEqual(result.original_line, original_content)

    async def test_malformed_line_prefix(self):
        """
        Verifies that a line without the expected timestamp prefix is handled gracefully.
        """
        log_line = "Just some junk data without a timestamp"

        result = parse_log_line(log_line)

        self.assertIsInstance(result, UnknownLogEntry)
        self.assertEqual(result.original_line, log_line)
        # The timestamp will be datetime.now(), so we just check it exists
        self.assertIsInstance(result.timestamp, datetime)


class ProcessLogEventTestCase(TestCase):
  async def test_player_chat_message(self):
    event = PlayerChatMessageLogEvent(
      timestamp=datetime.now(),
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

  async def test_player_entered_vehicle(self):
    event = PlayerEnteredVehicleLogEvent(
      timestamp=datetime.now(),
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
        vehicle__name=event.vehicle_name,
        vehicle__id=event.vehicle_id,
        action=PlayerVehicleLog.Action.ENTERED
      ).aexists()
    )

