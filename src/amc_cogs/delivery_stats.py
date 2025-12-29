import discord
from discord import app_commands
from discord.ext import commands
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum, Count
from amc.models import Player, ServerCargoArrivedLog
from amc.enums import CargoKey
from .utils import create_player_autocomplete
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class DeliveryStatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Using http_client_game as seen in EconomyCog
        self.player_autocomplete_sys = create_player_autocomplete(
            self.bot.http_client_game
        )

    async def player_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete_sys(interaction, current)

    @app_commands.command(
        name="delivery_stats",
        description="Show delivery statistics grouped by cargo type",
    )
    @app_commands.describe(
        player="Player to show stats for (defaults to yourself)",
        days="Number of days to look back (1-365, default: 30)",
    )
    @app_commands.autocomplete(player=player_autocomplete)
    async def delivery_stats(
        self,
        interaction: discord.Interaction,
        player: Optional[str] = None,
        days: int = 30,
    ):
        await interaction.response.defer()

        # Sanitize days
        days = max(1, min(days, 365))

        target_player = None
        if player:
            try:
                # The autocomplete value is the player.unique_id
                target_player = await Player.objects.aget(unique_id=int(player))
            except (ValueError, Player.DoesNotExist):
                await interaction.followup.send(
                    "This player has not verified their account yet. Use `/verify` to link an account.",
                    ephemeral=True,
                )
                return
        else:
            # Default to the calling user
            try:
                target_player = await Player.objects.aget(
                    discord_user_id=interaction.user.id
                )
            except Player.DoesNotExist:
                await interaction.followup.send(
                    "You need to be verified to use this command. Use `/verify` to link your account.",
                    ephemeral=True,
                )
                return

        start_date = timezone.now() - timedelta(days=days)

        # Query ServerCargoArrivedLog
        stats_qs = (
            ServerCargoArrivedLog.objects.filter(
                player=target_player, timestamp__gte=start_date
            )
            .values("cargo_key")
            .annotate(
                count=Count("id"),
                total_payment=Sum("payment"),
                total_weight=Sum("weight"),
            )
            .order_by("-count")
        )

        # Get player name for display
        player_name = str(target_player.unique_id)
        try:
            latest_char = await target_player.get_latest_character()
            if latest_char:
                player_name = latest_char.name
        except Exception:
            pass

        embed = discord.Embed(
            title=f"📦 Delivery Stats: {player_name}",
            description=f"Statistics for the last {days} day(s)\nFrom {start_date.strftime('%Y-%m-%d')} to now",
            color=discord.Color.blue(),
            timestamp=timezone.now(),
        )

        if not await stats_qs.aexists():
            embed.description = (
                embed.description or ""
            ) + "\n\n**No deliveries found for this period.**"
            await interaction.followup.send(embed=embed)
            return

        # Prepare summary table
        lines = []
        grand_total_count = 0
        grand_total_payment = 0
        grand_total_weight = 0.0

        # CargoKey maps to labels via its choices
        cargo_labels = dict(CargoKey.choices)

        for item in [item async for item in stats_qs]:
            cargo_key = item["cargo_key"]
            cargo_name = cargo_labels.get(cargo_key, cargo_key)
            count = item["count"]
            payment = item["total_payment"] or 0
            weight = item["total_weight"] or 0.0

            grand_total_count += count
            grand_total_payment += payment
            grand_total_weight += weight

            lines.append(f"**{cargo_name}**")
            lines.append(f"└ {count} deliveries | ${payment:,} | {weight:,.1f} kg")

        # Discord embed fields have a limit, but we should be fine for most players
        # If too many cargo types, we might need pagination, but let's start simple
        chunk_size = 10
        for i in range(0, len(lines), chunk_size * 2):
            chunk = lines[i : i + chunk_size * 2]
            embed.add_field(
                name="\u200b" if i > 0 else "Cargo Breakdown",
                value="\n".join(chunk),
                inline=False,
            )

        embed.add_field(
            name="Grand Totals",
            value=(
                f"**Total Deliveries:** {grand_total_count}\n"
                f"**Total Payment:** ${grand_total_payment:,}\n"
                f"**Total Weight:** {grand_total_weight:,.1f} kg"
            ),
            inline=False,
        )

        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(DeliveryStatsCog(bot))
