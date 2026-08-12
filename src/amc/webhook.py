"""Webhook event processing — orchestrator.

This module is the entry point for webhook event processing.
Domain logic lives in amc/handlers/ and amc/pipeline/.
Handler modules are imported at the bottom to trigger registration.

For backward compatibility, all previously-exported symbols are
re-exported from their new locations.
"""

import json
import logging
import asyncio
import itertools
import os
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from amc.handlers import dispatch
from amc.webhook_context import EventContext

# ---------------------------------------------------------------------------
# Trigger handler registration (side-effect imports)
# ---------------------------------------------------------------------------
import amc.handlers.cargo  # noqa: F401
import amc.handlers.passenger  # noqa: F401
import amc.handlers.tow  # noqa: F401
import amc.handlers.contract  # noqa: F401
import amc.handlers.police  # noqa: F401
import amc.handlers.teleport  # noqa: F401
import amc.handlers.smuggling  # noqa: F401
import amc.handlers.events  # noqa: F401
import amc.handlers.chat  # noqa: F401
import amc.handlers.customization  # noqa: F401
import amc.handlers.house  # noqa: F401

# ---------------------------------------------------------------------------
# Backward-compatible re-exports — symbols imported by other modules or
# patched in tests via patch("amc.webhook.X").  Not used directly here.
# ---------------------------------------------------------------------------
from amc.pipeline.dedup import (
    LAST_SEQ_CACHE_KEY,  # noqa: F401
    LAST_TS_CACHE_KEY,  # noqa: F401
    LAST_EPOCH_CACHE_KEY,  # noqa: F401
    deduplicate_events,
    persist_watermarks,
)
from amc.pipeline.profit import (
    on_player_profit,  # noqa: F401
    on_player_profits,
    split_party_payment,
    PARTY_BONUS_ENABLED,
    PARTY_BONUS_RATE,  # noqa: F401
)
from amc.pipeline.delivery import atomic_process_delivery  # noqa: F401
from amc.pipeline.discord import post_discord_delivery_embed  # noqa: F401
from amc.handlers.cargo import process_cargo_log  # noqa: F401
from amc.handlers.police import handle_pickup_cargo  # noqa: F401
from amc.handlers.teleport import (
    _handle_teleport_or_respawn as handle_teleport_or_respawn,  # noqa: F401
)
from amc.handlers.smuggling import (
    SMUGGLING_TIPOFF_ENABLED,  # noqa: F401
    SMUGGLING_TIPOFF_DELAY,  # noqa: F401
    SMUGGLING_TIPOFF_COOLDOWN,  # noqa: F401
    _announce_smuggling_tipoff_after_delay,  # noqa: F401
)
from amc.mod_server import show_popup  # noqa: F401
from amc.mod_server import (
    get_webhook_events2,
    get_rp_mode,  # noqa: F401  (kept importable so tests' @patch("amc.webhook.get_rp_mode") remains resolvable)
    transfer_money,
    get_parties,
)
from amc.game_server import announce  # noqa: F401
from amc.subsidies import subsidise_player, set_aside_player_savings  # noqa: F401
from amc_finance.loans import repay_loan_for_profit  # noqa: F401
from amc_finance.services import (
    get_treasury_fund_balance,
    record_treasury_confiscation_income,  # noqa: F401
    send_fund_to_player_wallet,  # noqa: F401
)
from amc.mod_server import send_system_message, despawn_player_cargo  # noqa: F401
from amc.mod_detection import detect_custom_parts  # noqa: F401
from amc.jobs import on_delivery_job_fulfilled  # noqa: F401
from amc.models import Character, Player, MinistryTerm
from amc.utils import skip_if_running

logger = logging.getLogger("amc.webhook")

WEBHOOK_SSE_ENABLED = os.environ.get("WEBHOOK_SSE_ENABLED", "").lower() in (
    "1",
    "true",
    "yes",
)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


