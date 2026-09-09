"""Tests for the silent parts audit (amc/parts_audit.py + join reconcile)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amc.handlers.events import _reconcile_event_players
from amc.models import GameEvent
from amc.parts_audit import summarize_parts

pytestmark = [pytest.mark.asyncio]


def _part(key: str, slot: int) -> dict:
    return {"ID": 0, "Key": key, "Slot": slot, "Damage": 0}


def _installed_parts(
    engine: str | None = "SmallBlock_240HP",
    intake: str | None = "201",
    turbo: str | None = "Turbocharger_Stage1",
    tires: tuple[str, ...] = ("201_50", "201_50"),
):
    parts = []
    if engine:
        parts.append(_part(engine, 2))
    if intake:
        parts.append(_part(intake, 5))
    if turbo:
        parts.append(_part(turbo, 7))
    for i, tire in enumerate(tires):
        parts.append(_part(tire, 19 + i))
    return parts


class TestSummarizeParts:
    async def test_standard_combo(self):
        lines = summarize_parts(_installed_parts())
        assert lines[0].startswith("Power: ")
        assert "hp" in lines[0]
        assert "Engine: SmallBlock_240HP" in lines
        assert "Intake: 201" in lines
        assert "Turbocharger: Turbocharger_Stage1" in lines
        assert "Tires: 201_50, 201_50" in lines

    async def test_unknown_engine_degrades_to_power_unknown(self):
        lines = summarize_parts(_installed_parts(engine="Mystery_Engine_XYZ"))
        assert lines[0] == "Power: Unknown"
        assert "Engine: Mystery_Engine_XYZ" in lines

    async def test_no_engine_slot_omits_power_line(self):
        lines = summarize_parts(_installed_parts(engine=None))
        assert not any(line.startswith("Power:") for line in lines)
        assert "Engine: None" in lines
        assert "Intake: 201" in lines

    async def test_absent_slots_report_none(self):
        lines = summarize_parts(_installed_parts(intake=None, turbo=None, tires=()))
        assert "Intake: None" in lines
        assert "Turbocharger: None" in lines
        assert "Tires: None" in lines


class TestAuditCharacter:
    async def test_posts_embed_to_configured_channel(self):
        client = MagicMock()
        client.is_ready.return_value = True
        channel = MagicMock()
        channel.send = AsyncMock()
        client.get_channel.return_value = channel

        async def fake_vehicle(session, guid):
            return {"vehicle": {"fullName": "Elisa2"}}

        async def fake_parts(session, guid, complete=False):
            return {"parts": _installed_parts()}

        with (
            patch("amc.parts_audit.get_player_last_vehicle", fake_vehicle),
            patch("amc.parts_audit.get_player_last_vehicle_parts", fake_parts),
            patch("amc.parts_audit.settings") as mock_settings,
        ):
            mock_settings.DISCORD_PARTS_LOG_CHANNEL_ID = 12345
            embed = await _audit(client, "guid-1", "tester")

        assert embed is not None
        channel.send.assert_awaited_once()
        sent = channel.send.await_args.kwargs["embed"]
        assert sent.title == "Parts Audit — tester"
        assert "**Vehicle:** Elisa2" in sent.description
        assert "Power:" in sent.description
        assert "Tires: 201_50, 201_50" in sent.description
        assert sent.footer.text.startswith("source: event-join")

    async def test_fetch_failure_returns_none_without_post(self):
        client = MagicMock()
        client.is_ready.return_value = True
        channel = MagicMock()
        channel.send = AsyncMock()
        client.get_channel.return_value = channel

        async def boom(session, guid):
            raise RuntimeError("mod down")

        with (
            patch("amc.parts_audit.get_player_last_vehicle", boom),
            patch("amc.parts_audit.get_player_last_vehicle_parts", boom),
            patch("amc.parts_audit.settings") as mock_settings,
        ):
            mock_settings.DISCORD_PARTS_LOG_CHANNEL_ID = 12345
            result = await _audit(client, "guid-1", "tester")

        assert result is None
        channel.send.assert_not_awaited()

    async def test_channel_unset_returns_embed_without_post(self):
        client = MagicMock()
        client.is_ready.return_value = True

        async def fake_vehicle(session, guid):
            return {"vehicle": {"fullName": "Elisa2"}}

        async def fake_parts(session, guid, complete=False):
            return {"parts": _installed_parts()}

        with (
            patch("amc.parts_audit.get_player_last_vehicle", fake_vehicle),
            patch("amc.parts_audit.get_player_last_vehicle_parts", fake_parts),
            patch("amc.parts_audit.settings") as mock_settings,
        ):
            mock_settings.DISCORD_PARTS_LOG_CHANNEL_ID = 0
            result = await _audit(client, "guid-1", "tester")

        # Feature disabled: embed built (counts as audited), just nowhere
        # to post it.
        assert result is not None
        client.get_channel.assert_not_called()

    async def test_unresolvable_channel_returns_none_with_warning(self):
        """Private channel the bot can't see: NOT delivered, not counted."""
        client = MagicMock()
        client.is_ready.return_value = True
        client.get_channel.return_value = None  # bot lacks View Channel

        async def fake_vehicle(session, guid):
            return {"vehicle": {"fullName": "Elisa2"}}

        async def fake_parts(session, guid, complete=False):
            return {"parts": _installed_parts()}

        with (
            patch("amc.parts_audit.get_player_last_vehicle", fake_vehicle),
            patch("amc.parts_audit.get_player_last_vehicle_parts", fake_parts),
            patch("amc.parts_audit.settings") as mock_settings,
        ):
            mock_settings.DISCORD_PARTS_LOG_CHANNEL_ID = 12345
            result = await _audit(client, "guid-1", "tester")

        assert result is None
        client.get_channel.assert_called_once_with(12345)


