from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.forms.models import model_to_dict
from django.test import TestCase
from django.urls import reverse

from amc.game_server import GameApiError
from amc.models import Player

User = get_user_model()

INLINE_PREFIXES = ("characters", "team_memberships", "tickets")


class PlayerAdminBanToggleTests(TestCase):
    """The /admin/amc/player/<id>/change/ ban toggle drives the game ban list."""

    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            "admin", "admin@example.com", "password"
        )
        self.client.login(username="admin", password="password")
        self.player = Player.objects.create(unique_id=76561198000000001)
        self.uid = str(self.player.unique_id)
        self.url = reverse("admin:amc_player_change", args=[self.player.unique_id])

    def _post_data(self, **overrides):
        data = {
            key: value
            for key, value in model_to_dict(self.player).items()
            if value is not None
        }
        data["unique_id"] = self.uid
        for prefix in INLINE_PREFIXES:
            data[f"{prefix}-TOTAL_FORMS"] = "0"
            data[f"{prefix}-INITIAL_FORMS"] = "0"
            data[f"{prefix}-MIN_NUM_FORMS"] = "0"
            data[f"{prefix}-MAX_NUM_FORMS"] = "1000"
        data.update(overrides)
        return data

    def test_change_page_renders_ban_toggle_checked_when_banned(self):
        with (
            patch("amc.admin._banned_player_ids", return_value={self.uid}),
            patch(
                "amc.admin._banned_player_list",
                return_value=[
                    {
                        "name": "Troublemaker",
                        "unique_id": self.uid,
                        "reason": "troll",
                        "left_time_seconds": 0,
                    }
                ],
            ),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        self.assertTrue(form["banned"].value())

    def test_change_page_renders_unchecked_when_not_banned(self):
        with (
            patch("amc.admin._banned_player_ids", return_value=set()),
            patch("amc.admin._banned_player_list", return_value=[]),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        self.assertFalse(form["banned"].value())

    def test_change_page_disables_toggle_when_api_unreachable(self):
        with (
            patch(
                "amc.admin._banned_player_ids",
                side_effect=GameApiError("game server API unavailable: refused"),
            ),
            patch(
                "amc.admin._banned_player_list",
                side_effect=GameApiError("game server API unavailable: refused"),
            ),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        field = response.context["adminform"].form.fields["banned"]
        self.assertTrue(field.disabled)
        self.assertIsNone(field.initial)

    def test_ticking_ban_calls_ban_player(self):
        api_call = MagicMock(return_value={"succeeded": True, "message": "Player banned"})
        with (
            patch("amc.admin._banned_player_ids", return_value=set()),
            patch("amc.admin._banned_player_list", return_value=[]),
            patch("amc.admin._game_api_call", api_call),
        ):
            response = self.client.post(
                self.url,
                self._post_data(banned="on", ban_reason="troll", ban_hours=""),
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(api_call.call_count, 1)
        args = api_call.call_args.args
        self.assertEqual(args[0].__name__, "ban_player")
        self.assertEqual(args[1], self.player.unique_id)
        self.assertIn("banned (permanent)", response.content.decode())

    def test_unticking_ban_calls_unban_player(self):
        api_call = MagicMock(
            return_value={"succeeded": True, "message": "Player unbanned"}
        )
        with (
            patch("amc.admin._banned_player_ids", return_value={self.uid}),
            patch(
                "amc.admin._banned_player_list",
                return_value=[
                    {
                        "unique_id": self.uid,
                        "reason": "troll",
                        "left_time_seconds": 0,
                    }
                ],
            ),
            patch("amc.admin._game_api_call", api_call),
        ):
            response = self.client.post(self.url, self._post_data(), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(api_call.call_count, 1)
        self.assertEqual(api_call.call_args.args[0].__name__, "unban_player")
        self.assertIn("unbanned", response.content.decode())

    def test_unchanged_toggle_makes_no_api_call(self):
        api_call = MagicMock(return_value={"succeeded": True})
        with (
            patch("amc.admin._banned_player_ids", return_value=set()),
            patch("amc.admin._banned_player_list", return_value=[]),
            patch("amc.admin._game_api_call", api_call),
        ):
            response = self.client.post(self.url, self._post_data(), follow=True)

        self.assertEqual(response.status_code, 200)
        api_call.assert_not_called()

    def test_rejected_ban_surfaces_the_server_message(self):
        api_call = MagicMock(
            return_value={"succeeded": False, "message": "Invalid password"}
        )
        with (
            patch("amc.admin._banned_player_ids", return_value=set()),
            patch("amc.admin._banned_player_list", return_value=[]),
            patch("amc.admin._game_api_call", api_call),
        ):
            response = self.client.post(
                self.url, self._post_data(banned="on"), follow=True
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Invalid password", response.content.decode())
