"""Tests for the EventsCog Discord commands (join/kick player to event).

Covers: the player-autocomplete wiring (must target the Main Server client —
the event-server instance is not guaranteed to be running), event selection
(single / ambiguous / name / GUID prefix), the deferred-response flow, and the
error paths (fetch failure, join/kick failure).

The previous version asserted against ``response.send_message``; since these
commands now defer and reply via followups, the assertions target
``followup.send``. Written as explicit ``pytest.mark.asyncio`` functions (the
``async def``-in-``TestCase`` style of the originals also executes — Django 5.2
wraps it with ``async_to_sync`` — but the explicit markers make the execution
path obvious).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amc_cogs.events import EventsCog, _select_event

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


def _make_interaction():
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_cog():
    bot = MagicMock()
    bot.http_client_game = object()
    bot.http_client_mod = object()
    bot.event_http_client_game = object()
    bot.event_http_client_mod = object()
    with patch("amc_cogs.events.create_player_autocomplete") as factory:
        cog = EventsCog(bot)
    return cog, factory


def test_player_autocomplete_uses_main_server_client():
    """The factory must receive the Main Server client — join/kick act on
    Main Server events (#80) and the event server may not be running."""
    with patch("amc_cogs.events.create_player_autocomplete") as factory:
        bot = MagicMock()
        bot.http_client_game = object()
        cog = EventsCog(bot)
    factory.assert_called_once_with(bot.http_client_game)
    assert cog.player_autocomplete_factory is factory.return_value


def test_select_event_no_selector_single_event():
    assert _select_event(TWO_EVENTS[:1], None) == TWO_EVENTS[0]


def test_select_event_ambiguous_without_selector():
    assert _select_event(TWO_EVENTS, None) is None


def test_select_event_matches_name_and_guid_prefix():
    assert _select_event(TWO_EVENTS, "Semi")["EventGuid"] == "BBB222"
    assert _select_event(TWO_EVENTS, "bbb")["EventGuid"] == "BBB222"
    assert _select_event(TWO_EVENTS, "AAA")["EventGuid"] == "AAA111"


def test_select_event_no_match():
    assert _select_event(TWO_EVENTS, "zzzz") is None


@pytest.mark.asyncio
@patch("amc_cogs.events.join_player_to_event", new_callable=AsyncMock)
@patch("amc_cogs.events.get_events", new_callable=AsyncMock)
async def test_join_single_event(get_events_mock, join_mock):
    cog, _factory = _make_cog()
    interaction = _make_interaction()
    get_events_mock.return_value = TWO_EVENTS[:1]

    await EventsCog.join_player_to_event.callback(cog, interaction, "424242")

    interaction.response.defer.assert_awaited_once()
    join_mock.assert_awaited_once_with(
        cog.bot.http_client_mod, "AAA111", "424242"
    )
    interaction.followup.send.assert_awaited_once()
    message = interaction.followup.send.await_args.args[0]
    assert "joined **Auto TT - Gwang**" in message
    # Deferred commands answer via followup only.
    assert interaction.response.send_message.await_count == 0


@pytest.mark.asyncio
@patch("amc_cogs.events.get_events", new_callable=AsyncMock)
async def test_join_lists_events_when_ambiguous(get_events_mock):
    cog, _factory = _make_cog()
    interaction = _make_interaction()
    get_events_mock.return_value = TWO_EVENTS

    await EventsCog.join_player_to_event.callback(cog, interaction, "424242")

    message = interaction.followup.send.await_args.args[0]
    assert "Multiple active events" in message
    assert "Semi Truck Racing" in message
    assert "Auto TT - Gwang" in message


@pytest.mark.asyncio
@patch("amc_cogs.events.join_player_to_event", new_callable=AsyncMock)
@patch("amc_cogs.events.get_events", new_callable=AsyncMock)
async def test_join_selector_by_name_and_guid_prefix(
    get_events_mock, join_mock
):
    cog, _factory = _make_cog()
    get_events_mock.return_value = TWO_EVENTS
    for selector, expected_guid in [("Semi", "BBB222"), ("bbb", "BBB222")]:
        interaction = _make_interaction()
        await EventsCog.join_player_to_event.callback(
            cog, interaction, "424242", selector
        )
        assert join_mock.await_args.args[1] == expected_guid


@pytest.mark.asyncio
@patch("amc_cogs.events.get_events", new_callable=AsyncMock)
async def test_join_no_active_events(get_events_mock):
    cog, _factory = _make_cog()
    interaction = _make_interaction()
    get_events_mock.return_value = []

    await EventsCog.join_player_to_event.callback(cog, interaction, "424242")

    interaction.followup.send.assert_awaited_once_with("No active events")


@pytest.mark.asyncio
@patch("amc_cogs.events.join_player_to_event", new_callable=AsyncMock)
@patch("amc_cogs.events.get_events", new_callable=AsyncMock)
async def test_join_failure_reports_error(get_events_mock, join_mock):
    cog, _factory = _make_cog()
    interaction = _make_interaction()
    get_events_mock.return_value = TWO_EVENTS[:1]
    join_mock.side_effect = Exception(
        "Failed to join event (HTTP 400, body=player offline)"
    )

    await EventsCog.join_player_to_event.callback(cog, interaction, "424242")

    message = interaction.followup.send.await_args.args[0]
    assert "Join failed" in message
    assert "HTTP 400" in message
    # The success line must not fire on failure.
    assert "joined **" not in message


@pytest.mark.asyncio
@patch("amc_cogs.events.get_events", new_callable=AsyncMock)
async def test_join_fetch_failure_reports_error(get_events_mock):
    cog, _factory = _make_cog()
    interaction = _make_interaction()
    get_events_mock.side_effect = Exception(
        "Failed to fetch events (HTTP 503, body=timeout)"
    )

    await EventsCog.join_player_to_event.callback(cog, interaction, "424242")

    message = interaction.followup.send.await_args.args[0]
    assert "Could not fetch active events" in message
    assert "HTTP 503" in message


@pytest.mark.asyncio
@patch("amc_cogs.events.kick_player_from_event", new_callable=AsyncMock)
@patch("amc_cogs.events.get_events", new_callable=AsyncMock)
async def test_kick_uses_selected_event(get_events_mock, kick_mock):
    cog, _factory = _make_cog()
    interaction = _make_interaction()
    get_events_mock.return_value = TWO_EVENTS

    await EventsCog.kick_player_from_event.callback(
        cog, interaction, "424242", "semi"
    )

    interaction.response.defer.assert_awaited_once()
    kick_mock.assert_awaited_once_with(
        cog.bot.http_client_mod, "BBB222", "424242"
    )
    message = interaction.followup.send.await_args.args[0]
    assert "kicked from **Semi Truck Racing**" in message


@pytest.mark.asyncio
@patch("amc_cogs.events.kick_player_from_event", new_callable=AsyncMock)
@patch("amc_cogs.events.get_events", new_callable=AsyncMock)
async def test_kick_failure_reports_error(get_events_mock, kick_mock):
    cog, _factory = _make_cog()
    interaction = _make_interaction()
    get_events_mock.return_value = TWO_EVENTS[:1]
    kick_mock.side_effect = Exception(
        "Failed to kick player from event (HTTP 400, body=offline)"
    )

    await EventsCog.kick_player_from_event.callback(cog, interaction, "424242")

    message = interaction.followup.send.await_args.args[0]
    assert "Kick failed" in message
    assert "HTTP 400" in message
