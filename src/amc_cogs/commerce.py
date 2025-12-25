import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from django.utils import timezone
from datetime import timedelta
from amc.models import Player, MinistryElection, MinistryCandidacy, MinistryVote, MinistryTerm
from django.db.models import Count
from amc_finance.services import allocate_ministry_budget
from django.conf import settings

class CandidateSelect(ui.Select):
    def __init__(self, candidates):
        options = [
            discord.SelectOption(
                label=c.candidate.discord_name or str(c.candidate.unique_id),
                description=c.manifesto[:100] if c.manifesto else "No manifesto provided",
                value=str(c.id)
            ) for c in candidates
        ]
        super().__init__(placeholder="Choose a candidate to vote for...", options=options)

    async def callback(self, interaction: discord.Interaction):
        candidate_id = int(self.values[0])
        try:
            player = await Player.objects.aget(discord_user_id=interaction.user.id)
        except Player.DoesNotExist:
            await interaction.response.send_message("You must be verified to vote.", ephemeral=True)
            return

        election = await MinistryElection.objects.filter(
            poll_end_at__gt=timezone.now(),
            candidacy_end_at__lt=timezone.now()
        ).afirst()
        
        if not election:
            await interaction.response.send_message("There is no active voting period.", ephemeral=True)
            return

        # Record or update vote
        await MinistryVote.objects.aupdate_or_create(
            election=election,
            voter=player,
            defaults={'candidate_id': candidate_id}
        )
        await interaction.response.send_message("Your vote has been recorded!", ephemeral=True)

class VoteView(ui.View):
    def __init__(self, candidates):
        super().__init__(timeout=None)
        self.add_item(CandidateSelect(candidates))

