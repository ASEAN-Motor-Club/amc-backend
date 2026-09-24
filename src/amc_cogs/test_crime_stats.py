from datetime import timedelta

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.factories import CharacterFactory, PlayerFactory
from amc.models import Delivery
from amc_cogs.crime_stats import CrimeStatsCog


async def _setup_character(suffix: str):
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player, guid=f"crime-stats-{suffix}"
    )
    return player, character


async def _make_delivery(character, cargo_key, payment, age_hours=0.0):
    return await Delivery.objects.acreate(
        timestamp=timezone.now() - timedelta(hours=age_hours),
        character=character,
        cargo_key=cargo_key,
        quantity=1,
        payment=payment,
    )


class CrimeStatsIllegalDeliveriesTests(TestCase):
    async def test_illegal_cargo_deliveries_appear_in_report(self):
        cog = CrimeStatsCog(bot=None)

        _, crim = await _setup_character("a")
        _, other = await _setup_character("b")

        # Illicit, non-Money cargo — must be counted.
        await _make_delivery(crim, "Ganja", 50_000)
        await _make_delivery(crim, "Cocaine", 20_000)
        # Money is reported by the laundering section — excluded here.
        await _make_delivery(crim, "Money", 999_999)
        # Legit cargo — excluded.
        await _make_delivery(other, "Fish", 10_000)
        # Illicit but older than the 24h window — excluded.
        await _make_delivery(other, "Moonshine", 5_000, age_hours=30.0)

        embed = await cog.build_daily_crime_stats_embed()

        illegal_field = next(
            f for f in embed.fields if f.name.startswith("⚖️")
        )
        self.assertIn("$70,000", illegal_field.value)
        self.assertIn("**2** deliveries", illegal_field.value)
        self.assertIn("**1** criminal", illegal_field.value)
        self.assertIn(crim.name, illegal_field.value)
        self.assertNotIn(other.name, illegal_field.value)
        # Money stays only in the laundering section.
        laundering_field = next(
            f for f in embed.fields if f.name.startswith("💰")
        )
        self.assertIn("$999,999", laundering_field.value)

    async def test_no_illegal_deliveries_shows_placeholder(self):
        cog = CrimeStatsCog(bot=None)

        embed = await cog.build_daily_crime_stats_embed()

        illegal_field = next(
            f for f in embed.fields if f.name.startswith("⚖️")
        )
        self.assertIn("No illegal cargo deliveries.", illegal_field.value)
