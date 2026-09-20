"""Smuggling / load cargo handler.

Handles: ServerLoadCargo
"""

from __future__ import annotations

import asyncio
import logging
import os

from django.core.cache import cache

from amc.handlers import register
from amc.mod_detection import detect_custom_parts, POLICE_DUTY_WHITELIST
from amc.mod_server import get_player_last_vehicle, get_player_last_vehicle_parts, show_popup
from amc.game_server import announce
from amc.models import PoliceSession
from amc.special_cargo import ILLICIT_CARGO_KEYS

logger = logging.getLogger("amc.webhook.handlers.smuggling")

SMUGGLING_TIPOFF_ENABLED = os.environ.get("SMUGGLING_TIPOFF_ENABLED", "").lower() in (
    "1",
    "true",
    "yes",
)
SMUGGLING_TIPOFF_DELAY = 15  # seconds — delay before broadcasting
SMUGGLING_TIPOFF_COOLDOWN = 60  # seconds — throttle window per player


@register("ServerLoadCargo")
async def handle_load_cargo(event, player, character, ctx):
    if not ctx.http_client_mod:
        return 0, 0, 0, 0

    cargo = event["data"].get("Cargo", {})
    cargo_key = cargo.get("Net_CargoKey", "")
    if cargo_key not in ILLICIT_CARGO_KEYS:
        return 0, 0, 0, 0

    is_on_duty = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()

    # Mark-as-criminal on LOAD is gone: the criminal score (and thus any
    # suspect/criminal status) accumulates only on DELIVERY.
    # Throttled smuggling tip-off announcement
    if SMUGGLING_TIPOFF_ENABLED and ctx.http_client:
        tipoff_cache_key = f"smuggling_tipoff:{character.guid}"
        already_tipped = await cache.aget(tipoff_cache_key)
        if not already_tipped:
            await cache.aset(tipoff_cache_key, True, timeout=SMUGGLING_TIPOFF_COOLDOWN)
            asyncio.create_task(
                _announce_smuggling_tipoff_after_delay(
                    ctx.http_client,
                    delay=SMUGGLING_TIPOFF_DELAY,
                )
            )

    try:
        last_vehicle, parts_data = await asyncio.gather(
            get_player_last_vehicle(ctx.http_client_mod, str(character.guid)),
            get_player_last_vehicle_parts(
                ctx.http_client_mod, str(character.guid), complete=False
            ),
        )
        main_vehicle = last_vehicle.get("vehicle")
        if not main_vehicle:
            return 0, 0, 0, 0

        # Whitelist police parts for officers on active duty
        whitelist = None
        if is_on_duty:
            whitelist = POLICE_DUTY_WHITELIST
        custom_parts = detect_custom_parts(
            parts_data.get("parts", []), whitelist=whitelist
        )
        if custom_parts:
            asyncio.create_task(
                show_popup(
                    ctx.http_client_mod,
                    "You are now allowed to use modified vehicles for criminal gameplay",
                    character_guid=character.guid,
                    player_id=str(player.unique_id),
                )
            )
    except Exception as e:
        logger.warning(f"Failed to check custom parts for load cargo: {e}")

    return 0, 0, 0, 0


async def _announce_smuggling_tipoff_after_delay(
    http_client, delay=SMUGGLING_TIPOFF_DELAY
):
    """Wait for the delay, then announce a vague smuggling tip-off."""
    await asyncio.sleep(delay)
    await announce(
        "Intelligence reports suggest a smuggling operation is underway",
        http_client,
        color="E67E22",
    )
