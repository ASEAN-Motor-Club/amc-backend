"""Tests for the TT start-line disqualification (handlers/tt_dq.py).

Covers the enforcement contract agreed for the tester hand-off:

* fires only when the event carries a TT class (no class -> no-op)
* compliant build -> no kick
* over-power engine -> force-leave via the mod's /leave endpoint (the
  native ServerLeaveEvent path), keyed on CharacterId.UniqueNetId
* missing vehicle/parts data -> skip, never DQ on unverifiable data
* mod tire -> DQ
* a failed kick is contained (logged, not raised) and the player is not
  reported as disqualified
"""

from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings

from amc.handlers.tt_dq import _disqualify_illegal_starters
from amc.models import Character, GameEvent, GameEventCharacter, TTClass

pytestmark = pytest.mark.django_db


def _player(guid, unique_id, name):
    return {
        "CharacterId": {"CharacterGuid": guid, "UniqueNetId": unique_id},
        "PlayerName": name,
    }


def _ok_parts():
    # SmallBlock_240HP ~238.6hp; vanilla tire on slot 19.
    return [
        {"Slot": 2, "Key": "SmallBlock_240HP"},
        {"Slot": 19, "Key": "201"},
    ]


def _over_parts():
    return [{"Slot": 2, "Key": "SmallBlock_240HP"}]  # 238.6hp > 140+5


async def _make_tt_event(max_hp=140):
    tt = await sync_to_async(TTClass.objects.get)(name=f"TT-{max_hp}")
    return await sync_to_async(GameEvent.objects.create)(
        guid="GUIDDQ00000000000000000000001",
        name="DQ Test Event [TT-140]",
        state=2,
        tt_class=tt,
    )


async def _make_participant(event, guid, unique_id, name):
    """Create the Character + GameEventCharacter the real pipeline would."""
    character, *_ = await Character.objects.aget_or_create_character_player(
        name, int(unique_id), character_guid=guid
    )
    return await sync_to_async(GameEventCharacter.objects.create)(
        game_event=event, character=character, rank=0
    )


async def _fake_player_data(guid, parts, vehicle_full="Vehicle_X_C"):
    return (
        {"vehicle": {"fullName": vehicle_full}},
        {"parts": parts},
    )


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_no_tt_class_is_noop(kick, last_veh, last_parts, post, db):
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDNOCLS000000000000000000001", name="Plain Event", state=2
    )
    out = await _disqualify_illegal_starters(
        object(), event, {"Players": [_player("G1", "U1", "Alice")]}, None
    )
    assert out == []
    kick.assert_not_awaited()
    last_veh.assert_not_awaited()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_compliant_player_not_kicked(kick, last_veh, last_parts, post, db):
    event = await _make_tt_event(max_hp=270)  # 238.6hp fits TT-270
    last_veh.return_value = {"vehicle": {"fullName": "Vehicle_X_C"}}
    last_parts.return_value = {"parts": _ok_parts()}
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDOK0000000000000000000000001", "U77", "Alice")]},
        None,
    )
    assert out == []
    kick.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_overpower_player_kicked_and_row_deleted(
    kick, last_veh, last_parts, post, db
):
    event = await _make_tt_event()
    await _make_participant(event, "GUIDDQP000000000000000000000001", "99", "Bob")
    last_veh.return_value = {"vehicle": {"fullName": "Vehicle_X_C"}}
    last_parts.return_value = {"parts": _over_parts()}
    with override_settings(DISCORD_PARTS_LOG_CHANNEL_ID=123):
        out = await _disqualify_illegal_starters(
            object(),
            event,
            {"Players": [_player("GUIDDQP000000000000000000000001", "U99", "Bob")]},
            object(),  # discord client present -> DQ card posted
        )
    assert out == ["Bob"]
    assert kick.await_count == 1
    assert kick.await_args.args[1] == event.guid
    assert kick.await_args.args[2] == "U99"
    remaining = await sync_to_async(GameEventCharacter.objects.count)()
    assert remaining == 0
    post.assert_awaited_once()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_mod_tire_kicks(kick, last_veh, last_parts, post, db):
    event = await _make_tt_event()
    last_veh.return_value = {"vehicle": {"fullName": "Vehicle_X_C"}}
    last_parts.return_value = {
        "parts": [{"Slot": 19, "Key": "HeavyDutyBaja60FrontTire"}]
    }
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDDQMOD0000000000000000000001", "U5", "Cara")]},
        None,
    )
    assert out == ["Cara"]
    kick.assert_awaited_once()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_unverifiable_parts_skipped(kick, last_veh, last_parts, post, db):
    event = await _make_tt_event()
    last_veh.return_value = {"vehicle": None}
    last_parts.return_value = {"parts": []}
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDDQUNK0000000000000000000001", "U3", "Dave")]},
        None,
    )
    assert out == []
    kick.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_kick_failure_contained(kick, last_veh, last_parts, post, db):
    event = await _make_tt_event()
    last_veh.return_value = {"vehicle": {"fullName": "Vehicle_X_C"}}
    last_parts.return_value = {"parts": _over_parts()}
    kick.side_effect = RuntimeError("mod down")
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDDQERR0000000000000000000001", "U2", "Eve")]},
        None,
    )
    assert out == []
    post.assert_not_awaited()
