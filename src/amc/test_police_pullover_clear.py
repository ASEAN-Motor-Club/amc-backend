import asyncio
import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.factories import CharacterFactory, PlayerFactory
from amc.handlers.police import handle_police_penalty
from amc.models import Confiscation, Player, PoliceSession, Wanted
from amc.webhook_context import EventContext


async def _confiscation_exists(character):
    return await Confiscation.objects.filter(character=character).aexists()


def _pullover_event(officer_guid, suspect_guid, warning_only=False):
    return {
        "hook": "ServerSelectPolicePullOverPenaltyResponse",
        "timestamp": int(time.time()),
        "data": {
            "bWarningOnly": warning_only,
            "CharacterGuid": str(officer_guid),
            "SuspectCharacter": {"CharacterGuid": str(suspect_guid)},
        },
    }


async def _drain_pending_tasks():
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=2
        )


async def _cleanup_players(*players):
    # async acreate rows leak across test files — delete own rows (cascades)
    ids = [p.unique_id for p in players if p is not None]
    if ids:
        await Player.objects.filter(unique_id__in=ids).adelete()


class PulloverClearWantedTests(TestCase):
    """Pull-over penalty resolves an active Wanted even without a score."""

    async def _setup_officer_and_suspect(self):
        officer_player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(player=officer_player)
        await PoliceSession.objects.acreate(character=officer)

        suspect_player = await sync_to_async(PlayerFactory)()
        suspect = await sync_to_async(CharacterFactory)(player=suspect_player)
        return officer, suspect

    def _ctx(self):
        return EventContext(http_client=AsyncMock(), http_client_mod=AsyncMock())

    @patch("amc.handlers.police.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.handlers.police.clear_suspect", new_callable=AsyncMock)
    @patch("amc.handlers.police.perform_arrest", new_callable=AsyncMock)
    async def test_penalty_clears_wanted_without_score(
        self, mock_perform, mock_clear, mock_refresh
    ):
        officer, suspect = await self._setup_officer_and_suspect()
        try:
            wanted = await Wanted.objects.acreate(
                character=suspect,
                wanted_remaining=300,
                amount=0,
                set_by=officer,
            )

            event = _pullover_event(officer.guid, suspect.guid, warning_only=False)
            await handle_police_penalty(event, officer.player, officer, self._ctx())
            await _drain_pending_tasks()

            await wanted.arefresh_from_db(fields=["wanted_remaining", "expired_at"])
            self.assertIsNotNone(wanted.expired_at)
            self.assertEqual(wanted.wanted_remaining, 0)

            # No money moved — no confiscation for this suspect
            self.assertFalse(await _confiscation_exists(suspect))

            # Suspect overlay dropped, name tag refreshed
            mock_clear.assert_awaited_once()
            mock_refresh.assert_called_once()

            # The confiscation arrest path must NOT have run
            mock_perform.assert_not_awaited()
        finally:
            await _cleanup_players(officer.player, suspect.player)

    @patch("amc.handlers.police.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.handlers.police.clear_suspect", new_callable=AsyncMock)
    @patch("amc.handlers.police.perform_arrest", new_callable=AsyncMock)
    async def test_warning_only_keeps_wanted(
        self, mock_perform, mock_clear, mock_refresh
    ):
        officer, suspect = await self._setup_officer_and_suspect()
        try:
            wanted = await Wanted.objects.acreate(
                character=suspect,
                wanted_remaining=300,
                amount=0,
                set_by=officer,
            )

            event = _pullover_event(officer.guid, suspect.guid, warning_only=True)
            await handle_police_penalty(event, officer.player, officer, self._ctx())
            await _drain_pending_tasks()

            await wanted.arefresh_from_db(fields=["wanted_remaining", "expired_at"])
            self.assertIsNone(wanted.expired_at)
            self.assertEqual(wanted.wanted_remaining, 300)
            mock_clear.assert_not_awaited()
            mock_perform.assert_not_awaited()
        finally:
            await _cleanup_players(officer.player, suspect.player)

    @patch("amc.handlers.police.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.handlers.police.clear_suspect", new_callable=AsyncMock)
    @patch("amc.handlers.police.perform_arrest", new_callable=AsyncMock)
    async def test_no_wanted_no_score_is_noop(
        self, mock_perform, mock_clear, mock_refresh
    ):
        officer, suspect = await self._setup_officer_and_suspect()
        try:
            event = _pullover_event(officer.guid, suspect.guid, warning_only=False)
            await handle_police_penalty(event, officer.player, officer, self._ctx())
            await _drain_pending_tasks()

            mock_clear.assert_not_awaited()
            mock_perform.assert_not_awaited()
        finally:
            await _cleanup_players(officer.player, suspect.player)

    @patch("amc.handlers.police.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.handlers.police.clear_suspect", new_callable=AsyncMock)
    @patch("amc.handlers.police.get_players", new_callable=AsyncMock)
    @patch("amc.handlers.police.perform_arrest", new_callable=AsyncMock)
    async def test_score_still_runs_full_arrest(
        self, mock_perform, mock_get_players, mock_clear, mock_refresh
    ):
        officer, suspect = await self._setup_officer_and_suspect()
        try:
            suspect.criminal_score = 5000
            await suspect.asave(update_fields=["criminal_score"])
            wanted = await Wanted.objects.acreate(
                character=suspect,
                wanted_remaining=300,
                amount=100,
                set_by=officer,
            )

            event = _pullover_event(officer.guid, suspect.guid, warning_only=False)
            mock_get_players.return_value = [
                (
                    "1",
                    {
                        "unique_id": str(suspect.player.unique_id),
                        "character_guid": str(suspect.guid),
                        "location": "X=100.0 Y=200.0 Z=0.0",
                        "vehicle": "",
                    },
                )
            ]

            await handle_police_penalty(event, officer.player, officer, self._ctx())
            await _drain_pending_tasks()

            # Full arrest path, not the clear path
            mock_perform.assert_awaited_once()
            mock_clear.assert_not_awaited()

            await wanted.arefresh_from_db(fields=["expired_at"])
            self.assertIsNone(wanted.expired_at)  # execute_arrest owns the expiry
        finally:
            await _cleanup_players(officer.player, suspect.player)

    @patch("amc.handlers.police.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.handlers.police.clear_suspect", new_callable=AsyncMock)
    @patch("amc.handlers.police.perform_arrest", new_callable=AsyncMock)
    async def test_off_duty_officer_does_not_clear(
        self, mock_perform, mock_clear, mock_refresh
    ):
        officer, suspect = None, None
        try:
            officer_player = await sync_to_async(PlayerFactory)()
            officer = await sync_to_async(CharacterFactory)(player=officer_player)
            suspect_player = await sync_to_async(PlayerFactory)()
            suspect = await sync_to_async(CharacterFactory)(player=suspect_player)
            wanted = await Wanted.objects.acreate(
                character=suspect,
                wanted_remaining=300,
                amount=0,
                set_by=None,
            )

            event = _pullover_event(officer.guid, suspect.guid, warning_only=False)
            await handle_police_penalty(event, officer.player, officer, self._ctx())
            await _drain_pending_tasks()

            await wanted.arefresh_from_db(fields=["expired_at"])
            self.assertIsNone(wanted.expired_at)
            mock_clear.assert_not_awaited()
            mock_perform.assert_not_awaited()
        finally:
            await _cleanup_players(officer.player, suspect.player)


