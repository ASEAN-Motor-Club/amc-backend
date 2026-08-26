from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from amc.models import Character, Player
from amc.tasks import (
    _resolve_guid,
    _resolve_guid_for_login,
    _resolve_guid_from_game_server,
    _spawn_with_retry,
    aget_or_create_character,
    get_welcome_message,
    spawn_restarting_dealerships,
    spawn_restarting_garages,
)


class LoginWelcomeUsesForcedNameTests(TestCase):
    """The 'Welcome back' greeting must use the forced/renamed name, not the raw one."""

    async def test_welcome_back_uses_forced_name(self):
        from unittest.mock import patch

        import amc.tasks as tasks_module
        from amc.server_logs import PlayerLoginLogEvent

        player = await Player.objects.acreate(unique_id=555001)
        character_name = "RawChosenName"
        await Character.objects.acreate(
            name=character_name, player=player, guid="welcome_guid"
        )
        # Force a rename (the LLM/admin forced_name lock).
        await Player.objects.filter(unique_id=player.unique_id).aupdate(
            forced_name="RenamedDriver"
        )
        # > 1h since last seen (and < 7d) → triggers "Welcome back".
        await Character.objects.filter(player=player).aupdate(
            last_online=timezone.now() - timedelta(hours=5)
        )

        event = PlayerLoginLogEvent(
            timestamp=timezone.now(),
            player_id=player.unique_id,
            player_name=character_name,
        )
        greeted_with = []

        def fake_get_welcome_message(name, is_new, last_online=None):
            greeted_with.append(name)
            return f"Welcome back {name}!", False

        # Patch get_welcome_message (called synchronously in the login path) to
        # capture which name is used for the "Welcome back" greet.
        with patch.object(tasks_module, "get_welcome_message", new=fake_get_welcome_message):
            await tasks_module.process_log_event(
                event,
                ctx={
                    "http_client": None,
                    "http_client_mod": None,
                    "startup_time": timezone.now() - timedelta(hours=1),
                },
            )

        self.assertTrue(greeted_with, "a 'Welcome back' greet must fire")
        self.assertEqual(greeted_with[0], "RenamedDriver")

    async def test_no_forced_name_uses_chosen_name(self):
        """Without a forced name, the welcome keeps the player's chosen name."""
        from unittest.mock import patch

        import amc.tasks as tasks_module
        from amc.server_logs import PlayerLoginLogEvent

        player = await Player.objects.acreate(unique_id=555002)
        await Character.objects.acreate(
            name="PlainJoe", player=player, guid="welcome_guid2"
        )
        event = PlayerLoginLogEvent(
            timestamp=timezone.now(),
            player_id=player.unique_id,
            player_name="PlainJoe",
        )
        greeted_with = []

        def fake_get_welcome_message(name, is_new, last_online=None):
            greeted_with.append(name)
            return f"Welcome back {name}!", False

        with patch.object(tasks_module, "get_welcome_message", new=fake_get_welcome_message):
            await tasks_module.process_log_event(
                event,
                ctx={
                    "http_client": None,
                    "http_client_mod": None,
                    "startup_time": timezone.now() - timedelta(hours=1),
                },
            )

        self.assertTrue(greeted_with, "a 'Welcome back' greet must fire")
        self.assertEqual(greeted_with[0], "PlainJoe")


