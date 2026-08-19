"""Manual-review action buttons for name moderation.

When the LLM judge routes a name to manual review, `name_policy._log_manual_review`
enqueues a message with a `NameReviewView`. The two buttons let an admin act
directly from Discord:

- **Rename** — applies the forced-name lock via the shared `name_policy` path
  (set `forced_name` + `ForcedNameLog` audit + in-game refresh + announce).
- **Whitelist** — persists a per-player `NameWhitelist` row so the LLM judge is
  skipped for that name on future logins (scoped to that player only).

Both are gated to the admin role (`DISCORD_ADMIN_ROLE_ID`), disable their buttons
after acting, and stamp the `NameModerationLog` audit row with the outcome.

Important: each button's `custom_id` must be unique per message (it embeds the
`log_id`). discord.py's `ViewStore` keys live views by `custom_id`, so a fixed id
shared across many review messages would let the newest message clobber the older
messages' button callbacks. Buttons are therefore built dynamically in `__init__`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from django.conf import settings

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

logger = logging.getLogger(__name__)


def _review_custom_id(log_id: int, action: str) -> str:
    return f"namer_review_{log_id}_{action}"


class NameReviewView(discord.ui.View):
    def __init__(self, bot: "AMCDiscordBot", log_id: int, *, timeout=None):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.log_id = log_id

        rename = discord.ui.Button(
            label="Rename", style=discord.ButtonStyle.danger, emoji="✏️",
            custom_id=_review_custom_id(log_id, "rename"),
        )
        rename.callback = self.rename_callback
        self.add_item(rename)

        whitelist = discord.ui.Button(
            label="Whitelist", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=_review_custom_id(log_id, "whitelist"),
        )
        whitelist.callback = self.whitelist_callback
        self.add_item(whitelist)

    def _drop_from_store(self):
        views = getattr(self.bot, "_review_views", None)
        if views is not None:
            views.pop(self.log_id, None)

    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is None:
            return False
        member = guild.get_member(interaction.user.id)
        if member is None:
            return False
        admin_role_id = int(settings.DISCORD_ADMIN_ROLE_ID or 0)
        if not admin_role_id:
            return False
        role = guild.get_role(admin_role_id)
        return bool(role and role in member.roles)

    async def _disable_buttons(self, interaction: discord.Interaction):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        message = interaction.message
        if message is not None:
            try:
                await message.edit(view=self)
            except discord.NotFound:
                pass

    async def rename_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await self._is_admin(interaction):
            await interaction.followup.send("Admins only.", ephemeral=True)
            return
        try:
            from amc.name_policy import apply_review_rename

            new_name = await apply_review_rename(
                self.log_id,
                actor_discord_id=interaction.user.id,
                http_client=self.bot.http_client_game,
                http_client_mod=self.bot.http_client_mod,
            )
            await self._disable_buttons(interaction)
            self._drop_from_store()
            await interaction.followup.send(
                f"Renamed to **{new_name}**.", ephemeral=True
            )
        except Exception as e:
            logger.exception("NameReview rename failed for log %s", self.log_id)
            await interaction.followup.send(
                f"Rename failed: {e}", ephemeral=True
            )

    async def whitelist_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await self._is_admin(interaction):
            await interaction.followup.send("Admins only.", ephemeral=True)
            return
        try:
            from amc.name_policy import apply_review_whitelist

            base = await apply_review_whitelist(
                self.log_id, actor_discord_id=interaction.user.id
            )
            await self._disable_buttons(interaction)
            self._drop_from_store()
            await interaction.followup.send(
                f"Whitelisted `{base}` for this player.", ephemeral=True
            )
        except Exception as e:
            logger.exception("NameReview whitelist failed for log %s", self.log_id)
            await interaction.followup.send(
                f"Whitelist failed: {e}", ephemeral=True
            )

    async def on_timeout(self):
        self._drop_from_store()


def _undo_custom_id(log_id: int) -> str:
    return f"namer_undo_{log_id}"


class NameAutoRenameView(NameReviewView):
    """View for a COMPLETED auto-rename audit — an Undo & Whitelist button.

    Reuses NameReviewView's admin gate / disable / store-drop helpers but
    exposes a single "Undo & Whitelist" button instead of Rename/Whitelist.
    It lets an admin revert a false-positive auto-rename (e.g. N17R0 -> NITRO)
    straight from the audit message, restoring the original name AND whitelisting
    it for that player so the LLM judge skips it on future logins.
    """

    def __init__(self, bot: "AMCDiscordBot", log_id: int, *, timeout=None):
        # Base adds the manual-review Rename/Whitelist buttons — clear them.
        super().__init__(bot, log_id, timeout=timeout)
        self.clear_items()

        undo = discord.ui.Button(
            label="Undo & Whitelist",
            style=discord.ButtonStyle.success,
            emoji="↩️",
            custom_id=_undo_custom_id(log_id),
        )
        undo.callback = self.undo_callback
        self.add_item(undo)

    async def undo_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await self._is_admin(interaction):
            await interaction.followup.send("Admins only.", ephemeral=True)
            return
        try:
            from amc.name_policy import apply_auto_rename_undo

            new_name = await apply_auto_rename_undo(
                self.log_id,
                actor_discord_id=interaction.user.id,
                http_client=self.bot.http_client_game,
                http_client_mod=self.bot.http_client_mod,
            )
            await self._disable_buttons(interaction)
            self._drop_from_store()
            await interaction.followup.send(
                f"Restored **{new_name}** and whitelisted it.", ephemeral=True
            )
        except Exception as e:
            logger.exception("NameAutoRename undo failed for log %s", self.log_id)
            await interaction.followup.send(
                f"Undo failed: {e}", ephemeral=True
            )


async def send_review_message(bot, channel_id, log_id, content) -> None:
    """Send a manual-review message with action buttons on the bot's loop.

    Runs on the bot's event loop (scheduled via run_coroutine_threadsafe). Keeps a
    reference to the view on the bot so button interactions stay alive.
    """
    if not bot.is_ready():
        await bot.wait_until_ready()
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        logger.warning("review channel %s not found for log %s", channel_id, log_id)
        return
    view = NameReviewView(bot, log_id)
    await channel.send(content, view=view)
    bot._review_views[log_id] = view


async def send_rename_audit_message(bot, channel_id, log_id, content) -> None:
    """Send an auto-rename audit message WITH an Undo & Whitelist button.

    Same store/retrieval mechanics as `send_review_message`, but the view is a
    `NameAutoRenameView` (a single "Undo & Whitelist" button). Keyed by `log_id`
    in `bot._review_views` like the manual-review views.
    """
    if not bot.is_ready():
        await bot.wait_until_ready()
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        logger.warning("rename-audit channel %s not found for log %s", channel_id, log_id)
        return
    view = NameAutoRenameView(bot, log_id)
    await channel.send(content, view=view)
    bot._review_views[log_id] = view


class NameReviewCog(commands.Cog):
    def __init__(self, bot: "AMCDiscordBot"):
        self.bot = bot
