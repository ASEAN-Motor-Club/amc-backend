"""Tests for the pinned-banner push on server start.

The banner (GameState.Net_ServerConfig.PinnedAnnounce) is runtime-only game
state: the game clears it on every boot, so the backend re-sets it when the
ServerStarted log event is processed. The write goes through the mod's
ChatManager direct-write branch (bogus playerId → no online admin needed).

Verified live on asean-mt-server 2026-09-04: both the ServerAnnouncePinned RPC
path (valid admin playerId) and the direct-write path set the banner; the
native game Web API POST /chat (type=Message/Announce) does NOT touch it.
"""

from unittest.mock import AsyncMock, patch

from django.conf import settings
from django.test import SimpleTestCase

from amc.mod_server import set_pinned_announcement
from amc.tasks import spawn_pinned_announcement


class SetPinnedAnnouncementRequestTests(SimpleTestCase):
    """set_pinned_announcement must hit the ChatManager direct-write branch."""

    class _FakeResponse:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

    class _FakeSession:
        def __init__(self, status=200):
            self.status = status
            self.calls = []

        def post(self, url, json=None):
            self.calls.append((url, json))
            return SetPinnedAnnouncementRequestTests._FakeResponse(self.status)

    async def test_posts_lowercase_direct_write_payload(self):
        session = self._FakeSession()

        await set_pinned_announcement(session, "hello banner")

        self.assertEqual(len(session.calls), 1)
        url, payload = session.calls[0]
        self.assertEqual(url, "/messages/announce")
        # ChatManager.lua reads data.message/data.playerId/data.isPinned —
        # lowercase keys, unlike the PascalCase used by /players/{id}/chat.
        self.assertEqual(
            payload,
            {"message": "hello banner", "playerId": "0", "isPinned": True},
        )

    async def test_raises_on_non_200(self):
        session = self._FakeSession(status=400)

        with self.assertRaisesRegex(Exception, "Failed to set pinned announcement"):
            await set_pinned_announcement(session, "hello banner")


class PinnedAnnouncementSpawnTests(SimpleTestCase):
    """spawn_pinned_announcement pushes settings.PINNED_ANNOUNCEMENT."""

    async def test_sets_banner_with_setting_text(self):
        with patch("amc.tasks.set_pinned_announcement", AsyncMock()) as mock_set:
            await spawn_pinned_announcement(http_client_mod=object())

        mock_set.assert_called_once()
        args = mock_set.call_args.args
        self.assertEqual(args[1], settings.PINNED_ANNOUNCEMENT)

    async def test_skipped_when_setting_empty(self):
        with (
            self.settings(PINNED_ANNOUNCEMENT=""),
            patch("amc.tasks.set_pinned_announcement", AsyncMock()) as mock_set,
        ):
            await spawn_pinned_announcement(http_client_mod=object())

        mock_set.assert_not_called()

    async def test_retries_while_world_is_loading(self):
        """The mod 400s until the GameState exists — recover within attempts."""
        calls = []

        async def flaky(session, message):
            calls.append(message)
            if len(calls) < 3:
                raise RuntimeError("HTTP 400 — world not ready")

        patcher, waits = _patch_sleep()
        with patcher, patch("amc.tasks.set_pinned_announcement", new=flaky):
            await spawn_pinned_announcement(http_client_mod=object())

        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [5, 10])  # base_delay=5, exponential backoff


def _patch_sleep():
    """Replace asyncio.sleep inside amc.tasks with a no-op that records waits."""
    recorded: list[float] = []

    async def fake_sleep(seconds, *args, **kwargs):
        recorded.append(seconds)

    return patch("asyncio.sleep", new=fake_sleep), recorded
