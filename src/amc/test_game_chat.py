"""Tests for the game-API /chat announcement path (amc.game_server).

The HostWebAPI password enforcement (prod 2026-09-29) made the game reject
every /chat request with HTTP 200 + succeeded=false "Invalid password" — a
silently dead announcement channel. Lock in:

* GAME_SERVER_API_PASSWORD is forwarded when the caller passes no password
* a succeeded=false response is logged as a WARNING (visible via the
  dedicated "amc.game_server" child-logger entry in settings.LOGGING)
"""

from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

from amc import game_server


def _fake_game_api(responder):
    return patch("amc.game_server.game_api_request", side_effect=responder)


class AnnouncementRequestTest(SimpleTestCase):
    async def test_env_password_forwarded(self):
        sent = {}

        async def fake_request(
            session, url, method="get", password="", params=None, timeout=15
        ):
            sent["password"] = password
            sent["params"] = params
            return {"succeeded": True}

        with (
            patch.dict("os.environ", {"GAME_SERVER_API_PASSWORD": "sekrit"}),
            _fake_game_api(fake_request),
        ):
            await game_server.announcement_request("hello", object())
        self.assertEqual(sent["password"], "sekrit")
        self.assertEqual(sent["params"]["message"], "hello")

    async def test_explicit_password_wins_over_env(self):
        sent = {}

        async def fake_request(
            session, url, method="get", password="", params=None, timeout=15
        ):
            sent["password"] = password
            return {"succeeded": True}

        with (
            patch.dict("os.environ", {"GAME_SERVER_API_PASSWORD": "sekrit"}),
            _fake_game_api(fake_request),
        ):
            await game_server.announcement_request(
                "hello", object(), password="explicit"
            )
        self.assertEqual(sent["password"], "explicit")

    async def test_rejection_is_logged(self):
        async def fake_request(
            session, url, method="get", password="", params=None, timeout=15
        ):
            return {
                "data": {},
                "message": "Invalid password",
                "succeeded": False,
                "code": -1,
            }

        with (
            patch.dict("os.environ", {"GAME_SERVER_API_PASSWORD": "sekrit"}),
            _fake_game_api(fake_request),
            self.assertLogs("amc.game_server", level="WARNING") as captured,
        ):
            await game_server.announcement_request("hello", object())
        self.assertTrue(
            any("Invalid password" in line for line in captured.output),
            captured.output,
        )

    async def test_success_not_logged(self):
        async def fake_request(
            session, url, method="get", password="", params=None, timeout=15
        ):
            return {"succeeded": True}

        with (
            patch.dict("os.environ", {"GAME_SERVER_API_PASSWORD": "sekrit"}),
            _fake_game_api(fake_request),
            self.assertNoLogs("amc.game_server", level="WARNING"),
        ):
            await game_server.announcement_request("hello", object())

    async def test_broadcast_uses_game_api_path(self):
        # broadcast_server_message lives in mod_server but resolves
        # announcement_request from amc.game_server at call time.
        from amc.mod_server import broadcast_server_message

        with patch(
            "amc.game_server.announcement_request", new_callable=AsyncMock
        ) as req:
            await broadcast_server_message(object(), "msg")
        req.assert_awaited_once()
        self.assertEqual(req.await_args.args[1], "msg")
