"""Tests for the EventsCog Discord commands (join/kick player to event)."""

from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase

from amc_cogs.events import EventsCog

TWO_EVENTS = [
    {
        "EventGuid": "AAA111",
        "EventName": "Auto TT - Gwang",
        "State": 1,
        "OwnerCharacterId": {"UniqueNetId": "0"},
    },
    {
        "EventGuid": "BBB222",
        "EventName": "Semi Truck Racing",
        "State": 1,
        "OwnerCharacterId": {"UniqueNetId": "76561198378447512"},
    },
]


def _make_cog():
    bot = MagicMock()
    bot.http_client_mod = MagicMock()
    bot.http_client_game = MagicMock()
    return EventsCog(bot)


class JoinPlayerToEventTests(TestCase):
    async def test_joins_when_exactly_one_event(self):
        cog = _make_cog()
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()

        with patch(
            "amc_cogs.events.get_events",
            new=AsyncMock(return_value=[TWO_EVENTS[0]]),
        ), patch(
            "amc_cogs.events.join_player_to_event", new=AsyncMock()
        ) as mock_join:
            await EventsCog.join_player_to_event.callback(
                cog, interaction, "76561198039953945"
            )

        mock_join.assert_awaited_once()
        self.assertEqual(mock_join.await_args.args[1], "AAA111")
        interaction.response.send_message.assert_awaited_once()

    async def test_lists_events_when_ambiguous_and_no_selector(self):
        cog = _make_cog()
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()

        with patch(
            "amc_cogs.events.get_events", new=AsyncMock(return_value=TWO_EVENTS)
        ), patch(
            "amc_cogs.events.join_player_to_event", new=AsyncMock()
        ) as mock_join:
            await EventsCog.join_player_to_event.callback(
                cog, interaction, "76561198039953945"
            )

        mock_join.assert_not_awaited()
        msg = interaction.response.send_message.await_args.args[0]
        self.assertIn("Semi Truck Racing", msg)
        self.assertIn("Auto TT - Gwang", msg)

    async def test_selector_by_name_and_guid_prefix(self):
        cog = _make_cog()
        for selector, expected_guid in [("Semi", "BBB222"), ("bbb", "BBB222")]:
            interaction = MagicMock()
            interaction.response.send_message = AsyncMock()

            with patch(
                "amc_cogs.events.get_events", new=AsyncMock(return_value=TWO_EVENTS)
            ), patch(
                "amc_cogs.events.join_player_to_event", new=AsyncMock()
            ) as mock_join:
                await EventsCog.join_player_to_event.callback(
                    cog, interaction, "76561198039953945", selector
                )

            self.assertEqual(mock_join.await_args.args[1], expected_guid)

    async def test_no_active_events(self):
        cog = _make_cog()
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()

        with patch("amc_cogs.events.get_events", new=AsyncMock(return_value=[])):
            await EventsCog.join_player_to_event.callback(
                cog, interaction, "76561198039953945"
            )

        interaction.response.send_message.assert_awaited_once_with("No active events")


class KickPlayerFromEventTests(TestCase):
    async def test_kick_uses_selected_event(self):
        cog = _make_cog()
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()

        with patch(
            "amc_cogs.events.get_events", new=AsyncMock(return_value=TWO_EVENTS)
        ), patch(
            "amc_cogs.events.kick_player_from_event", new=AsyncMock()
        ) as mock_kick:
            await EventsCog.kick_player_from_event.callback(
                cog, interaction, "76561198039953945", "semi"
            )

        mock_kick.assert_awaited_once()
        self.assertEqual(mock_kick.await_args.args[1], "BBB222")