class GetWelcomeMessageTests(SimpleTestCase):
    def test_new_player(self):
        """is_new=True → new player greeting."""
        message, is_new = get_welcome_message("TestPlayer", is_new=True)
        self.assertTrue(is_new)
        self.assertIn("Welcome TestPlayer", message)
        self.assertIn("/help", message)

    def test_new_player_ignores_last_online(self):
        """is_new=True takes priority even if last_online is set."""
        last_online = timezone.now() - timedelta(hours=5)
        message, is_new = get_welcome_message(
            "TestPlayer", is_new=True, last_online=last_online
        )
        self.assertTrue(is_new)
        self.assertIn("/help", message)

    def test_existing_player_no_last_online(self):
        """Existing player with last_online=None → generic 'Welcome back'."""
        message, is_new = get_welcome_message("TestPlayer", is_new=False)
        self.assertEqual(message, "Welcome back TestPlayer!")
        self.assertFalse(is_new)

    def test_recent_login_under_1_hour(self):
        """last_online < 1 hour ago → no greeting (returns None)."""
        last_online = timezone.now() - timedelta(minutes=30)
        message, is_new = get_welcome_message(
            "TestPlayer", is_new=False, last_online=last_online
        )
        self.assertIsNone(message)
        self.assertFalse(is_new)

    def test_returning_player_over_1_hour(self):
        """last_online > 1 hour but < 7 days → 'Welcome back'."""
        last_online = timezone.now() - timedelta(hours=5)
        message, is_new = get_welcome_message(
            "TestPlayer", is_new=False, last_online=last_online
        )
        self.assertEqual(message, "Welcome back TestPlayer!")
        self.assertFalse(is_new)

    def test_long_absence_over_7_days(self):
        """last_online > 7 days → 'Long time no see'."""
        last_online = timezone.now() - timedelta(days=10)
        message, is_new = get_welcome_message(
            "TestPlayer", is_new=False, last_online=last_online
        )
        self.assertEqual(message, "Long time no see! Welcome back TestPlayer")
        self.assertFalse(is_new)

    def test_total_seconds_not_seconds(self):
        """Regression: 8 days ago must use total_seconds, not .seconds.

        timedelta(days=8, hours=2).seconds == 7200 (ignores days!),
        but .total_seconds() == 698400. The old code would wrongly
        return 'Welcome back' instead of 'Long time no see'.
        """
        last_online = timezone.now() - timedelta(days=8, hours=2)
        message, _ = get_welcome_message(
            "TestPlayer", is_new=False, last_online=last_online
        )
        self.assertEqual(message, "Long time no see! Welcome back TestPlayer")

    def test_just_over_1_hour(self):
        """Just over 1 hour returns 'Welcome back'."""
        last_online = timezone.now() - timedelta(hours=1, seconds=1)
        message, is_new = get_welcome_message(
            "TestPlayer", is_new=False, last_online=last_online
        )
        self.assertEqual(message, "Welcome back TestPlayer!")
        self.assertFalse(is_new)


# ---------------------------------------------------------------------------
# GUID resolution tests
# ---------------------------------------------------------------------------


VALID_GUID = "AAAA1111BBBB2222CCCC3333DDDD4444"
PLAYER_ID = 999_000_001


def _make_game_players(player_id, guid):
    """Build the list-of-tuples format returned by game_server.get_players()."""
    return [
        (player_id, {"unique_id": player_id, "character_guid": guid, "name": "Test"}),
    ]


def _make_player_info(player_id, guid, name="Test", is_admin=False):
    """Build the normalized dict returned by game_server.get_player_info()."""
    return {
        "CharacterGuid": guid.upper(),
        "PlayerName": name,
        "Location": None,
        "VehicleKey": "None",
        "bIsAdmin": is_admin,
        "unique_id": player_id,
    }


