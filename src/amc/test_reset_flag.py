"""Roadside-recovery flag.

ServerResetVehicleAt where the vehicle was recovered from >1 km away
(RESET_FAR_RECOVERY_UNITS) flags the character: opaque popup + Discord
alert, and every cargo arrival from that character for the next
CARGO_IGNORE_SECONDS is ignored entirely (no logs, no payment).

The earlier near-delivery-point trigger was removed (freeman 2026-09-26):
legit long-distance tow jobs deliver wrecks right at delivery points and
false-flagged towers.
"""

import asyncio
import itertools
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from asgiref.sync import sync_to_async

from amc.factories import CharacterFactory, DeliveryPointFactory, PlayerFactory
from amc.handlers.teleport import (
    FLAG_POPUP_TEXT,
    RESET_FAR_RECOVERY_UNITS,
)
from amc.models import ServerCargoArrivedLog


# Strictly increasing timestamps: the webhook dedup watermark drops events
# whose timestamp is <= the last processed one, so same-second test events
# would silently vanish when the suite runs together.
_EVENT_TS = itertools.count(int(time.time()))


def _reset_event(guid):
    return {
        "hook": "ServerResetVehicleAt",
        "timestamp": next(_EVENT_TS),
        "data": {"CharacterGuid": str(guid)},
    }


def _cargo_event(character_guid):
    return {
        "hook": "ServerCargoArrived",
        "timestamp": next(_EVENT_TS),
        "data": {
            "PlayerId": "1",
            "CharacterGuid": str(character_guid),
            "Cargos": [
                {
                    "Net_CargoKey": "oranges",
                    "Net_Payment": 10_000,
                    "Net_Weight": 100.0,
                    "Net_Damage": 0.0,
                    "Net_SenderAbsoluteLocation": {"X": 0, "Y": 0, "Z": 0},
                    "Net_DestinationLocation": {"X": 1000, "Y": 1000, "Z": 0},
                }
            ],
        },
    }