@skip_if_running
async def monitor_webhook(ctx):
    http_client = ctx.get("http_client")
    http_client_mod = ctx.get("http_client_mod")
    http_client_webhook = ctx.get("http_client_webhook")
    discord_client = ctx.get("discord_client")
    events = await get_webhook_events2(http_client_webhook)
    await process_events(events, http_client, http_client_mod, discord_client)


@skip_if_running
async def monitor_webhook_test(ctx):
    http_client = ctx.get("http_client_test")
    http_client_mod = ctx.get("http_client_test_mod")
    http_client_webhook = ctx.get("http_client_test_webhook")
    discord_client = ctx.get("discord_client")
    try:
        events = await get_webhook_events2(http_client_webhook)
    except Exception as e:
        print(f"Failed to get webhook events: {e}")
        return
    await process_events(events, http_client, http_client_mod, discord_client)


# ---------------------------------------------------------------------------
# Event aggregation
# ---------------------------------------------------------------------------


def aggregate_homogenous_events(sorted_events):
    grouped_events = itertools.groupby(
        sorted_events, key=lambda e: (e["key_id"], e["hook"])
    )
    aggregated_events = []

    for key, group in grouped_events:
        if not key[0]:  # key_id
            continue

        group_events = list(group)
        match key[1]:  # hook
            case "ServerCargoArrived":
                cargos = [
                    cargo for event in group_events for cargo in event["data"]["Cargos"]
                ]
                aggregated_events.append(
                    {
                        "hook": key[1],
                        "timestamp": group_events[0]["timestamp"],
                        "data": {
                            "CharacterGuid": key[0],
                            "Cargos": cargos,
                        },
                    }
                )
            case "ServerResetVehicleAt":
                aggregated_events.append(
                    {
                        "hook": key[1],
                        "timestamp": group_events[0]["timestamp"],
                        "data": {
                            "CharacterGuid": key[0],
                            "VehicleId": group_events[0]["data"].get("VehicleId"),
                        },
                    }
                )
            case _:
                aggregated_events.extend(group_events)
    return aggregated_events


# ---------------------------------------------------------------------------
# Main pipeline: process_events → process_event (via registry)
# ---------------------------------------------------------------------------


