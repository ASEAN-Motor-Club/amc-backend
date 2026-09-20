"""Tests for the Criminal Score rework: boss tax, bounty-at-trigger, arrest
negation, and the /criminals leaderboard.

Migration note: 0243_criminal_score_carryforward is NOT unit-tested here — its
data path reads the historical Character.criminal_laundered_total column, which
no longer exists on the real model registry, so the function can only run
against the historical migration state. It is exercised end-to-end by
`manage.py migrate` on every fresh test DB (no-op there, zero rows) and by the
staging DB-copy dry run (plan §12 level-continuity check) before prod.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.commands.wanted import cmd_criminals
from amc.criminals import create_or_refresh_wanted
from amc.factories import CharacterFactory, PlayerFactory
from amc.special_cargo import (
    BOSS_CUT_CAP,
    BOSS_CUT_FLOOR,
    calculate_boss_cut_ratio,
    collect_boss_tax,
)


def _sync_create(factory_or_model, **kwargs):
    """Create a DB row on the sync side; supports the call shapes used here.

    - `_sync_create(SomeModel, **kw)` → `Model.objects.create(**kw)`.
    - `_sync_create(SomeFactory, **kw)` → build via the factory now.
    - `_sync_create(SomeFactory)(**kw)` / `()` → apply-then-call
      (factory_boy factories are not models).
    """
    if hasattr(factory_or_model, "objects"):
        target = factory_or_model.objects.create
    else:
        target = factory_or_model

    async def _run(**run_kwargs):
        return await sync_to_async(target)(**run_kwargs)

    if kwargs:
        return _run(**kwargs)
    if hasattr(factory_or_model, "objects"):
        return _run()  # bare model create with defaults
    return _run  # apply-then-call for factories


class _MockAsyncIterable:
    """Minimal async-iterable wrapper (mirrors tests_commands.py's pattern)."""

    def __init__(self, items):
        self._items = list(items)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


async def _no_police():
    """Stand-in for get_active_police_characters: empty roster.

    Must be a plain coroutine function returning an async-iterable —
    execute_arrest does `async for c in await get_active_police_characters()`,
    so an AsyncMock returning a plain list dies with
    "'async for' requires an object with __aiter__".
    """
    return _MockAsyncIterable([])


class BossCutRatioTests(TestCase):
    """Pure unit tests for the progressive, clamped boss-cut curve."""

    def test_low_ratio_pays_just_above_floor(self):
        # Progressive curve: r=0.1 → 0.05 + 0.20*0.01 = 0.052 (not the floor)
        self.assertAlmostEqual(calculate_boss_cut_ratio(1, 10), 0.052)

    def test_close_to_boss_pays_up_to_cap(self):
        self.assertEqual(calculate_boss_cut_ratio(10, 10), BOSS_CUT_CAP)

    def test_progressive_midpoint(self):
        # r = 0.5 → 0.05 + 0.20 * 0.25 = 0.10
        self.assertAlmostEqual(calculate_boss_cut_ratio(5, 10), 0.10)

    def test_hard_cap_never_exceeds_25pct(self):
        # r clamped to 1.0 even if level somehow exceeds boss level
        self.assertEqual(calculate_boss_cut_ratio(99, 10), BOSS_CUT_CAP)

    def test_no_boss_returns_zero(self):
        self.assertEqual(calculate_boss_cut_ratio(5, 0), 0.0)

    def test_floor_binds_at_zero_ratio(self):
        # r=0 → raw equals the floor exactly; clamp keeps it
        self.assertEqual(calculate_boss_cut_ratio(0, 1000), BOSS_CUT_FLOOR)


class CollectBossTaxTests(TestCase):
    """collect_boss_tax: wallet deduction payer-side, bank deposit boss-side."""

    async def _setup_pair(self, boss_score=600_000, courier_score=100_000):
        boss = await _sync_create(CharacterFactory)(name="BossMan")
        boss.criminal_score = boss_score
        await boss.asave(update_fields=["criminal_score"])
        courier = await _sync_create(CharacterFactory)(name="Courier")
        courier.criminal_score = courier_score
        await courier.asave(update_fields=["criminal_score"])
        return boss, courier

    @patch("amc.special_cargo.show_popup", new_callable=AsyncMock)
    @patch("amc.special_cargo.register_player_deposit", new_callable=AsyncMock)
    @patch("amc.special_cargo.transfer_money", new_callable=AsyncMock)
    async def test_payer_wallet_deducted_boss_bank_credited(
        self, mock_transfer, mock_deposit, mock_popup
    ):
        boss, courier = await self._setup_pair()
        # courier level 3, boss level 13 → r = 3/13
        # ratio = 0.05 + 0.20·r² = 0.060650887…  → cut = int(20_000 · ratio) = 1213
        await collect_boss_tax(courier, 20_000, MagicMock())

        mock_transfer.assert_awaited_once()
        args, _ = mock_transfer.await_args
        self.assertEqual(args[1], -1213)
        self.assertEqual(args[2], "Boss Cut")
        self.assertEqual(args[3], str(courier.player_id))

        mock_deposit.assert_awaited_once()
        d_args, d_kwargs = mock_deposit.await_args
        self.assertEqual(d_args[0], -args[1])  # deposit == |deduction|
        self.assertEqual(d_args[1], boss)
        self.assertIn("Boss Cut from Courier", d_kwargs["description"])

    @patch("amc.special_cargo.show_popup", new_callable=AsyncMock)
    @patch("amc.special_cargo.register_player_deposit", new_callable=AsyncMock)
    @patch("amc.special_cargo.transfer_money", new_callable=AsyncMock)
    async def test_boss_is_taxed_offline_via_ledger(
        self, mock_transfer, mock_deposit, mock_popup
    ):
        """The deposit is a pure ledger op — no online check, works offline."""
        boss, courier = await self._setup_pair()
        # The boss has no game session — the deposit call must still happen.
        await collect_boss_tax(courier, 10_000, MagicMock())
        mock_deposit.assert_awaited_once()
        self.assertEqual(mock_deposit.await_args[0][1], boss)

    @patch("amc.special_cargo.show_popup", new_callable=AsyncMock)
    @patch("amc.special_cargo.register_player_deposit", new_callable=AsyncMock)
    @patch("amc.special_cargo.transfer_money", new_callable=AsyncMock)
    async def test_boss_does_not_tax_self(
        self, mock_transfer, mock_deposit, mock_popup
    ):
        """The boss delivering illicit cargo pays no cut to himself."""
        boss, _ = await self._setup_pair()
        await collect_boss_tax(boss, 20_000, MagicMock())

        mock_transfer.assert_not_awaited()
        mock_deposit.assert_not_awaited()

    @patch("amc.special_cargo.show_popup", new_callable=AsyncMock)
    @patch("amc.special_cargo.register_player_deposit", new_callable=AsyncMock)
    @patch("amc.special_cargo.transfer_money", new_callable=AsyncMock)
    async def test_no_boss_means_no_tax(
        self, mock_transfer, mock_deposit, mock_popup
    ):
        """A lone criminal with a score has nobody to pay."""
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(player=player)
        character.criminal_score = 50_000
        await character.asave(update_fields=["criminal_score"])

        await collect_boss_tax(character, 10_000, MagicMock())
        mock_transfer.assert_not_awaited()
        mock_deposit.assert_not_awaited()

    @patch("amc.special_cargo.show_popup", new_callable=AsyncMock)
    @patch("amc.special_cargo.register_player_deposit", new_callable=AsyncMock)
    @patch("amc.special_cargo.transfer_money", new_callable=AsyncMock)
    async def test_deposit_failure_refunds_payer(
        self, mock_transfer, mock_deposit, mock_popup
    ):
        """Money conservation: ledger-leg failure refunds the wallet leg."""
        _, courier = await self._setup_pair()
        mock_deposit.side_effect = RuntimeError("db down")

        await collect_boss_tax(courier, 10_000, MagicMock())

        calls = mock_transfer.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[1], -calls[1].args[1])  # refund == deduction
        self.assertEqual(calls[1].args[2], "Boss Cut Refund")