async def _audit(client, guid, name):
    from amc.parts_audit import audit_character

    return await audit_character(
        object(), guid, name, discord_client=client, source="event-join: test"
    )


# ---------------------------------------------------------------------------
# Join reconcile wiring
# ---------------------------------------------------------------------------

RACE_SETUP_RAW = {
    "NumLaps": 0,
    "Route": {
        "RouteName": "Test Route",
        "Waypoints": [
            {"Location": {"X": -254858.0, "Y": 118884.0, "Z": -19609.0}},
            {"Location": {"X": -240477.0, "Y": 99544.0, "Z": -19115.0}},
        ],
    },
    "VehicleKeys": [],
    "EngineKeys": [],
}

GUID = "5B11926A45D1869C3AA6309F3F564829"


def _player(net_id: str, name: str) -> dict:
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


def _event_data(state, players):
    return {
        "EventGuid": GUID,
        "EventName": "Test Event",
        "State": state,
        "EventType": 1,
        "OwnerCharacterId": _player("9001", "host")["CharacterId"],
        "RaceSetup": RACE_SETUP_RAW,
        "Players": players,
    }


@pytest.mark.django_db
class TestJoinAuditReconcile:
    async def test_audit_fires_once_for_new_joiner_and_not_for_known_row(self):
        audit = AsyncMock()
        live = _event_data(1, [_player("1", "alpha"), _player("2", "beta")])
        try:
            with patch("amc.handlers.events.audit_event_join", audit):
                game_event = await _reconcile_event_players(
                    object(), GUID, live_event=live
                )
                assert game_event is not None
                assert audit.await_count == 2  # fresh join + fresh join
                names = [call.args[2] for call in audit.await_args_list]
                assert names == ["alpha", "beta"]

                # Reconcile again with the same roster: rows exist → no re-audit.
                await _reconcile_event_players(object(), GUID, live_event=live)
                assert audit.await_count == 2
        finally:
            await GameEvent.objects.filter(guid=GUID).adelete()

    async def test_no_audit_on_state_2_reconcile(self):
        audit = AsyncMock()
        # Event created while racing with an unrecorded lobby (the
        # start-transition backfill) — must not mass-audit.
        live = _event_data(2, [_player("3", "late")])
        try:
            with patch("amc.handlers.events.audit_event_join", audit):
                await _reconcile_event_players(object(), GUID, live_event=live)
                assert audit.await_count == 0
        finally:
            await GameEvent.objects.filter(guid=GUID).adelete()

    async def test_no_audit_for_empty_roster_or_missing_players_key(self):
        audit = AsyncMock()
        try:
            with patch("amc.handlers.events.audit_event_join", audit):
                empty = _event_data(1, [])
                await _reconcile_event_players(object(), GUID, live_event=empty)
                malformed = dict(_event_data(1, [_player("4", "x")]))
                malformed.pop("Players")
                game_event = await _reconcile_event_players(
                    object(), GUID, live_event=malformed
                )
                assert game_event is not None
                assert audit.await_count == 0
        finally:
            await GameEvent.objects.filter(guid=GUID).adelete()


