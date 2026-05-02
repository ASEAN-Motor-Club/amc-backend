"""Tests for the guild achievement system."""

import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.factories import CharacterFactory, PlayerFactory
from amc.guilds import (
    _criteria_matches_cargo,
    _criteria_matches_passenger,
    check_guild_achievements,
    evaluate_achievement,
)
from amc.models import (
    Guild,
    GuildAchievement,
    GuildCargoRequirement,
    GuildCharacter,
    GuildCharacterAchievement,
    GuildPassengerRequirement,
    GuildSession,
    ServerCargoArrivedLog,
    ServerPassengerArrivedLog,
)
from amc.special_cargo import ILLICIT_CARGO_KEYS
from amc.webhook import process_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_passenger_log(**kwargs):
    defaults = dict(
        timestamp=timezone.now(),
        passenger_type=2,
        distance=1000.0,
        payment=5000,
        arrived=True,
        comfort=False,
        urgent=False,
        limo=False,
        offroad=False,
        comfort_rating=None,
        urgent_rating=None,
    )
    defaults.update(kwargs)
    return ServerPassengerArrivedLog(**defaults)


def _make_cargo_log(**kwargs):
    defaults = dict(
        timestamp=timezone.now(),
        cargo_key="SmallBox",
        payment=5000,
        weight=100.0,
        damage=0.0,
    )
    defaults.update(kwargs)
    return ServerCargoArrivedLog(**defaults)


# ---------------------------------------------------------------------------
# _criteria_matches_passenger
# ---------------------------------------------------------------------------


class CriteriaMatchesPassengerTests(TestCase):
    def test_comfort_false_matches(self):
        criteria = {"comfort": False}
        log = _make_passenger_log(comfort=False)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_comfort_false_no_match(self):
        criteria = {"comfort": False}
        log = _make_passenger_log(comfort=True)
        self.assertFalse(_criteria_matches_passenger(criteria, log))

    def test_urgent_true_matches(self):
        criteria = {"urgent": True}
        log = _make_passenger_log(urgent=True)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_urgent_true_no_match(self):
        criteria = {"urgent": True}
        log = _make_passenger_log(urgent=False)
        self.assertFalse(_criteria_matches_passenger(criteria, log))

    def test_limo_true_matches(self):
        criteria = {"limo": True}
        log = _make_passenger_log(limo=True)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_offroad_true_matches(self):
        criteria = {"offroad": True}
        log = _make_passenger_log(offroad=True)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_passenger_type_filter(self):
        criteria = {"passenger_type": 2}
        log = _make_passenger_log(passenger_type=2)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_passenger_type_no_match(self):
        criteria = {"passenger_type": 2}
        log = _make_passenger_log(passenger_type=1)
        self.assertFalse(_criteria_matches_passenger(criteria, log))

    def test_max_comfort_rating_matches(self):
        criteria = {"max_comfort_rating": 0}
        log = _make_passenger_log(comfort_rating=0)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_max_comfort_rating_no_match(self):
        criteria = {"max_comfort_rating": 0}
        log = _make_passenger_log(comfort_rating=3)
        self.assertFalse(_criteria_matches_passenger(criteria, log))

    def test_max_comfort_rating_null_value_fails(self):
        criteria = {"max_comfort_rating": 0}
        log = _make_passenger_log(comfort_rating=None)
        self.assertFalse(_criteria_matches_passenger(criteria, log))

    def test_min_comfort_rating_matches(self):
        criteria = {"min_comfort_rating": 3}
        log = _make_passenger_log(comfort_rating=4)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_min_comfort_rating_no_match(self):
        criteria = {"min_comfort_rating": 3}
        log = _make_passenger_log(comfort_rating=2)
        self.assertFalse(_criteria_matches_passenger(criteria, log))

    def test_min_distance_matches(self):
        criteria = {"min_distance": 500}
        log = _make_passenger_log(distance=1000)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_min_distance_no_match(self):
        criteria = {"min_distance": 5000}
        log = _make_passenger_log(distance=1000)
        self.assertFalse(_criteria_matches_passenger(criteria, log))

    def test_min_payment_matches(self):
        criteria = {"min_payment": 1000}
        log = _make_passenger_log(payment=5000)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_min_payment_no_match(self):
        criteria = {"min_payment": 10000}
        log = _make_passenger_log(payment=5000)
        self.assertFalse(_criteria_matches_passenger(criteria, log))

    def test_combined_criteria_all_match(self):
        criteria = {"comfort": False, "urgent": True, "offroad": True}
        log = _make_passenger_log(comfort=False, urgent=True, offroad=True)
        self.assertTrue(_criteria_matches_passenger(criteria, log))

    def test_combined_criteria_one_fails(self):
        criteria = {"comfort": False, "urgent": True, "offroad": True}
        log = _make_passenger_log(comfort=False, urgent=True, offroad=False)
        self.assertFalse(_criteria_matches_passenger(criteria, log))

    def test_empty_criteria_matches_anything(self):
        criteria = {}
        log = _make_passenger_log()
        self.assertTrue(_criteria_matches_passenger(criteria, log))


