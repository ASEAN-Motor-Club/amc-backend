import logging
import asyncio
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

import discord
from discord import app_commands
from discord.ext import commands
from django.conf import settings
from amc.game_server import get_players, announce

logger = logging.getLogger(__name__)

# Path to the least-privilege wrapper script on the host.
# This script ONLY triggers motortown-server-update.service.
UPDATE_SCRIPT = getattr(
    settings, "UPDATE_MOTORTOWN_SCRIPT", "/usr/local/bin/update-motortown"
)

# Cooldown period in seconds between updates
UPDATE_COOLDOWN_SECONDS = 600  # 10 minutes


def _format_time(seconds: int) -> str:
    """Format seconds into a human-readable string."""
    if seconds >= 60:
        minutes = seconds // 60
        remaining = seconds % 60
        if remaining > 0:
            return f"{minutes}m {remaining}s"
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


class UpdateConfirmView(discord.ui.View):
    """
    Confirmation view with Confirm/Cancel buttons for server updates.
    After confirmation, runs a countdown with in-game announcements,
    then triggers the update.
    """

    def __init__(
        self,
        cog: "ServerCog",
        interaction: discord.Interaction,
        countdown_seconds: int,
        reason: Optional[str],
        player_count: int,
    ):
        super().__init__(timeout=60)
        self.cog = cog
        self.original_interaction = interaction
        self.countdown_seconds = countdown_seconds
        self.reason = reason
        self.player_count = player_count
        self.cancelled = False
        self._countdown_task: Optional[asyncio.Task] = None

    async def disable_buttons(self):
        for item in self.children:
            if hasattr(item, "disabled"):
                setattr(item, "disabled", True)
        try:
            await self.original_interaction.edit_original_response(view=self)
        except discord.NotFound:
            pass

    async def on_timeout(self):
        await self.disable_buttons()
        try:
            await self.original_interaction.followup.send(
                "⏰ Update confirmation timed out.", ephemeral=True
            )
        except discord.NotFound:
            pass

    @discord.ui.button(
        label="Confirm Update", style=discord.ButtonStyle.danger, emoji="⬆️"
    )
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        await self.disable_buttons()
        self.stop()

        # Start the countdown in a background task
        self._countdown_task = asyncio.create_task(self._run_countdown(interaction))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.cancelled = True
        await self.disable_buttons()
        self.stop()
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()
        await interaction.response.send_message(
            "❌ Server update cancelled.", ephemeral=True
        )

    async def _run_countdown(self, interaction: discord.Interaction):
        """Run countdown with periodic in-game announcements, then trigger update."""
        countdown = self.countdown_seconds
        reason_suffix = f" Reason: {self.reason}" if self.reason else ""

        # Determine announcement milestones
        announce_at = set()
        halfway = countdown // 2
        if halfway > 60:
            announce_at.add(halfway)
        for t in [300, 120, 60, 30, 10]:
            if t < countdown:
                announce_at.add(t)

        await interaction.followup.send(
            f"⬆️ Server update initiated. Countdown: **{countdown}s**.{reason_suffix}",
            ephemeral=True,
        )

        # Initial in-game announcement
        try:
            await announce(
                f"⬆ Server updating in {_format_time(countdown)}.{reason_suffix}",
                self.cog.bot.http_client_game,
                type="announce",
                color="FF8800",
            )
        except Exception:
            logger.exception("Failed to send initial update announcement")

        start_time = asyncio.get_event_loop().time()

        while countdown > 0:
            if self.cancelled:
                try:
                    await announce(
                        "✅ Server update has been cancelled.",
                        self.cog.bot.http_client_game,
                    )
                except Exception:
                    logger.exception("Failed to send cancel announcement")
                return

            await asyncio.sleep(1)
            elapsed = asyncio.get_event_loop().time() - start_time
            countdown = max(0, self.countdown_seconds - int(elapsed))

            if countdown in announce_at:
                announce_at.discard(countdown)
                try:
                    await announce(
                        f"⬆ Server updating in {_format_time(countdown)}.{reason_suffix}",
                        self.cog.bot.http_client_game,
                        type="announce",
                        color="FF8800",
                    )
                except Exception:
                    logger.exception(
                        f"Failed to send countdown announcement at {countdown}s"
                    )

        if self.cancelled:
            return

        # Execute update
        await self._execute_update(interaction)

    async def _execute_update(self, interaction: discord.Interaction):
        """Trigger the update via the least-privilege wrapper script."""
        try:
            process = await asyncio.create_subprocess_exec(
                UPDATE_SCRIPT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

            if process.returncode == 0:
                self.cog._last_update_time = time.monotonic()
                await interaction.followup.send(
                    "✅ Server update triggered successfully."
                )
                await self._send_audit_log(success=True)
            else:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                await interaction.followup.send(
                    f"❌ Server update failed (exit code {process.returncode}):\n```\n{error_msg}\n```"
                )
                await self._send_audit_log(success=False, error=error_msg)

        except asyncio.TimeoutError:
            await interaction.followup.send(
                "❌ Server update timed out after 30 seconds."
            )
            await self._send_audit_log(success=False, error="Subprocess timed out")
        except Exception as e:
            logger.exception("Failed to execute update script")
            await interaction.followup.send(f"❌ Server update failed: {e}")
            await self._send_audit_log(success=False, error=str(e))

    async def _send_audit_log(self, success: bool, error: Optional[str] = None):
        """Send an audit log embed to the admin channel."""
        admin_channel = self.cog.bot.get_channel(self.cog.audit_channel_id)
        if not isinstance(admin_channel, discord.abc.Messageable):
            logger.warning("Audit channel not found or not messageable")
            return

        embed = discord.Embed(
            title="⬆️ Server Update" if success else "❌ Server Update Failed",
            color=discord.Color.green() if success else discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Triggered by",
            value=f"{self.original_interaction.user.mention} ({self.original_interaction.user.display_name})",
            inline=True,
        )
        embed.add_field(
            name="Countdown", value=f"{self.countdown_seconds}s", inline=True
        )
        embed.add_field(
            name="Players online at trigger",
            value=str(self.player_count),
            inline=True,
        )
        if self.reason:
            embed.add_field(name="Reason", value=self.reason, inline=False)
        if error:
            embed.add_field(
                name="Error",
                value=f"```\n{error}\n```\nNote: server will be restarted via failure recovery.",
                inline=False,
            )

        try:
            await admin_channel.send(embed=embed)
        except Exception:
            logger.exception("Failed to send audit log")


class ServerCog(commands.Cog):
    """Server management commands (admin-only)."""

    admin = app_commands.Group(
        name="server",
        description="Server management commands",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: "AMCDiscordBot"):
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self._last_update_time: float = 0
        self.audit_channel_id = getattr(settings, "DISCORD_AUDIT_CHANNEL_ID", 0)

    @admin.command(name="update", description="Update the Motor Town game server")
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    @app_commands.describe(
        countdown_seconds="Countdown in seconds before update (default: 300)",
        reason="Optional reason for the update",
    )
    async def update_server(
        self,
        interaction: discord.Interaction,
        countdown_seconds: int = 300,
        reason: Optional[str] = None,
    ):
        # Validate countdown
        if countdown_seconds < 10:
            await interaction.response.send_message(
                "❌ Countdown must be at least 10 seconds.", ephemeral=True
            )
            return
        if countdown_seconds > 3600:
            await interaction.response.send_message(
                "❌ Countdown cannot exceed 3600 seconds (1 hour).", ephemeral=True
            )
            return

        # Cooldown check
        elapsed = time.monotonic() - self._last_update_time
        if elapsed < UPDATE_COOLDOWN_SECONDS and self._last_update_time > 0:
            remaining = int(UPDATE_COOLDOWN_SECONDS - elapsed)
            await interaction.response.send_message(
                f"⏳ Cooldown active. Please wait **{remaining}s** before triggering another update.",
                ephemeral=True,
            )
            return

        # Fetch player count for the confirmation prompt
        try:
            players = await get_players(self.bot.http_client_game)
            player_count = len(players)
        except Exception:
            self.logger.exception("Failed to fetch player count")
            player_count = -1  # Unknown

        # Build confirmation embed
        embed = discord.Embed(
            title="⬆️ Confirm Server Update",
            color=discord.Color.orange(),
            description="Are you sure you want to update the Motor Town server?\n\nThe server will go down during the update.",
        )
        if player_count >= 0:
            embed.add_field(name="Players Online", value=str(player_count), inline=True)
        else:
            embed.add_field(name="Players Online", value="⚠️ Unknown", inline=True)
        embed.add_field(
            name="Countdown",
            value=_format_time(countdown_seconds),
            inline=True,
        )
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text="This confirmation expires in 60 seconds.")

        view = UpdateConfirmView(
            cog=self,
            interaction=interaction,
            countdown_seconds=countdown_seconds,
            reason=reason,
            player_count=max(player_count, 0),
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
