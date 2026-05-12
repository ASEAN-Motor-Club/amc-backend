import discord
from django.utils import timezone
from django.db.models import Prefetch
from discord import app_commands
from discord.ext import commands, tasks
from django.conf import settings
from amc.models import ServerCargoArrivedLog, DeliveryJob, Delivery
from amc.webhook import on_delivery_job_fulfilled
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot


class JobsCog(commands.Cog):
    def __init__(self, bot: "AMCDiscordBot"):
        self.bot = bot
        self.channel_id = settings.DISCORD_JOBS_CHANNEL_ID
        self.deliveries_channel_id = settings.DISCORD_DELIVERIES_CHANNEL_ID
        self.message_id = None

    async def cog_load(self):
        self.update_loop.start()

    async def cog_unload(self):
        self.update_loop.cancel()

    def _build_job_embed(self, job, stale=False) -> discord.Embed:
        """Builds a Discord Embed for a single DeliveryJob object."""

        # --- Create the title and description (value part of the field) ---
        description = ""
        if job.cargo_key:
            cargo_key = job.get_cargo_key_display()
        else:
            cargo_key = ", ".join([cargo.label for cargo in job.cargos.all()])

        description += f"\n**Cargo:**: {cargo_key}"

        if job.completion_bonus:
            description += "\n**Completion Reward**: "
            description += f"{job.completion_bonus:,}"

        if not stale:
            description += f"\n**Expires in**: <t:{int(job.expired_at.timestamp())}:R>"

        source_points = list(job.source_points.all())
        if source_points:
            description += "\n**ONLY from**: "
            description += ", ".join([point.name for point in source_points])

        destination_points = list(job.destination_points.all())
        if destination_points:
            description += "\n**ONLY to**: "
            description += ", ".join([point.name for point in destination_points])

        if job.description:
            description += f"\n**Description**: {job.description}"

        if deliveries := list(job.deliveries.all()):
            contributors = {}
            for delivery in deliveries:
                if delivery.character:
                    name = delivery.character.name
                    contributors[name] = contributors.get(name, 0) + delivery.quantity

            if contributors:
                description += "\n\n**Contributors**:"
                sorted_contributors = sorted(
                    contributors.items(), key=lambda item: item[1], reverse=True
                )

                for name, quantity in sorted_contributors:
                    bonus = int(
                        job.completion_bonus * quantity / job.quantity_requested
                    )
                    description += (
                        f"\n**{name}**: {quantity} ({bonus:,} bonus upon completion)"
                    )

        color = discord.Color.blue()
        if stale:
            if job.quantity_fulfilled == job.quantity_requested:
                color = discord.Color.green()
            else:
                color = discord.Color.red()

        # --- Assemble the embed ---
        embed = discord.Embed(
            title=f"{job.name} ({job.quantity_fulfilled}/{job.quantity_requested})",
            description=description.strip(),
            color=color,
            timestamp=job.requested_at,  # Use job creation time for consistency
        )

        # --- Add the prominent RP Mode field if applicable ---
        if job.rp_mode:
            embed.add_field(
                name="🚨 Requirement 🚨",
                value="**This job requires RP mode to be enabled for bonuses.**",
                inline=False,  # Ensures the field spans the full width of the embed
            )

        embed.set_footer(text=f"Job ID: {job.id}")
        return embed

    async def get_channel_messageable(
        self, channel_id: int
    ) -> Optional[discord.abc.Messageable]:
        channel = self.bot.get_channel(channel_id)
        if isinstance(channel, discord.abc.Messageable):
            return channel
        return None

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
        job=None,
        delivery_id=None,
    ) -> discord.Embed:
        description = ""
        description += "\n**Payment**: "
        description += f"{payment + subsidy:,}"
        if subsidy:
            description += f" ({payment:,} + Subsidy {subsidy:,})"

        if source_name:
            description += "\n**From**: "
            description += source_name

        if destination_name:
            description += "\n**To**: "
            description += destination_name

        if vehicle_key:
            from amc.enums import VehicleKey

            try:
                vehicle_label = VehicleKey(vehicle_key).label
            except ValueError:
                vehicle_label = vehicle_key
            description += "\n**Vehicle**: "
            description += vehicle_label

        # --- Assemble the embed ---
        embed = discord.Embed(
            title=f"{character_name} delivered {quantity} {cargo_key}",
            description=description.strip(),
            color=discord.Color.green(),
            timestamp=timezone.now(),
        )
        footer_parts = []
        if delivery_id:
            footer_parts.append(f"Delivery #{delivery_id}")
        if job:
            footer_parts.append(f"Job: {job.name} (#{job.id})")
        if footer_parts:
            embed.set_footer(text=" · ".join(footer_parts))

        return embed

    async def post_delivery_embed(self, *args, **kwargs):
        deliveries_channel = await self.get_channel_messageable(
            self.deliveries_channel_id
        )
        if not deliveries_channel:
            print(
                f"Error: Could not find deliveries channel with ID {self.deliveries_channel_id}"
            )
            return
        await deliveries_channel.send(embed=self._build_delivery_embed(*args, **kwargs))

    async def update_jobs(self):
        """
        Synchronizes Discord messages with the jobs in the database.
        Handles creating, updating, and deleting job messages.
        """
        channel = await self.get_channel_messageable(self.channel_id)
        if not channel:
            print(
                f"Error: Could not find messageable channel with ID {self.channel_id}"
            )
            return

        active_jobs = (
            DeliveryJob.objects.prefetch_related(
                "source_points",
                "destination_points",
                "cargos",
            )
            .prefetch_related(
                Prefetch(
                    "deliveries", queryset=Delivery.objects.select_related("character")
                )
            )
            .filter_active()
        )

        active_job_ids = set()

        async for job in active_jobs:
            active_job_ids.add(job.id)
            embed = self._build_job_embed(job)
            print("Embed CREATED")

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
                try:
                    new_message = await channel.send(embed=embed)
                    job.discord_message_id = new_message.id
                    await job.asave(update_fields=["discord_message_id"])
                except Exception as e:
                    print(f"Error creating message for job {job.id}: {e}")
            print("Embed CREATED done")

        # --- 2. CLEAN UP STALE MESSAGES (DELETE) ---
        # Find jobs that have a message ID but are no longer active
        stale_jobs = (
            DeliveryJob.objects.filter(discord_message_id__isnull=False)
            .exclude(id__in=active_job_ids)
            .prefetch_related(
                "source_points",
                "destination_points",
                "cargos",
            )
            .prefetch_related(
                Prefetch(
                    "deliveries", queryset=Delivery.objects.select_related("character")
                )
            )
        )

        async for job in stale_jobs:
            embed = self._build_job_embed(job, stale=True)
            try:
                if not channel:
                    continue
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
                await job.asave(update_fields=["discord_message_id"])

        return active_job_ids, stale_jobs

    @tasks.loop(minutes=1)  # Reduced loop time for better responsiveness
    async def update_loop(self):
        """Periodically runs the synchronization logic."""
        try:
            print("Running job synchronization...")
            await self.update_jobs()
            print("Job synchronization finished.")
        except Exception as e:
            print(f"Error in job synchronization loop: {e}")

    @update_loop.before_loop
    async def before_update_loop(self):
        """Wait until the bot is ready before starting the loop."""
        await self.bot.wait_until_ready()

    async def cargo_autocomplete(self, interaction, current):
        unique_cargo_keys = ServerCargoArrivedLog.objects.values_list(
            "cargo_key", flat=True
        ).distinct()
        return [
            app_commands.Choice(name=f"{cargo_key}", value=cargo_key)
            async for cargo_key in unique_cargo_keys
            if current.lower() in cargo_key.lower()
        ][:25]  # Discord max choices: 25

    @app_commands.command(name="sync_jobs_channel", description="Sync the jobs channel")
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    async def sync_jobs_channel(self, interaction):
        await interaction.response.defer()
        await self.update_jobs()
        await interaction.followup.send("Synced", ephemeral=True)

    @app_commands.command(
        name="update_jobs_embeds", description="Manually update jobs embeds"
    )
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    async def update_jobs_embeds(self, interaction):
        active_job_ids = await self.update_jobs()
        await interaction.response.send_message(
            f"Updated {str(active_job_ids)}", ephemeral=True
        )

    @app_commands.command(name="finish_job", description="Manually finish a job")
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    async def finish_job(self, interaction, job_id: int):
        job = await DeliveryJob.objects.aget(pk=job_id)
        await on_delivery_job_fulfilled(job, self.bot.http_client_game)
        await interaction.response.send_message(
            f"Finished {str(job_id)}", ephemeral=True
        )

    @app_commands.command(
        name="job_config", description="View current job posting configuration"
    )
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    async def job_config(self, interaction):
        from amc.models import JobPostingConfig

        config = await JobPostingConfig.aget_config()
        embed = discord.Embed(
            title="⚙️ Job Posting Configuration",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Target Success Rate",
            value=f"{config.target_success_rate:.0%}",
            inline=True,
        )
        embed.add_field(
            name="Min Multiplier",
            value=str(config.min_multiplier),
            inline=True,
        )
        embed.add_field(
            name="Max Multiplier",
            value=str(config.max_multiplier),
            inline=True,
        )
        embed.add_field(
            name="Players per Job",
            value=str(config.players_per_job),
            inline=True,
        )
        embed.add_field(
            name="Min Base Jobs",
            value=str(config.min_base_jobs),
            inline=True,
        )
        embed.add_field(
            name="Posting Rate Multiplier",
            value=f"{config.posting_rate_multiplier}x",
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="set_job_config",
        description="Update a job posting configuration parameter",
    )
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    @app_commands.describe(
        param="Configuration parameter to update",
        value="New value for the parameter",
    )
    @app_commands.choices(
        param=[
            app_commands.Choice(
                name="Target Success Rate – completion % to aim for (0.0-1.0)",
                value="target_success_rate",
            ),
            app_commands.Choice(
                name="Min Multiplier – lowest scaling when jobs expire too often",
                value="min_multiplier",
            ),
            app_commands.Choice(
                name="Max Multiplier – highest scaling when jobs are completed fast",
                value="max_multiplier",
            ),
            app_commands.Choice(
                name="Players per Job – base ratio, 1 job slot per N players",
                value="players_per_job",
            ),
            app_commands.Choice(
                name="Min Base Jobs – minimum job slots regardless of player count",
                value="min_base_jobs",
            ),
            app_commands.Choice(
                name="Posting Rate – global chance multiplier (0.5=half, 2.0=double)",
                value="posting_rate_multiplier",
            ),
        ]
    )
    async def set_job_config(
        self,
        interaction: discord.Interaction,
        param: app_commands.Choice[str],
        value: float,
    ):
        from amc.models import JobPostingConfig

        config = await JobPostingConfig.aget_config()
        field_name = param.value

        # Validation
        validations = {
            "target_success_rate": (0.0, 1.0),
            "min_multiplier": (0.1, 10.0),
            "max_multiplier": (0.1, 10.0),
            "players_per_job": (1, 100),
            "min_base_jobs": (0, 50),
            "posting_rate_multiplier": (0.0, 10.0),
        }

        min_val, max_val = validations[field_name]
        if not (min_val <= value <= max_val):
            await interaction.response.send_message(
                f"❌ Value must be between {min_val} and {max_val}.",
                ephemeral=True,
            )
            return

        old_value = getattr(config, field_name)

        # Integer fields
        if field_name in ("players_per_job", "min_base_jobs"):
            value = int(value)

        setattr(config, field_name, value)
        await config.asave()

        await interaction.response.send_message(
            f"✅ **{param.name}** updated: `{old_value}` → `{value}`",
            ephemeral=True,
        )

    async def template_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        from amc.models import DeliveryJobTemplate

        templates = DeliveryJobTemplate.objects.filter(
            enabled=True, rp_mode=False, name__icontains=current
        ).order_by("name")[:25]
        return [
            app_commands.Choice(name=t.name[:100], value=t.pk) async for t in templates
        ]

    @app_commands.command(
        name="post_job",
        description="Force-post a job from a template (bypasses probability/rate limits)",
    )
    @app_commands.checks.has_any_role(settings.DISCORD_ADMIN_ROLE_ID)
    @app_commands.describe(template="Template to create a job from")
    @app_commands.autocomplete(template=template_autocomplete)
    async def post_job(self, interaction: discord.Interaction, template: int):
        import random
        from datetime import timedelta
        from amc.models import DeliveryJobTemplate, MinistryTerm, JobPostingConfig
        from amc.game_server import announce
        from amc.jobs import calculate_treasury_multiplier
        from amc_finance.services import (
            get_treasury_fund_balance,
            escrow_ministry_funds,
        )

        await interaction.response.defer(ephemeral=True)

        try:
            tmpl = await DeliveryJobTemplate.objects.prefetch_related(
                "cargos", "source_points", "destination_points"
            ).aget(pk=template)
        except DeliveryJobTemplate.DoesNotExist:
            await interaction.followup.send("❌ Template not found.", ephemeral=True)
            return

        # Treasury multiplier for bonus/duration scaling
        config = await JobPostingConfig.aget_config()
        treasury_balance = await get_treasury_fund_balance()
        treasury_mult = calculate_treasury_multiplier(
            float(treasury_balance),
            equilibrium=float(config.treasury_equilibrium),
            sensitivity=float(config.treasury_sensitivity),
            cap_ratio=float(config.treasury_cap_ratio),
        )

        quantity_requested = tmpl.default_quantity
        base_bonus = tmpl.completion_bonus
        scaling_factor = max(treasury_mult * 0.5, min(2.0, treasury_mult * random.uniform(0.7, 1.3)))
        completion_bonus = int(base_bonus * scaling_factor)
        duration_hours = tmpl.duration_hours

        active_term = await MinistryTerm.objects.filter(is_active=True).afirst()

        new_job = await DeliveryJob.objects.acreate(
            name=tmpl.name,
            quantity_requested=quantity_requested,
            expired_at=timezone.now() + timedelta(hours=duration_hours),
            bonus_multiplier=0,
            completion_bonus=completion_bonus,
            description=tmpl.description,
            rp_mode=False,
            created_from=tmpl,
            funding_term=active_term,
        )

        if active_term:
            if await escrow_ministry_funds(completion_bonus, new_job):
                new_job.escrowed_amount = completion_bonus
                await new_job.asave()

        cargos = [c async for c in tmpl.cargos.all()]
        source_points = [sp async for sp in tmpl.source_points.all()]
        destination_points = [dp async for dp in tmpl.destination_points.all()]
        await new_job.cargos.aadd(*cargos)
        await new_job.source_points.aadd(*source_points)
        await new_job.destination_points.aadd(*destination_points)

        try:
            await announce(
                f"New job posting! {tmpl.name} - {completion_bonus:,} bonus on completion. See /jobs for more details",
                self.bot.http_client_game,
            )
        except Exception:
            pass  # Don't fail the command if announce fails

        await interaction.followup.send(
            f"✅ Posted **{tmpl.name}** (Job #{new_job.id})\n"
            f"Bonus: {completion_bonus:,} · Duration: {duration_hours:.1f}h · Qty: {quantity_requested}",
            ephemeral=True,
        )
