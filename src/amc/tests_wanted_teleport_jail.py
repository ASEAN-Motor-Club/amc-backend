"""Tests: portal auto-arrest + /tp and webhook no-arrest behavior.

Three entry points are tested:
  1. /tp command (cmd_tp_name) — wanted criminals can teleport freely (no arrest)
  2. Portal zones (_check_pois_and_portals) — wanted criminals ARE arrested
  3. Webhook events (_handle_teleport_or_respawn) — no arrest (just logging)

Since portal auto-arrest calls execute_arrest (which lives in faction.py),
mocks are applied at amc.commands.faction.* — the location where the
functions are actually looked up at call time.

NOTE on mock argument order with stacked @patch decorators
---------------------------------------------------------
When multiple @patch decorators are stacked, the *bottommost* patch
(closest to the function definition) is passed as the *first* extra arg.
So decorators listed top-to-bottom arrive as args bottom-to-top:

    @patch("...teleport_player")   # outermost → last arg
    @patch("...refresh_player_name")  # innermost → first arg after self
    async def test_foo(self, mock_refresh, mock_tp):

We avoid this complexity by using a single shared helper that applies all
patches at once, and by naming everything explicitly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from amc.factories import CharacterFactory, PlayerFactory
from amc.models import TeleportPoint, TeleportPortal, Wanted


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

JAIL_POINT = Point(5_000, 6_000, 7_000, srid=0)


async def _make_jail_tp():
    tp, _ = await TeleportPoint.objects.aget_or_create(
        name="jail",
        character=None,
        defaults={"location": JAIL_POINT},
    )
    return tp


async def _make_wanted_character():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    wanted = await Wanted.objects.acreate(
        character=character,
        amount=500_000,
        wanted_remaining=Wanted.INITIAL_WANTED_LEVEL,
    )
    return player, character, wanted


async def _make_clean_character():
    """A character with no Wanted record."""
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    return player, character


def _make_http_client_mod():
    return AsyncMock()


def _faction_patches():
    """Return a dict of all external-call patches needed for execute_arrest."""

    async def _empty_police():
        if False:
            yield  # make this an async generator that yields nothing

    return {
        "amc.commands.faction.teleport_player": AsyncMock(),
        "amc.commands.faction.force_exit_vehicle": AsyncMock(),
        "amc.commands.faction.transfer_money": AsyncMock(),
        "amc.commands.faction.record_treasury_confiscation_income": AsyncMock(),
        "amc.commands.faction.send_fund_to_player_wallet": AsyncMock(),
        "amc.commands.faction.refresh_player_name": AsyncMock(),
        "amc.commands.faction.show_popup": AsyncMock(),
        "amc.commands.faction.get_active_police_characters": _empty_police,
        # sleep is patched to avoid 1.5s real wait in tests
        "asyncio.sleep": AsyncMock(),
    }


# ---------------------------------------------------------------------------
# 1. /tp command (cmd_tp_name)
# ---------------------------------------------------------------------------


class TpCommandWantedJailTests(TestCase):
    """Wanted criminals can /tp freely — no arrest or block."""

    async def test_clean_player_not_arrested(self):
        """A player without a Wanted record can /tp normally — no arrest."""
        from amc.command_framework import CommandContext
        from amc.commands.teleport import cmd_tp_name

        player, character = await _make_clean_character()

        dest_point = Point(1_000, 2_000, 3_000, srid=0)
        await TeleportPoint.objects.aget_or_create(
            name="arena", character=None, defaults={"location": dest_point}
        )

        ctx = MagicMock(spec=CommandContext)
        ctx.character = character
        ctx.player = player
        ctx.player_info = {"bIsAdmin": False}
        ctx.http_client_mod = _make_http_client_mod()
        ctx.reply = AsyncMock()

        with patch("amc.commands.teleport.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": None}), \
             patch("amc.commands.teleport.teleport_player", new_callable=AsyncMock) as mock_cmd_tp, \
             patch("amc.commands.teleport.PoliceSession") as mock_ps:
            mock_ps.objects.filter.return_value.aexists = AsyncMock(return_value=False)
            await cmd_tp_name(ctx, "arena")

        # Not arrested — went to the named destination
        mock_cmd_tp.assert_awaited_once()
        dest = mock_cmd_tp.call_args[0][2]
        self.assertEqual(dest["X"], dest_point.x)

        await character.arefresh_from_db()
        self.assertIsNone(character.jailed_until)

    async def test_wanted_criminal_can_tp(self):
        """Wanted criminals can /tp freely — no arrest, goes to destination."""
        from amc.command_framework import CommandContext
        from amc.commands.teleport import cmd_tp_name

        player, character, _ = await _make_wanted_character()

        dest_point = Point(9_000, 8_000, 7_000, srid=0)
        await TeleportPoint.objects.aget_or_create(
            name="hub", character=None, defaults={"location": dest_point}
        )

        ctx = MagicMock(spec=CommandContext)
        ctx.character = character
        ctx.player = player
        ctx.player_info = {"bIsAdmin": False}
        ctx.http_client_mod = _make_http_client_mod()
        ctx.reply = AsyncMock()

        with patch("amc.commands.teleport.get_player_last_vehicle", new_callable=AsyncMock, return_value={"vehicle": None}), \
             patch("amc.commands.teleport.teleport_player", new_callable=AsyncMock) as mock_cmd_tp, \
             patch("amc.commands.teleport.PoliceSession") as mock_ps:
            mock_ps.objects.filter.return_value.aexists = AsyncMock(return_value=False)
            await cmd_tp_name(ctx, "hub")

        # Went directly to destination — no arrest
        mock_cmd_tp.assert_awaited_once()
        dest = mock_cmd_tp.call_args[0][2]
        self.assertEqual(dest["X"], dest_point.x)

        await character.arefresh_from_db()
        self.assertIsNone(character.jailed_until)


# ---------------------------------------------------------------------------
# 2. Portal zones (_check_pois_and_portals)
# ---------------------------------------------------------------------------


class PortalWantedJailTests(TestCase):
    """Wanted criminals who enter a portal are arrested instead of teleported."""

    async def test_wanted_criminal_entering_portal_gets_arrested(self):
        from amc.locations import _check_pois_and_portals

        await _make_jail_tp()
        player, character, _ = await _make_wanted_character()
        ctx = {"http_client_mod": _make_http_client_mod()}

        portal = await TeleportPortal.objects.acreate(
            name="Test Portal",
            source=Point(100000, 200000, 30000, srid=0),
            source_radius=120,
            target=Point(50000, 60000, 7000, srid=0),
            active=True,
        )
        source_point = portal.source
        source_radius = portal.source_radius
        old_loc = Point(source_point.x + source_radius + 500, source_point.y, source_point.z, srid=0)
        new_loc = Point(source_point.x, source_point.y, source_point.z, srid=0)

        faction_tp_mock = AsyncMock()
        with patch("amc.commands.faction.teleport_player", faction_tp_mock), \
             patch("amc.commands.faction.force_exit_vehicle", new_callable=AsyncMock), \
             patch("amc.commands.faction.transfer_money", new_callable=AsyncMock), \
             patch("amc.commands.faction.record_treasury_confiscation_income", new_callable=AsyncMock), \
             patch("amc.commands.faction.send_fund_to_player_wallet", new_callable=AsyncMock), \
             patch("amc.commands.faction.refresh_player_name", new_callable=AsyncMock), \
             patch("amc.commands.faction.show_popup", new_callable=AsyncMock), \
             patch("amc.commands.faction.get_active_police_characters", _faction_patches()["amc.commands.faction.get_active_police_characters"]), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await _check_pois_and_portals(character, old_loc, new_loc, ctx)

        # Arrest teleport to jail (not portal target)
        faction_tp_mock.assert_awaited()
        dest = faction_tp_mock.call_args[0][2]
        self.assertEqual(dest["X"], JAIL_POINT.x)
        self.assertEqual(dest["Y"], JAIL_POINT.y)

        # Wanted expired, character jailed
        wanted = await Wanted.objects.aget(character=character)
        self.assertIsNotNone(wanted.expired_at)
        await character.arefresh_from_db()
        self.assertIsNotNone(character.jailed_until)

    async def test_clean_player_goes_through_portal_normally(self):
        from amc.locations import _check_pois_and_portals

        player, character = await _make_clean_character()
        ctx = {"http_client_mod": _make_http_client_mod()}

        portal = await TeleportPortal.objects.acreate(
            name="Test Portal",
            source=Point(100000, 200000, 30000, srid=0),
            source_radius=120,
            target=Point(50000, 60000, 7000, srid=0),
            active=True,
        )
        source_point = portal.source
        source_radius = portal.source_radius
        target_point = portal.target
        old_loc = Point(source_point.x + source_radius + 500, source_point.y, source_point.z, srid=0)
        new_loc = Point(source_point.x, source_point.y, source_point.z, srid=0)

        faction_tp_mock = AsyncMock()
        with patch("amc.commands.faction.teleport_player", faction_tp_mock), \
             patch("amc.locations.teleport_player", new_callable=AsyncMock) as mock_portal_tp:
            await _check_pois_and_portals(character, old_loc, new_loc, ctx)

        # Clean player goes to portal destination (via locations.teleport_player)
        mock_portal_tp.assert_awaited_once()
        dest = mock_portal_tp.call_args[0][2]
        self.assertEqual(dest["X"], target_point.x)

        # Arrest flow NOT triggered
        faction_tp_mock.assert_not_awaited()
        await character.arefresh_from_db()
        self.assertIsNone(character.jailed_until)


# ---------------------------------------------------------------------------
# 3. Webhook events (_handle_teleport_or_respawn)
# ---------------------------------------------------------------------------


class WebhookTeleportNoArrestTests(TestCase):
    """Webhook teleport events no longer trigger auto-arrest."""

    def _make_event(self, hook="ServerTeleportCharacter"):
        return {
            "hook": hook,
            "timestamp": timezone.now().timestamp(),
            "data": {"CharacterGuid": "test-guid"},
        }

    def _make_ctx(self):
        from amc.webhook_context import EventContext
        ctx = MagicMock(spec=EventContext)
        ctx.is_rp_mode = False
        ctx.http_client_mod = _make_http_client_mod()
        ctx.http_client = AsyncMock()
        return ctx

    async def test_clean_player_not_arrested_on_teleport_event(self):
        from amc.handlers.teleport import _handle_teleport_or_respawn

        player, character = await _make_clean_character()
        event = self._make_event("ServerTeleportCharacter")
        ctx = self._make_ctx()

        faction_tp_mock = AsyncMock()
        with patch("amc.commands.faction.teleport_player", faction_tp_mock):
            result = await _handle_teleport_or_respawn(event, character, ctx)

        self.assertEqual(result, (0, 0, 0, 0))
        faction_tp_mock.assert_not_awaited()

        await character.arefresh_from_db()
        self.assertIsNone(character.jailed_until)

    async def test_expired_wanted_player_not_arrested(self):
        """A player whose Wanted has expired is treated as clean."""
        from amc.handlers.teleport import _handle_teleport_or_respawn

        player, character, wanted = await _make_wanted_character()
        wanted.expired_at = timezone.now()
        wanted.wanted_remaining = 0
        await wanted.asave(update_fields=["expired_at", "wanted_remaining"])

        event = self._make_event("ServerTeleportCharacter")
        ctx = self._make_ctx()

        faction_tp_mock = AsyncMock()
        with patch("amc.commands.faction.teleport_player", faction_tp_mock):
            await _handle_teleport_or_respawn(event, character, ctx)

        faction_tp_mock.assert_not_awaited()

    async def test_wanted_player_not_arrested_on_teleport_event(self):
        """Active wanted players are NOT arrested by webhook teleport events."""
        from amc.handlers.teleport import _handle_teleport_or_respawn

        player, character, wanted = await _make_wanted_character()
        event = self._make_event("ServerTeleportCharacter")
        ctx = self._make_ctx()

        faction_tp_mock = AsyncMock()
        with patch("amc.commands.faction.teleport_player", faction_tp_mock):
            result = await _handle_teleport_or_respawn(event, character, ctx)

        self.assertEqual(result, (0, 0, 0, 0))

        # No arrest — faction teleport_player not called
        faction_tp_mock.assert_not_awaited()

        # Wanted not expired
        await wanted.arefresh_from_db()
        self.assertIsNone(wanted.expired_at)

        # Character not jailed
        await character.arefresh_from_db()
        self.assertIsNone(character.jailed_until)