# ---------------------------------------------------------------------------
# _criteria_matches_cargo
# ---------------------------------------------------------------------------


class CriteriaMatchesCargoTests(TestCase):
    def test_is_illicit_true_matches_illicit(self):
        criteria = {"is_illicit": True}
        log = _make_cargo_log(cargo_key="Money")
        self.assertTrue(_criteria_matches_cargo(criteria, log))

    def test_is_illicit_true_no_match_normal(self):
        criteria = {"is_illicit": True}
        log = _make_cargo_log(cargo_key="SmallBox")
        self.assertFalse(_criteria_matches_cargo(criteria, log))

    def test_is_illicit_false_matches_normal(self):
        criteria = {"is_illicit": False}
        log = _make_cargo_log(cargo_key="SmallBox")
        self.assertTrue(_criteria_matches_cargo(criteria, log))

    def test_is_illicit_false_no_match_illicit(self):
        criteria = {"is_illicit": False}
        log = _make_cargo_log(cargo_key="Ganja")
        self.assertFalse(_criteria_matches_cargo(criteria, log))

    def test_cargo_key_exact_match(self):
        criteria = {"cargo_key": "Money"}
        log = _make_cargo_log(cargo_key="Money")
        self.assertTrue(_criteria_matches_cargo(criteria, log))

    def test_cargo_key_exact_no_match(self):
        criteria = {"cargo_key": "Money"}
        log = _make_cargo_log(cargo_key="Ganja")
        self.assertFalse(_criteria_matches_cargo(criteria, log))

    def test_cargo_key_in_matches(self):
        criteria = {"cargo_key_in": ["Ganja", "Cocaine", "CocaLeavesPallet"]}
        log = _make_cargo_log(cargo_key="Cocaine")
        self.assertTrue(_criteria_matches_cargo(criteria, log))

    def test_cargo_key_in_no_match(self):
        criteria = {"cargo_key_in": ["Ganja", "Cocaine"]}
        log = _make_cargo_log(cargo_key="Moonshine")
        self.assertFalse(_criteria_matches_cargo(criteria, log))

    def test_min_payment_matches(self):
        criteria = {"min_payment": 1000}
        log = _make_cargo_log(payment=5000)
        self.assertTrue(_criteria_matches_cargo(criteria, log))

    def test_min_payment_no_match(self):
        criteria = {"min_payment": 10000}
        log = _make_cargo_log(payment=5000)
        self.assertFalse(_criteria_matches_cargo(criteria, log))

    def test_combined_criteria_all_match(self):
        criteria = {"is_illicit": True, "cargo_key": "Money", "min_payment": 1000}
        log = _make_cargo_log(cargo_key="Money", payment=5000)
        self.assertTrue(_criteria_matches_cargo(criteria, log))

    def test_combined_criteria_one_fails(self):
        criteria = {"is_illicit": True, "cargo_key": "Money", "min_payment": 10000}
        log = _make_cargo_log(cargo_key="Money", payment=5000)
        self.assertFalse(_criteria_matches_cargo(criteria, log))

    def test_all_illicit_keys_detected(self):
        for key in ILLICIT_CARGO_KEYS:
            log = _make_cargo_log(cargo_key=key)
            self.assertTrue(
                _criteria_matches_cargo({"is_illicit": True}, log),
                f"{key} should be illicit",
            )


