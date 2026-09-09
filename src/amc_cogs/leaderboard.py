import asyncio
import io
import logging
import time
import discord
from discord import app_commands
from discord.ext import tasks, commands
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot
from django.utils import timezone
from django.conf import settings
from datetime import timedelta
from django.db.models import Sum, Count, F
from amc.models import (
    Delivery,
    PlayerVehicleLog,
    PlayerStatusLog,
    PlayerRestockDepotLog,
)
from amc.gov_employee import (
    calculate_gov_level,
    strip_gov_name,
    top_gov_all_time,
    top_gov_monthly,
    GOV_BOARD_LIMIT,
)

logger = logging.getLogger(__name__)

GOV_BOARD_FILE = "gov_board.png"
AVATAR_CACHE_TTL = 24 * 3600.0
AVATAR_SIZE = 128


def _fmt_money(value: int) -> str:
    if value >= 1_000_000:
        s = f"${value / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return s + "M"
    if value >= 1_000:
        return f"${value / 1_000:,.0f}K"
    return f"${value:,}"


def _circle_rgba(png_bytes: bytes):
    """Decode avatar PNG bytes and apply an antialiased circular alpha mask."""
    import numpy as np
    from matplotlib import image as mimage

    arr = mimage.imread(io.BytesIO(png_bytes))
    if arr.ndim == 2:
        arr = np.dstack([arr] * 3)
    if arr.shape[2] == 3:
        arr = np.dstack([arr, np.ones(arr.shape[:2])])
    arr = np.asarray(arr, dtype=float)
    h, w = arr.shape[:2]
    n = min(h, w)
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt((yy - (h - 1) / 2) ** 2 + (xx - (w - 1) / 2) ** 2)
    alpha = np.clip(n / 2 - d + 0.5, 0.0, 1.0)
    out = arr.copy()
    out[..., 3] = np.minimum(out[..., 3], alpha)
    return out


def _placeholder_rgba():
    """Neutral 'no avatar' disc for unlinked players / failed fetches."""
    import numpy as np

    n = 128
    yy, xx = np.mgrid[0:n, 0:n]
    d = np.sqrt((yy - (n - 1) / 2) ** 2 + (xx - (n - 1) / 2) ** 2)
    alpha = np.clip(n / 2 - d + 0.5, 0.0, 1.0)
    rgb = np.zeros((n, n, 3))
    rgb[..., 0] = 0.29
    rgb[..., 1] = 0.31
    rgb[..., 2] = 0.35
    return np.dstack([rgb, alpha])


def _draw_panel(ax, title, rows, accent, rank_colors, dim, text, green) -> None:
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage

    bg_dk = "#3E4147"
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(
        0.02, 0.965, title, color=accent, fontsize=13, fontweight="bold",
        ha="left", va="top", transform=ax.transAxes,
    )
    ax.plot(
        [0.02, 0.98], [0.905, 0.905], color=bg_dk, linewidth=1.2,
        transform=ax.transAxes, zorder=1,
    )
    if not rows:
        ax.text(
            0.5, 0.5, "No contributions yet.", color=dim, fontsize=11,
            ha="center", va="center", transform=ax.transAxes,
        )
        return
    for i, row in enumerate(rows):
        yc = 0.79 - i * 0.155
        rank_color = rank_colors[i] if i < len(rank_colors) else dim
        ax.text(
            0.02, yc, f"{i + 1}.", color=rank_color, fontsize=12,
            fontweight="bold", ha="left", va="center", transform=ax.transAxes,
        )
        ab = AnnotationBbox(
            OffsetImage(row["avatar_rgba"], zoom=0.32, interpolation="bilinear"),
            (0.115, yc),
            xycoords="axes fraction",
            frameon=False,
            zorder=3,
        )
        ax.add_artist(ab)
        ax.text(
            0.20, yc, row["name"], color=text, fontsize=11,
            ha="left", va="center", transform=ax.transAxes,
        )
        ax.text(
            0.62, yc, f"Lv {row['level']}", color=dim, fontsize=10,
            ha="left", va="center", transform=ax.transAxes,
        )
        ax.text(
            0.98, yc, _fmt_money(row["value"]), color=green, fontsize=11.5,
            fontweight="bold", ha="right", va="center", transform=ax.transAxes,
        )


