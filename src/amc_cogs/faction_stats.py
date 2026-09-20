import logging
from datetime import time as dt_time, timedelta, timezone as dt_timezone

from django.utils import timezone
from django.db.models import Sum, Count, F, Q

import discord
from discord.ext import tasks, commands
from django.conf import settings

from amc.models import Delivery, Confiscation, Wanted, Character, PoliceSession
from amc.special_cargo import ILLICIT_CARGO_KEYS

logger = logging.getLogger(__name__)


class FactionStatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.daily_criminal_stats_task.start()
        self.daily_cop_stats_task.start()

    @tasks.loop(time=dt_time(hour=2, minute=20, tzinfo=dt_timezone.utc))
    async def daily_criminal_stats_task(self):
        embed = await self.build_criminal_stats_embed()
        channel_id = getattr(settings, "DISCORD_CRIMINAL_STATS_CHANNEL_ID", 0)
        channel = self.bot.get_channel(channel_id)
        if channel:
            await channel.send(embed=embed)
        else:
            logger.warning("Criminal stats channel %s not found", channel_id)

    @tasks.loop(time=dt_time(hour=2, minute=20, tzinfo=dt_timezone.utc))
    async def daily_cop_stats_task(self):
        embed = await self.build_cop_stats_embed()
        channel_id = getattr(settings, "DISCORD_COP_STATS_CHANNEL_ID", 0)
        channel = self.bot.get_channel(channel_id)
        if channel:
            await channel.send(embed=embed)
        else:
            logger.warning("Cop stats channel %s not found", channel_id)

    async def build_criminal_stats_embed(self):
        now = timezone.now()
        yesterday = now - timedelta(days=1)

        # --- Illicit Deliveries ---
        illicit_qs = Delivery.objects.filter(
            cargo_key__in=ILLICIT_CARGO_KEYS,
            timestamp__gte=yesterday,
            timestamp__lte=now,
        )

        total_illicit = await illicit_qs.acount()
        total_illicit_payment = (
            await illicit_qs.aaggregate(total=Sum("payment"))
        )["total"] or 0

        # Per-cargo breakdown
        cargo_breakdown = []
        async for row in (
            illicit_qs.values("cargo_key")
            .annotate(total=Sum("payment"), count=Count("id"))
            .order_by("-total")
        ):
            cargo_breakdown.append(
                f"**{row['cargo_key']}:** `${row['total']:,}` ({row['count']})"
            )
        cargo_str = "\n".join(cargo_breakdown) if cargo_breakdown else "No activity."

        # --- Top Launderers ---
        launderers = []
        async for row in (
            illicit_qs.values("character")
            .annotate(
                total_laundered=Sum("payment"),
                num_deliveries=Count("id"),
                name=F("character__name"),
            )
            .order_by("-total_laundered")[:5]
        ):
            launderers.append(
                f"**{row['name']}:** `${row['total_laundered']:,}` ({row['num_deliveries']} runs)"
            )
        launderers_str = (
            "\n".join(launderers) if launderers else "No runners today."
        )

        # --- Active Bounties ---
        active_wanted_qs = Wanted.objects.filter(
            expired_at__isnull=True, wanted_remaining__gt=0
        )
        active_wanted_count = await active_wanted_qs.acount()
        highest_bounty = (
            await active_wanted_qs.aaggregate(max_bounty=Sum("amount"))
        )["max_bounty"] or 0

        # --- Criminals active yesterday (last illicit delivery in window) ---
        active_criminals = await Character.objects.filter(
            last_illicit_delivery_at__gte=yesterday,
            last_illicit_delivery_at__lte=now,
        ).acount()

        # --- Build embed ---
        embed = discord.Embed(
            title="Daily Criminal Underground Report",
            description=f"Underground briefing for {yesterday.strftime('%A, %-d %B %Y')}",
            color=discord.Color.dark_red(),
            timestamp=now,
        )

        embed.add_field(
            name=f"Illicit Deliveries — `${total_illicit_payment:,}`",
            value=(
                f"**{total_illicit}** deliveries moved today\n\n{cargo_str}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Top Launderers",
            value=launderers_str,
            inline=False,
        )

        embed.add_field(
            name="Heat Check",
            value=(
                f"**{active_wanted_count}** players currently running from the law\n"
                f"Highest bounty: **`${highest_bounty:,}`**"
            ),
            inline=False,
        )

        embed.add_field(
            name="Criminal Records",
            value=f"**{active_criminals}** criminals moved illicit cargo today",
            inline=False,
        )

        embed.add_field(
            name="Call to Action",
            value="The underground needs runners. Log in and move product.",
            inline=False,
        )

        return embed

    async def build_cop_stats_embed(self):
        now = timezone.now()
        yesterday = now - timedelta(days=1)

        # --- Confiscations ---
        confiscation_qs = Confiscation.objects.filter(
            created_at__gte=yesterday,
            created_at__lte=now,
        )

        total_confiscation_count = await confiscation_qs.acount()
        total_confiscated = (
            await confiscation_qs.filter(amount__gt=0).aaggregate(
                total=Sum("amount")
            )
        )["total"] or 0

        # Top Officers (exclude system-generated confiscations with no officer)
        officers = []
        async for row in (
            confiscation_qs.filter(officer__isnull=False)
            .values("officer")
            .annotate(
                total_confiscated=Sum("amount", filter=Q(amount__gt=0)),
                num_confiscations=Count("id"),
                name=F("officer__name"),
            )
            .order_by("-total_confiscated")[:5]
        ):
            officers.append(
                f"**{row['name']}:** `${row['total_confiscated']:,}` ({row['num_confiscations']} busts)"
            )
        officers_str = (
            "\n".join(officers) if officers else "No officer activity today."
        )

        # --- Active Wanted Suspects ---
        active_wanted_qs = Wanted.objects.filter(
            expired_at__isnull=True, wanted_remaining__gt=0
        )
        active_suspect_count = await active_wanted_qs.acount()
        total_bounty_pool = (
            await active_wanted_qs.aaggregate(pool=Sum("amount"))
        )["pool"] or 0

        # --- Police Sessions ---
        session_count = await PoliceSession.objects.filter(
            started_at__gte=yesterday,
            started_at__lte=now,
        ).acount()

        # --- Conviction Rate ---
        conviction_rate = (
            (total_confiscation_count / active_suspect_count * 100)
            if active_suspect_count > 0
            else 0
        )

        # --- Build embed ---
        embed = discord.Embed(
            title="Daily Police Department Report",
            description=f"Department briefing for {yesterday.strftime('%A, %-d %B %Y')}",
            color=discord.Color.blue(),
            timestamp=now,
        )

        embed.add_field(
            name=f"Confiscations — `${total_confiscated:,}`",
            value=(
                f"**{total_confiscation_count}** confiscations executed today\n\n{officers_str}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Active Wanted Suspects",
            value=(
                f"**{active_suspect_count}** suspects on the run\n"
                f"Total bounty pool: **`${total_bounty_pool:,}`**"
            ),
            inline=False,
        )

        embed.add_field(
            name="On-Duty Activity",
            value=f"**{session_count}** police sessions started today",
            inline=False,
        )

        embed.add_field(
            name="Conviction Rate",
            value=(
                f"**{conviction_rate:.0f}%** of suspects brought to justice\n"
                f"({total_confiscation_count} confiscations / {active_suspect_count} active wanted)"
            ),
            inline=False,
        )

        embed.add_field(
            name="Call to Action",
            value="Criminals are running wild. Clock in and clean up the streets.",
            inline=False,
        )

        return embed


async def setup(bot):
    await bot.add_cog(FactionStatsCog(bot))