class BountyAtTriggerTests(TestCase):
    """create_or_refresh_wanted sets the bounty = 10% of score on system
    creation only; police-set flags and refreshes never re-price."""

    async def _setup_character(self, score=200_000):
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(player=player)
        character.criminal_score = score
        await character.asave(update_fields=["criminal_score"])
        return character

    @patch("amc.criminals.make_suspect", new_callable=AsyncMock)
    @patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
    async def test_system_trigger_sets_bounty_to_10pct_of_score(
        self, mock_refresh, mock_suspect
    ):

        character = await self._setup_character(score=200_000)
        wanted, created = await create_or_refresh_wanted(character, MagicMock())
        self.assertTrue(created)
        self.assertEqual(wanted.amount, 20_000)

    @patch("amc.criminals.make_suspect", new_callable=AsyncMock)
    @patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
    async def test_police_set_wanted_is_flag_only(
        self, mock_refresh, mock_suspect
    ):

        character = await self._setup_character(score=200_000)
        admin = await _sync_create(CharacterFactory)(name="AdminGuy")
        wanted, created = await create_or_refresh_wanted(
            character, MagicMock(), set_by=admin
        )
        self.assertTrue(created)
        self.assertEqual(wanted.amount, 0)

    @patch("amc.criminals.make_suspect", new_callable=AsyncMock)
    @patch("amc.criminals.refresh_player_name", new_callable=AsyncMock)
    async def test_refresh_does_not_reprice_bounty(
        self, mock_refresh, mock_suspect
    ):
        """Deliveries during a chase grow the score but not the frozen bounty."""
        from amc.models import Wanted

        character = await self._setup_character(score=100_000)
        existing = await _sync_create(
            Wanted, character=character, wanted_remaining=100, amount=10_000
        )
        character.criminal_score = 500_000  # score grew mid-chase
        await character.asave(update_fields=["criminal_score"])

        wanted, created = await create_or_refresh_wanted(character, MagicMock())
        self.assertFalse(created)
        self.assertEqual(wanted.pk, existing.pk)
        await wanted.arefresh_from_db(fields=["amount"])
        self.assertEqual(wanted.amount, 10_000)


