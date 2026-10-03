"""Tests for the underground racing additions (Yuuka 2026-09-29).

* Blood Money position ladder (config.underground_blood_money)
* Rotation-end payout (events.pay_underground_rotation_rewards): settles
  once (rewards_paid claim), pays only racers (laps > 0) in finish order,
  halves per place, flattens from 5th, skips live events
* Vehicle-type DQ rule (handlers/tt_dq): Small/Pickup only, fail-closed
"""

from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from amc import config as config_mod
from amc import events as events_mod
from amc.config import (
    UNDERGROUND_CHAMPIONSHIP_NAME,
    underground_blood_money,
)
from amc.events import pay_underground_rotation_rewards
from amc.handlers.tt_dq import _disqualify_illegal_starters, vehicle_type_violation
from amc.models import (
    Championship,
    Character,
    GameEvent,
    GameEventCharacter,
    RaceSetup,
    ScheduledEvent,
    TTClass,
)

pytestmark = [pytest.mark.django_db, pytest.mark.asyncio]


ROUTE = {
    "Route": {
        "RouteName": "Underground Payout Route",
        "Waypoints": [{"Location": {"X": 1.0, "Y": 2.0, "Z": 3.0}} for _ in range(4)],
    },
    "NumLaps": 0,
    "VehicleKeys": [],
    "EngineKeys": [],
}


# --------------------------------------------------------------------------
# Ladder
# --------------------------------------------------------------------------


async def test_blood_money_ladder():
    c = 4  # checkpoints
    rate = 4000  # explicit — BLOOD_MONEY_PER_CHECKPOINT is 0 right now
    assert underground_blood_money(c, 1, rate) == 4 * rate
    assert underground_blood_money(c, 2, rate) == 2 * rate
    assert underground_blood_money(c, 3, rate) == rate
    assert underground_blood_money(c, 4, rate) == rate // 2
    assert underground_blood_money(c, 5, rate) == rate // 4
    assert underground_blood_money(c, 6, rate) == underground_blood_money(c, 5, rate)
    assert underground_blood_money(c, 50, rate) == underground_blood_money(c, 5, rate)


@pytest.mark.asyncio
async def test_blood_money_rate_zero_disables_payouts():
    """Yuuka 2026-09-30: BLOOD_MONEY_PER_CHECKPOINT = 0 while the rate is
    under community discussion — the ladder pays nothing and the payout
    pass transfers nothing (amount > 0 guard)."""
    assert underground_blood_money(9, 1, 0) == 0
    assert underground_blood_money(9, 5, 0) == 0


async def test_blood_money_odd_checkpoints_floors():
    # 5 checkpoints x 4000 = 20000; halves: 10000, 5000, 2500, 1250
    rate = 4000
    assert underground_blood_money(5, 1, rate) == 20000
    assert underground_blood_money(5, 3, rate) == 5000
    assert underground_blood_money(5, 4, rate) == 2500
    assert underground_blood_money(5, 5, rate) == 1250
    assert underground_blood_money(5, 7, rate) == 1250


async def test_vehicle_type_violation_text():
    assert "Sedan" in vehicle_type_violation("Sedan", ["Small", "Pickup"])
    assert "Small & Pickup" in vehicle_type_violation("Sedan", ["Small", "Pickup"])
    assert "verified" in vehicle_type_violation(None, ["Small", "Pickup"])
    # Custom class wording
    assert "Truck & Bus" in vehicle_type_violation(None, ["Truck", "Bus"])


# --------------------------------------------------------------------------
# Rotation-end payout
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("amc.events.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc.events.check_treasury_floor", new_callable=AsyncMock, return_value=False)
@patch("amc.events.transfer_money", new_callable=AsyncMock)
async def test_payout_skipped_when_treasury_at_floor(transfer_mock, floor_mock, ledger_mock, monkeypatch, db):
    """Treasury-funded payouts: when the Treasury Fund would breach its
    floor, BOTH the game transfer and the ledger entry are skipped (same
    gating pattern as subsidise_player) — the event still settles once."""
    monkeypatch.setattr(config_mod, "BLOOD_MONEY_PER_CHECKPOINT", 4000)
    event, _mirror = await _underground_world("5")
    await _racer(event, "A0005", 501, "BrokeGovt", laps=1, finished=True, net_time=3.0)
    await pay_underground_rotation_rewards(
        {"http_client_mod": object()}, live_guids=set()
    )
    transfer_mock.assert_not_awaited()
    ledger_mock.assert_not_awaited()
    await event.arefresh_from_db()
    assert event.rewards_paid is True  # settled regardless
    await _cleanup_world(event, _mirror)


