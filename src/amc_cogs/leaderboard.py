import logging
import discord
from discord import app_commands
from discord.ext import tasks, commands
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum, Count, F
from amc.models import (
    Delivery,
    PlayerVehicleLog,
    PlayerStatusLog,
    PlayerRestockDepotLog,
)

logger = logging.getLogger(__name__)

class LeaderboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.leaderboard_channel_name = "leaderboards"
        self.update_leaderboards.start()

    def cog_unload(self):
        self.update_leaderboards.cancel()

    async def get_leaderboard_data(self, days: int):
        now = timezone.now()
        start_date = now - timedelta(days=days)

        # 1. Most Revenue
        revenue_qs = (
            Delivery.objects.filter(timestamp__gte=start_date)
            .values("character__name")
            .annotate(total=Sum(F("payment") + F("subsidy")))
            .order_by("-total")[:10]
        )
        revenue = [
            {"name": item["character__name"], "value": item["total"]}
            async for item in revenue_qs
        ]

        # 2. Most Vehicles Bought
        vehicles_qs = (
            PlayerVehicleLog.objects.filter(
                timestamp__gte=start_date, action=PlayerVehicleLog.Action.BOUGHT
            )
            .values("character__name")
            .annotate(total=Count("id"))
            .order_by("-total")[:10]
        )
        vehicles = [
            {"name": item["character__name"], "value": item["total"]}
            async for item in vehicles_qs
        ]

        # 3. Most Active (Total Hours)
        active_qs = (
            PlayerStatusLog.objects.filter(timespan__startswith__gte=start_date)
            .values("character__name")
            .annotate(total=Sum("duration"))
            .order_by("-total")[:10]
        )
        active = [
            {
                "name": item["character__name"],
                "value": item["total"].total_seconds() / 3600 if item["total"] else 0,
            }
            async for item in active_qs
        ]

        # 4. Most Depot Restocks
        restocks_qs = (
            PlayerRestockDepotLog.objects.filter(timestamp__gte=start_date)
            .values("character__name")
            .annotate(total=Count("id"))
            .order_by("-total")[:10]
        )
        restocks = [
            {"name": item["character__name"], "value": item["total"]}
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
            return f"**{title}**\nNo data yet."
        
        lines = []
        for i, item in enumerate(data, 1):
            val = item['value']
            if is_money:
                val_str = f"${val:,.0f}"
            elif unit == "h":
                val_str = f"{val:.1f}h"
            else:
                val_str = f"{val:,}"
            
            lines.append(f"{i}. **{item['name']}** - {val_str}{unit}")
        
        return f"**{title}**\n" + "\n".join(lines)

    async def create_leaderboard_embeds(self):
        data_24h = await self.get_leaderboard_data(1)
        data_7d = await self.get_leaderboard_data(7)

        embed = discord.Embed(
            title="🏆 ASEAN Motor Club Leaderboards",
            description="Last updated: " + discord.utils.format_dt(timezone.now(), "R"),
            color=discord.Color.gold(),
        )

        # 24 Hours Section
        embed.add_field(
            name="📅 Last 24 Hours",
            value="---",
            inline=False
        )
        embed.add_field(
            name="💰 Revenue",
            value=self.format_leaderboard("", data_24h["revenue"], is_money=True),
            inline=True
        )
        embed.add_field(
            name="🏎️ Vehicles Bought",
            value=self.format_leaderboard("", data_24h["vehicles"]),
            inline=True
        )
        embed.add_field(
            name="🕒 Time Active",
            value=self.format_leaderboard("", data_24h["active"], unit="h"),
            inline=True
        )
        embed.add_field(
            name="📦 Depot Restocks",
            value=self.format_leaderboard("", data_24h["restocks"]),
            inline=True
        )

        # Spacer
        embed.add_field(name="\u200b", value="\u200b", inline=False)

        # 7 Days Section
        embed.add_field(
            name="🗓️ Last 7 Days",
            value="---",
            inline=False
        )
        embed.add_field(
            name="💰 Revenue",
            value=self.format_leaderboard("", data_7d["revenue"], is_money=True),
            inline=True
        )
        embed.add_field(
            name="🏎️ Vehicles Bought",
            value=self.format_leaderboard("", data_7d["vehicles"]),
            inline=True
        )
        embed.add_field(
            name="🕒 Time Active",
            value=self.format_leaderboard("", data_7d["active"], unit="h"),
            inline=True
        )
        embed.add_field(
            name="📦 Depot Restocks",
            value=self.format_leaderboard("", data_7d["restocks"]),
            inline=True
        )

        embed.set_footer(text="Updates every hour • Only top 10 shown")
        return embed

    @tasks.loop(hours=1)
    async def update_leaderboards(self):
        await self.bot.wait_until_ready()
        
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.channels, name=self.leaderboard_channel_name)
            if not channel:
                continue
            
            embed = await self.create_leaderboard_embeds()
            
            # Find last message from bot in this channel
            last_message = None
            async for message in channel.history(limit=10):
                if message.author == self.bot.user:
                    last_message = message
                    break
            
            if last_message:
                await last_message.edit(embed=embed)
            else:
                await channel.send(embed=embed)

    @update_leaderboards.before_loop
    async def before_update_leaderboards(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="setup_leaderboards", description="Setup the leaderboards channel and post initial message")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_leaderboards(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        channel = discord.utils.get(guild.channels, name=self.leaderboard_channel_name)
        
        if not channel:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(send_messages=False),
                guild.me: discord.PermissionOverwrite(send_messages=True, embed_links=True)
            }
            channel = await guild.create_text_channel(self.leaderboard_channel_name, overwrites=overwrites)
            await interaction.followup.send(f"Created channel #{self.leaderboard_channel_name}", ephemeral=True)
        else:
            await interaction.followup.send(f"Channel #{self.leaderboard_channel_name} already exists. Posting/updating leaderboard...", ephemeral=True)
        
        embed = await self.create_leaderboard_embeds()
        await channel.send(embed=embed)

async def setup(bot):
    await bot.add_cog(LeaderboardCog(bot))
