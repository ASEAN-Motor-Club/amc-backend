"""Tests for the in-game rent-blocked popups.

The popup must tell the player WHICH plot they just tried to rent (house key)
and HOW to rent it through Discord, since in-game renting is blocked.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amc.handlers.house import handle_rent, handle_rent_extend
from amc.webhook_context import EventContext

HOUSE_GUID = "a" * 32
CHARACTER_GUID = "c" * 32
HOUSE_KEY = "Aewol_01"


def _make_ctx():
    return EventContext(
        http_client=None,
        http_client_mod=AsyncMock(),
        discord_client=None,
        treasury_balance=0,
        is_rp_mode=False,
        used_shortcut=False,
        active_term=None,
    )


def _make_event(hook, house_guid=HOUSE_GUID, money=None):
    data = {"HouseGuid": house_guid, "Blocked": True}
    if money is not None:
        data["Money"] = money
    return {"hook": hook, "data": data}


def _make_player():
    return MagicMock(unique_id=76561198000000000)


def _make_character():
    return MagicMock(guid=CHARACTER_GUID)


def _popup_call(popup):
    call = popup.await_args
    assert call is not None
    return call


def _rent_info(**overrides):
    info = {
        "HousegKey": HOUSE_KEY,
        "HousingPlotRentalPriceRatio": 5.0,
        "MaxHousingPlotRentalDays": 15,
    }
    info.update(overrides)
    return info


@pytest.mark.asyncio
async def test_blocked_rent_popup_shows_house_key_and_discord_steps():
    popup = AsyncMock()
    refund = AsyncMock()

    with (
        patch(
            "amc.handlers.house.get_rent_info",
            new_callable=AsyncMock,
            return_value=_rent_info(),
        ),
        patch(
            "amc.handlers.house.House.objects.aget",
            new_callable=AsyncMock,
            return_value=MagicMock(cost=10000),
        ),
        patch(
            "amc.handlers.house.get_houses",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "amc.handlers.house.get_market_multiplier",
            new_callable=AsyncMock,
            return_value=(1.0, 0),
        ),
        patch("amc.handlers.house.transfer_money", refund),
        patch("amc.handlers.house.show_popup", popup),
    ):
        await handle_rent(
            _make_event("ServerRentHouse"),
            _make_player(),
            _make_character(),
            _make_ctx(),
        )

    refund.assert_awaited_once()
    popup.assert_awaited_once()
    text = _popup_call(popup).args[1]
    assert HOUSE_KEY in text
    assert "/house buy" in text
    assert "Discord" in text
    assert "refunded" in text
    # Character-addressed, never server-wide.
    assert _popup_call(popup).kwargs["character_guid"] == CHARACTER_GUID


@pytest.mark.asyncio
async def test_blocked_rent_popup_omits_refund_line_when_refund_fails():
    popup = AsyncMock()
    refund = AsyncMock(side_effect=RuntimeError("bank down"))

    with (
        patch(
            "amc.handlers.house.get_rent_info",
            new_callable=AsyncMock,
            return_value=_rent_info(),
        ),
        patch(
            "amc.handlers.house.House.objects.aget",
            new_callable=AsyncMock,
            return_value=MagicMock(cost=10000),
        ),
        patch(
            "amc.handlers.house.get_houses",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "amc.handlers.house.get_market_multiplier",
            new_callable=AsyncMock,
            return_value=(1.0, 0),
        ),
        patch("amc.handlers.house.transfer_money", refund),
        patch("amc.handlers.house.show_popup", popup),
    ):
        await handle_rent(
            _make_event("ServerRentHouse"),
            _make_player(),
            _make_character(),
            _make_ctx(),
        )

    popup.assert_awaited_once()
    text = _popup_call(popup).args[1]
    assert HOUSE_KEY in text
    assert "refunded" not in text


@pytest.mark.asyncio
async def test_blocked_rent_popup_falls_back_to_guid_when_rent_info_missing():
    popup = AsyncMock()

    with (
        patch(
            "amc.handlers.house.get_rent_info",
            new_callable=AsyncMock,
            side_effect=RuntimeError("mod offline"),
        ),
        patch("amc.handlers.house.transfer_money", AsyncMock()),
        patch("amc.handlers.house.show_popup", popup),
    ):
        await handle_rent(
            _make_event("ServerRentHouse"),
            _make_player(),
            _make_character(),
            _make_ctx(),
        )

    popup.assert_awaited_once()
    text = _popup_call(popup).args[1]
    assert HOUSE_GUID in text
    assert "/house buy" in text


@pytest.mark.asyncio
async def test_blocked_extend_popup_shows_house_key_and_discord_steps():
    popup = AsyncMock()

    with (
        patch(
            "amc.handlers.house.get_rent_info",
            new_callable=AsyncMock,
            return_value=_rent_info(),
        ),
        patch("amc.handlers.house.show_popup", popup),
    ):
        await handle_rent_extend(
            _make_event("ServerRentExtendHouse", money=5000),
            _make_player(),
            _make_character(),
            _make_ctx(),
        )

    popup.assert_awaited_once()
    text = _popup_call(popup).args[1]
    assert HOUSE_KEY in text
    assert "/house extend" in text
    assert _popup_call(popup).kwargs["character_guid"] == CHARACTER_GUID