class ArrestScoreNegationTests(TestCase):
    """execute_arrest negates the bounty from the criminal score."""

    async def _arrest(self, character, wanted_amount=None):
        from amc.commands.faction import execute_arrest
        from amc.models import TeleportPoint, Wanted

        await _sync_create(TeleportPoint, name="Jail", location="POINT (0 0 0)")

        if wanted_amount is not None:
            await _sync_create(
                Wanted,
                character=character,
                wanted_remaining=300,
                amount=wanted_amount,
            )

        targets = {
            character.guid: (str(character.player.unique_id), (0, 0, 0), False)
        }
        target_chars = {character.guid: character}

        with (
            patch(
                "amc.commands.faction.refresh_player_name", new_callable=AsyncMock
            ),
            patch(
                "amc.commands.faction.get_active_police_characters",
                new=_no_police,
            ),
            patch("amc.commands.faction.force_exit_vehicle", new_callable=AsyncMock),
            patch("amc.commands.faction.teleport_player", new_callable=AsyncMock),
            patch("amc.commands.faction.show_popup", new_callable=AsyncMock),
            patch("amc.commands.faction.clear_suspect", new_callable=AsyncMock),
            patch("amc.commands.faction.transfer_money", new_callable=AsyncMock) as mock_transfer,
            patch(
                "amc.commands.faction.record_treasury_confiscation_income",
                new_callable=AsyncMock,
            ),
            patch("amc.commands.faction.on_player_profit", new_callable=AsyncMock),
            patch("amc.commands.faction.send_system_message", new_callable=AsyncMock),
        ):
            await execute_arrest(
                officer_character=None,
                targets=targets,
                target_chars=target_chars,
                http_client=AsyncMock(),
                http_client_mod=AsyncMock(),
            )
        return mock_transfer

    async def test_positive_bounty_negates_score(self):
        from amc.models import Confiscation

        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(player=player)
        character.criminal_score = 50_000
        await character.asave(update_fields=["criminal_score"])

        mock_transfer = await self._arrest(character, wanted_amount=10_000)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 40_000)

        confiscation = await Confiscation.objects.aget(character=character)
        self.assertEqual(confiscation.amount, 10_000)
        mock_transfer.assert_awaited_once()
        self.assertEqual(mock_transfer.await_args[0][1], -10_000)

    async def test_no_wanted_is_jail_only(self):
        """Jail-only arrest: no money, no score change."""
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(player=player)
        character.criminal_score = 30_000
        await character.asave(update_fields=["criminal_score"])

        mock_transfer = await self._arrest(character, wanted_amount=None)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 30_000)
        mock_transfer.assert_not_awaited()

    async def test_negative_bounty_never_inflates_score(self):
        """Wrongful-wanted compensation does not increase the score."""
        player = await _sync_create(PlayerFactory)()
        character = await _sync_create(CharacterFactory)(player=player)
        character.criminal_score = 30_000
        await character.asave(update_fields=["criminal_score"])

        await self._arrest(character, wanted_amount=-5_000)

        await character.arefresh_from_db(fields=["criminal_score"])
        self.assertEqual(character.criminal_score, 30_000)


class CriminalsLeaderboardTests(TestCase):
    """/criminals: top-10 by score, boss marker, empty state."""

    def _ctx(self):
        ctx = MagicMock()
        ctx.reply = AsyncMock()
        return ctx

    async def test_empty_state(self):
        ctx = self._ctx()
        await cmd_criminals(ctx)
        ctx.reply.assert_awaited_once_with("No criminals yet")

    async def test_ranking_and_boss_marker(self):
        player_a = await _sync_create(PlayerFactory)()
        await _sync_create(CharacterFactory)(
            player=player_a, name="BossMan", criminal_score=600_000
        )
        player_b = await _sync_create(PlayerFactory)()
        await _sync_create(
            CharacterFactory, player=player_b, name="Henchman", criminal_score=100_000
        )
        player_c = await _sync_create(PlayerFactory)()
        await _sync_create(
            CharacterFactory, player=player_c, name="ZeroGuy", criminal_score=0
        )

        ctx = self._ctx()
        await cmd_criminals(ctx)

        output = ctx.reply.await_args[0][0]
        self.assertIn("Criminal Leaderboard", output)
        self.assertIn("BOSS", output)
        self.assertIn("Henchman", output)
        # Zero-score players are not criminals for the board
        self.assertNotIn("ZeroGuy", output)
        # Rank 1 = highest score
        self.assertLess(output.index("BossMan"), output.index("Henchman"))
        # Levels are derived live from score
        self.assertIn("C13", output)  # 600_000 // 50_000 + 1
        self.assertIn("C3", output)  # 100_000 // 50_000 + 1