class ResolveGuidFromGameServerTests(SimpleTestCase):
    """Unit tests for _resolve_guid_from_game_server — no DB needed."""

    async def test_returns_guid_when_player_found(self):
        """Returns the GUID when the player is in the game server list."""
        mock_http = AsyncMock()
        players = _make_game_players(PLAYER_ID, VALID_GUID)
        with patch("amc.tasks.get_players", AsyncMock(return_value=players)):
            result = await _resolve_guid_from_game_server(mock_http, PLAYER_ID)
        self.assertEqual(result, VALID_GUID)

    async def test_returns_none_when_player_not_found(self):
        """Returns None when the player_id is not in the list."""
        mock_http = AsyncMock()
        players = _make_game_players(12345, VALID_GUID)   # different player_id
        with patch("amc.tasks.get_players", AsyncMock(return_value=players)):
            result = await _resolve_guid_from_game_server(mock_http, PLAYER_ID)
        self.assertIsNone(result)

    async def test_filters_invalid_guid(self):
        """All-zeros INVALID_GUID is treated as absent."""
        mock_http = AsyncMock()
        players = _make_game_players(PLAYER_ID, Character.INVALID_GUID)
        with patch("amc.tasks.get_players", AsyncMock(return_value=players)):
            result = await _resolve_guid_from_game_server(mock_http, PLAYER_ID)
        self.assertIsNone(result)

    async def test_returns_none_when_player_list_empty(self):
        """Returns None when the game server returns an empty list."""
        mock_http = AsyncMock()
        with patch("amc.tasks.get_players", AsyncMock(return_value=[])):
            result = await _resolve_guid_from_game_server(mock_http, PLAYER_ID)
        self.assertIsNone(result)

    async def test_matches_by_string_comparison(self):
        """player_id comparison is string-safe (int vs str)."""
        mock_http = AsyncMock()
        players = [(str(PLAYER_ID), {"unique_id": str(PLAYER_ID), "character_guid": VALID_GUID, "name": "Test"})]
        with patch("amc.tasks.get_players", AsyncMock(return_value=players)):
            result = await _resolve_guid_from_game_server(mock_http, PLAYER_ID)
        self.assertEqual(result, VALID_GUID)

    async def test_normalizes_lowercase_guid_to_uppercase(self):
        """GUIDs from the native game API may be lowercase; they must be uppercased."""
        mock_http = AsyncMock()
        players = _make_game_players(PLAYER_ID, VALID_GUID.lower())
        with patch("amc.tasks.get_players", AsyncMock(return_value=players)):
            result = await _resolve_guid_from_game_server(mock_http, PLAYER_ID)
        self.assertEqual(result, VALID_GUID)  # always uppercase


