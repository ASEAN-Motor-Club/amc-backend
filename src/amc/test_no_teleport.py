"""Tests for the backend-pushed invisible no-teleport flag (freeman 2026-09-23).

The mod (MTDediMod NoTeleportManager) blocks teleport RPCs for GUIDs the
backend pushes. Backend rules tested here:
- grace-window trigger pushes the flag ON at pending creation;
- apply (Wanted created) pushes it OFF;
- dormant drop (pending deleted) pushes it OFF;
- push_no_teleport is best-effort (swallows mod-client failures).
"""

import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.criminals import tick_wanted_countdown
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import PendingWanted, Wanted
from amc.no_teleport import push_no_teleport
from amc.webhook import process_event


def _sync_create(factory_or_model, **kwargs):
    if hasattr(factory_or_model, "objects"):
        target = factory_or_model.objects.create
    else:
        target = factory_or_model

    async def _run(**run_kwargs):
        return await sync_to_async(target)(**run_kwargs)

    if kwargs:
        return _run(**kwargs)
    if hasattr(factory_or_model, "objects"):
        return _run()
    return _run


def _cargo_event(character, cargo_key, payment=5000):
    return {
        "hook": "ServerCargoArrived",
        "timestamp": int(time.time()),
        "data": {
            "CharacterGuid": str(character.guid),
            "Cargos": [
                {
                    "Net_CargoKey": cargo_key,
                    "Net_Payment": payment,
                    "Net_Weight": 10.0,
                    "Net_Damage": 0.0,
                    "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                    "Net_DestinationLocation": {"X": 100, "Y": 100, "Z": 0},
                }
            ],
        },
    }


async def _make_character(score=50_000):
    player = await _sync_create(PlayerFactory)()
    return await _sync_create(CharacterFactory)(
        player=player, criminal_score=score
    )


class PushNoTeleportTests(TestCase):
    async def test_push_failure_is_swallowed(self):
        character = await _make_character()
        with patch(
            "amc.mod_server.set_no_teleport",
            new_callable=AsyncMock,
            side_effect=Exception("mod down"),
        ):
            # must not raise
            await push_no_teleport(character, AsyncMock(), True)

    async def test_push_without_client_or_guid_is_noop(self):
        character = await _make_character()
        with patch(
            "amc.mod_server.set_no_teleport", new_callable=AsyncMock
        ) as mock_set:
            await push_no_teleport(character, None, True)
            mock_set.assert_not_awaited()


class EffectiveFlagSyncTests(TestCase):
    """sync_no_teleport: effective = manual OR wanted OR on-duty police."""

    async def _make(self, **char_kwargs):
        player = await _sync_create(PlayerFactory)()
        return await _sync_create(CharacterFactory)(
            player=player, criminal_score=50_000, **char_kwargs
        )

    async def _sync(self, character):
        from amc.no_teleport import sync_no_teleport

        with patch(
            "amc.mod_server.set_no_teleport", new_callable=AsyncMock
        ) as mock_set:
            await sync_no_teleport(character, AsyncMock())
        self.assertTrue(mock_set.await_args, "push never fired")
        return mock_set.await_args[0][2]

    async def test_nothing_active_pushes_false(self):
        character = await self._make()
        assert await self._sync(character) is False

    async def test_manual_flag_pushes_true(self):
        character = await self._make(no_teleport=True)
        assert await self._sync(character) is True

    async def test_active_wanted_pushes_true(self):
        from amc.models import Wanted

        character = await self._make()
        await _sync_create(
            Wanted, character=character, wanted_remaining=600, amount=0
        )
        assert await self._sync(character) is True

    async def test_on_duty_police_pushes_true(self):
        from amc.models import PoliceSession

        character = await self._make()
        await _sync_create(PoliceSession, character=character)
        assert await self._sync(character) is True

    async def test_wanted_creation_pushes_flag(self):
        from amc.criminals import create_or_refresh_wanted

        character = await self._make()
        with patch(
            "amc.no_teleport.push_no_teleport", new_callable=AsyncMock
        ) as mock_push:
            await create_or_refresh_wanted(character, AsyncMock(), amount=0)
        mock_push.assert_awaited()
        self.assertIs(mock_push.await_args_list[0][0][2], True)

    async def test_police_activation_pushes_flag(self):
        from amc.police import activate_police, deactivate_police

        character = await self._make()
        mod = AsyncMock()
        with patch(
            "amc.no_teleport.push_no_teleport", new_callable=AsyncMock
        ) as mock_push:
            await activate_police(character, mod)
            await deactivate_police(character, mod)
        states = [c.args[2] for c in mock_push.await_args_list]
        self.assertEqual(states, [True, False])