# ---------------------------------------------------------------------------
# Hook wiring — the AddEvent hook records the auto-joined host's row BEFORE
# the crosscheck reconcile ever runs, so the audit must fire there too
# (prod miss 2026-09-09: host-only events were never audited).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestJoinAuditHooks:
    @staticmethod
    def _ctx(http_client_mod=object(), discord_client=None):
        from types import SimpleNamespace

        return SimpleNamespace(
            http_client_mod=http_client_mod, discord_client=discord_client
        )

    async def test_add_event_hook_audits_auto_joined_host(self):
        from amc.handlers.events import handle_add_event

        audit = AsyncMock()
        try:
            with patch("amc.handlers.events.audit_event_join", audit):
                await handle_add_event(
                    {"data": {"Event": _event_data(1, [_player("5", "host-1")])}},
                    None,
                    None,
                    self._ctx(),
                )
                assert audit.await_count == 1
                calls = audit.await_args_list
                assert calls[0].args[1] == "CHAR5"
                assert calls[0].args[2] == "host-1"
                assert calls[0].args[3] == "Test Event"
        finally:
            await GameEvent.objects.filter(guid=GUID).adelete()

    async def test_add_event_hook_no_audit_when_state_not_ready(self):
        from amc.handlers.events import handle_add_event

        audit = AsyncMock()
        try:
            with patch("amc.handlers.events.audit_event_join", audit):
                await handle_add_event(
                    {"data": {"Event": _event_data(2, [_player("6", "host-2")])}},
                    None,
                    None,
                    self._ctx(),
                )
                assert audit.await_count == 0
        finally:
            await GameEvent.objects.filter(guid=GUID).adelete()

    async def test_change_state_hook_audits_new_row_in_ready_payload(self):
        from amc.handlers.events import handle_add_event, handle_change_event_state

        audit = AsyncMock()
        try:
            with patch("amc.handlers.events.audit_event_join", audit):
                # Event appears pre-seeded with an empty roster, then a
                # state-1 payload arrives with a brand-new player.
                await handle_add_event(
                    {"data": {"Event": _event_data(1, [])}},
                    None,
                    None,
                    self._ctx(),
                )
                await handle_change_event_state(
                    {
                        "data": {
                            "Event": _event_data(1, [_player("7", "late-host")])
                        }
                    },
                    None,
                    None,
                    self._ctx(),
                )
                assert audit.await_count == 1
                calls = audit.await_args_list
                assert calls[0].args[2] == "late-host"
        finally:
            await GameEvent.objects.filter(guid=GUID).adelete()

    async def test_add_event_hook_no_audit_without_mod_client(self):
        from amc.handlers.events import handle_add_event

        audit = AsyncMock()
        try:
            with patch("amc.handlers.events.audit_event_join", audit):
                await handle_add_event(
                    {"data": {"Event": _event_data(1, [_player("8", "host-3")])}},
                    None,
                    None,
                    self._ctx(http_client_mod=None),
                )
                assert audit.await_count == 0
        finally:
            await GameEvent.objects.filter(guid=GUID).adelete()