class AgetOrCreateCharacterFallbackTests(TestCase):
    """Integration tests for aget_or_create_character with game server fallback."""

    async def test_game_server_guid_used_when_available(self):
        """When game server returns a good GUID, it is used for character creation."""
        game_players = _make_game_players(PLAYER_ID, VALID_GUID)
        player_info = _make_player_info(PLAYER_ID, VALID_GUID, name="TestPlayer")

        with patch("amc.tasks.get_players", AsyncMock(return_value=game_players)):
            with patch("amc.tasks.get_player_info", AsyncMock(return_value=player_info)):
                character, player, created, returned_info = await aget_or_create_character(
                    "TestPlayer", PLAYER_ID, http_client_mod=AsyncMock(), http_client=AsyncMock()
                )

        self.assertEqual(character.guid, VALID_GUID)

    async def test_game_server_guid_normalized_to_uppercase(self):
        """GUIDs from the native game API (lowercase) are uppercased before storage."""
        lowercase_guid = VALID_GUID.lower()
        game_players = _make_game_players(PLAYER_ID, lowercase_guid)
        player_info = _make_player_info(PLAYER_ID, lowercase_guid, name="TestPlayer")

        with patch("amc.tasks.get_players", AsyncMock(return_value=game_players)):
            with patch("amc.tasks.get_player_info", AsyncMock(return_value=player_info)):
                character, player, created, returned_info = await aget_or_create_character(
                    "TestPlayer", PLAYER_ID, http_client_mod=AsyncMock(), http_client=AsyncMock()
                )

        self.assertEqual(character.guid, VALID_GUID)  # stored as uppercase

    async def test_falls_back_to_mod_server_when_game_server_empty(self):
        """Falls back to mod server when game server returns no players."""
        mod_player_info = {"CharacterGuid": VALID_GUID, "PlayerName": "Test"}

        with patch("amc.tasks.get_players", AsyncMock(return_value=[])):
            with patch("amc.tasks.get_player_info", AsyncMock(return_value=None)):
                with patch("amc.tasks.get_player", AsyncMock(return_value=mod_player_info)):
                    character, player, created, returned_info = await aget_or_create_character(
                        "TestPlayer", PLAYER_ID, http_client_mod=AsyncMock(), http_client=AsyncMock()
                    )

        self.assertEqual(character.guid, VALID_GUID)

    async def test_falls_back_to_mod_when_game_server_returns_invalid_guid(self):
        """Falls back to mod server when game server returns INVALID_GUID."""
        game_players = _make_game_players(PLAYER_ID, Character.INVALID_GUID)
        mod_player_info = {"CharacterGuid": VALID_GUID, "PlayerName": "Test"}

        with patch("amc.tasks.get_players", AsyncMock(return_value=game_players)):
            with patch("amc.tasks.get_player_info", AsyncMock(return_value=None)):
                with patch("amc.tasks.get_player", AsyncMock(return_value=mod_player_info)):
                    character, player, created, returned_info = await aget_or_create_character(
                        "TestPlayer", PLAYER_ID, http_client_mod=AsyncMock(), http_client=AsyncMock()
                    )

        self.assertEqual(character.guid, VALID_GUID)

    async def test_falls_back_to_mod_when_game_server_player_not_found(self):
        """Falls back to mod server when the player is not in the game server list."""
        game_players = _make_game_players(99999, VALID_GUID)  # different player_id
        mod_player_info = {"CharacterGuid": VALID_GUID, "PlayerName": "Test"}

        with patch("amc.tasks.get_players", AsyncMock(return_value=game_players)):
            with patch("amc.tasks.get_player_info", AsyncMock(return_value=None)):
                with patch("amc.tasks.get_player", AsyncMock(return_value=mod_player_info)):
                    character, player, created, returned_info = await aget_or_create_character(
                        "TestPlayer", PLAYER_ID, http_client_mod=AsyncMock(), http_client=AsyncMock()
                    )

        self.assertEqual(character.guid, VALID_GUID)

    async def test_falls_back_to_mod_when_game_server_raises(self):
        """Falls back to mod server when game server raises an exception."""
        mod_player_info = {"CharacterGuid": VALID_GUID, "PlayerName": "Test"}

        with patch("amc.tasks.get_players", AsyncMock(side_effect=Exception("timeout"))):
            with patch("amc.tasks.get_player_info", AsyncMock(side_effect=Exception("timeout"))):
                with patch("amc.tasks.get_player", AsyncMock(return_value=mod_player_info)):
                    character, player, created, returned_info = await aget_or_create_character(
                        "TestPlayer", PLAYER_ID, http_client_mod=AsyncMock(), http_client=AsyncMock()
                    )

        self.assertEqual(character.guid, VALID_GUID)

    async def test_returns_none_when_both_fail(self):
        """When both APIs fail to return a GUID, no character is created."""
        with patch("amc.tasks.get_player_info", AsyncMock(return_value=None)):
            with patch("amc.tasks.get_player", AsyncMock(return_value=None)):
                with patch("amc.tasks.get_players", AsyncMock(return_value=[])):
                    character, player, created, returned_info = await aget_or_create_character(
                        "TestPlayer", PLAYER_ID, http_client_mod=AsyncMock(), http_client=AsyncMock()
                    )

        self.assertIsNone(character)
        self.assertFalse(created)

    async def test_no_http_client_returns_none(self):
        """aget_or_create_character returns no character when no http clients are available."""
        character, player, created, player_info = await aget_or_create_character(
            "TestPlayer", PLAYER_ID
        )
        self.assertIsNone(character)
        self.assertFalse(created)
        self.assertIsNone(player_info)