class GraceWindowFlagTests(TestCase):
    """The grace window pushes the flag; apply and dormant-drop clear it."""

    async def test_trigger_pushes_flag(self):
        character = await _make_character()
        mod_client = AsyncMock()

        with (
            patch(
                "amc.webhook.get_rp_mode", new_callable=AsyncMock, return_value=False
            ),
            patch(
                "amc.webhook.get_treasury_fund_balance",
                new_callable=AsyncMock,
                return_value=100_000,
            ),
            patch(
                "amc.handlers.cargo.accumulate_illicit_delivery",
                new_callable=AsyncMock,
                return_value=100_000,
            ),
            patch(
                "amc.handlers.cargo.nearest_effective_cop_distance_m",
                new_callable=AsyncMock,
                return_value=(True, None),
            ),
            patch(
                "amc.handlers.cargo._check_modded_vehicle",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "amc.handlers.cargo.show_popup", new_callable=AsyncMock
            ),
            patch("amc.criminals.refresh_player_name", new_callable=AsyncMock),
            patch("amc.criminals.send_system_message", new_callable=AsyncMock),
            patch(
                "amc.handlers.cargo.push_no_teleport_later"
            ) as mock_push,
            patch("amc.special_cargo.random") as mock_rng,
        ):
            mock_rng.random.return_value = 0.0  # passes any chance
            await process_event(
                _cargo_event(character, "Ganja"),
                player := await _sync_create(PlayerFactory)(),
                character,
                http_client_mod=mod_client,
            )

        mock_push.assert_called_once_with(character, mod_client, True)
        self.assertIsNotNone(
            await PendingWanted.objects.filter(character=character).afirst()
        )
        assert player  # keeps the factory row alive for the FK

    def _tick_patches(self, armed):
        return (
            patch(
                "amc.criminals.active_police_present",
                new_callable=AsyncMock,
                return_value=armed,
            ),
            patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=[]),
            patch("amc.criminals.refresh_player_name", new_callable=AsyncMock),
            patch("amc.criminals.send_system_message", new_callable=AsyncMock),
            patch("amc.criminals.make_suspect", new_callable=AsyncMock),
            patch(
                "amc.criminals.get_player_last_vehicle",
                new_callable=AsyncMock,
                return_value={"vehicle": None},
            ),
            patch(
                "amc.criminals.get_player_last_vehicle_parts",
                new_callable=AsyncMock,
                return_value={"parts": []},
            ),
            patch(
                "amc.special_cargo.announce_illicit_delivery",
                new_callable=AsyncMock,
            ),
            patch("amc.criminals.push_no_teleport", new_callable=AsyncMock),
        )

    async def test_apply_clears_flag(self):
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(
            player=player, criminal_score=50_000
        )
        await _sync_create(
            PendingWanted,
            character=character,
            apply_at=timezone.now() - timedelta(seconds=1),
            trigger_amount=100_000,
        )
        patches = self._tick_patches(armed=True)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patches[5], patches[6], patches[7],
            patches[8] as mock_push,
        ):
            await tick_wanted_countdown(AsyncMock(), AsyncMock())

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        mock_push.assert_awaited_once()
        self.assertEqual(mock_push.await_args[0][0].pk, character.pk)
        self.assertIs(mock_push.await_args[0][2], False)

    async def test_dormant_drop_clears_flag(self):
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(
            player=player, criminal_score=50_000
        )
        await _sync_create(
            PendingWanted,
            character=character,
            apply_at=timezone.now() - timedelta(seconds=1),
            trigger_amount=100_000,
        )
        patches = self._tick_patches(armed=False)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patches[5], patches[6], patches[7],
            patches[8] as mock_push,
        ):
            await tick_wanted_countdown(AsyncMock(), AsyncMock())

        self.assertFalse(
            await PendingWanted.objects.filter(character=character).aexists()
        )
        mock_push.assert_awaited_once()
        self.assertEqual(mock_push.await_args[0][0].pk, character.pk)
        self.assertIs(mock_push.await_args[0][2], False)
