import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from django.utils import timezone

from amc.models import Player, Voucher
from amc_cogs.models import TuningWorkshopSubmission

logger = logging.getLogger(__name__)


class TuningWorkshopCog(commands.Cog):
    """Monitors #tuning-workshop forum and rewards posts based on reactions (on-demand)."""

    FORUM_CHANNEL_ID = 1353368480988008448
    REWARD_PER_REACTION = 100_000
    MAX_POSTS_PER_WEEK = 2

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        # Only handle threads in the tuning workshop forum
        if not thread.parent or thread.parent_id != self.FORUM_CHANNEL_ID:
            return

        now = timezone.now()
        author_id = thread.owner_id

        # Check weekly post limit
        week_ago = now - timedelta(days=7)
        recent_count = await TuningWorkshopSubmission.objects.filter(
            author_discord_id=author_id,
            created_at__gte=week_ago,
            skipped=False,
        ).acount()

        skipped = recent_count >= self.MAX_POSTS_PER_WEEK

        await TuningWorkshopSubmission.objects.acreate(
            thread_id=thread.id,
            author_discord_id=author_id,
            created_at=now,
            skipped=skipped,
        )

        if skipped:
            logger.info(
                f"Tuning workshop submission {thread.id} by {author_id} skipped "
                f"(weekly limit of {self.MAX_POSTS_PER_WEEK} reached)"
            )
            embed = discord.Embed(
                title="🔧 Tuning Workshop",
                description=(
                    "⚠️ You've reached the weekly limit of "
                    f"**{self.MAX_POSTS_PER_WEEK} rewardable posts**.\n\n"
                    "This post will not be eligible for rewards. "
                    "Try again next week!"
                ),
                color=discord.Color.orange(),
            )
        else:
            embed = discord.Embed(
                title="🔧 Tuning Workshop",
                description=(
                    "Thanks for your submission! Here's how rewards work:\n\n"
                    f"💰 **${self.REWARD_PER_REACTION:,}** per unique reaction from other members\n"
                    "📈 Rewards **accumulate over time** — there's no deadline!\n"
                    "⚡ Use `/claim_reward` in this thread whenever you want to cash out\n"
                    "🔄 You can claim **multiple times** — each claim pays out only new reactions\n"
                    "🎟️ You'll receive a voucher code to redeem in-game with `/claim_voucher <code>`"
                ),
                color=discord.Color.blue(),
            )
            embed.set_footer(
                text=f"Weekly limit: {self.MAX_POSTS_PER_WEEK} rewardable posts per user"
            )

        await thread.join()
        await thread.send(embed=embed)

    @app_commands.command(
        name="claim_reward",
        description="Claim your tuning workshop reward",
    )
    async def claim_reward(self, interaction: discord.Interaction):
        """Let the thread author claim their accumulated reward."""
        channel = interaction.channel

        # Must be in a thread within the tuning workshop forum
        if (
            not isinstance(channel, discord.Thread)
            or channel.parent_id != self.FORUM_CHANNEL_ID
        ):
            await interaction.response.send_message(
                "❌ This command can only be used inside a #tuning-workshop thread.",
                ephemeral=True,
            )
            return

        # Look up submission
        try:
            submission = await TuningWorkshopSubmission.objects.aget(
                thread_id=channel.id
            )
        except TuningWorkshopSubmission.DoesNotExist:
            await interaction.response.send_message(
                "❌ No tracked submission found for this thread.",
                ephemeral=True,
            )
            return

        # Must be the author
        if interaction.user.id != submission.author_discord_id:
            await interaction.response.send_message(
                "❌ Only the thread author can claim the reward.",
                ephemeral=True,
            )
            return

        if submission.skipped:
            await interaction.response.send_message(
                "❌ This submission was skipped (weekly limit exceeded).",
                ephemeral=True,
            )
            return

        # Defer before the slow reaction-counting phase to avoid
        # Discord's 3-second interaction timeout (error 10062).
        await interaction.response.defer(ephemeral=True)

        # Count current reactions across starter + author's image messages
        unique_reactors = await self._count_unique_reactors(
            channel, submission.author_discord_id
        )
        if unique_reactors is None:
            await interaction.followup.send(
                "❌ Could not fetch messages to count reactions.",
            )
            return

        current_count = len(unique_reactors)
        new_reactions = current_count - submission.rewarded_reaction_count

        if new_reactions <= 0:
            await interaction.followup.send(
                "❌ No new reactions to claim since your last cashout."
                if submission.rewarded_reaction_count > 0
                else "❌ This post has no reactions from other users yet.",
            )
            return

        # Look up the author's Player record
        try:
            player = await Player.objects.aget(
                discord_user_id=submission.author_discord_id
            )
        except Player.DoesNotExist:
            await interaction.followup.send(
                "❌ No linked game account found. Make sure your Discord is linked to your in-game account.",
            )
            return

        # Create voucher for the delta, tied to the author's player
        reward_amount = new_reactions * self.REWARD_PER_REACTION
        code = Voucher.generate_code(prefix="TW")
        await Voucher.objects.acreate(
            code=code,
            amount=reward_amount,
            reason=f"Tuning Workshop: {new_reactions} new reactions",
            player=player,
        )

        # Update submission
        submission.reaction_count = current_count
        submission.rewarded_reaction_count = current_count
        await submission.asave(
            update_fields=["reaction_count", "rewarded_reaction_count"]
        )

        # Send voucher code ephemerally (only visible to the author)
        await interaction.followup.send(
            f"✅ Voucher issued for **${reward_amount:,}**!\n"
            f"🎟️ Code: `{code}`\n"
            f"Use `/claim_voucher {code}` in-game to deposit.",
        )

        # Post a public announcement (without the code)
        embed = discord.Embed(
            title="🔧 Tuning Workshop Reward Claimed",
            description=(
                f"**{new_reactions}** new reaction{'s' if new_reactions != 1 else ''} cashed out!\n\n"
                f"💰 Reward: **${reward_amount:,}**"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(
            text=f"Total reactions rewarded: {current_count}"
        )
        await channel.send(embed=embed)

    @app_commands.command(
        name="backfill_workshop",
        description="Import existing tuning workshop threads from the last 30 days",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def backfill_workshop(self, interaction: discord.Interaction):
        """Backfill tuning workshop threads that predate the reward system."""
        await interaction.response.defer(ephemeral=True)

        forum = self.bot.get_channel(self.FORUM_CHANNEL_ID)
        if not isinstance(forum, discord.ForumChannel):
            await interaction.followup.send("❌ Forum channel not found.")
            return

        cutoff = timezone.now() - timedelta(days=30)

        # Gather active + archived threads
        threads = list(forum.threads)
        async for thread in forum.archived_threads(limit=None):
            threads.append(thread)

        # Filter to recent threads only
        threads = [t for t in threads if t.created_at and t.created_at >= cutoff]

        backfilled = 0
        skipped = 0
        for thread in threads:
            exists = await TuningWorkshopSubmission.objects.filter(
                thread_id=thread.id
            ).aexists()
            if exists:
                skipped += 1
                continue

            await TuningWorkshopSubmission.objects.acreate(
                thread_id=thread.id,
                author_discord_id=thread.owner_id,
                created_at=thread.created_at,
                skipped=False,
            )

            embed = discord.Embed(
                title="🔧 Tuning Workshop",
                description=(
                    "This thread is now eligible for rewards! Here's how it works:\n\n"
                    f"💰 **${self.REWARD_PER_REACTION:,}** per unique reaction from other members\n"
                    "📈 Rewards **accumulate over time** — there's no deadline!\n"
                    "⚡ Use `/claim_reward` in this thread whenever you want to cash out\n"
                    "🔄 You can claim **multiple times** — each claim pays out only new reactions\n"
                    "🎟️ You'll receive a voucher code to redeem in-game with `/claim_voucher <code>`"
                ),
                color=discord.Color.blue(),
            )
            embed.set_footer(
                text=f"Weekly limit: {self.MAX_POSTS_PER_WEEK} rewardable posts per user"
            )
            await thread.join()
            await thread.send(embed=embed)
            backfilled += 1

        await interaction.followup.send(
            f"✅ Backfill complete: **{backfilled}** threads imported, "
            f"**{skipped}** already tracked.",
            ephemeral=True,
        )

    async def _count_unique_reactors(self, channel, author_id):
        """Count unique reactors across the starter message and author's image messages.

        Returns a set of unique reactor user IDs, or None if the starter message
        could not be fetched.
        """
        try:
            starter_message = await channel.fetch_message(channel.id)
        except (discord.NotFound, discord.HTTPException):
            return None

        unique_reactors = set()

        # Collect reactions from starter message
        for reaction in starter_message.reactions:
            async for user in reaction.users():
                if user.bot or user.id == author_id:
                    continue
                unique_reactors.add(user.id)

        # Collect reactions from author's image messages in the thread
        async for message in channel.history(limit=None):
            if message.id == starter_message.id:
                continue  # Already counted
            if message.author.id != author_id:
                continue
            if not any(
                a.content_type and a.content_type.startswith("image/")
                for a in message.attachments
            ):
                continue
            for reaction in message.reactions:
                async for user in reaction.users():
                    if user.bot or user.id == author_id:
                        continue
                    unique_reactors.add(user.id)

        return unique_reactors
