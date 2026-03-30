import logging
from datetime import time as dt_time, timedelta, timezone as dt_timezone

from django.utils import timezone
from django.db.models import Sum, Count, F

import discord
from discord.ext import tasks, commands
from django.conf import settings

from amc.models import Delivery, Confiscation

logger = logging.getLogger(__name__)


class CrimeStatsCog(commands.Cog):
    def __init__(self, bot, general_channel_id=settings.DISCORD_GENERAL_CHANNEL_ID):
        self.bot = bot
        self.general_channel_id = general_channel_id

    async def cog_load(self):
        self.daily_crime_stats_task.start()

    @tasks.loop(time=dt_time(hour=2, minute=10, tzinfo=dt_timezone.utc))
    async def daily_crime_stats_task(self):
        embed = await self.build_daily_crime_stats_embed()
        treasury_channel_id = getattr(
            settings, "DISCORD_TREASURY_CHANNEL_ID", 1402660537619320872
        )
        treasury_channel = self.bot.get_channel(treasury_channel_id)
        if treasury_channel:
            sent_message = await treasury_channel.send(embed=embed)
            general_channel = self.bot.get_channel(self.general_channel_id)
            if general_channel:
                await sent_message.forward(general_channel)

    async def build_daily_crime_stats_embed(self):
        now = timezone.now()
        yesterday = now - timedelta(days=1)

        # --- Money Laundering stats ---
        laundering_qs = (
            Delivery.objects.filter(
                cargo_key="Money",
                timestamp__gte=yesterday,
                timestamp__lte=now,
            )
            .values("character")
            .annotate(
                total_laundered=Sum("payment"),
                num_deliveries=Count("id"),
                name=F("character__name"),
            )
            .order_by("-total_laundered")
        )

        laundering_list = []
        total_laundered = 0
        total_laundering_deliveries = 0
        num_criminals = 0

        async for row in laundering_qs:
            num_criminals += 1
            total_laundered += row["total_laundered"]
            total_laundering_deliveries += row["num_deliveries"]
            laundering_list.append(
                f"**{row['name']}:** `${row['total_laundered']:,}` ({row['num_deliveries']} deliveries)"
            )

        laundering_str = (
            "\n".join(laundering_list)
            if laundering_list
            else "No money laundering activity."
        )

        # --- Confiscation stats ---
        confiscation_qs = (
            Confiscation.objects.filter(
                created_at__gte=yesterday,
                created_at__lte=now,
            )
            .values("officer")
            .annotate(
                total_confiscated=Sum("amount"),
                num_confiscations=Count("id"),
                name=F("officer__name"),
            )
            .order_by("-total_confiscated")
        )

        confiscation_list = []
        total_confiscated = 0
        total_confiscation_count = 0
        num_officers = 0

        async for row in confiscation_qs:
            num_officers += 1
            total_confiscated += row["total_confiscated"]
            total_confiscation_count += row["num_confiscations"]
            confiscation_list.append(
                f"**{row['name']}:** `${row['total_confiscated']:,}` ({row['num_confiscations']} confiscations)"
            )

        confiscation_str = (
            "\n".join(confiscation_list)
            if confiscation_list
            else "No confiscations."
        )

        # --- Build embed ---
        embed = discord.Embed(
            title="🔫 Daily Crime Report",
            description=f"Generated for {yesterday.strftime('%A, %-d %B %Y')}",
            color=discord.Color.dark_red(),
            timestamp=now,
        )

        embed.add_field(
            name=f"💰 Money Laundering — `${total_laundered:,}`",
            value=(
                f"**{total_laundering_deliveries}** deliveries by **{num_criminals}** criminal{'s' if num_criminals != 1 else ''}\n\n"
                + laundering_str
            ),
            inline=False,
        )

        embed.add_field(
            name=f"🚔 Confiscations — `${total_confiscated:,}`",
            value=(
                f"**{total_confiscation_count}** confiscations by **{num_officers}** officer{'s' if num_officers != 1 else ''}\n\n"
                + confiscation_str
            ),
            inline=False,
        )

        return embed


async def setup(bot):
    await bot.add_cog(CrimeStatsCog(bot))