async def process_events(
    events, http_client=None, http_client_mod=None, discord_client=None
):
    events, max_seq, last_processed = deduplicate_events(events)

    if not events:
        return

    # Pre-process events: tag each with a character key and split into
    # character-linked vs character-less buckets.  Character-less events
    # (e.g. ServerAddEvent for ownerless auto-events) don't go through
    # the profit pipeline — they are dispatched directly to their handler.
    character_events = []
    characterless_events = []
    for event in events:
        player_id = event["data"].get("CharacterGuid", "")
        if not player_id:
            player_id = event["data"].get("PlayerId", "")
        event["key_id"] = player_id
        if player_id:
            character_events.append(event)
        else:
            characterless_events.append(event)

    # --- Dispatch character-less events directly (race events, etc.) ---
    if characterless_events:
        ctx = EventContext(
            http_client=http_client,
            http_client_mod=http_client_mod,
            discord_client=discord_client,
        )
        for event in characterless_events:
            try:
                await dispatch(event["hook"], event, None, None, ctx)
            except Exception:
                logger.exception(
                    "Failed to dispatch character-less event %s",
                    event.get("hook"),
                )

    if not character_events:
        # Nothing for the character-linked pipeline — just persist watermarks.
        persist_watermarks(max_seq, last_processed, events)
        return

    # --- Character-linked pipeline (cargo, profit, etc.) ---

    def key_fn(event):
        return (event["key_id"], event["hook"])

    sorted_events = sorted(character_events, key=key_fn)
    aggregated_events = aggregate_homogenous_events(sorted_events)

    def key_by_character(event):
        player_id = event["data"].get("CharacterGuid", "")
        if not player_id:
            player_id = event["data"].get("PlayerId", "")
        return player_id

    sorted_player_events = sorted(aggregated_events, key=key_by_character)
    grouped_player_events = itertools.groupby(
        sorted_player_events, key=key_by_character
    )

    player_profits = []

    treasury_balance = await get_treasury_fund_balance()
    parties = (
        await get_parties(http_client_mod)
        if (PARTY_BONUS_ENABLED and http_client_mod)
        else []
    )
    active_term = await MinistryTerm.objects.filter(is_active=True).afirst()

    for character_guid, es in grouped_player_events:
        if not character_guid:
            continue

        try:
            character_q = Q(guid=character_guid, guid__isnull=False)
            try:
                character_q = character_q | Q(player__unique_id=int(character_guid))
            except ValueError:
                pass

            character = await (
                Character.objects.select_related("player")
                .with_last_login()
                .filter(character_q)
                .order_by("-last_login")
                .afirst()
            )
            if not character:
                continue
            player = character.player
        except Player.DoesNotExist:
            continue

        total_base_payment = 0
        total_subsidy = 0
        total_contract_payment = 0
        total_clawback = 0

        is_rp_mode = character.rp_mode
        used_shortcut = (
            character.shortcut_zone_entered_at is not None
            and character.shortcut_zone_entered_at > timezone.now() - timedelta(hours=1)
        )

        for event in es:
            try:
                base_pay, subsidy, contract_pay, clawback = await process_event(
                    event,
                    player,
                    character,
                    is_rp_mode,
                    used_shortcut,
                    treasury_balance,
                    http_client,
                    http_client_mod,
                    discord_client,
                    active_term=active_term,
                )
                total_base_payment += base_pay
                total_subsidy += subsidy
                total_contract_payment += contract_pay
                total_clawback += clawback
            except Exception as e:
                event_str = json.dumps(event)
                asyncio.create_task(
                    show_popup(
                        http_client_mod,
                        f"Webhook failed, please send to discord:\n{e}\n{event_str}",
                        character_guid=character.guid,
                    )
                )
                raise e

        # Claw back money deposited by the game for zero-delivery cargos.
        if total_clawback > 0 and http_client_mod:
            await transfer_money(
                http_client_mod,
                int(-total_clawback),
                "Non-Delivery Cargo",
                str(character.player.unique_id),
            )
            total_base_payment -= total_clawback

        # Party bonus + payment splitting
        party_result = await split_party_payment(
            character,
            parties,
            total_base_payment,
            total_subsidy,
            total_contract_payment,
            http_client_mod,
            used_shortcut=used_shortcut,
        )
        if party_result is not None:
            player_profits.extend(party_result)
            continue

        # Solo path: shortcut zones zero out subsidy
        if used_shortcut:
            total_subsidy = 0

        player_profits.append(
            (character, total_subsidy, total_base_payment, total_contract_payment)
        )

    if http_client_mod:
        await on_player_profits(player_profits, http_client_mod, http_client)

    # Persist high-water marks after successful processing
    persist_watermarks(max_seq, last_processed, events)


async def process_event(
    event,
    player,
    character,
    is_rp_mode=False,
    used_shortcut=False,
    treasury_balance=None,
    http_client=None,
    http_client_mod=None,
    discord_client=None,
    active_term=None,
):
    """Process a single webhook event.

    Returns:
        (base_payment, subsidy, contract_payment, clawback)
    """
    print(event)

    ctx = EventContext(
        http_client=http_client,
        http_client_mod=http_client_mod,
        discord_client=discord_client,
        treasury_balance=treasury_balance,
        is_rp_mode=is_rp_mode,
        used_shortcut=used_shortcut,
        active_term=active_term,
    )

    base_payment, subsidy, contract_payment, clawback = await dispatch(
        event["hook"], event, player, character, ctx
    )

    return base_payment, subsidy, contract_payment, clawback