async def _underground_world(suffix: str):
    """Championship + template + mirror SE + a posted underground event
    whose setup has 4 waypoints. Returns (game_event, mirror).

    `suffix` is unique per test: pytest-django does not guarantee cross-test
    cleanup on the async ORM connection path, so tests are isolated by
    unique route hash / GUIDs instead of a wipe."""
    route = {**ROUTE, "Route": {**ROUTE["Route"], "RouteName": f"Underground Payout Route {suffix}"}}
    champ, _ = await sync_to_async(Championship.objects.get_or_create)(
        name=UNDERGROUND_CHAMPIONSHIP_NAME, defaults={"description": ""}
    )
    setup = await sync_to_async(RaceSetup.objects.create)(
        config=route, hash=RaceSetup.calculate_hash(route), name=f"Underground Payout Route {suffix}"
    )
    template = await sync_to_async(ScheduledEvent.objects.create)(
        name="Payout Track - Illegal TT",
        race_setup=setup,
        time_trial=True,
        championship=champ,
        start_time=timezone.now(),
        end_time=timezone.now() + timezone.timedelta(days=7),
    )
    mirror = await sync_to_async(ScheduledEvent.objects.create)(
        name="Payout Track - Illegal TT (001) [TT-270]",
        race_setup=setup,
        time_trial=True,
        championship=champ,
        start_time=timezone.now(),
        end_time=timezone.now() + timezone.timedelta(days=7),
        tt_class=(await sync_to_async(TTClass.objects.get_or_create)(
            name="TT-270", defaults={"max_hp": 270}
        ))[0],
        is_rotation_instance=True,
    )
    event = await sync_to_async(GameEvent.objects.create)(
        guid=f"GUIDPAY0000000000000{suffix}",  # unique per test, <=32 chars
        name="Payout Track - Illegal TT (001) [TT-270]",
        state=3,
        auto_created=True,
        scheduled_event=mirror,
        race_setup=setup,
    )
    _ = template
    return event, mirror


async def _cleanup_world(event, mirror):
    """Explicit end-of-test deletion: this stack's async ORM rows persist
    across tests (pytest-asyncio can't run async autouse fixtures here), so
    every test removes what it created to avoid polluting other suites
    (parts-audit 'most recent event' picks, etc.)."""
    await sync_to_async(event.delete)()
    await sync_to_async(mirror.delete)()
    await sync_to_async(
        RaceSetup.objects.filter(name__startswith="Underground Payout Route").delete
    )()


async def _racer(event, guid_suffix, unique_id, name, laps, finished, net_time):
    character, *_ = await Character.objects.aget_or_create_character_player(
        name, int(unique_id), character_guid=f"GUIDCHR{unique_id:06d}"
    )
    await sync_to_async(GameEventCharacter.objects.create)(
        game_event=event,
        character=character,
        rank=0,
        laps=laps,
        finished=finished,
        last_section_total_time_seconds=net_time,
        first_section_total_time_seconds=0.0,
    )
    return character


