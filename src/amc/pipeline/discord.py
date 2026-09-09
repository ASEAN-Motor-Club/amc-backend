"""Discord notification helpers for webhook events.

Extracted from webhook.py.
"""

from __future__ import annotations

import asyncio
import logging

from discord import Embed
from django.conf import settings

logger = logging.getLogger("amc.pipeline.discord")


async def post_discord_delivery_embed(
    discord_client,
    character,
    cargo_key,
    quantity,
    delivery_source,
    delivery_destination,
    payment,
    subsidy,
    vehicle_key,
    job=None,
    delivery_id=None,
):
    jobs_cog = discord_client.get_cog("JobsCog")
    delivery_source_name = ""
    delivery_destination_name = ""
    if delivery_source:
        delivery_source_name = delivery_source.name
    if delivery_destination:
        delivery_destination_name = delivery_destination.name

    if jobs_cog and hasattr(jobs_cog, "post_delivery_embed"):
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            lambda: asyncio.run_coroutine_threadsafe(
                jobs_cog.post_delivery_embed(
                    character.name,
                    cargo_key,
                    quantity,
                    delivery_source_name,
                    delivery_destination_name,
                    payment,
                    subsidy,
                    vehicle_key,
                    job=job,
                    delivery_id=delivery_id,
                ),
                discord_client.loop,
            ),
        )


def post_discord_fraud_alert(
    discord_client,
    *,
    kind: str,
    character_name: str,
    player_id: str,
    original_payment: int,
    clawed_back: int,
    final_payment: int | None = None,
    detail: str = "",
) -> None:
    """Fire-and-forget Discord alert for a fraud clawback.

    Schedules an embed on the bot's event loop (the worker runs on a
    different loop than the Discord client). Silent no-op when no alert
    channel is configured or no client is available, and never raises:
    an alerting failure must never break payment processing.
    """
    channel_id = settings.DISCORD_FRAUD_ALERT_CHANNEL_ID
    if not channel_id or discord_client is None:
        return
    try:
        channel = discord_client.get_channel(channel_id)
        if channel is None:
            logger.warning(
                "Fraud alert channel %s not found; dropping alert for %s",
                channel_id,
                kind,
            )
            return

        embed = Embed(
            title="Fraud clawback",
            color=0xE74C3C,
            description=detail or None,
        )
        embed.add_field(name="Player", value=f"{character_name} ({player_id})")
        embed.add_field(name="Original payment", value=f"${original_payment:,}")
        embed.add_field(name="Clawed back", value=f"${clawed_back:,}")
        if final_payment is not None:
            embed.add_field(name="Final payment", value=f"${final_payment:,}")

        asyncio.run_coroutine_threadsafe(channel.send(embed=embed), discord_client.loop)
    except Exception:
        logger.exception("Failed to post fraud alert for %s", kind)
