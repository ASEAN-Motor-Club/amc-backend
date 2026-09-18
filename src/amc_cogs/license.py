"""/drivers_license — render the member's AMC Driver's License card."""

from __future__ import annotations

import asyncio
import io
import time
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from django.db.models import Max, Min

from amc.gov_employee import calculate_gov_level
from amc.models import Character, Player, PlayerStatusLog
from amc_cogs.license_card import render_license_card

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

# Avatar fetch size: the card's avatar window is 236px, fetch above it
# (Discord CDN default avatars ignore ?size= and serve 256 — the renderer
# normalizes every source to the window size, so both paths are safe).
AVATAR_FETCH_SIZE = 256
_AVATAR_CACHE_TTL = 24 * 3600

_EMBLEM_NAME = "amc_emblem.png"

# A license is only (re)issued to members who played within this window.
RECENT_LOGIN_WINDOW = timedelta(days=7)


def _emblem_bytes() -> bytes:
    return (resources.files("amc_cogs") / "assets" / _EMBLEM_NAME).read_bytes()


async def _fetch_avatar_bytes(bot: AMCDiscordBot, user_id: int,
                              cache: dict) -> bytes | None:
    """Fetch a member's avatar PNG bytes (24h cache). None on failure."""
    hit = cache.get(user_id)
    now = time.monotonic()
    if hit and now - hit[0] < _AVATAR_CACHE_TTL:
        return hit[1]
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        data = await user.display_avatar.with_size(AVATAR_FETCH_SIZE).with_format(
            "png"
        ).read()
    except Exception:  # noqa: BLE001 — a failed avatar fetch must never block the card
        return None
    cache[user_id] = (now, data)
    return data


class DriversLicenseCog(commands.Cog):
    def __init__(self, bot: AMCDiscordBot):
        self.bot = bot
        self._avatar_cache: dict[int, tuple[float, bytes]] = {}

    @app_commands.command(
        name="drivers_license",
        description="Show your AMC Driver's License card",
    )
    async def drivers_license(self, interaction: discord.Interaction):
        await interaction.response.defer()  # public — the card is visible to all
        user_id = interaction.user.id

        try:
            player = await Player.objects.aget(discord_user_id=user_id)
        except Player.DoesNotExist:
            await interaction.followup.send(
                "No verified AMC player is linked to this Discord account. "
                "Use `/verify` in game to link it.",
                ephemeral=True,
            )
            return

        # First observed login across all the player's characters, plus the
        # most recent one — the recency gate decides whether a license is
        # issued at all.
        login_agg = await PlayerStatusLog.objects.filter(
            character__player=player
        ).aaggregate(
            first=Min("timespan__startswith"),
            latest=Max("timespan__startswith"),
        )
        first_login: datetime | None = login_agg["first"]
        latest_login: datetime | None = login_agg["latest"]
        if latest_login is not None and latest_login.tzinfo is None:
            latest_login = latest_login.replace(tzinfo=UTC)

        # Recency gate: no license unless the member logged in within the
        # window. (No login rows at all also fails the gate.)
        now = datetime.now(UTC)
        if latest_login is None or now - latest_login > RECENT_LOGIN_WINDOW:
            await interaction.followup.send(
                "Your driver's license can't be issued: you haven't "
                "logged in to AMC in the last 7 days. Hop in game and "
                "try again!",
                ephemeral=True,
            )
            return

        avatar = await _fetch_avatar_bytes(self.bot, user_id, self._avatar_cache)
        name = player.discord_name or interaction.user.display_name
        # Government officials (level 50+) get the gold Government Official
        # theme. Level alone qualifies — no active-term requirement (freeman:
        # "It shouldn't need the player to be on active government worker
        # duty to get the government worker design").
        # The employee's level comes from their most recently active character.
        # NOTE: derive from contributions, NOT the stored gov_employee_level —
        # deactivate_gov_role() zeroes the stored field when the 24h term
        # lapses, but the lifetime rank (contribs // GOV_LEVEL_STEP + 1) is
        # what the license should show (worked case: fattron, 43.9M contribs,
        # stored 0, real level 88).
        gov_level: int | None = None
        try:
            character = await (
                player.characters.with_last_login()
                .filter(last_login__isnull=False)
                .alatest("last_login")
            )
            gov_level = calculate_gov_level(character.gov_employee_contributions)
        except Character.DoesNotExist:
            pass
        png = await asyncio.to_thread(
            render_license_card,
            name=name,
            discord_id=str(user_id),
            issued=datetime.now(UTC).date(),
            joined=(first_login.date() if first_login else None),
            logo_bytes=_emblem_bytes(),
            avatar_bytes=avatar,
            gov_level=gov_level,
        )
        await interaction.followup.send(
            file=discord.File(io.BytesIO(png), filename="amc_license.png"),
        )


async def setup(bot):
    await bot.add_cog(DriversLicenseCog(bot))
