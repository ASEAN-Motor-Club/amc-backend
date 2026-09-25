"""Live "State of the Economy" board in #economy.

One self-maintaining message: a tasks.loop(minutes=1) tick re-renders a
matplotlib sector-health chart (all sectors worst-first, 15% starved
threshold, overall input-supply headline, top-5 contributor board) and
edits the bot's existing message in place, re-attaching the PNG. Data
comes straight from the merged economy-dashboard helpers
(`amc.economy_dashboard.sector_health` / `contribution_leaderboard`),
not from the HTTP API — the cog runs in the same process.
"""

import asyncio
import io
import logging
from datetime import UTC, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks
from django.conf import settings
from django.utils import timezone

from amc.economy_dashboard import contribution_leaderboard, sector_health

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

logger = logging.getLogger(__name__)

BOARD_FILE = "economy_board.png"
LIVE_PAGE_URL = "https://www.aseanmotorclub.com/releases/economy.html"

# chart palette (matches the web dashboard)
_BG = "#0d1117"
_SURFACE = "#141a23"
_INK = "#e6edf3"
_MUTED = "#8b98a9"
_FAINT = "#5c6878"
_TRACK = "#232b37"
_GREEN = "#3fb950"
_AMBER = "#d29922"
_RED = "#f85149"

SECTOR_NAMES = {
    "retail": "Retail",
    "construction": "Construction",
    "metal": "Steel & Metal",
    "energy": "Energy",
    "food": "Food",
    "mining": "Mining",
    "logging": "Logging",
    "furniture": "Furniture",
    "chemical": "Chemicals",
}

GREEN = 0x3FB950
AMBER = 0xD29922
RED = 0xF85149


def health_color(fill):
    """Hex string for chart colors; int for embed colors via _int_color."""
    if fill >= 0.6:
        return "#3fb950"
    if fill >= 0.3:
        return "#d29922"
    return "#f85149"


def _int_color(fill):
    if fill >= 0.6:
        return GREEN
    if fill >= 0.3:
        return AMBER
    return RED


def _fmt_compact(value):
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(round(value))


