import discord
from datetime import timedelta
from django.utils import timezone
from django.db.models import F, Prefetch
from discord import app_commands
from discord.ext import commands, tasks
from django.conf import settings
from amc.models import (
  ServerCargoArrivedLog,
  DeliveryJob,
  Delivery
)
from amc.utils import get_time_difference_string

class JobsCog(commands.Cog):
  def __init__(self, bot: commands.Bot):
    self.bot = bot
    self.channel_id = settings.DISCORD_JOBS_CHANNEL_ID
    self.deliveries_channel_id = settings.DISCORD_DELIVERIES_CHANNEL_ID
    self.message_id = None
    self.update_loop.start()

  def cog_unload(self):
    self.update_loop.cancel()

  def _build_job_embed(self, job, stale=False) -> discord.Embed:
    """Builds a Discord Embed for a single DeliveryJob object."""

    # --- Create the title and description (value part of the field) ---
    description = ''
    if job.completion_bonus:
        description += '\n**Completion Reward**: '
        description += f"{job.completion_bonus:,}"
    description += f"\n**Bonus multiplier**: {job.bonus_multiplier*100:.0f}%"

    description += f"\n**Expires in**: {get_time_difference_string(timezone.now(), job.expired_at)}"

    source_points = list(job.source_points.all())
    if source_points:
        description += '\n**ONLY from**: '
        description += ', '.join([point.name for point in source_points])
        
    destination_points = list(job.destination_points.all())
    if destination_points:
        description += '\n**ONLY to**: '
        description += ', '.join([point.name for point in destination_points])

    if job.description:
        description += f'\n**Description**: {job.description}'

    if deliveries := list(job.deliveries.all()):
      contributors = {}
      for delivery in deliveries:
        if delivery.character: 
          name = delivery.character.name
          contributors[name] = contributors.get(name, 0) + delivery.quantity

      if contributors:
        description += '\n\n**Contributors**:'
        sorted_contributors = sorted(
          contributors.items(), 
          key=lambda item: item[1], 
          reverse=True
        )

        for name, quantity in sorted_contributors:
          bonus = int(job.completion_bonus * quantity / job.quantity_requested)
          description += f'\n**{name}**: {quantity} ({bonus:,} bonus upon completion)'

    color = discord.Color.blue()
    if stale:
      if job.quantity_fulfilled == job.quantity_requested:
        color = discord.Color.green()
      else:
        color = discord.Color.red()
    # --- Assemble the embed ---
    embed = discord.Embed(
        title=f"Deliver: {job.get_cargo_key_display()} ({job.quantity_fulfilled}/{job.quantity_requested})",
        description=description.strip(),
        color=color,
        timestamp=job.requested_at # Use job creation time for consistency
    )
    embed.set_footer(text=f"Job ID: {job.id}")
    return embed

  def _build_delivery_embed(
    self,
    character_name,
    cargo_key,
    quantity,
    source_name,
    destination_name,
    payment,
    subsidy,
    vehicle_key,
  ) -> discord.Embed:
    description = ''
    description += '\n**Payment**: '
    description += f"{payment:,}"
    if subsidy:
      description += f" + Subsidy {subsidy:,} (Total: {payment+subsidy:,}"

    if source_name:
        description += '\n**From**: '
        description += source_name
        
    if destination_name:
        description += '\n**To**: '
        description += destination_name

    if vehicle_key:
        description += '\n**Vehicle**: '
        description += vehicle_key

    # --- Assemble the embed ---
    embed = discord.Embed(
        title=f"{character_name} delivered {quantity} {cargo_key}",
        description=description.strip(),
        color=discord.Color.green(),
        timestamp=timezone.now()
    )
    # embed.set_footer(text=f"Job ID: {job.id}")
    return embed

  async def post_delivery_embed(self, *args):
    deliveries_channel = self.bot.get_channel(self.deliveries_channel_id)
    if not deliveries_channel:
      print(f"Error: Could not find channel with ID {self.deliveries_channel_id}")
      return
    await deliveries_channel.send(embed=self._build_delivery_embed(*args))

  async def update_jobs(self):
    """
    Synchronizes Discord messages with the jobs in the database.
    Handles creating, updating, and deleting job messages.
    """
    channel = self.bot.get_channel(self.channel_id)
    if not channel:
        print(f"Error: Could not find channel with ID {self.channel_id}")
        return

    active_jobs = DeliveryJob.objects.prefetch_related(
        'source_points', 'destination_points'
    ).prefetch_related(
      Prefetch('deliveries', queryset=Delivery.objects.select_related('character'))
    ).filter(
        quantity_fulfilled__lt=F('quantity_requested'),
        expired_at__gte=timezone.now()
    )
    
    active_job_ids = set()

    async for job in active_jobs:
        active_job_ids.add(job.id)
        embed = self._build_job_embed(job)
        print('Embed CREATED')
        
        # UPDATE path
        if job.discord_message_id:
            try:
                message = await channel.fetch_message(job.discord_message_id)
                await message.edit(embed=embed)
            except discord.NotFound:
                # Message was deleted in Discord. Clear the invalid ID.
                # It will be recreated in the CREATE path below.
                job.discord_message_id = None
            except Exception as e:
                print(f"Error updating message for job {job.id}: {e}")

        # CREATE path
        if not job.discord_message_id:
            new_message = await channel.send(embed=embed)
            job.discord_message_id = new_message.id
            await job.asave(update_fields=['discord_message_id'])
        print('Embed CREATED done')

    # --- 2. CLEAN UP STALE MESSAGES (DELETE) ---
    # Find jobs that have a message ID but are no longer active
    stale_jobs = DeliveryJob.objects.filter(
      discord_message_id__isnull=False
    ).exclude(
      id__in=active_job_ids
    ).prefetch_related(
      Prefetch('deliveries', queryset=Delivery.objects.select_related('character'))
    )

    async for job in stale_jobs:
      embed = self._build_job_embed(job, stale=True)
      try:
        message = await channel.fetch_message(job.discord_message_id)
        await message.edit(embed=embed)
        print(f"Updated message for stale job {job.id}")
      except discord.NotFound:
        # Message already gone, which is fine.
        pass
      except Exception as e:
        print(f"Error deleting message for job {job.id}: {e}")
      finally:
        # Clear the ID from the database regardless
        job.discord_message_id = None
        await job.asave(update_fields=['discord_message_id'])

    return active_job_ids, stale_jobs

  @tasks.loop(minutes=1) # Reduced loop time for better responsiveness
  async def update_loop(self):
    """Periodically runs the synchronization logic."""
    print("Running job synchronization...")
    await self.update_jobs()
    print("Job synchronization finished.")

  @update_loop.before_loop
  async def before_update_loop(self):
    """Wait until the bot is ready before starting the loop."""
    await self.bot.wait_until_ready()

  async def cargo_autocomplete(self, interaction, current):
    unique_cargo_keys = ServerCargoArrivedLog.objects.values_list('cargo_key', flat=True).distinct()
    return [
      app_commands.Choice(name=f"{cargo_key}", value=cargo_key)
      async for cargo_key in unique_cargo_keys
      if current.lower() in cargo_key.lower()
    ][:25]  # Discord max choices: 25

  @app_commands.command(name='post_delivery_job', description='Post a delivery job')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
  @app_commands.autocomplete(cargo=cargo_autocomplete)
  async def post_delivery_job(self, interaction, cargo: str, quantity: int, expire_hours: int, bonus_multiplier: float):
    await DeliveryJob.objects.acreate(
      cargo_key=cargo,
      quantity_requested=quantity,
      expired_at=timezone.now() + timedelta(hours=expire_hours),
      bonus_multiplier=bonus_multiplier,
    )
    await interaction.response.send_message("Job Created", ephemeral=True)


  @app_commands.command(name='update_jobs_embeds', description='Manually update jobs embeds')
  @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
  async def update_jobs_embeds(self, interaction):
    active_job_ids = await self.update_jobs()
    await interaction.response.send_message(f"Updated {str(active_job_ids)}", ephemeral=True)