class ResolveGuidRetryTests(SimpleTestCase):
    """Unit tests for _resolve_guid — game server tried first, then mod retry loop."""

    async def test_game_server_wins_before_mod_retry(self):
        """When game server has the GUID, mod server retry loop is never entered."""
        game_players = _make_game_players(PLAYER_ID, VALID_GUID)

        with patch("amc.tasks.get_players", AsyncMock(return_value=game_players)):
            with patch("amc.tasks.get_player", AsyncMock()) as mock_mod:
                guid, player_info = await _resolve_guid(
                    http_client_mod=AsyncMock(),
                    player_id=PLAYER_ID,
                    player_name="Test",
                    http_client=AsyncMock(),
                )

        self.assertEqual(guid, VALID_GUID)
        self.assertIsNone(player_info)   # game server path returns (guid, None)
        mock_mod.assert_not_called()

    async def test_falls_back_to_mod_when_game_server_empty(self):
        """Falls through to mod server retry loop when game server returns nothing."""
        mod_player_info = {"CharacterGuid": VALID_GUID}

        with patch("amc.tasks.get_players", AsyncMock(return_value=[])):
            with patch("amc.tasks.get_player", AsyncMock(return_value=mod_player_info)):
                guid, player_info = await _resolve_guid(
                    http_client_mod=AsyncMock(),
                    player_id=PLAYER_ID,
                    player_name="Test",
                    http_client=AsyncMock(),
                    max_attempts=1,
                )

        self.assertEqual(guid, VALID_GUID)
        self.assertIsNotNone(player_info)

    async def test_returns_none_when_all_fail(self):
        """Returns (None, None) after exhausting all attempts."""
        with patch("amc.tasks.get_players", AsyncMock(return_value=[])):
            with patch("amc.tasks.get_player", AsyncMock(return_value=None)):
                guid, player_info = await _resolve_guid(
                    http_client_mod=AsyncMock(),
                    player_id=PLAYER_ID,
                    player_name="Test",
                    http_client=AsyncMock(),
                    max_attempts=1,
                )

        self.assertIsNone(guid)
        self.assertIsNone(player_info)

    async def test_no_http_client_goes_straight_to_mod(self):
        """When http_client is None, skips game server and goes to mod retry loop."""
        mod_player_info = {"CharacterGuid": VALID_GUID}

        with patch("amc.tasks.get_players", AsyncMock()) as mock_game:
            with patch("amc.tasks.get_player", AsyncMock(return_value=mod_player_info)):
                guid, _ = await _resolve_guid(
                    http_client_mod=AsyncMock(),
                    player_id=PLAYER_ID,
                    player_name="Test",
                    http_client=None,
                    max_attempts=1,
                )

        mock_game.assert_not_called()
        self.assertEqual(guid, VALID_GUID)


class ResolveGuidForLoginTests(SimpleTestCase):
    """Tests for _resolve_guid_for_login — login-specific retry with cache busting."""

    async def test_cache_bust_on_first_attempt(self):
        """First attempt should use force_refresh=True to bypass cache."""
        mock_http = AsyncMock()

        with patch(
            "amc.tasks._resolve_guid_from_game_server",
            AsyncMock(return_value=VALID_GUID),
        ) as mock_resolve:
            guid, player_info = await _resolve_guid_for_login(
                http_client=mock_http,
                http_client_mod=AsyncMock(),
                player_id=PLAYER_ID,
                player_name="Test",
            )

        self.assertEqual(guid, VALID_GUID)
        self.assertIsNone(player_info)
        mock_resolve.assert_called_once_with(mock_http, PLAYER_ID, force_refresh=True)

    async def test_retries_on_failure_then_succeeds(self):
        """Retries game + mod server until GUID is found."""
        with patch(
            "amc.tasks._resolve_guid_from_game_server",
            AsyncMock(return_value=None),
        ):
            with patch("amc.tasks.get_player", AsyncMock(return_value={"CharacterGuid": VALID_GUID})):
                with patch("amc.tasks.asyncio.sleep", new_callable=AsyncMock):
                    guid, player_info = await _resolve_guid_for_login(
                        http_client=AsyncMock(),
                        http_client_mod=AsyncMock(),
                        player_id=PLAYER_ID,
                        player_name="Test",
                        max_attempts=3,
                    )

        self.assertEqual(guid, VALID_GUID)
        self.assertIsNotNone(player_info)

    async def test_returns_none_when_all_retries_exhausted(self):
        """Returns (None, None) after all retries fail."""
        with patch(
            "amc.tasks._resolve_guid_from_game_server",
            AsyncMock(return_value=None),
        ):
            with patch("amc.tasks.get_player", AsyncMock(return_value=None)):
                with patch("amc.tasks.asyncio.sleep", new_callable=AsyncMock):
                    guid, player_info = await _resolve_guid_for_login(
                        http_client=AsyncMock(),
                        http_client_mod=AsyncMock(),
                        player_id=PLAYER_ID,
                        player_name="Test",
                        max_attempts=2,
                    )

        self.assertIsNone(guid)
        self.assertIsNone(player_info)

    async def test_no_http_client_skips_game_server(self):
        """When http_client is None, skips game server and goes to mod retry."""
        with patch("amc.tasks.get_player", AsyncMock(return_value={"CharacterGuid": VALID_GUID})):
            with patch("amc.tasks.asyncio.sleep", new_callable=AsyncMock):
                guid, player_info = await _resolve_guid_for_login(
                    http_client=None,
                    http_client_mod=AsyncMock(),
                    player_id=PLAYER_ID,
                    player_name="Test",
                    max_attempts=1,
                )

        self.assertEqual(guid, VALID_GUID)

    async def test_game_server_succeeds_on_cache_busted_attempt(self):
        """If the cache-busted game server call finds the player, return immediately."""
        mock_http = AsyncMock()
        with patch(
            "amc.tasks._resolve_guid_from_game_server",
            AsyncMock(return_value=VALID_GUID),
        ) as mock_resolve:
            guid, player_info = await _resolve_guid_for_login(
                http_client=mock_http,
                http_client_mod=AsyncMock(),
                player_id=PLAYER_ID,
                player_name="Test",
            )

        self.assertEqual(guid, VALID_GUID)
        mock_resolve.assert_called_once_with(mock_http, PLAYER_ID, force_refresh=True)