def render_board_png(sectors, contributors, stamp_text):
    """Render the sector-health + top-contributor card. Pure function.

    sectors: list of {sector, amount, capacity, fill, starved_sites}
    contributors: list of {name, units, payment, score}
    stamp_text: footer stamp (e.g. "14:03 UTC")
    Returns PNG bytes, or None when there is no data to show at all.
    """
    from datetime import datetime

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    rows = [s for s in sectors if s.get("fill") is not None]
    if not rows:
        return None

    cap = sum(s["capacity"] for s in rows)
    amt = sum(s["amount"] for s in rows)
    overall = amt / cap if cap else 0
    starved = sum(s["starved_sites"] for s in rows)
    rows = sorted(rows, key=lambda s: s["fill"])
    # recompute stamp server-side when callers pass empty text
    if not stamp_text:
        stamp_text = datetime.now(UTC).strftime("%H:%M UTC")

    fig = Figure(figsize=(10.2, 4.6), dpi=110, facecolor=_BG)
    FigureCanvasAgg(fig)
    ax, axlb = fig.subplots(
        1, 2, gridspec_kw={"width_ratios": [1.45, 1], "wspace": 0.08}
    )

    # left: sector bars
    ax.set_facecolor(_BG)
    n = len(rows)
    for y, s in zip(reversed(range(n)), rows):
        fill = s["fill"]
        color = health_color(fill)
        ax.barh(y, 100, height=0.62, color=_TRACK, zorder=1)
        ax.barh(y, min(fill * 100, 100), height=0.62, color=color, zorder=2)
        ax.axvline(15, color=_INK, alpha=0.5, lw=1, zorder=3)
        ax.text(
            101.5, y, f"{fill * 100:.0f}%", va="center", ha="left",
            fontsize=11, fontweight="bold", color=color,
        )
        ax.text(
            -2, y, SECTOR_NAMES.get(s["sector"], s["sector"]),
            va="center", ha="right", fontsize=11, color=_INK,
        )
    ax.set_xlim(-26, 110)
    ax.set_ylim(-0.7, n - 0.3)
    ax.axis("off")
    ax.set_title(
        "Input supply by sector  —  15% line = starved",
        fontsize=10.5, color=_MUTED, loc="left", pad=10,
    )

    # right: overall headline + top contributors
    axlb.set_facecolor(_SURFACE)
    axlb.axis("off")
    axlb.text(
        0.5, 0.97, f"{overall * 100:.0f}%", transform=axlb.transAxes,
        fontsize=40, fontweight="bold", color=health_color(overall),
        ha="center", va="top",
    )
    axlb.text(
        0.5, 0.80, "overall input supply", transform=axlb.transAxes,
        fontsize=10, color=_FAINT, ha="center",
    )
    axlb.text(
        0.5, 0.74, f"{starved} sites critically short",
        transform=axlb.transAxes, fontsize=10, color=_FAINT, ha="center",
    )
    axlb.text(
        0.06, 0.66, "TOP CONTRIBUTORS — 7 DAYS",
        transform=axlb.transAxes, fontsize=9, color=_MUTED, ha="left",
    )
    y = 0.57
    for i, p in enumerate(contributors[:5], 1):
        axlb.text(
            0.06, y, f"{i}.  {p['name']}", transform=axlb.transAxes,
            fontsize=11, color=_INK, ha="left", fontweight="bold",
        )
        axlb.text(
            0.94, y, f"{_fmt_compact(p['score'])} pts",
            transform=axlb.transAxes, fontsize=11, color=_MUTED, ha="right",
        )
        y -= 0.085
    axlb.text(
        0.47, y - 0.02, "full board:  aseanmotorclub.com/releases/economy.html",
        transform=axlb.transAxes, fontsize=8.5, color="#a8b3c2", ha="center",
    )

    fig.suptitle(
        f"STATE OF THE ECONOMY   -   {stamp_text}",
        x=0.05, y=0.985, fontsize=13, fontweight="bold", color=_INK, ha="left",
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight")
    return buf.getvalue()


def build_board_embed(sectors, contributors):
    """Pure: build the embed (without image — caller attaches the file)."""
    rows = [s for s in sectors if s.get("fill") is not None]
    cap = sum(s["capacity"] for s in rows) if rows else 0
    amt = sum(s["amount"] for s in rows) if rows else 0
    overall = amt / cap if cap else 0
    starved = sum(s["starved_sites"] for s in rows) if rows else 0

    worst = [
        s for s in sorted(rows, key=lambda x: x["fill"])[:3] if s["fill"] < 0.3
    ]
    desc = f"Overall input supply **{overall * 100:.0f}%** - {starved} sites critically short."
    if worst:
        names = ", ".join(
            SECTOR_NAMES.get(s["sector"], s["sector"]) for s in worst
        )
        desc += f"\nMost needed right now: **{names}**."

    embed = discord.Embed(
        title="State of the Economy",
        url=LIVE_PAGE_URL,
        description=desc,
        color=_int_color(overall),
    )
    embed.set_image(url=f"attachment://{BOARD_FILE}")
    embed.set_footer(text="Updates every minute")
    return embed


class EconomyBoardCog(commands.Cog):
    """Self-updating State-of-the-Economy card in the #economy channel."""

    def __init__(self, bot: "AMCDiscordBot"):
        self.bot = bot
        self.channel_id = settings.DISCORD_ECONOMY_BOARD_CHANNEL_ID
        if not self.update_board.is_running():
            self.update_board.start()

    async def cog_unload(self):
        self.update_board.cancel()

    async def _gather_data(self):
        week_ago = timezone.now() - timedelta(days=7)
        sectors = await sector_health()
        contributors = await contribution_leaderboard(week_ago, limit=5)
        return sectors, contributors

    def _board_file(self, png_bytes):
        return discord.File(io.BytesIO(png_bytes), filename=BOARD_FILE)

    async def _edit_message_with_file(self, message, embed, file):
        # discord.py 2.5: Message.edit() cannot upload files; drive the
        # library's multipart builder directly (same path as WebhookMessage
        # .edit). files alone REPLACE the attachment list — no accumulation.
        from discord.http import handle_message_parameters

        with handle_message_parameters(embed=embed, files=[file]) as params:
            await self.bot.http.edit_message(
                message.channel.id, message.id, params=params
            )

    async def _upsert_board_message(self, channel, embed, file):
        last_message = None
        async for message in channel.history(limit=10):
            if message.author == self.bot.user:
                last_message = message
                break

        if last_message:
            await self._edit_message_with_file(last_message, embed, file)
        else:
            await channel.send(embed=embed, file=file)

    @tasks.loop(minutes=1)
    async def update_board(self):
        for guild in self.bot.guilds:
            if not guild:
                continue
            channel = guild.get_channel(self.channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                sectors, contributors = await self._gather_data()
                png = await asyncio.to_thread(
                    render_board_png, sectors, contributors, ""
                )
                if png is None:
                    continue  # no data (empty DB) — leave the board as-is
                embed = build_board_embed(sectors, contributors)
                await self._upsert_board_message(
                    channel, embed, self._board_file(png)
                )
            except Exception:
                logger.exception(
                    "Economy board update failed in guild %s", guild.name
                )

    @update_board.before_loop
    async def before_update_board(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="setup_economy_board",
        description="Post/reset the State of the Economy board (admin)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_economy_board(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                f"Channel {self.channel_id} not found.", ephemeral=True
            )
            return
        sectors, contributors = await self._gather_data()
        png = await asyncio.to_thread(render_board_png, sectors, contributors, "")
        embed = build_board_embed(sectors, contributors)
        await channel.send(
            embed=embed,
            file=self._board_file(png) if png is not None else None,
        )
        await interaction.followup.send("Board posted.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(EconomyBoardCog(bot))