@pytest.mark.asyncio
@patch("amc.events.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc.events.check_treasury_floor", new_callable=AsyncMock, return_value=True)
@patch("amc.events.transfer_money", new_callable=AsyncMock)
async def test_payout_ladder_by_finish_order(transfer_mock, floor_mock, ledger_mock, monkeypatch, db):
    monkeypatch.setattr(config_mod, "BLOOD_MONEY_PER_CHECKPOINT", 4000)
    event, _mirror = await _underground_world("1")
    await _racer(event, "A0001", 101, "Racer1", laps=1, finished=True, net_time=10.0)
    await _racer(event, "B0002", 102, "Racer2", laps=1, finished=True, net_time=20.0)
    await _racer(event, "C0003", 103, "Racer3", laps=1, finished=True, net_time=30.0)
    await _racer(event, "D0004", 104, "Racer4", laps=1, finished=True, net_time=40.0)
    await _racer(event, "E0005", 105, "Racer5", laps=1, finished=True, net_time=50.0)
    await _racer(event, "F0006", 106, "Racer6", laps=1, finished=True, net_time=60.0)

    await pay_underground_rotation_rewards(
        {"http_client_mod": object()}, live_guids=set()
    )

    amounts = [call.args[1] for call in transfer_mock.await_args_list]
    rate = 4000  # 4 checkpoints: 16000, 8000, 4000, 2000, 1000, 1000
    assert amounts == [
        4 * rate, 2 * rate, rate, rate // 2, rate // 4, rate // 4,
    ]
    # Treasury pipe: every game transfer is mirrored by a ledger entry
    # (Dr Treasury Expenses / Cr Treasury Fund — the government loses it).
    ledger_amounts = [call.args[0] for call in ledger_mock.await_args_list]
    assert ledger_amounts == amounts
    assert ledger_mock.await_count == transfer_mock.await_count == 6
    await event.arefresh_from_db()
    assert event.rewards_paid is True
    await _cleanup_world(event, _mirror)


@pytest.mark.asyncio
@patch("amc.events.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc.events.check_treasury_floor", new_callable=AsyncMock, return_value=True)
@patch("amc.events.transfer_money", new_callable=AsyncMock)
async def test_payout_skips_non_racers_and_live_events(transfer_mock, floor_mock, ledger_mock, monkeypatch, db):
    monkeypatch.setattr(config_mod, "BLOOD_MONEY_PER_CHECKPOINT", 4000)
    event, _mirror = await _underground_world("2")
    # Lobby joiner who never raced -> no money.
    await _racer(event, "A0001", 201, "LobbyGuy", laps=0, finished=False, net_time=None)
    await pay_underground_rotation_rewards(
        {"http_client_mod": object()}, live_guids=set()
    )
    transfer_mock.assert_not_awaited()
    await event.arefresh_from_db()
    assert event.rewards_paid is True

    # A still-live event is never paid.
    transfer_mock.reset_mock()
    event2 = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDPAY0000000000000000000000002",
        name="Payout Track - Illegal TT (002) [TT-270]",
        state=1,
        auto_created=True,
        scheduled_event=_mirror,
        race_setup_id=event.race_setup_id,
    )
    await _racer(event2, "B0002", 202, "RacerX", laps=1, finished=True, net_time=5.0)
    await pay_underground_rotation_rewards(
        {"http_client_mod": object()}, live_guids={event2.guid}
    )
    transfer_mock.assert_not_awaited()
    await event2.arefresh_from_db()
    assert event2.rewards_paid is False
    # Async ORM rows persist across tests on this stack — remove the
    # deliberately-unpaid event2 so it can't leak into the next test's
    # unpaid set.
    await sync_to_async(event2.delete)()
    await _cleanup_world(event, _mirror)


@pytest.mark.asyncio
@patch("amc.events.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc.events.check_treasury_floor", new_callable=AsyncMock, return_value=True)
@patch("amc.events.transfer_money", new_callable=AsyncMock)
async def test_payout_is_idempotent(transfer_mock, floor_mock, ledger_mock, monkeypatch, db):
    monkeypatch.setattr(config_mod, "BLOOD_MONEY_PER_CHECKPOINT", 4000)
    event, _mirror = await _underground_world("3")
    await _racer(event, "A0001", 301, "Solo", laps=1, finished=True, net_time=7.0)
    ctx = {"http_client_mod": object()}
    await pay_underground_rotation_rewards(ctx, live_guids=set())
    assert transfer_mock.await_count == 1
    # Row already claimed -> second pass pays nothing again.
    await pay_underground_rotation_rewards(ctx, live_guids=set())
    assert transfer_mock.await_count == 1
    await _cleanup_world(event, _mirror)