def _render_gov_board_png(all_rows, monthly_rows, month_label) -> io.BytesIO:
    """Render the two gov-employee top-5 panels as one dark-themed PNG card.

    Row dicts carry: name, level, value (int), avatar_rgba (HxWx4 float
    array) — all prepared by the caller; this function is pure rendering so
    it can run inside asyncio.to_thread and be smoke-tested without DB or
    Discord objects.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    bg = "#23272A"
    text = "#FFFFFF"
    dim = "#99AAB5"
    green = "#57F287"
    rank_colors = ["#FFD700", "#E5E7EB", "#E39774"]
    accents = ["#FAA61A", "#00B0F4"]

    placeholder = _placeholder_rgba()
    for row in all_rows + monthly_rows:
        if row["avatar_rgba"] is None:
            row["avatar_rgba"] = placeholder

    fig = Figure(figsize=(9.0, 4.8), dpi=150, facecolor=bg)
    FigureCanvasAgg(fig)
    ax_left = fig.add_axes([0.02, 0.07, 0.47, 0.86])
    ax_right = fig.add_axes([0.51, 0.07, 0.47, 0.86])
    _draw_panel(
        ax_left, "TOP EMPLOYEES — ALL-TIME", all_rows,
        accents[0], rank_colors, dim, text, green,
    )
    _draw_panel(
        ax_right, f"TOP EMPLOYEES — {month_label.upper()}", monthly_rows,
        accents[1], rank_colors, dim, text, green,
    )
    fig.text(
        0.5, 0.018,
        "Government Employee level = every $500K contributed to the treasury",
        color=dim, fontsize=9, ha="center",
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    buf.seek(0)
    return buf


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: "AMCDiscordBot"):
        self.bot = bot
        self.leaderboard_channel_id = settings.DISCORD_LEADERBOARD_CHANNEL_ID
        self._avatar_cache: dict[int, tuple[float, bytes]] = {}
        self.update_leaderboards.start()

    async def cog_unload(self):
        self.update_leaderboards.cancel()

    async def get_leaderboard_data(self, days: int):
        now = timezone.now()
        start_date = now - timedelta(days=days)

        # 1. Most Revenue
        revenue_qs = (
            Delivery.objects.filter(timestamp__gte=start_date)
            .values("character__name")
            .annotate(total=Sum(F("payment") + F("subsidy")))
            .filter(total__gt=0)
            .order_by("-total")[:10]
        )
        revenue = [
            {"name": item["character__name"] or "Unknown", "value": item["total"]}
            async for item in revenue_qs
        ]

        # 2. Most Vehicles Bought
        vehicles_qs = (
            PlayerVehicleLog.objects.filter(
                timestamp__gte=start_date, action=PlayerVehicleLog.Action.BOUGHT
            )
            .values("character__name")
            .annotate(total=Count("id"))
            .filter(total__gt=0)
            .order_by("-total")[:10]
        )
        vehicles = [
            {"name": item["character__name"] or "Unknown", "value": item["total"]}
            async for item in vehicles_qs
        ]

        # 3. Most Active (Total Hours)
        active_qs = (
            PlayerStatusLog.objects.filter(timespan__startswith__gte=start_date)
            .values("character__name")
            .annotate(total=Sum("duration"))
            .filter(total__gt=timedelta(0))
            .order_by("-total")[:10]
        )
        active = [
            {
                "name": item["character__name"] or "Unknown",
                "value": item["total"].total_seconds() / 3600 if item["total"] else 0,
            }
            async for item in active_qs
        ]

        # 4. Most Depot Restocks
        restocks_qs = (
            PlayerRestockDepotLog.objects.filter(timestamp__gte=start_date)
            .values("character__name")
            .annotate(total=Count("id"))
            .filter(total__gt=0)
            .order_by("-total")[:10]
        )
        restocks = [
            {"name": item["character__name"] or "Unknown", "value": item["total"]}
            async for item in restocks_qs
        ]

        return {
            "revenue": revenue,
            "vehicles": vehicles,
            "active": active,
            "restocks": restocks,
        }

    def format_leaderboard(self, title, data, unit="", is_money=False):
        if not data:
            return "No data yet."

        lines: list[str] = []
        for i, item in enumerate(data, 1):
            val = item["value"]
            if is_money:
                val_str = f"${val:,.0f}"
            elif unit == "h":
                val_str = f"{val:.1f}h"
            else:
                val_str = f"{val:,}{unit}"

            lines.append(f"{i}. {item['name']} - {val_str}")

        header = f"{title}\n" if title else ""
        return header + "\n".join(lines)

    async def _get_avatar(self, discord_user_id: int) -> bytes | None:
        """Fetch a Discord user's avatar PNG bytes, cached for 24h."""
        now = time.monotonic()
        cached = self._avatar_cache.get(discord_user_id)
        if cached is not None and now - cached[0] < AVATAR_CACHE_TTL:
            return cached[1]
        try:
            user = self.bot.get_user(discord_user_id)
            if user is None:
                user = await self.bot.fetch_user(discord_user_id)
            data = await user.display_avatar.with_size(AVATAR_SIZE).with_format(
                "png"
            ).read()
        except Exception:
            logger.warning(
                "Avatar fetch failed for discord user %s",
                discord_user_id,
                exc_info=True,
            )
            return None
        self._avatar_cache[discord_user_id] = (now, data)
        return data

    async def _gov_board_rows(self):
        """Assemble the gov board row dicts (DB + avatar fetches)."""
        all_chars = await top_gov_all_time(GOV_BOARD_LIMIT)
        monthly = await top_gov_monthly(GOV_BOARD_LIMIT)
        month_label = timezone.localtime(timezone.now()).strftime("%B")

        async def build_row(character, value):
            avatar_bytes = None
            player = character.player
            if player is not None and player.discord_user_id:
                avatar_bytes = await self._get_avatar(player.discord_user_id)
            avatar_rgba = None
            if avatar_bytes is not None:
                try:
                    avatar_rgba = await asyncio.to_thread(
                        _circle_rgba, avatar_bytes
                    )
                except Exception:
                    logger.warning(
                        "Avatar decode failed for discord user %s",
                        player.discord_user_id if player else None,
                        exc_info=True,
                    )
            return {
                "name": (strip_gov_name(character.name or "")[:14] or "Unknown"),
                "level": calculate_gov_level(character.gov_employee_contributions),
                "value": int(value),
                "avatar_rgba": avatar_rgba,
            }

        all_rows = [await build_row(ch, ch.gov_employee_contributions) for ch in all_chars]
        monthly_rows = [await build_row(ch, total) for ch, total in monthly]
        return all_rows, monthly_rows, month_label

    async def create_leaderboard_embeds(self):
        data_24h = await self.get_leaderboard_data(1)
        data_7d = await self.get_leaderboard_data(7)

        embed = discord.Embed(
            title="🏆 ASEAN Motor Club Leaderboards",
            description="Last updated: " + discord.utils.format_dt(timezone.now(), "R"),
            color=discord.Color.gold(),
        )

        # 24 Hours Section
        embed.add_field(name="📅 Last 24 Hours", value="---", inline=False)
        embed.add_field(
            name="💰 Revenue",
            value=self.format_leaderboard("", data_24h["revenue"], is_money=True),
            inline=True,
        )
        embed.add_field(
            name="🏎️ Vehicles Bought",
            value=self.format_leaderboard("", data_24h["vehicles"]),
            inline=True,
        )
        embed.add_field(
            name="🕒 Time Active",
            value=self.format_leaderboard("", data_24h["active"], unit="h"),
            inline=True,
        )
        embed.add_field(
            name="📦 Depot Restocks",
            value=self.format_leaderboard("", data_24h["restocks"]),
            inline=True,
        )

        # Spacer
        embed.add_field(name="\u200b", value="\u200b", inline=False)

        # 7 Days Section
        embed.add_field(name="🗓️ Last 7 Days", value="---", inline=False)
        embed.add_field(
            name="💰 Revenue",
            value=self.format_leaderboard("", data_7d["revenue"], is_money=True),
            inline=True,
        )
        embed.add_field(
            name="🏎️ Vehicles Bought",
            value=self.format_leaderboard("", data_7d["vehicles"]),
            inline=True,
        )
        embed.add_field(
            name="🕒 Time Active",
            value=self.format_leaderboard("", data_7d["active"], unit="h"),
            inline=True,
        )
        embed.add_field(
            name="📦 Depot Restocks",
            value=self.format_leaderboard("", data_7d["restocks"]),
            inline=True,
        )

        # Gov employees card (all-time + this month) as the embed image.
        gov_file = None
        try:
            all_rows, monthly_rows, month_label = await self._gov_board_rows()
            if all_rows or monthly_rows:
                buf = await asyncio.to_thread(
                    _render_gov_board_png, all_rows, monthly_rows, month_label
                )
                gov_file = discord.File(buf, filename=GOV_BOARD_FILE)
                embed.set_image(url=f"attachment://{GOV_BOARD_FILE}")
        except Exception:
            logger.exception("Gov board rendering failed")

        embed.set_footer(text="Updates every hour • Only top 10 shown")
        return embed, gov_file

    async def _edit_message_with_file(
        self, message: discord.Message, embed: discord.Embed, file: discord.File
    ):
        """Edit a message and swap in a new attachment.

        discord.py 2.5's Message.edit() cannot upload new files; this drives
        the library's own multipart builder + HTTP client (the same path
        WebhookMessage.edit uses) so the card's image updates in place
        without deleting/reposting the message. The builder derives the
        payload's `attachments` array from the files alone, which per the
        Discord edit semantics REPLACES the attachment list — the previous
        card image is dropped, so the message never accumulates copies.
        """
        from discord.http import handle_message_parameters

        with handle_message_parameters(embed=embed, files=[file]) as params:
            await self.bot.http.edit_message(
                message.channel.id, message.id, params=params
            )

    async def _upsert_leaderboard_message(
        self, channel: discord.TextChannel, embed: discord.Embed,
        gov_file: discord.File | None,
    ):
        last_message = None
        async for message in channel.history(limit=10):
            if message.author == self.bot.user:
                last_message = message
                break

        if last_message:
            if gov_file is not None:
                await self._edit_message_with_file(last_message, embed, gov_file)
            else:
                await last_message.edit(embed=embed)
            logger.info(
                f"Updated existing leaderboard message in #{channel.name} "
                f"({channel.guild.name})"
            )
        else:
            if gov_file is not None:
                await channel.send(embed=embed, file=gov_file)
            else:
                await channel.send(embed=embed)
            logger.info(
                f"Posted new leaderboard message in #{channel.name} "
                f"({channel.guild.name})"
            )

    @tasks.loop(hours=1)
    async def update_leaderboards(self):
        await self.bot.wait_until_ready()

        logger.info("Starting hourly leaderboard update")
        for guild in self.bot.guilds:
            if not guild:
                continue

            try:
                channel = guild.get_channel(self.leaderboard_channel_id)

                if not isinstance(channel, discord.TextChannel):
                    logger.warning(
                        f"Leaderboard channel {self.leaderboard_channel_id} not found in guild {guild.name}"
                    )
                    continue

                logger.debug(f"Updating leaderboard in #{channel.name} ({guild.name})")
                embed, gov_file = await self.create_leaderboard_embeds()
                await self._upsert_leaderboard_message(channel, embed, gov_file)
            except Exception as e:
                logger.error(
                    f"Failed to update leaderboard in guild {guild.name}: {e}",
                    exc_info=True,
                )

    @update_leaderboards.before_loop
    async def before_update_leaderboards(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="setup_leaderboards",
        description="Setup the leaderboards channel and post initial message",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_leaderboards(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send(
                "This command must be used in a server", ephemeral=True
            )
            return

        channel = guild.get_channel(self.leaderboard_channel_id)

        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                f"Leaderboard channel with ID {self.leaderboard_channel_id} not found in this server.",
                ephemeral=True,
            )
            return
        else:
            await interaction.followup.send(
                f"Channel #{channel.name} found. Posting/updating leaderboard...",
                ephemeral=True,
            )

        try:
            embed, gov_file = await self.create_leaderboard_embeds()
            await self._upsert_leaderboard_message(channel, embed, gov_file)

            await interaction.followup.send(
                "Leaderboard successfully updated.", ephemeral=True
            )
        except Exception as e:
            logger.error(f"Failed manual leaderboard setup/update: {e}", exc_info=True)
            await interaction.followup.send(
                f"Failed to update leaderboard: {e}", ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(LeaderboardCog(bot))