class ResetNearDeliveryPointTests(TestCase):
    async def _setup(self, *, x=0, y=0, last_online_age_s=0):
        await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(
            guid="reset-flag-guid",
            last_location=Point(x, y, 0),
            last_online=timezone.now() - timedelta(seconds=last_online_age_s),
        )
        return character

    async def _dp(self, guid, name, x, y):
        return await sync_to_async(DeliveryPointFactory)(
            guid=guid, name=name, coord=Point(x, y, 0, srid=3857)
        )

    @patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock, return_value=100_000)
    @patch("amc.webhook.announce", new_callable=AsyncMock)
    async def test_reset_on_delivery_point_does_not_flag(
        self, mock_announce, mock_treasury
    ):
        """Near-DP trigger removed (freeman 2026-09-26): legit tow jobs land
        wrecks right at delivery points — a reset parked on a DP must NOT
        flag by proximity alone."""
        from amc.webhook import process_events

        character = await self._setup(x=0, y=0)
        await self._dp("dp-rf-1", "Mine", 1, 1)  # ~1.4 units away

        with patch("amc.handlers.teleport.show_popup", new_callable=AsyncMock) as popup:
            await process_events(
                [_reset_event(character.guid)],
                http_client=MagicMock(),
                http_client_mod=MagicMock(),
            )

        await character.arefresh_from_db()
        assert character.cargo_ignore_until is None
        popup.assert_not_called()

    @patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock, return_value=100_000)
    @patch("amc.webhook.announce", new_callable=AsyncMock)
    async def test_reset_far_from_dp_does_not_flag(
        self, mock_announce, mock_treasury
    ):
        from amc.webhook import process_events

        character = await self._setup(x=0, y=0)
        await self._dp("dp-rf-2", "Far DP", 5_000_000, 5_000_000)

        with patch("amc.handlers.teleport.show_popup", new_callable=AsyncMock) as popup:
            await process_events(
                [_reset_event(character.guid)],
                http_client=MagicMock(),
                http_client_mod=MagicMock(),
            )

        await character.arefresh_from_db()
        assert character.cargo_ignore_until is None
        popup.assert_not_called()

    @patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock, return_value=100_000)
    @patch("amc.webhook.announce", new_callable=AsyncMock)
    async def test_stale_location_does_not_flag(
        self, mock_announce, mock_treasury
    ):
        from amc.webhook import process_events

        character = await self._setup(x=0, y=0, last_online_age_s=600)

        with patch("amc.handlers.teleport.show_popup", new_callable=AsyncMock):
            await process_events(
                [_reset_event(character.guid)],
                http_client=MagicMock(),
                http_client_mod=MagicMock(),
            )

        await character.arefresh_from_db()
        assert character.cargo_ignore_until is None

    @patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock, return_value=100_000)
    @patch("amc.webhook.announce", new_callable=AsyncMock)
    async def test_far_recovery_flags(
        self, mock_announce, mock_treasury
    ):
        """Roadside recovery that teleported the vehicle >1 km to the
        character flags them even away from any delivery point."""
        from amc.models import CharacterLocation
        from amc.webhook import process_events

        character = await self._setup(x=0, y=0)
        # No delivery points needed — trigger 2 only.
        # The vehicle's last known driving position: 150,000 units (1.5 km)
        # away, observed 2 minutes ago.
        await CharacterLocation.objects.acreate(
            character=character,
            location=Point(150_000, 0, 0),
            vehicle_key="Golima_Semi",
            timestamp=timezone.now() - timedelta(minutes=2),
        )

        posted = []
        with patch(
            "amc.handlers.teleport.show_popup", new_callable=AsyncMock
        ) as _popup, patch(
            "amc.pipeline.discord.post_discord_reset_flag_alert",
            side_effect=lambda *a, **k: posted.append(k),
        ):
            await process_events(
                [_reset_event(character.guid)],
                http_client=MagicMock(),
                http_client_mod=MagicMock(),
            )
            for _ in range(20):
                await asyncio.sleep(0.05)

        await character.arefresh_from_db()
        assert character.cargo_ignore_until is not None
        assert posted and "1.5 km" in str(posted[0].get("detail", "")) or posted and "1500 m" in str(posted[0].get("detail", ""))

    @patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock, return_value=100_000)
    @patch("amc.webhook.announce", new_callable=AsyncMock)
    async def test_nearby_recovery_does_not_flag(
        self, mock_announce, mock_treasury
    ):
        """Vehicle was parked right there — normal recovery, no flag."""
        from amc.models import CharacterLocation
        from amc.webhook import process_events

        character = await self._setup(x=0, y=0)
        await CharacterLocation.objects.acreate(
            character=character,
            location=Point(5_000, 0, 0),  # 50 m away
            vehicle_key="Golima_Semi",
            timestamp=timezone.now() - timedelta(minutes=1),
        )

        with patch("amc.handlers.teleport.show_popup", new_callable=AsyncMock):
            await process_events(
                [_reset_event(character.guid)],
                http_client=MagicMock(),
                http_client_mod=MagicMock(),
            )

        await character.arefresh_from_db()
        assert character.cargo_ignore_until is None

    @patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock, return_value=100_000)
    @patch("amc.webhook.announce", new_callable=AsyncMock)
    async def test_flagged_cargo_arrivals_are_ignored(
        self, mock_announce, mock_treasury
    ):
        from amc.webhook import process_event

        character = await self._setup(x=0, y=0)
        character.cargo_ignore_until = timezone.now() + timedelta(seconds=30)
        await character.asave(update_fields=["cargo_ignore_until"])

        result = await process_event(
            _cargo_event(character.guid),
            character.player,
            character,
        )
        assert result == (0, 0, 0, 0)
        assert await ServerCargoArrivedLog.objects.acount() == 0

    @patch("amc.webhook.get_treasury_fund_balance", new_callable=AsyncMock, return_value=100_000)
    @patch("amc.webhook.announce", new_callable=AsyncMock)
    async def test_expired_flag_processes_normally(
        self, mock_announce, mock_treasury
    ):
        from amc.webhook import process_event

        character = await self._setup(x=0, y=0)
        character.cargo_ignore_until = timezone.now() - timedelta(seconds=1)
        await character.asave(update_fields=["cargo_ignore_until"])

        await process_event(
            _cargo_event(character.guid),
            character.player,
            character,
        )
        assert await ServerCargoArrivedLog.objects.acount() == 1


def test_flag_popup_copy_is_opaque():
    assert "flagged" in FLAG_POPUP_TEXT.lower()
    assert "60" not in FLAG_POPUP_TEXT
    assert "second" not in FLAG_POPUP_TEXT.lower()
    assert "admin" in FLAG_POPUP_TEXT.lower()
    assert RESET_FAR_RECOVERY_UNITS == 100_000  # 1 km
