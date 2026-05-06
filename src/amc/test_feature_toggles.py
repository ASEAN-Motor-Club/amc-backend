"""Tests for the wealth/tax + subsidy-protection + responsive-scaling
feature toggles in `amc.config`.

When all three flags are False, the corresponding code paths short-circuit
to the pre-overlay (master before econ-update1) behavior.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc import config
from amc.factories import CharacterFactory, PlayerFactory
from amc.jobs import calculate_treasury_scale, compute_payout_factor_for_character
from amc.models import DeliveryPoint
from amc.subsidies import (
    apply_subsidy_player_cuts,
    clamp_subsidy_for_treasury_health,
)
from amc.tax import apply_tax_player_cuts, get_tax_for_cargo


class ResponsiveScalingToggleTest(TestCase):
    def test_treasury_scale_neutral_when_disabled(self):
        # Off -> always 1.0; matches master where bonus_multiplier and
        # completion_bonus aren't reshaped by treasury balance.
        with patch.object(config, "RESPONSIVE_SCALING_ENABLED", False):
            self.assertEqual(calculate_treasury_scale(0), 1.0)
            self.assertEqual(calculate_treasury_scale(1), 1.0)
            self.assertEqual(calculate_treasury_scale(10**12), 1.0)

    def test_treasury_scale_curve_active_when_enabled(self):
        # Live curve sanity-check so the disabled-mode test isn't a
        # false positive (constants forced for determinism).
        with patch.object(config, "RESPONSIVE_SCALING_ENABLED", True), \
             patch.object(config, "TREASURY_FLOOR", 50_000_000), \
             patch.object(config, "TREASURY_CEILING", 150_000_000), \
             patch.object(config, "TREASURY_CURVE_EXPONENT", 0.7), \
             patch.object(config, "TREASURY_BOOM_CAP", 2.0):
            self.assertEqual(calculate_treasury_scale(0), 0.0)
            mid = calculate_treasury_scale(100_000_000)
            self.assertGreater(mid, 0.0)
            self.assertLess(mid, 1.0)


class WealthTaxToggleTest(TestCase):
    def setUp(self):
        self.point_in = DeliveryPoint.objects.create(
            guid="ft_in", name="In", type="T",
            coord=Point(0, 0, 0, srid=3857),
        )
        self.point_out = DeliveryPoint.objects.create(
            guid="ft_out", name="Out", type="T",
            coord=Point(1, 1, 0, srid=3857),
        )

    async def _make_character(self, is_gov=False):
        player = await sync_to_async(PlayerFactory)()
        kwargs = {"player": player, "name": "WT"}
        if is_gov:
            kwargs["gov_employee_until"] = timezone.now() + timedelta(days=7)
        return await sync_to_async(CharacterFactory)(**kwargs)

    async def test_get_tax_for_cargo_returns_zero_when_disabled(self):
        # Disabled tax overlay returns the same (0, 0.0, None) the master
        # branch produced before TaxRule was a thing.
        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Coal"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = self.point_in
        mock_cargo.destination_point = self.point_out
        mock_cargo.data = {}
        mock_cargo.damage = 0.0

        with patch.object(config, "WEALTH_TAX_SYSTEM_ENABLED", False):
            amount, factor, rule = await get_tax_for_cargo(mock_cargo)
        self.assertEqual(amount, 0)
        self.assertEqual(factor, 0.0)
        self.assertIsNone(rule)

    async def test_apply_tax_player_cuts_zero_when_disabled(self):
        char = await self._make_character()
        with patch.object(config, "WEALTH_TAX_SYSTEM_ENABLED", False):
            cut = await apply_tax_player_cuts(
                10_000, char, treasury_balance=100_000_000,
            )
        self.assertEqual(cut, 0)

    async def test_subsidy_player_cuts_skip_wealth_curve_when_disabled(self):
        # Pre-overlay master: gov skip + treasury hard cap + modded mult
        # only. No wealth lookup, no wealth ramp.
        char = await self._make_character()
        with patch.object(config, "WEALTH_TAX_SYSTEM_ENABLED", False), \
             patch("amc.subsidies.compute_wealth_state", new_callable=AsyncMock) as mock_ws:
            result = await apply_subsidy_player_cuts(
                subsidy=100_000,
                character=char,
                http_client_mod=None,
                treasury_balance=10**12,
                wealth_state=None,
            )
        self.assertEqual(result, 100_000)
        mock_ws.assert_not_called()

    async def test_subsidy_player_cuts_gov_skip_preserved_when_disabled(self):
        # Gov skip predates the overlay and must keep working.
        char = await self._make_character(is_gov=True)
        with patch.object(config, "WEALTH_TAX_SYSTEM_ENABLED", False):
            result = await apply_subsidy_player_cuts(
                subsidy=100_000,
                character=char,
                http_client_mod=None,
                treasury_balance=10**12,
            )
        self.assertEqual(result, 0)

    async def test_payout_factor_neutral_when_disabled(self):
        char = await self._make_character()
        with patch.object(config, "WEALTH_TAX_SYSTEM_ENABLED", False):
            factor = await compute_payout_factor_for_character(
                char, treasury_balance=0.0,
            )
        self.assertEqual(factor, 1.0)


class SubsidyProtectionToggleTest(TestCase):
    async def _make_character(self, **kwargs):
        player = await sync_to_async(PlayerFactory)()
        return await sync_to_async(CharacterFactory)(
            player=player, name="SP", **kwargs,
        )

    async def test_clamp_passthrough_when_disabled(self):
        # Off -> subsidy returned unchanged even with treasury at zero
        # (where the enabled clamp would chop it down).
        char = await self._make_character()
        with patch.object(config, "SUBSIDY_PROTECTION_ENABLED", False):
            result = await clamp_subsidy_for_treasury_health(
                subsidy=100_000,
                character=char,
                treasury_balance=0.0,
            )
        self.assertEqual(result, 100_000)

    async def test_clamp_active_when_enabled(self):
        char = await self._make_character(driver_level=999)
        with patch.object(config, "SUBSIDY_PROTECTION_ENABLED", True), \
             patch.object(config, "EXPERIENCED_DRIVER_LEVEL_THRESHOLD", 200), \
             patch.object(config, "TREASURY_FLOOR", 50_000_000), \
             patch.object(config, "TREASURY_CEILING", 150_000_000), \
             patch.object(config, "TREASURY_GOOD_HEALTH_T", 0.9), \
             patch.object(config, "SUBSIDY_HEALTH_EXPONENT", 2.0):
            result = await clamp_subsidy_for_treasury_health(
                subsidy=100_000,
                character=char,
                treasury_balance=0.0,
            )
        self.assertEqual(result, 0)