@pytest.mark.asyncio
@patch("amc.events.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc.events.check_treasury_floor", new_callable=AsyncMock, return_value=True)
@patch("amc.events.transfer_money", new_callable=AsyncMock)
async def test_payout_transfer_failure_contained(transfer_mock, floor_mock, ledger_mock, monkeypatch, db):
    monkeypatch.setattr(config_mod, "BLOOD_MONEY_PER_CHECKPOINT", 4000)
    event, _mirror = await _underground_world("4")
    c1 = await _racer(event, "A0001", 401, "Winner", laps=1, finished=True, net_time=1.0)
    c2 = await _racer(event, "B0002", 402, "RunnerUp", laps=1, finished=True, net_time=2.0)
    transfer_mock.side_effect = [
        None,  # winner paid
        RuntimeError("mod down"),  # runner-up transfer fails
    ]
    await pay_underground_rotation_rewards(
        {"http_client_mod": object()}, live_guids=set()
    )
    assert transfer_mock.await_count == 2
    await event.arefresh_from_db()
    assert event.rewards_paid is True  # settled once, failure logged not retried
    await c1.arefresh_from_db()
    await c2.arefresh_from_db()
    assert c1.respect == 0  # RESPECT_PER_CHECKPOINT = 0 today


@pytest.mark.asyncio
@patch("amc.events.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc.events.check_treasury_floor", new_callable=AsyncMock, return_value=True)
@patch("amc.events.transfer_money", new_callable=AsyncMock)
async def test_respect_pipes_to_criminal_score(transfer_mock, floor_mock, ledger_mock, monkeypatch, db):
    """Yuuka 2026-09-30: underground respect feeds the CRIMINAL score —
    the rap sheet counts the race and the criminal level derives live
    from the score (same accrual pattern as illicit deliveries)."""
    monkeypatch.setattr(config_mod, "BLOOD_MONEY_PER_CHECKPOINT", 4000)
    monkeypatch.setattr(events_mod, "RESPECT_PER_CHECKPOINT", 100)
    event, _mirror = await _underground_world("6")
    char = await _racer(event, "A0006", 601, "RapSheet", laps=1, finished=True, net_time=4.0)
    await Character.objects.filter(pk=char.pk).aupdate(criminal_score=50)
    await pay_underground_rotation_rewards(
        {"http_client_mod": object()}, live_guids=set()
    )
    await char.arefresh_from_db()
    # 4 checkpoints x 100 respect + the seeded 50 score
    assert char.respect == 400
    assert char.criminal_score == 450
    await _cleanup_world(event, _mirror)


# --------------------------------------------------------------------------
# Vehicle-type DQ (Small/Pickup only, fail-closed)
# --------------------------------------------------------------------------


def _player(guid, unique_id, name):
    return {
        "CharacterId": {"CharacterGuid": guid, "UniqueNetId": unique_id},
        "PlayerName": name,
    }


async def _classed_event(vehicle_types=None, se_vehicle_types=None):
    tt, _ = await sync_to_async(TTClass.objects.get_or_create)(
        name="TT-270", defaults={"max_hp": 270}
    )
    if vehicle_types is not None:
        # get_or_create ignores defaults on an existing row (earlier tests
        # in this file create TT-270 with the field default) — set it.
        tt.allowed_vehicle_types = vehicle_types
        await tt.asave(update_fields=["allowed_vehicle_types"])
    mirror = None
    if se_vehicle_types is not None:
        mirror = await sync_to_async(ScheduledEvent.objects.create)(
            name="VType SE [TT-270]",
            race_setup=(await sync_to_async(RaceSetup.objects.create)(
                name="VType Setup",
                hash=RaceSetup.calculate_hash({}) + f"-{se_vehicle_types}",
                config={},
            )),
            time_trial=True,
            start_time=timezone.now(),
            end_time=timezone.now() + timezone.timedelta(days=1),
            tt_class=tt,
            allowed_vehicle_types=se_vehicle_types,
        )
    event = await sync_to_async(GameEvent.objects.create)(
        guid="GUIDVTYPE00000000000000000000001",
        name="VType Event [TT-270]",
        state=2,
        tt_class=tt,
        scheduled_event=mirror,
    )
    if mirror is not None:
        event.scheduled_event = mirror
        await sync_to_async(event.save)(update_fields=["scheduled_event"])
    return event