class ResolveGuidFromGameServerForceRefreshTests(SimpleTestCase):
    """Tests for _resolve_guid_from_game_server force_refresh parameter."""

    async def test_force_refresh_passed_to_get_players(self):
        """force_refresh=True is forwarded to get_players."""
        mock_http = AsyncMock()
        players = _make_game_players(PLAYER_ID, VALID_GUID)

        with patch("amc.tasks.get_players", AsyncMock(return_value=players)) as mock_get:
            result = await _resolve_guid_from_game_server(
                mock_http, PLAYER_ID, force_refresh=True
            )

        self.assertEqual(result, VALID_GUID)
        mock_get.assert_called_once_with(mock_http, force_refresh=True)

    async def test_default_no_force_refresh(self):
        """Default force_refresh=False is forwarded to get_players."""
        mock_http = AsyncMock()
        players = _make_game_players(PLAYER_ID, VALID_GUID)

        with patch("amc.tasks.get_players", AsyncMock(return_value=players)) as mock_get:
            result = await _resolve_guid_from_game_server(mock_http, PLAYER_ID)

        self.assertEqual(result, VALID_GUID)
        mock_get.assert_called_once_with(mock_http, force_refresh=False)


def _patch_sleep():
    """Replace asyncio.sleep inside amc.tasks with a no-op that records waits."""
    recorded: list[float] = []

    async def fake_sleep(seconds, *args, **kwargs):
        recorded.append(seconds)

    return patch("asyncio.sleep", new=fake_sleep), recorded


class SpawnRetryHelperTests(TestCase):
    async def test_returns_immediately_on_success(self):
        attempts = []

        async def make_coro():
            attempts.append(1)
            return "ok"

        result = await _spawn_with_retry(make_coro, "thing")

        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 1)

    async def test_retries_with_backoff_then_succeeds(self):
        calls = []
        patcher, waits = _patch_sleep()

        async def make_coro():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("boom")
            return "recovered"

        with patcher:
            result = await _spawn_with_retry(make_coro, "thing", base_delay=2)

        self.assertEqual(result, "recovered")
        self.assertEqual(len(calls), 3)
        # Exponential backoff between attempts.
        self.assertEqual(waits, [2, 4])

    async def test_exhausted_attempts_return_none_instead_of_raising(self):
        calls = []
        patcher, _waits = _patch_sleep()

        async def make_coro():
            calls.append(1)
            raise RuntimeError("Session is closed")

        with patcher:
            result = await _spawn_with_retry(make_coro, "thing", attempts=3)

        self.assertIsNone(result)
        self.assertEqual(len(calls), 3)

    async def test_make_coro_called_fresh_per_attempt(self):
        coros_seen = []

        def make_coro():
            async def attempt():
                raise ValueError("always fails")

            coros_seen.append(attempt)
            return attempt()

        p, _waits = _patch_sleep()
        with p:
            result = await _spawn_with_retry(make_coro, "thing", attempts=2)

        self.assertIsNone(result)
        # Each retry awaited a distinct coroutine object (cannot re-await one).
        self.assertEqual(len(coros_seen), 2)
        self.assertIsNot(coros_seen[0], coros_seen[1])


