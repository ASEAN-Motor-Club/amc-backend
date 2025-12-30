import logging
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

logger = logging.getLogger(__name__)

class LeaderboardCog(commands.Cog):
    def __init__(self, bot: "AMCDiscordBot"):
        self.bot = bot
        self.leaderboard_channel_id = settings.DISCORD_LEADERBOARD_CHANNEL_ID
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
            val = item['value']
            if is_money:
                val_str = f"${val:,.0f}"
            elif unit == "h":
                val_str = f"{val:.1f}h"
            else:
                val_str = f"{val:,}{unit}"
            
            lines.append(f"{i}. {item['name']} - {val_str}")
        
        header = f"{title}\n" if title else ""
        return header + "\n".join(lines)

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
        
        logger.info("Starting hourly leaderboard update")
        for guild in self.bot.guilds:
            if not guild:
                continue
            
            try:
                channel = guild.get_channel(self.leaderboard_channel_id)
                
                if not isinstance(channel, discord.TextChannel):
                    logger.warning(f"Leaderboard channel {self.leaderboard_channel_id} not found in guild {guild.name}")
                    continue
                
                logger.debug(f"Updating leaderboard in #{channel.name} ({guild.name})")
                embed = await self.create_leaderboard_embeds()
                
                # Find last message from bot in this channel
                last_message = None
                async for message in channel.history(limit=10):
                    if message.author == self.bot.user:
                        last_message = message
                        break
                
                if last_message:
                    await last_message.edit(embed=embed)
                    logger.info(f"Updated existing leaderboard message in #{channel.name} ({guild.name})")
                else:
                    await channel.send(embed=embed)
                    logger.info(f"Posted new leaderboard message in #{channel.name} ({guild.name})")
            except Exception as e:
                logger.error(f"Failed to update leaderboard in guild {guild.name}: {e}", exc_info=True)

    @update_leaderboards.before_loop
    async def before_update_leaderboards(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="setup_leaderboards", description="Setup the leaderboards channel and post initial message")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_leaderboards(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command must be used in a server", ephemeral=True)
            return
            
        channel = guild.get_channel(self.leaderboard_channel_id)
        
        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(f"Leaderboard channel with ID {self.leaderboard_channel_id} not found in this server.", ephemeral=True)
            return
        else:
            await interaction.followup.send(f"Channel #{channel.name} found. Posting/updating leaderboard...", ephemeral=True)
        
        try:
            embed = await self.create_leaderboard_embeds()
            
            # Use same logic as task to find last message
            last_message = None
            async for message in channel.history(limit=10):
                if message.author == self.bot.user:
                    last_message = message
                    break
            
            if last_message:
                await last_message.edit(embed=embed)
            else:
                await channel.send(embed=embed)
            
            await interaction.followup.send("Leaderboard successfully updated.", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed manual leaderboard setup/update: {e}", exc_info=True)
            await interaction.followup.send(f"Failed to update leaderboard: {e}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(LeaderboardCog(bot))
