"""Tests for the police teleport-redirect backstop (amc.handlers.teleport).

_redirect_police_near_wanted must send an on-duty officer who lands near a
wanted suspect to the nearest station OUTSIDE the 1 km wanted radius of
every online active-wanted suspect. The officer-nearest station can be the
very one the suspect is parked at — without the radius constraint the
redirect drops the cop back inside the radius and re-fires the handler on
its own ServerTeleportCharacter (a teleport loop).
"""

import math
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from django.utils import timezone

from amc.commands.police import SETWANTED_MIN_DISTANCE
from amc.factories import CharacterFactory, PlayerFactory
from amc.handlers.teleport import _redirect_police_near_wanted
from amc.models import Player, PoliceSession, Wanted


def _make_player_data(unique_id, character_guid, x, y, z):
    """Build a fake player dict matching the game server /player/list format."""
    return {
        "unique_id": str(unique_id),
        "character_guid": character_guid,
        "location": f"X={x} Y={y} Z={z}",
    }


def _make_players_list(player_datas):
    return [(d["unique_id"], d) for d in player_datas]


# Jeju Police Station (POLICE_STATIONS[0]) and a point 200 m east of it
_JEJU_STATION = (-42361, -141792, -21094)
_NEAR_STATION = (-22361, -141792, -21094)  # 200 m east — inside the radius
# Seoguipo Police Station (POLICE_STATIONS[2]) — the nearest station to the
# officer at _NEAR_STATION that is OUTSIDE the suspect's 1 km radius
_SEOGUIPO_STATION = (-8776, 144044, -21084)


class PoliceRedirectTests(TestCase):
    """Redirect backstop: target station must be outside the wanted radius."""

    async def _make_character(self):
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            player=player,
            last_online=timezone.now(),
        )
        await character.asave(update_fields=["last_online"])
        return player, character

    async def test_redirect_targets_station_outside_radius(self):
        """Suspect parked AT a station: the redirect must NOT pick that
        station (officer-nearest) — it picks the nearest station outside the
        suspect's 1 km radius instead."""
        cop_player, cop = await self._make_character()
        sus_player, suspect = await self._make_character()
        flag_player, flag_admin = await self._make_character()
        try:
            await PoliceSession.objects.acreate(character=cop)
            await Wanted.objects.acreate(
                character=suspect, wanted_remaining=600, set_by=flag_admin
            )

            players = _make_players_list([
                _make_player_data(cop_player.unique_id, cop.guid, *_NEAR_STATION),
                _make_player_data(sus_player.unique_id, suspect.guid, *_JEJU_STATION),
            ])

            ctx = MagicMock()
            ctx.http_client = object()
            ctx.http_client_mod = object()

            with patch(
                "amc.handlers.teleport.get_players",
                new_callable=AsyncMock,
                return_value=players,
            ), patch(
                "amc.handlers.teleport.teleport_player", new_callable=AsyncMock
            ) as mock_tp, patch(
                "amc.handlers.teleport.send_system_message", new_callable=AsyncMock
            ):
                await _redirect_police_near_wanted(cop, cop_player, ctx, "Teleport")

            mock_tp.assert_awaited_once()
            coords = mock_tp.await_args.args[2]
            station = (coords["X"], coords["Y"], coords["Z"])

            # NOT the suspect-adjacent station the old code picked
            self.assertNotEqual(station, _JEJU_STATION)
            # Outside the suspect's 1 km radius
            dist = math.sqrt(sum(
                (a - b) ** 2 for a, b in zip(station, _JEJU_STATION)
            ))
            self.assertGreaterEqual(dist, SETWANTED_MIN_DISTANCE)
            # Nearest qualifying station to the officer: Seoguipo
            self.assertEqual(station, _SEOGUIPO_STATION)
        finally:
            await PoliceSession.objects.filter(character=cop).adelete()
            await Wanted.objects.filter(character=suspect).adelete()
            await Player.objects.filter(
                unique_id__in=[cop_player.unique_id, sus_player.unique_id,
                               flag_player.unique_id]
            ).adelete()

    async def test_no_redirect_when_officer_not_near_suspect(self):
        """Officer far from every suspect: no redirect fires."""
        cop_player, cop = await self._make_character()
        sus_player, suspect = await self._make_character()
        flag_player, flag_admin = await self._make_character()
        try:
            await PoliceSession.objects.acreate(character=cop)
            await Wanted.objects.acreate(
                character=suspect, wanted_remaining=600, set_by=flag_admin
            )

            # Suspect at Jeju station, officer at Seoguipo (~2.9 km apart)
            players = _make_players_list([
                _make_player_data(cop_player.unique_id, cop.guid, *_SEOGUIPO_STATION),
                _make_player_data(sus_player.unique_id, suspect.guid, *_JEJU_STATION),
            ])

            ctx = MagicMock()
            ctx.http_client = object()
            ctx.http_client_mod = object()

            with patch(
                "amc.handlers.teleport.get_players",
                new_callable=AsyncMock,
                return_value=players,
            ), patch(
                "amc.handlers.teleport.teleport_player", new_callable=AsyncMock
            ) as mock_tp:
                await _redirect_police_near_wanted(cop, cop_player, ctx, "Teleport")

            mock_tp.assert_not_awaited()
        finally:
            await PoliceSession.objects.filter(character=cop).adelete()
            await Wanted.objects.filter(character=suspect).adelete()
            await Player.objects.filter(
                unique_id__in=[cop_player.unique_id, sus_player.unique_id,
                               flag_player.unique_id]
            ).adelete()

    async def test_no_qualifying_station_skips_redirect(self):
        """When every station is inside a suspect's radius, the redirect is
        skipped (logged) rather than teleporting the officer back inside."""
        cop_player, cop = await self._make_character()
        sus_player, suspect = await self._make_character()
        flag_player, flag_admin = await self._make_character()
        try:
            await PoliceSession.objects.acreate(character=cop)
            await Wanted.objects.acreate(
                character=suspect, wanted_remaining=600, set_by=flag_admin
            )

            players = _make_players_list([
                _make_player_data(cop_player.unique_id, cop.guid, *_NEAR_STATION),
                _make_player_data(sus_player.unique_id, suspect.guid, *_JEJU_STATION),
            ])

            ctx = MagicMock()
            ctx.http_client = object()
            ctx.http_client_mod = object()

            only_bad_station = [("OnlyStation", *_JEJU_STATION)]
            with patch(
                "amc.handlers.teleport.get_players",
                new_callable=AsyncMock,
                return_value=players,
            ), patch(
                "amc.handlers.teleport.POLICE_STATIONS", only_bad_station
            ), patch(
                "amc.handlers.teleport.teleport_player", new_callable=AsyncMock
            ) as mock_tp:
                await _redirect_police_near_wanted(cop, cop_player, ctx, "Teleport")

            mock_tp.assert_not_awaited()
        finally:
            await PoliceSession.objects.filter(character=cop).adelete()
            await Wanted.objects.filter(character=suspect).adelete()
            await Player.objects.filter(
                unique_id__in=[cop_player.unique_id, sus_player.unique_id,
                               flag_player.unique_id]
            ).adelete()