# ---------------------------------------------------------------------------
# evaluate_achievement
# ---------------------------------------------------------------------------


class EvaluatePassengerAchievementTests(TestCase):
    async def _setup(self, criteria):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gc = await GuildCharacter.objects.acreate(guild=guild, character=character)
        achievement = await GuildAchievement.objects.acreate(
            guild=guild, name="Test Ach", criteria=criteria, order=1
        )
        return gc, achievement

    async def test_matching_delivery_increments_progress(self):
        gc, ach = await self._setup({"tracking": "count", "goal": 5, "log_model": "passenger", "comfort": False})
        log = _make_passenger_log(comfort=False)

        progress, completed = await evaluate_achievement(gc, ach, log)

        self.assertEqual(progress, 1)
        self.assertFalse(completed)
        ca = await GuildCharacterAchievement.objects.aget(guild_character=gc, achievement=ach)
        self.assertEqual(ca.progress, 1)
        self.assertIsNone(ca.completed_at)

    async def test_non_matching_delivery_no_increment(self):
        gc, ach = await self._setup({"tracking": "count", "goal": 5, "log_model": "passenger", "comfort": False})
        log = _make_passenger_log(comfort=True)

        result = await evaluate_achievement(gc, ach, log)

        self.assertEqual(result, (None, False))
        exists = await GuildCharacterAchievement.objects.filter(
            guild_character=gc, achievement=ach
        ).aexists()
        self.assertFalse(exists)

    async def test_progress_caps_at_goal_and_completes(self):
        gc, ach = await self._setup({"tracking": "count", "goal": 2, "log_model": "passenger", "comfort": False})

        log = _make_passenger_log(comfort=False)
        progress1, c1 = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress1, 1)
        self.assertFalse(c1)

        progress2, c2 = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress2, 2)
        self.assertTrue(c2)

        ca = await GuildCharacterAchievement.objects.aget(guild_character=gc, achievement=ach)
        self.assertEqual(ca.progress, 2)
        self.assertIsNotNone(ca.completed_at)

    async def test_already_completed_not_re_evaluated(self):
        gc, ach = await self._setup({"tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False})
        log = _make_passenger_log(comfort=False)

        await evaluate_achievement(gc, ach, log)

        progress, completed = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress, 1)
        self.assertFalse(completed)

    async def test_sum_payment_tracking(self):
        gc, ach = await self._setup({"tracking": "sum_payment", "goal": 10000, "log_model": "passenger"})

        log1 = _make_passenger_log(payment=4000)
        p1, c1 = await evaluate_achievement(gc, ach, log1)
        self.assertEqual(p1, 4000)
        self.assertFalse(c1)

        log2 = _make_passenger_log(payment=6000)
        p2, c2 = await evaluate_achievement(gc, ach, log2)
        self.assertEqual(p2, 10000)
        self.assertTrue(c2)

    async def test_comfort_true_max_rating_zero(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 5, "log_model": "passenger", "comfort": True, "max_comfort_rating": 0}
        )
        log = _make_passenger_log(comfort=True, comfort_rating=0)

        progress, completed = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress, 1)
        self.assertFalse(completed)

    async def test_comfort_true_max_rating_zero_rejects_nonzero_rating(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 5, "log_model": "passenger", "comfort": True, "max_comfort_rating": 0}
        )
        log = _make_passenger_log(comfort=True, comfort_rating=3)

        result = await evaluate_achievement(gc, ach, log)
        self.assertEqual(result, (None, False))

    async def test_comfort_true_max_rating_zero_rejects_comfort_false(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 5, "log_model": "passenger", "comfort": True, "max_comfort_rating": 0}
        )
        log = _make_passenger_log(comfort=False, comfort_rating=0)

        result = await evaluate_achievement(gc, ach, log)
        self.assertEqual(result, (None, False))

    async def test_gopnik_spirit_combined_criteria(self):
        gc, ach = await self._setup({
            "tracking": "count", "goal": 10, "log_model": "passenger",
            "comfort": False, "urgent": True, "offroad": True,
        })

        log_match = _make_passenger_log(comfort=False, urgent=True, offroad=True)
        progress, _ = await evaluate_achievement(gc, ach, log_match)
        self.assertEqual(progress, 1)

        log_no_offroad = _make_passenger_log(comfort=False, urgent=True, offroad=False)
        result = await evaluate_achievement(gc, ach, log_no_offroad)
        self.assertEqual(result, (None, False))

    async def test_sum_payment_caps_at_goal(self):
        gc, ach = await self._setup({"tracking": "sum_payment", "goal": 10000, "log_model": "passenger"})

        log = _make_passenger_log(payment=15000)
        progress, completed = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress, 10000)
        self.assertTrue(completed)

    async def test_min_distance_filter(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 1, "log_model": "passenger", "min_distance": 50000}
        )

        log_short = _make_passenger_log(distance=10000)
        result = await evaluate_achievement(gc, ach, log_short)
        self.assertEqual(result, (None, False))

        log_long = _make_passenger_log(distance=60000)
        progress, completed = await evaluate_achievement(gc, ach, log_long)
        self.assertEqual(progress, 1)
        self.assertTrue(completed)