class AiPulloverArrestTests(TestCase):
    """AI-police pull-overs (caller guid == suspect guid) act like arrests."""

    async def _setup_suspect(self, criminal_score=0):
        suspect_player = await sync_to_async(PlayerFactory)()
        suspect = await sync_to_async(CharacterFactory)(player=suspect_player)
        if criminal_score:
            suspect.criminal_score = criminal_score
            await suspect.asave(update_fields=["criminal_score"])
        return suspect

    def _ctx(self):
        return EventContext(http_client=AsyncMock(), http_client_mod=AsyncMock())

    @patch("amc.handlers.police.announce", new_callable=AsyncMock)
    @patch("amc.handlers.police.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.handlers.police.clear_suspect", new_callable=AsyncMock)
    @patch("amc.handlers.police.get_players", new_callable=AsyncMock)
    @patch("amc.handlers.police.perform_arrest", new_callable=AsyncMock)
    async def test_ai_pullover_arrests_wanted_suspect(
        self, mock_perform, mock_get_players, mock_clear, mock_refresh, mock_announce
    ):
        suspect = await self._setup_suspect(criminal_score=5000)
        try:
            wanted = await Wanted.objects.acreate(
                character=suspect,
                wanted_remaining=300,
                amount=100,
                set_by=None,
            )
            mock_perform.return_value = ([suspect.name], 100)
            mock_get_players.return_value = [
                (
                    "1",
                    {
                        "unique_id": str(suspect.player.unique_id),
                        "character_guid": str(suspect.guid),
                        "location": "X=100.0 Y=200.0 Z=0.0",
                        "vehicle": "",
                    },
                )
            ]

            event = _pullover_event(suspect.guid, suspect.guid, warning_only=False)
            await handle_police_penalty(event, suspect.player, suspect, self._ctx())
            await _drain_pending_tasks()

            # System-side arrest: no officer character
            kwargs = mock_perform.await_args.kwargs
            self.assertIsNone(kwargs["officer_character"])

            # Wanted expiry is owned by execute_arrest — untouched here
            await wanted.arefresh_from_db(fields=["expired_at"])
            self.assertIsNone(wanted.expired_at)

            # Visible server announcement for the AI arrest
            mock_announce.assert_awaited_once()
            self.assertIn(suspect.name, mock_announce.await_args.args[0])
        finally:
            await _cleanup_players(suspect.player)

    @patch("amc.handlers.police.announce", new_callable=AsyncMock)
    @patch("amc.handlers.police.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.handlers.police.clear_suspect", new_callable=AsyncMock)
    @patch("amc.handlers.police.send_system_message", new_callable=AsyncMock)
    @patch("amc.handlers.police.perform_arrest", new_callable=AsyncMock)
    async def test_ai_pullover_zero_score_wanted_clears(
        self,
        mock_perform,
        mock_send_system,
        mock_clear,
        mock_refresh,
        mock_announce,
    ):
        suspect = await self._setup_suspect(criminal_score=0)
        try:
            wanted = await Wanted.objects.acreate(
                character=suspect,
                wanted_remaining=300,
                amount=0,
                set_by=None,
            )

            event = _pullover_event(suspect.guid, suspect.guid, warning_only=False)
            await handle_police_penalty(event, suspect.player, suspect, self._ctx())
            await _drain_pending_tasks()

            await wanted.arefresh_from_db(fields=["expired_at", "wanted_remaining"])
            self.assertIsNotNone(wanted.expired_at)
            self.assertEqual(wanted.wanted_remaining, 0)

            mock_clear.assert_awaited_once()
            mock_perform.assert_not_awaited()
            # No officer player → no officer-directed system message
            mock_send_system.assert_not_awaited()
        finally:
            await _cleanup_players(suspect.player)

    @patch("amc.handlers.police.refresh_player_name", new_callable=AsyncMock)
    @patch("amc.handlers.police.clear_suspect", new_callable=AsyncMock)
    @patch("amc.handlers.police.perform_arrest", new_callable=AsyncMock)
    async def test_ai_pullover_warning_only_is_noop(
        self, mock_perform, mock_clear, mock_refresh
    ):
        suspect = await self._setup_suspect(criminal_score=5000)
        try:
            wanted = await Wanted.objects.acreate(
                character=suspect,
                wanted_remaining=300,
                amount=100,
                set_by=None,
            )

            event = _pullover_event(suspect.guid, suspect.guid, warning_only=True)
            await handle_police_penalty(event, suspect.player, suspect, self._ctx())
            await _drain_pending_tasks()

            await wanted.arefresh_from_db(fields=["expired_at"])
            self.assertIsNone(wanted.expired_at)
            mock_clear.assert_not_awaited()
            mock_perform.assert_not_awaited()
        finally:
            await _cleanup_players(suspect.player)
