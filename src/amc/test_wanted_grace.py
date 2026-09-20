"""Tests for the wanted-trigger grace period (freeman 2026-09-20).

A rolled illicit-cargo trigger does NOT create the Wanted row immediately:
the criminal gets a private warning popup (WANTED_GRACE_POPUP) and
WANTED_GRACE_SECONDS to switch to a suitable vehicle. tick_wanted_countdown
applies the wanted (bounty = 10% of score + laundered announce) when the
window elapses, drops due pending rows while the system is dormant (zero
effective cops), and logging out during the window is an arrest with no
proximity gate (the criminal was warned privately — no dodge).
"""

import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from amc.criminals import (
    WANTED_GRACE_POPUP,
    WANTED_GRACE_SECONDS,
    escalate_heat_on_logout,
    tick_wanted_countdown,
)
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import PendingWanted, Wanted
from amc.webhook import process_event


def _sync_create(factory_or_model, **kwargs):
    """Create a DB row on the sync side (same helper as test_criminal_score)."""
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


class GracePeriodApplyTests(TestCase):
    """tick_wanted_countdown applies due pending triggers when armed."""

    async def _make_pending(self, score=50_000, *, due=True, trigger_amount=100_000):
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(
            player=player, criminal_score=score
        )
        apply_at = timezone.now() + (
            timedelta(seconds=-1) if due else timedelta(seconds=WANTED_GRACE_SECONDS)
        )
        pending = await _sync_create(
            PendingWanted,
            character=character,
            apply_at=apply_at,
            trigger_amount=trigger_amount,
        )
        return character, pending

    @staticmethod
    def _tick_patches(armed=True):
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
                "amc.special_cargo._announce_laundered_after_delay",
                new_callable=AsyncMock,
            ),
        )

    async def test_due_pending_applies_wanted_with_bounty(self):
        character, _pending = await self._make_pending(score=50_000)
        patches = self._tick_patches(armed=True)
        with (
            patches[0] as mock_armed,
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as mock_announce,
        ):
            await tick_wanted_countdown(AsyncMock(), AsyncMock())

        mock_armed.assert_awaited_once()
        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        # Bounty = 10% of the criminal score at apply (creation path).
        self.assertEqual(wanted.amount, 5_000)
        self.assertEqual(wanted.wanted_remaining, Wanted.INITIAL_WANTED_LEVEL)
        # The pending row is consumed.
        still_pending = await PendingWanted.objects.filter(
            character=character
        ).aexists()
        self.assertFalse(still_pending)
        # Police notice: the laundered announce carries the frozen trigger total.
        cached = await cache.aget(f"money_laundered:{character.guid}")
        self.assertEqual(cached["total"], 100_000)
        self.assertEqual(cached["name"], character.name)
        mock_announce.assert_awaited_once()

    async def test_future_pending_not_applied(self):
        character, _pending = await self._make_pending(due=False)
        patches = self._tick_patches(armed=True)
        with (
            patches[0] as mock_armed,
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as mock_announce,
        ):
            await tick_wanted_countdown(AsyncMock(), AsyncMock())

        mock_armed.assert_not_awaited()  # early return — nothing due
        still_pending = await PendingWanted.objects.filter(
            character=character
        ).aexists()
        self.assertTrue(still_pending)
        wanted_exists = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).aexists()
        self.assertFalse(wanted_exists)
        mock_announce.assert_not_awaited()

    async def test_dormant_tick_drops_due_pending(self):
        character, _pending = await self._make_pending()
        patches = self._tick_patches(armed=False)
        with (
            patches[0] as mock_armed,
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as mock_announce,
        ):
            await tick_wanted_countdown(AsyncMock(), AsyncMock())

        mock_armed.assert_awaited_once()
        # Dormant rule: the trigger never applies with zero effective cops.
        still_pending = await PendingWanted.objects.filter(
            character=character
        ).aexists()
        self.assertFalse(still_pending)
        wanted_exists = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).aexists()
        self.assertFalse(wanted_exists)
        mock_announce.assert_not_awaited()

    async def test_apply_with_admin_flag_refreshes_without_repricing(self):
        """An admin /setwanted flag landing during the grace window absorbs the
        apply as a refresh: countdown reset, flag-only amount untouched."""
        character, _pending = await self._make_pending(score=50_000)
        officer = await _sync_create(CharacterFactory)(name="FlagCop")
        await _sync_create(
            Wanted,
            character=character,
            wanted_remaining=300,
            amount=0,
            set_by=officer,
        )
        patches = self._tick_patches(armed=True)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as mock_announce,
        ):
            await tick_wanted_countdown(AsyncMock(), AsyncMock())

        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        self.assertEqual(wanted.wanted_remaining, Wanted.INITIAL_WANTED_LEVEL)
        self.assertEqual(wanted.amount, 0)  # admin flag stays bounty-free
        still_pending = await PendingWanted.objects.filter(
            character=character
        ).aexists()
        self.assertFalse(still_pending)
        mock_announce.assert_not_awaited()  # refresh, not a creation