class DealershipRestartSpawnTests(TestCase):
    async def test_one_dead_dealership_does_not_abort_the_batch(self):
        from django.contrib.gis.geos import Point

        from amc.models import VehicleDealership

        dead_key = "Elisa_Police"
        ok_key = "Police_01"
        spawn_calls = []

        async def flaky_spawn(self, http_client_mod):
            spawn_calls.append(self.vehicle_key)
            if self.vehicle_key == dead_key:
                raise RuntimeError("Session is closed")

        patcher, _waits = _patch_sleep()
        with patcher, patch(
            "amc.models.VehicleDealership.spawn", new=flaky_spawn
        ):
            first = await VehicleDealership.objects.acreate(
                vehicle_key=dead_key,
                location=Point(1.0, 2.0, 3.0),
                yaw=270,
                spawn_on_restart=True,
            )
            second = await VehicleDealership.objects.acreate(
                vehicle_key=ok_key,
                location=Point(4.0, 5.0, 6.0),
                yaw=90,
                spawn_on_restart=True,
            )

            await spawn_restarting_dealerships(http_client_mod=object())

        self.assertIn(first.vehicle_key, spawn_calls)
        self.assertIn(second.vehicle_key, spawn_calls)
        # The broken dealership consumed all its attempts; the healthy one
        # was still attempted exactly once afterwards.
        self.assertEqual(spawn_calls.count(dead_key), 3)
        self.assertEqual(spawn_calls.count(ok_key), 1)

    async def test_dealership_without_restart_flag_is_skipped(self):
        from django.contrib.gis.geos import Point

        from amc.models import VehicleDealership

        async def any_spawn(self, http_client_mod):  # pragma: no cover
            raise AssertionError("must not be called")

        patcher, _waits = _patch_sleep()
        with patcher, patch(
            "amc.models.VehicleDealership.spawn", new=any_spawn
        ):
            await VehicleDealership.objects.acreate(
                vehicle_key="Elisa2_Police",
                location=Point(318628.0, 1335942.0, -20000.0),
                yaw=270,
                spawn_on_restart=False,
            )
            await spawn_restarting_dealerships(http_client_mod=object())


class GarageRestartSpawnTests(TestCase):
    async def test_garage_tag_saved_after_transient_failures(self):
        from amc.models import Garage

        garage_spawn = AsyncMock(
            side_effect=[
                ConnectionError("mod not ready"),
                ConnectionError("mod not ready"),
                {"tag": "GarageTag123"},
            ]
        )

        patcher, _waits = _patch_sleep()
        with patcher, patch("amc.tasks.spawn_garage", new=garage_spawn):
            garage = await Garage.objects.acreate(
                hostname="test-host",
                config={"Location": {"X": 1}, "Rotation": {"Yaw": 0}},
                spawn_on_restart=True,
            )
            await spawn_restarting_garages(http_client_mod=object())

        refreshed = await Garage.objects.aget(id=garage.id)
        self.assertEqual(refreshed.tag, "GarageTag123")
        self.assertEqual(garage_spawn.await_count, 3)

    async def test_garage_permanent_failure_does_not_crash_batch_or_save_tag(self):
        from amc.models import Garage

        garage_spawn = AsyncMock(side_effect=RuntimeError("Session is closed"))

        patcher, _waits = _patch_sleep()
        with patcher, patch("amc.tasks.spawn_garage", new=garage_spawn):
            garage = await Garage.objects.acreate(
                hostname="test-host",
                config={"Location": {"X": 1}, "Rotation": {"Yaw": 0}},
                spawn_on_restart=True,
            )
            # Must complete without raising despite the permanently failing
            # garage — this is the exact crash shape from the Aug 26 incident
            # where one closed session aborted the remaining restart spawns.
            await spawn_restarting_garages(http_client_mod=object())

        refreshed = await Garage.objects.aget(id=garage.id)
        self.assertIsNone(refreshed.tag)
        self.assertEqual(garage_spawn.await_count, 3)