class EvaluateCargoAchievementTests(TestCase):
    async def _setup(self, criteria):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        gc = await GuildCharacter.objects.acreate(guild=guild, character=character)
        achievement = await GuildAchievement.objects.acreate(
            guild=guild, name="Test Ach", criteria=criteria, order=1
        )
        return gc, achievement

    async def test_illicit_cargo_matches(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 5, "log_model": "cargo", "is_illicit": True}
        )
        log = _make_cargo_log(cargo_key="Money")

        progress, completed = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress, 1)
        self.assertFalse(completed)

    async def test_illicit_cargo_no_match_normal(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 5, "log_model": "cargo", "is_illicit": True}
        )
        log = _make_cargo_log(cargo_key="SmallBox")

        result = await evaluate_achievement(gc, ach, log)
        self.assertEqual(result, (None, False))

    async def test_cargo_key_exact_match(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 5, "log_model": "cargo", "cargo_key": "Money"}
        )
        log = _make_cargo_log(cargo_key="Money")

        progress, _ = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress, 1)

    async def test_cargo_key_in_match(self):
        gc, ach = await self._setup(
            {
                "tracking": "count",
                "goal": 5,
                "log_model": "cargo",
                "cargo_key_in": ["Ganja", "Cocaine"],
            }
        )
        log = _make_cargo_log(cargo_key="Ganja")

        progress, _ = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress, 1)

    async def test_sum_payment_cargo(self):
        gc, ach = await self._setup(
            {"tracking": "sum_payment", "goal": 500000, "log_model": "cargo", "cargo_key": "Money"}
        )

        log1 = _make_cargo_log(cargo_key="Money", payment=200000)
        p1, c1 = await evaluate_achievement(gc, ach, log1)
        self.assertEqual(p1, 200000)
        self.assertFalse(c1)

        log2 = _make_cargo_log(cargo_key="Money", payment=300000)
        p2, c2 = await evaluate_achievement(gc, ach, log2)
        self.assertEqual(p2, 500000)
        self.assertTrue(c2)

    async def test_passenger_log_does_not_match_cargo_criteria(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 1, "log_model": "cargo", "is_illicit": True}
        )
        log = _make_passenger_log()

        result = await evaluate_achievement(gc, ach, log)
        self.assertEqual(result, (None, False))

    async def test_cargo_log_does_not_match_passenger_criteria(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False}
        )
        log = _make_cargo_log()

        result = await evaluate_achievement(gc, ach, log)
        self.assertEqual(result, (None, False))

    async def test_cargo_key_in_no_match_empty_set(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 1, "log_model": "cargo", "cargo_key_in": []}
        )
        log = _make_cargo_log(cargo_key="Money")

        result = await evaluate_achievement(gc, ach, log)
        self.assertEqual(result, (None, False))

    async def test_combined_cargo_key_in_and_min_payment(self):
        gc, ach = await self._setup({
            "tracking": "count", "goal": 1, "log_model": "cargo",
            "cargo_key_in": ["Ganja", "Cocaine"], "min_payment": 1000,
        })

        log_match = _make_cargo_log(cargo_key="Ganja", payment=5000)
        progress, _ = await evaluate_achievement(gc, ach, log_match)
        self.assertEqual(progress, 1)

    async def test_combined_cargo_key_in_and_min_payment_fails(self):
        gc, ach = await self._setup({
            "tracking": "count", "goal": 1, "log_model": "cargo",
            "cargo_key_in": ["Ganja", "Cocaine"], "min_payment": 10000,
        })

        log_low = _make_cargo_log(cargo_key="Ganja", payment=5000)
        result = await evaluate_achievement(gc, ach, log_low)
        self.assertEqual(result, (None, False))

    async def test_sum_payment_caps_at_goal(self):
        gc, ach = await self._setup(
            {"tracking": "sum_payment", "goal": 100000, "log_model": "cargo", "is_illicit": True}
        )
        log = _make_cargo_log(cargo_key="Money", payment=200000)

        progress, completed = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress, 100000)
        self.assertTrue(completed)

    async def test_moonshine_exact_key(self):
        gc, ach = await self._setup(
            {"tracking": "count", "goal": 10, "log_model": "cargo", "cargo_key": "Moonshine"}
        )
        log = _make_cargo_log(cargo_key="Moonshine")

        progress, _ = await evaluate_achievement(gc, ach, log)
        self.assertEqual(progress, 1)

        log_other = _make_cargo_log(cargo_key="Money")
        result = await evaluate_achievement(gc, ach, log_other)
        self.assertEqual(result, (None, False))