class CommerceCog(commands.Cog):
    def __init__(self, bot, channel_id=settings.DISCORD_GENERAL_CHANNEL_ID):
        self.bot = bot
        self.channel_id = channel_id
        self.manage_elections_task.start()

    def cog_unload(self):
        self.manage_elections_task.cancel()

    async def get_announcement_channel(self):
        channel = self.bot.get_channel(self.channel_id)
        if not channel and self.channel_id:
            try:
                channel = await self.bot.fetch_channel(self.channel_id)
            except discord.HTTPException as e:
                print(f"Error fetching announcement channel {self.channel_id}: {e}")
        return channel

    @tasks.loop(minutes=30)
    async def manage_elections_task(self):
        now = timezone.now()
        # 1. Check if there is an active election
        active_term = await MinistryTerm.objects.filter(is_active=True).afirst()
        active_election = await MinistryElection.objects.filter(is_processed=False, poll_end_at__gt=now).afirst()
        
        if not active_election:
            # Check for election whose poll ended but not processed
            ended_election = await MinistryElection.objects.filter(is_processed=False, poll_end_at__lte=now).afirst()
            if ended_election:
                # Calculate winner
                winner_candidacy = await (ended_election.candidates
                    .select_related('candidate')
                    .annotate(num_votes=Count('votes'))
                    .order_by('-num_votes')
                    .afirst()
                )
                
                channel = await self.get_announcement_channel()
                
                if winner_candidacy:
                    ended_election.winner_id = winner_candidacy.candidate.unique_id
                    
                    # Deactivate old term
                    if active_term:
                        active_term.is_active = False
                        await active_term.asave()
                    
                    # Create new term
                    start_date = active_term.end_date if (active_term and active_term.end_date > now) else now
                    new_term = await MinistryTerm.objects.acreate(
                        minister_id=winner_candidacy.candidate.unique_id,
                        start_date=start_date,
                        end_date=start_date + timedelta(days=7),
                        initial_budget=50_000_000, # Default budget
                        current_budget=50_000_000,
                        is_active=True
                    )
                    ended_election.term_created_id = new_term.id
                    
                    # Allocate budget financially
                    await allocate_ministry_budget(50_000_000, new_term)
                    
                    if channel:
                        embed = discord.Embed(
                            title="🎉 Election Results: Minister of Commerce",
                            description=f"Congratulations to **{winner_candidacy.candidate.discord_name or winner_candidacy.candidate.unique_id}** for winning the election!",
                            color=discord.Color.gold()
                        )
                        embed.add_field(name="Tenure", value=f"From <t:{int(new_term.start_date.timestamp())}:D> to <t:{int(new_term.end_date.timestamp())}:D>")
                        embed.add_field(name="Budget Allocated", value="₱50,000,000")
                        await channel.send(embed=embed)
                else:
                    if channel:
                        await channel.send("⚠️ The Minister of Commerce election has ended with no candidates. A new election will begin shortly.")
                
                # Mark as processed regardless of winner
                ended_election.is_processed = True
                await ended_election.asave()
            else:
                # Start new election if term ending soon
                if not active_term or active_term.end_date < now + timedelta(days=7):
                    election = await MinistryElection.objects.acreate(
                        candidacy_end_at=now + timedelta(days=4),
                        poll_end_at=now + timedelta(days=7)
                    )
                    
                    channel = await self.get_announcement_channel()
                    if channel:
                        embed = discord.Embed(
                            title="📢 Elections: Minister of Commerce",
                            description="A new election for the Minister of Commerce has begun!",
                            color=discord.Color.blue()
                        )
                        embed.add_field(name="Candidacy Phase", value=f"Ends <t:{int(election.candidacy_end_at.timestamp())}:R>")
                        embed.add_field(name="How to Run", value="Use `/run_for_minister manifesto: ...` (Requires 50h in-game time)")
                        await channel.send(embed=embed)
        else:
            # Maybe announce transition from candidacy to polling?
            if active_election.phase == MinistryElection.Phase.POLLING:
                # We could check if we already announced it using a flag or just skip for now to avoid spam
                pass
        
    @app_commands.command(name="run_for_minister", description="Run for the position of Minister of Commerce")
    async def run_for_minister(self, interaction: discord.Interaction, manifesto: str):
        await interaction.response.defer(ephemeral=True)
        try:
            player = await Player.objects.with_total_session_time().aget(discord_user_id=interaction.user.id)
        except Player.DoesNotExist:
            await interaction.followup.send("You must be verified to run for office.", ephemeral=True)
            return

        # Check 50h requirement
        if player.total_session_time < timedelta(hours=50):
            hours = player.total_session_time.total_seconds() / 3600
            await interaction.followup.send(f"You need at least 50 hours of in-game time to run. You have {hours:.1f} hours.", ephemeral=True)
            return

        election = await MinistryElection.objects.filter(candidacy_end_at__gt=timezone.now()).afirst()
        if not election:
            await interaction.followup.send("There is no active candidacy period.", ephemeral=True)
            return
        
        if election.phase != MinistryElection.Phase.CANDIDACY:
            await interaction.followup.send("The candidacy period has ended.", ephemeral=True)
            return

        await MinistryCandidacy.objects.aupdate_or_create(
            election=election,
            candidate=player,
            defaults={'manifesto': manifesto}
        )
        await interaction.followup.send("You are now a candidate for Minister of Commerce!", ephemeral=True)

    @app_commands.command(name="vote", description="Vote for your candidate")
    async def vote(self, interaction: discord.Interaction):
        election = await MinistryElection.objects.filter(
            poll_end_at__gt=timezone.now(),
            candidacy_end_at__lt=timezone.now()
        ).afirst()
        
        if not election:
            await interaction.response.send_message("There is no active voting period.", ephemeral=True)
            return
            
        candidates = [c async for c in election.candidates.select_related('candidate').all()]
        if not candidates:
            await interaction.response.send_message("There are no candidates in this election.", ephemeral=True)
            return
            
        await interaction.response.send_message("Select a candidate to vote for:", view=VoteView(candidates), ephemeral=True)

    @app_commands.command(name="election_status", description="Show the current status of the Minister of Commerce election")
    async def election_status(self, interaction: discord.Interaction):
        election = await MinistryElection.objects.order_by('-created_at').afirst()
        if not election:
            await interaction.response.send_message("No elections have been held yet.")
            return

        phase = election.phase
        embed = discord.Embed(title="Minister of Commerce Election Status", color=discord.Color.blue())
        embed.add_field(name="Current Phase", value=election.get_phase_display())
        
        if phase == MinistryElection.Phase.CANDIDACY:
            embed.description = f"Candidacy ends at <t:{int(election.candidacy_end_at.timestamp())}:R>"
            candidates = [c.candidate.discord_name or str(c.candidate.unique_id) async for c in election.candidates.select_related('candidate').all()]
            embed.add_field(name="Candidates", value="\n".join(candidates) or "None yet", inline=False)
        elif phase == MinistryElection.Phase.POLLING:
            embed.description = f"Polling ends at <t:{int(election.poll_end_at.timestamp())}:R>"
            candidates = [c.candidate.discord_name or str(c.candidate.unique_id) async for c in election.candidates.select_related('candidate').all()]
            embed.add_field(name="Candidates", value="\n".join(candidates) or "None", inline=False)
        elif phase == MinistryElection.Phase.FINALIZED:
            if election.winner:
                embed.add_field(name="Winner", value=election.winner.discord_name or str(election.winner.unique_id))
            else:
                embed.description = "Election ended with no winner."
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ministry_budget", description="Show the current Minister of Commerce's budget status")
    async def ministry_budget(self, interaction: discord.Interaction):
        term = await MinistryTerm.objects.filter(is_active=True).select_related('minister').afirst()
        if not term:
            await interaction.response.send_message("There is no active Ministry of Commerce term.")
            return

        embed = discord.Embed(title="Ministry of Commerce Budget", color=discord.Color.gold())
        embed.set_author(name=f"Minister {term.minister.discord_name or term.minister.unique_id}")
        
        embed.add_field(name="Current Budget", value=f"${term.current_budget:,.2f}", inline=True)
        embed.add_field(name="Total Spent", value=f"${term.total_spent:,.2f}", inline=True)
        embed.add_field(name="Initial Budget", value=f"${term.initial_budget:,.2f}", inline=True)
        
        embed.add_field(name="Jobs Created", value=str(term.created_jobs_count), inline=True)
        embed.add_field(name="Jobs Expired", value=str(term.expired_jobs_count), inline=True)
        
        embed.description = f"Term ends <t:{int(term.end_date.timestamp())}:R>"
        
        # Add summary of subsidy allocations
        subsidy_spent = [s async for s in SubsidyRule.objects.filter(active=True, allocation__gt=0)]
        if subsidy_spent:
            subsidy_text = "\n".join([f"**{s.name}**: ${s.spent:,.0f} / ${s.allocation:,.0f}" for s in subsidy_spent])
            embed.add_field(name="Subsidy Allocations", value=subsidy_text, inline=False)

        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(CommerceCog(bot))