_OK_PARTS = [{"Slot": 2, "Key": "SmallBlock_240HP"}, {"Slot": 19, "Key": "201"}]


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq.vehicle_type_for", return_value="Sedan")
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_wrong_vehicle_type_kicked(
    kick, last_veh, last_parts, post, _vtype, db
):
    event = await _classed_event()
    last_veh.return_value = {"vehicle": {"fullName": "Sedan_X_C"}}
    last_parts.return_value = {"parts": _OK_PARTS}  # parts are legal
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDVT000000000000000000000001", "U9", "Trucker")]},
        None,
    )
    assert out == ["Trucker"]
    kick.assert_awaited_once()
    await sync_to_async(event.delete)()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq.vehicle_type_for", return_value=None)
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_unknown_vehicle_type_kicked_fail_closed(
    kick, last_veh, last_parts, post, _vtype, db
):
    event = await _classed_event()
    last_veh.return_value = {"vehicle": {"fullName": "Mystery_X_C"}}
    last_parts.return_value = {"parts": _OK_PARTS}
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDVT000000000000000000000002", "U8", "Ghost")]},
        None,
    )
    assert out == ["Ghost"]
    kick.assert_awaited_once()
    await sync_to_async(event.delete)()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq.vehicle_type_for", return_value="Pickup")
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_pickup_type_not_kicked(kick, last_veh, last_parts, post, _vtype, db):
    event = await _classed_event()
    last_veh.return_value = {"vehicle": {"fullName": "Pickup_X_C"}}
    last_parts.return_value = {"parts": _OK_PARTS}
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDVT000000000000000000000003", "U7", "Hauler")]},
        None,
    )
    assert out == []
    kick.assert_not_awaited()
    await sync_to_async(event.delete)()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq.vehicle_type_for", return_value="Truck")
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_class_allowlisted_vehicle_type_not_kicked(
    kick, last_veh, last_parts, post, _vtype, db
):
    """A class whose allowed_vehicle_types includes Truck admits Trucks
    (Yuuka 2026-10-02: allowed types are per-class data)."""
    event = await _classed_event(vehicle_types=["Small", "Truck"])
    last_veh.return_value = {"vehicle": {"fullName": "Trucker_X_C"}}
    last_parts.return_value = {"parts": _OK_PARTS}
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDVT000000000000000000000004", "U6", "Trucker")]},
        None,
    )
    assert out == []
    kick.assert_not_awaited()
    await sync_to_async(event.delete)()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq.vehicle_type_for", return_value="Truck")
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_se_override_admits_vehicle_class_excludes(
    kick, last_veh, last_parts, post, _vtype, db
):
    """Per-event override on the ScheduledEvent wins over the class list
    (Yuuka 2026-10-03): class = Small only, SE override adds Truck."""
    event = await _classed_event(vehicle_types=["Small"], se_vehicle_types=["Truck"])
    last_veh.return_value = {"vehicle": {"fullName": "Trucker_X_C"}}
    last_parts.return_value = {"parts": _OK_PARTS}
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDVT000000000000000000000005", "U7", "Trucker2")]},
        None,
    )
    assert out == []
    kick.assert_not_awaited()
    await sync_to_async(event.delete)()
    await sync_to_async(event.scheduled_event.delete)()


@pytest.mark.asyncio
@patch("amc.handlers.tt_dq.vehicle_type_for", return_value="Truck")
@patch("amc.handlers.tt_dq._post_audit_embed", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle_parts", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.get_player_last_vehicle", new_callable=AsyncMock)
@patch("amc.handlers.tt_dq.kick_player_from_event", new_callable=AsyncMock)
async def test_empty_se_override_falls_back_to_class(
    kick, last_veh, last_parts, post, _vtype, db
):
    """Empty/null SE override = inherit the class list."""
    event = await _classed_event(vehicle_types=["Small"], se_vehicle_types=[])
    last_veh.return_value = {"vehicle": {"fullName": "Trucker_X_C"}}
    last_parts.return_value = {"parts": _OK_PARTS}
    out = await _disqualify_illegal_starters(
        object(),
        event,
        {"Players": [_player("GUIDVT000000000000000000000006", "U8", "Trucker3")]},
        None,
    )
    assert out and "Truck" in out[0]
    kick.assert_awaited_once()
    await sync_to_async(event.delete)()
    await sync_to_async(event.scheduled_event.delete)()