# ---------------------------------------------------------------------------
# check_guild_achievements
# ---------------------------------------------------------------------------


class CheckGuildAchievementsTests(TestCase):
    async def _setup(self, achievement_criteria=None):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        await GuildCharacter.objects.acreate(guild=guild, character=character)
        session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        if achievement_criteria:
            for crit in achievement_criteria:
                await GuildAchievement.objects.acreate(
                    guild=guild, name=crit.get("name", "Test"), criteria=crit, order=0
                )
        return character, session, guild

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_evaluates_all_achievements(self, mock_popup):
        character, session, guild = await self._setup([
            {"name": "A1", "tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False},
            {"name": "A2", "tracking": "count", "goal": 5, "log_model": "passenger", "comfort": False},
        ])
        log = _make_passenger_log(comfort=False)

        await check_guild_achievements(character, session, log, AsyncMock())

        ca1 = await GuildCharacterAchievement.objects.aget(
            achievement__name="A1", guild_character__character=character
        )
        ca2 = await GuildCharacterAchievement.objects.aget(
            achievement__name="A2", guild_character__character=character
        )
        self.assertEqual(ca1.progress, 1)
        self.assertIsNotNone(ca1.completed_at)
        self.assertEqual(ca2.progress, 1)
        self.assertIsNone(ca2.completed_at)

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_no_achievements_noop(self, mock_popup):
        character, session, guild = await self._setup()
        log = _make_passenger_log()

        await check_guild_achievements(character, session, log, AsyncMock())
        mock_popup.assert_not_called()

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_completion_triggers_popup(self, mock_popup):
        character, session, guild = await self._setup([
            {"name": "Easy", "tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False},
        ])
        log = _make_passenger_log(comfort=False)

        await check_guild_achievements(character, session, log, AsyncMock())
        mock_popup.assert_awaited_once()

    @patch("amc.tasks.enqueue_discord_message")
    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_completion_triggers_discord_when_thread_id_set(self, mock_popup, mock_enqueue):
        character, session, guild = await self._setup([
            {"name": "Easy", "tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False},
        ])
        guild.discord_thread_id = "123456789"
        await guild.asave(update_fields=["discord_thread_id"])

        log = _make_passenger_log(comfort=False)
        await check_guild_achievements(character, session, log, AsyncMock())

        mock_enqueue.assert_called_once()
        args = mock_enqueue.call_args[0]
        self.assertEqual(args[0], "123456789")
        self.assertIn("Easy", args[1])

    @patch("amc.tasks.enqueue_discord_message")
    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_no_discord_when_no_thread_id(self, mock_popup, mock_enqueue):
        character, session, guild = await self._setup([
            {"name": "Easy", "tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False},
        ])
        log = _make_passenger_log(comfort=False)

        await check_guild_achievements(character, session, log, AsyncMock())
        mock_enqueue.assert_not_called()

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_no_guild_character_skips_evaluation(self, mock_popup):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        session = await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        await GuildAchievement.objects.acreate(
            guild=guild,
            name="A1",
            criteria={"tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False},
            order=0,
        )
        log = _make_passenger_log(comfort=False)

        await check_guild_achievements(character, session, log, AsyncMock())

        exists = await GuildCharacterAchievement.objects.filter(
            achievement__name="A1"
        ).aexists()
        self.assertFalse(exists)
        mock_popup.assert_not_called()

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_error_in_one_achievement_does_not_block_others(self, mock_popup):
        character, session, guild = await self._setup([
            {"name": "Broken", "tracking": "count", "goal": 1, "log_model": "invalid_model"},
            {"name": "Good", "tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False},
        ])
        log = _make_passenger_log(comfort=False)

        await check_guild_achievements(character, session, log, AsyncMock())

        good_ca = await GuildCharacterAchievement.objects.aget(
            achievement__name="Good", guild_character__character=character
        )
        self.assertEqual(good_ca.progress, 1)
        self.assertIsNotNone(good_ca.completed_at)

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_cargo_achievement_via_check_guild_achievements(self, mock_popup):
        character, session, guild = await self._setup([
            {"name": "Petty Crime", "tracking": "count", "goal": 1, "log_model": "cargo", "is_illicit": True},
        ])
        log = _make_cargo_log(cargo_key="Money")

        await check_guild_achievements(character, session, log, AsyncMock())

        ca = await GuildCharacterAchievement.objects.aget(
            achievement__name="Petty Crime", guild_character__character=character
        )
        self.assertEqual(ca.progress, 1)
        self.assertIsNotNone(ca.completed_at)
        mock_popup.assert_awaited_once()

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_non_matching_log_does_not_create_progress_row(self, mock_popup):
        character, session, guild = await self._setup([
            {"name": "Comfort", "tracking": "count", "goal": 1, "log_model": "passenger", "comfort": True},
        ])
        log = _make_passenger_log(comfort=False)

        await check_guild_achievements(character, session, log, AsyncMock())

        exists = await GuildCharacterAchievement.objects.filter(
            achievement__name="Comfort"
        ).aexists()
        self.assertFalse(exists)

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_popup_contains_achievement_name_and_icon(self, mock_popup):
        character, session, guild = await self._setup([
            {"name": "TestAch", "tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False},
        ])
        ach = await GuildAchievement.objects.aget(name="TestAch")
        ach.icon = "🚗"
        await ach.asave(update_fields=["icon"])

        log = _make_passenger_log(comfort=False)
        await check_guild_achievements(character, session, log, AsyncMock())

        popup_text = mock_popup.call_args[0][1]
        self.assertIn("TestAch", popup_text)
        self.assertIn("🚗", popup_text)
        self.assertIn("Achievement Unlocked", popup_text)

    @patch("amc.tasks.enqueue_discord_message")
    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_discord_message_contains_achievement_and_character(self, mock_popup, mock_enqueue):
        character, session, guild = await self._setup([
            {"name": "Speed Demon", "tracking": "count", "goal": 1, "log_model": "passenger", "urgent": True},
        ])
        guild.discord_thread_id = "999"
        await guild.asave(update_fields=["discord_thread_id"])

        log = _make_passenger_log(urgent=True)
        await check_guild_achievements(character, session, log, AsyncMock())

        discord_text = mock_enqueue.call_args[0][1]
        self.assertIn("Speed Demon", discord_text)
        self.assertIn(character.name, discord_text)


# ---------------------------------------------------------------------------
# Integration tests — passenger handler + achievements
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class PassengerAchievementIntegrationTests(TestCase):
    async def _setup(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        return player, character

    async def _activate_guild_with_achievement(self, character, criteria, passenger_req_kwargs=None):
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        if passenger_req_kwargs is not None:
            await GuildPassengerRequirement.objects.acreate(guild=guild, **passenger_req_kwargs)
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        await GuildCharacter.objects.acreate(guild=guild, character=character)
        await GuildAchievement.objects.acreate(
            guild=guild, name="Test Achievement", criteria=criteria, order=1
        )
        return guild

    def _passenger_event(self, character, player, flags=0, payment=3000):
        return {
            "hook": "ServerPassengerArrived",
            "timestamp": int(time.time()),
            "data": {
                "CharacterGuid": str(character.guid),
                "PlayerId": str(player.unique_id),
                "Passenger": {
                    "Net_PassengerType": 2,
                    "Net_Payment": payment,
                    "Net_Distance": 500.0,
                    "Net_bArrived": True,
                    "Net_PassengerFlags": flags,
                    "Net_LCComfortSatisfaction": 0,
                    "Net_TimeLimitPoint": 0.5,
                },
            },
        }

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_delivery_with_guild_triggers_achievement_check(self, mock_popup, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()
        criteria = {"tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False}
        await self._activate_guild_with_achievement(character, criteria, {"bonus_pct": 10})

        await process_event(
            self._passenger_event(character, player, flags=0, payment=3000),
            player,
            character,
        )

        log = await ServerPassengerArrivedLog.objects.afirst()
        self.assertIsNotNone(log.guild_session_id)

        ca = await GuildCharacterAchievement.objects.afirst()
        self.assertIsNotNone(ca)
        self.assertEqual(ca.progress, 1)
        self.assertIsNotNone(ca.completed_at)
        mock_popup.assert_awaited()

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_delivery_without_guild_no_achievement(self, mock_popup, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        await GuildAchievement.objects.acreate(
            guild=guild,
            name="Test Achievement",
            criteria={"tracking": "count", "goal": 1, "log_model": "passenger", "comfort": False},
            order=1,
        )

        await process_event(
            self._passenger_event(character, player, flags=0, payment=3000),
            player,
            character,
        )

        log = await ServerPassengerArrivedLog.objects.afirst()
        self.assertIsNone(log.guild_session_id)

        exists = await GuildCharacterAchievement.objects.aexists()
        self.assertFalse(exists)

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_urgent_passenger_achievement(self, mock_popup, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()
        criteria = {"tracking": "count", "goal": 1, "log_model": "passenger", "urgent": True}
        await self._activate_guild_with_achievement(character, criteria, {"bonus_pct": 10})

        await process_event(
            self._passenger_event(character, player, flags=2, payment=3000),
            player,
            character,
        )

        ca = await GuildCharacterAchievement.objects.afirst()
        self.assertIsNotNone(ca)
        self.assertEqual(ca.progress, 1)
        self.assertIsNotNone(ca.completed_at)


# ---------------------------------------------------------------------------
# Integration tests — cargo handler + achievements
# ---------------------------------------------------------------------------


@patch("amc.webhook.get_rp_mode", new_callable=AsyncMock)
@patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock)
class CargoAchievementIntegrationTests(TestCase):
    async def _setup(self):
        from amc.models import CharacterLocation, DeliveryPoint
        from django.contrib.gis.geos import Point

        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)
        await CharacterLocation.objects.acreate(
            character=character, location=Point(0, 0, 0), vehicle_key="TestVehicle"
        )
        await DeliveryPoint.objects.acreate(guid="gs", name="Mine", coord=Point(0, 0, 0))
        await DeliveryPoint.objects.acreate(
            guid="gd", name="Factory", coord=Point(100_000, 0, 0)
        )
        return player, character

    async def _activate_guild_with_achievement(self, character, criteria, cargo_req_kwargs=None):
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        if cargo_req_kwargs is not None:
            await GuildCargoRequirement.objects.acreate(guild=guild, **cargo_req_kwargs)
        await GuildSession.objects.acreate(
            guild=guild, character=character, started_at=timezone.now()
        )
        await GuildCharacter.objects.acreate(guild=guild, character=character)
        await GuildAchievement.objects.acreate(
            guild=guild, name="Test Achievement", criteria=criteria, order=1
        )
        return guild

    def _cargo_event(self, character, player, cargo_key="Money", payment=5000, damage=0.0):
        return {
            "hook": "ServerCargoArrived",
            "timestamp": int(time.time()),
            "data": {
                "Cargos": [
                    {
                        "Net_CargoKey": cargo_key,
                        "Net_Payment": payment,
                        "Net_Weight": 100.0,
                        "Net_Damage": damage,
                        "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                        "Net_DestinationLocation": {"X": 100_000, "Y": 0, "Z": 0},
                    }
                ],
                "PlayerId": str(player.unique_id),
                "CharacterGuid": str(character.guid),
            },
        }

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_illicit_cargo_triggers_achievement(self, mock_popup, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()
        criteria = {"tracking": "count", "goal": 1, "log_model": "cargo", "is_illicit": True}
        await self._activate_guild_with_achievement(character, criteria, {"bonus_pct": 10})

        await process_event(
            self._cargo_event(character, player, cargo_key="Money", payment=5000),
            player,
            character,
        )

        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertIsNotNone(log.guild_session_id)

        ca = await GuildCharacterAchievement.objects.afirst()
        self.assertIsNotNone(ca)
        self.assertEqual(ca.progress, 1)
        self.assertIsNotNone(ca.completed_at)
        mock_popup.assert_awaited()

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_normal_cargo_no_achievement_when_illicit_required(self, mock_popup, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()
        criteria = {"tracking": "count", "goal": 1, "log_model": "cargo", "is_illicit": True}
        await self._activate_guild_with_achievement(character, criteria, {"bonus_pct": 10})

        await process_event(
            self._cargo_event(character, player, cargo_key="SmallBox", payment=5000),
            player,
            character,
        )

        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertIsNotNone(log.guild_session_id)

        exists = await GuildCharacterAchievement.objects.aexists()
        self.assertFalse(exists)

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_cargo_without_guild_session_no_achievement(self, mock_popup, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()
        guild = await Guild.objects.acreate(name="Test", abbreviation="TST")
        await GuildAchievement.objects.acreate(
            guild=guild,
            name="Test Achievement",
            criteria={"tracking": "count", "goal": 1, "log_model": "cargo", "is_illicit": True},
            order=1,
        )

        await process_event(
            self._cargo_event(character, player, cargo_key="Money", payment=5000),
            player,
            character,
        )

        log = await ServerCargoArrivedLog.objects.afirst()
        self.assertIsNone(log.guild_session_id)

        exists = await GuildCharacterAchievement.objects.aexists()
        self.assertFalse(exists)

    @patch("amc.guilds.show_popup", new_callable=AsyncMock)
    async def test_cargo_key_specific_achievement(self, mock_popup, mock_treasury, mock_rp):
        mock_rp.return_value = False
        mock_treasury.return_value = 100_000
        player, character = await self._setup()
        criteria = {"tracking": "count", "goal": 1, "log_model": "cargo", "cargo_key": "Moonshine"}
        await self._activate_guild_with_achievement(character, criteria, {"bonus_pct": 10})

        await process_event(
            self._cargo_event(character, player, cargo_key="Moonshine", payment=3000),
            player,
            character,
        )

        ca = await GuildCharacterAchievement.objects.afirst()
        self.assertIsNotNone(ca)
        self.assertEqual(ca.progress, 1)
        self.assertIsNotNone(ca.completed_at)
