import discord
from discord import app_commands
from discord.ext import commands

from amc.models import Player, Voucher


class VouchersCog(commands.Cog):
    """Let players look up their own voucher codes."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="my_vouchers",
        description="List your unclaimed voucher codes (visible only to you)",
    )
    async def my_vouchers(self, interaction: discord.Interaction):
        try:
            player = await Player.objects.aget(discord_user_id=interaction.user.id)
        except Player.DoesNotExist:
            await interaction.response.send_message(
                "❌ No linked game account found. Make sure your Discord is linked "
                "to your in-game account.",
                ephemeral=True,
            )
            return

        vouchers = [
            v
            async for v in Voucher.objects.filter(
                player=player, claimed_at__isnull=True
            ).order_by("-created_at")
        ]

        if not vouchers:
            await interaction.response.send_message(
                "You have no unclaimed vouchers.", ephemeral=True
            )
            return

        total = sum(v.amount for v in vouchers)
        lines = [
            f"`{v.code}` — **${v.amount:,}** · {v.reason}"
            + (f" · <t:{int(v.created_at.timestamp())}:R>" if v.created_at else "")
            for v in vouchers
        ]
        embed = discord.Embed(
            title="🎟️ Your unclaimed vouchers",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(
            text=f"{len(vouchers)} voucher{'s' if len(vouchers) != 1 else ''} · "
            f"total ${total:,} — redeem in-game with /claim_voucher <code>"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
