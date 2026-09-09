"""Callback-level tests for the /check_all_players cog (amc_cogs/parts_audit.py).

Event-scoped: the command resolves a Ready/Racing event (explicit query or
most recent) and audits its recorded participants.  Event + participant
rows are built through the real handler helpers (`_upsert_game_event` /
`_upsert_game_event_character`) — direct GameEventCharacter creates die on
the NOT NULL rank column.

Async ORM writes bypass pytest-django's rollback, so every test deletes its
own rows in a finally block (suite convention: scope assertions, clean up
after yourself).  Each test also uses a UNIQUE event GUID so guid-keyed
upserts can't collide with leaked rows from a failed prior test.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amc.handlers.events import _upsert_game_event, _upsert_game_event_character
from amc.models import GameEvent
from amc_cogs.parts_audit import PartsAuditCog

pytestmark = [pytest.mark.asyncio]


def _guid(tag: str) -> str:
    return (tag * 4)[:32].ljust(32, "0").upper()


def _player_payload(net_id: str, name: str) -> dict:
    return {
        "CharacterId": {
            "UniqueNetId": net_id,
            "CharacterGuid": f"CHAR{net_id}",
        },
        "PlayerName": name,
        "Rank": 0,
        "SectionIndex": -1,
        "Laps": 0,
        "BestLapTime": 0.0,
        "LastSectionTotalTimeSeconds": 0.0,
        "bFinished": False,
        "bDisqualified": False,
        "bWrongVehicle": False,
        "bWrongEngine": False,
        "LapTimes": [],
        "Reward_Money": {"BaseValue": 0},
    }


def _event_data(state=1, players=None, name="Test Event", guid=""):
    return {
        "EventGuid": guid,
        "EventName": name,
        "State": state,
        "EventType": 1,
        "OwnerCharacterId": _player_payload("9001", "host")["CharacterId"],
        "RaceSetup": {},
        "Players": players or [],
    }


async def _make_event(state=1, roster=(("1", "alpha"), ("2", "beta")), name="Test Event", guid=""):
    event_data = _event_data(state=state, name=name, guid=guid)
    game_event, _ = await _upsert_game_event(event_data)
    for net_id, name_ in roster:
        await _upsert_game_event_character(game_event, _player_payload(net_id, name_))
    return game_event


async def _cleanup(*guids):
    await GameEvent.objects.filter(guid__in=guids).adelete()


def _make_interaction():
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user.display_name = "admin"
    return interaction


def _make_cog():
    cog = PartsAuditCog(MagicMock())
    cog.bot.http_client_game = object()
    cog.bot.http_client_mod = object()
    return cog


async def _run(cog, interaction, event=None):
    await PartsAuditCog.check_all_players.callback(cog, interaction, event)


@pytest.mark.django_db
async def test_audits_participants_of_most_recent_event():
    guid = _guid("aa")
    try:
        await _make_event(guid=guid)
        cog = _make_cog()
        interaction = _make_interaction()

        audit = AsyncMock(return_value=MagicMock())
        with patch("amc_cogs.parts_audit.audit_character", audit):
            await _run(cog, interaction)

        assert audit.await_count == 2
        guids = {call.args[1] for call in audit.await_args_list}
        assert guids == {"CHAR1", "CHAR2"}
        text = interaction.followup.send.await_args.args[0]
        assert "Test Event" in text
        assert "2 participant(s)" in text
        assert "2 report(s) delivered" in text
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    finally:
        await _cleanup(guid)


@pytest.mark.django_db
async def test_event_query_filters_by_name():
    sunday, old = _guid("sb"), _guid("ol")
    try:
        await _make_event(name="Sunday Race", guid=sunday)
        await _make_event(name="Old Event", guid=old, roster=())
        cog = _make_cog()
        interaction = _make_interaction()

        audit = AsyncMock(return_value=MagicMock())
        with patch("amc_cogs.parts_audit.audit_character", audit):
            await _run(cog, interaction, "sunday")

        assert audit.await_count == 2  # the Sunday Race roster, not the empty one
        assert "Sunday Race" in interaction.followup.send.await_args.args[0]
    finally:
        await _cleanup(sunday, old)


@pytest.mark.django_db
async def test_no_matching_event():
    try:
        cog = _make_cog()
        interaction = _make_interaction()

        await _run(cog, interaction, "nonexistent")

        interaction.followup.send.assert_awaited_once()
        assert "No Ready/Racing event found" in interaction.followup.send.await_args.args[0]
    finally:
        await _cleanup(_guid("xx"))


@pytest.mark.django_db
async def test_event_without_recorded_participants():
    guid = _guid("cc")
    try:
        await _make_event(guid=guid, roster=())
        cog = _make_cog()
        interaction = _make_interaction()

        await _run(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        assert "no recorded participants" in interaction.followup.send.await_args.args[0]
    finally:
        await _cleanup(guid)


@pytest.mark.django_db
async def test_zero_posted_with_channel_set_reports_delivery_problem():
    guid = _guid("dd")
    try:
        await _make_event(guid=guid)
        cog = _make_cog()
        interaction = _make_interaction()

        with (
            patch("amc_cogs.parts_audit.audit_character", AsyncMock(return_value=None)),
            patch("amc_cogs.parts_audit.settings") as mock_settings,
        ):
            mock_settings.DISCORD_PARTS_LOG_CHANNEL_ID = 12345
            await _run(cog, interaction)

        text = interaction.followup.send.await_args.args[0]
        assert "0 report(s) delivered" in text
        assert "may lack View Channel" in text
    finally:
        await _cleanup(guid)


@pytest.mark.django_db
async def test_finished_events_not_selected():
    guid = _guid("ee")
    try:
        # state 3 = finished: must not be picked up by the default selector
        await _make_event(state=3, guid=guid)
        cog = _make_cog()
        interaction = _make_interaction()

        await _run(cog, interaction)

        assert "No Ready/Racing event found" in interaction.followup.send.await_args.args[0]
    finally:
        await _cleanup(guid)