class GracePeriodLogoutTests(TestCase):
    """Logging out inside the grace window is an arrest, full stop."""

    async def _make_pending(self, score=50_000):
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(
            player=player, criminal_score=score
        )
        pending = await _sync_create(
            PendingWanted,
            character=character,
            apply_at=timezone.now() + timedelta(seconds=WANTED_GRACE_SECONDS),
            trigger_amount=100_000,
        )
        return character, pending

    @patch("amc.criminals.execute_arrest", new_callable=AsyncMock)
    async def test_logout_during_grace_is_arrest(self, mock_arrest):
        mock_arrest.return_value = (["Runner"], 5_000)
        character, _pending = await self._make_pending(score=50_000)

        await escalate_heat_on_logout(character, AsyncMock(), AsyncMock())

        mock_arrest.assert_awaited_once()
        arrest_kwargs = mock_arrest.await_args[1]
        self.assertIn("flagged as wanted", arrest_kwargs["reason"])
        # The pending was promoted so the arrest confiscates the bounty.
        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)
        self.assertEqual(wanted.amount, 5_000)  # 10% of the 50k score
        still_pending = await PendingWanted.objects.filter(
            character=character
        ).aexists()
        self.assertFalse(still_pending)

    async def test_logout_during_grace_without_mod_client_keeps_wanted(self):
        character, _pending = await self._make_pending(score=50_000)

        with patch(
            "amc.criminals.execute_arrest", new_callable=AsyncMock
        ) as mock_arrest:
            await escalate_heat_on_logout(character, AsyncMock(), None)

        mock_arrest.assert_not_awaited()
        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)  # flagged anyway — no dodge
        still_pending = await PendingWanted.objects.filter(
            character=character
        ).aexists()
        self.assertFalse(still_pending)

    @patch("amc.criminals.execute_arrest", new_callable=AsyncMock)
    @patch("amc.criminals.get_players", new_callable=AsyncMock, return_value=[])
    async def test_wanted_logout_without_pending_unchanged(
        self, mock_get_players, mock_arrest
    ):
        """The pending gate must not hijack the normal wanted-logout path."""
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(player=player)
        await _sync_create(
            Wanted,
            character=character,
            wanted_remaining=600,
            amount=2_000,
        )

        await escalate_heat_on_logout(character, AsyncMock(), AsyncMock())

        mock_arrest.assert_not_awaited()  # no location data → early return
        still_pending = await PendingWanted.objects.filter(
            character=character
        ).aexists()
        self.assertFalse(still_pending)
        wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).afirst()
        self.assertIsNotNone(wanted)


class GracePeriodPopupTests(TestCase):
    """Handler-level: a rolled trigger sends the private warning popup."""

    async def test_roll_hit_sends_grace_popup(self):
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(player=player)
        mod_client = AsyncMock()

        with (
            patch(
                "amc.webhook.get_rp_mode",
                new_callable=AsyncMock,
                return_value=False,
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
            ) as mock_popup,
            patch("amc.criminals.refresh_player_name", new_callable=AsyncMock),
            patch("amc.criminals.send_system_message", new_callable=AsyncMock),
            patch("amc.special_cargo.random") as mock_rng,
        ):
            mock_rng.random.return_value = 0.0  # passes any chance
            await process_event(
                _cargo_event(character, "Ganja"),
                player,
                character,
                http_client_mod=mod_client,
            )

        mock_popup.assert_awaited_once()
        self.assertIs(mock_popup.await_args[0][0], mod_client)
        self.assertEqual(mock_popup.await_args[0][1], WANTED_GRACE_POPUP)
        self.assertEqual(
            mock_popup.await_args[1]["character_guid"], character.guid
        )
        pending = await PendingWanted.objects.filter(character=character).afirst()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.trigger_amount, 100_000)
        wanted_exists = await Wanted.objects.filter(
            character=character, expired_at__isnull=True
        ).aexists()
        self.assertFalse(wanted_exists)
