import discord
from discord.ext import commands
from discord import app_commands, ui
from django.conf import settings

from amc.models import Player, PlayerShift, RescueRequest
from amc.game_server import announce

COMMON_TIMEZONES = [
    "Asia/Jakarta", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney",
    "Europe/London", "Europe/Berlin", "Europe/Moscow", "Asia/Kolkata",
    "US/Pacific", "US/Mountain", "US/Central", "US/Eastern",
]

# --- The Modal (Pop-up Form) ---
# This modal now receives the timezone when it's created.
class ShiftTimeModal(ui.Modal, title='Enter Shift Hours'):
    def __init__(self, timezone: str):
        super().__init__()
        self.selected_timezone = timezone

    # --- Form Fields ---
    start_hour = ui.TextInput(
        label='Shift Start Hour (0-23)',
        placeholder='e.g., 22 for 10:00 PM',
        required=True,
        min_length=1,
        max_length=2,
    )

    end_hour = ui.TextInput(
        label='Shift End Hour (0-23)',
        placeholder='e.g., 6 for 6:00 AM',
        required=True,
        min_length=1,
        max_length=2,
    )

    # --- Logic for when the form is submitted ---
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            start = int(self.start_hour.value)
            end = int(self.end_hour.value)

            if not (0 <= start <= 23 and 0 <= end <= 23):
                await interaction.followup.send("❌ **Error:** Hours must be between 0 and 23.", ephemeral=True)
                return
        except ValueError:
            await interaction.followup.send("❌ **Error:** Please enter valid numbers for the start and end hours.", ephemeral=True)
            return

        rescuer_role = discord.utils.get(interaction.guild.roles, name="Rescuer")
        if not rescuer_role:
            await interaction.followup.send("❌ **Configuration Error:** A role named 'Rescuer' could not be found.", ephemeral=True)
            return
        
        try:
            await interaction.user.add_roles(rescuer_role)
        except discord.Forbidden:
            await interaction.followup.send("❌ **Permissions Error:** I don't have permission to assign roles.", ephemeral=True)
            return

        # 3. Save the data
        try:
          player = await Player.objects.aget(discord_user_id=interaction.user.id)
        except Player.DoesNotExists:
          await interaction.followup.send(
              "You are not verified\n"
              "Please first use the `/verify` command",
              ephemeral=True
          )
          return
        await PlayerShift.objects.aupdate_or_create(
          player=player,
          defaults={
            'start_time_utc': f"{start:02d}:00",
            'end_time_utc': f"{end:02d}:00",
            "user_timezone": self.selected_timezone
          }
        )

        # 4. Confirm to the user
        await interaction.followup.send(
            f"✅ **Success!** Your shift has been registered from **{start}:00 to {end}:00 ({self.selected_timezone})**.\n"
            f"The '{rescuer_role.name}' role has been assigned to you.",
            ephemeral=True
        )

# --- The View containing the Timezone Dropdown ---
class TimezoneSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=300) # View times out after 5 minutes

    @ui.select(
        placeholder="Choose your timezone...",
        options=[discord.SelectOption(label=tz, description=f"Select if you are in the {tz} timezone.") for tz in COMMON_TIMEZONES]
    )
    async def select_callback(self, interaction: discord.Interaction, select: ui.Select):
        # The selected timezone is in select.values[0]
        selected_tz = select.values[0]
        # Now, show the modal for entering the time
        await interaction.response.send_modal(ShiftTimeModal(timezone=selected_tz))

# --- The View containing the persistent button ---
class PersistentShiftView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label='Sign Up for a Rescue Shift', style=discord.ButtonStyle.success, custom_id='persistent_shift_button')
    async def shift_button(self, interaction: discord.Interaction, button: ui.Button):
        # When the button is clicked, send the ephemeral view with the timezone dropdown
        await interaction.response.send_message(
            content="First, please select your timezone from the list below.",
            view=TimezoneSelectView(),
            ephemeral=True
        )

class RoleplayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Register the persistent view so it works after bot restarts
        self.bot.add_view(PersistentShiftView())

    @app_commands.command(name="create_shift_panel", description="Creates the panel for rescue shift signups.")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_shift_panel(self, interaction: discord.Interaction):
        """Admins can use this command to post the signup button."""
        embed = discord.Embed(
            title="📢 Rescue Team Shift Signups",
            description="Ready to help out? Click the button below to register your availability for rescue missions. \n\nYou will be notified if a rescue is required during your shift.",
            color=discord.Color.blue()
        )
        embed.set_footer(text="Your availability makes all the difference!")

        await interaction.response.send_message(embed=embed, view=PersistentShiftView())

    @create_shift_panel.error
    async def on_create_shift_panel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ You must be an administrator to use this command.", ephemeral=True)
        else:
            raise error

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
      channel = reaction.message.channel
      print('RESCUE')
      if channel and \
        channel.id == settings.DISCORD_RESCUE_CHANNEL_ID and not user.bot:
          await self.on_rescue_response(reaction.message, user)

    async def add_reaction_to_rescue_message(self, message_id, emoji):
      try:
        channel = self.bot.get_channel(settings.DISCORD_RESCUE_CHANNEL_ID)
        if channel is None:
          print(f"Error: Could not find channel with ID {settings.DISCORD_RESCUE_CHANNEL_ID}.")
          return

        # 2. Fetch the Message object from the channel (requires an API call)
        # fetch_message is an asynchronous operation, so we must await it.
        message = await channel.fetch_message(message_id)
        
        # 3. Add the reaction
        await message.add_reaction(emoji)
        print(f"Successfully added reaction '{emoji}' to message {message_id}.")

      except discord.NotFound:
        print(f"Error: Message with ID {message_id} not found in channel {settings.DISCORD_RESCUE_CHANNEL_ID}.")
      except discord.HTTPException as e:
        # Handles errors like Invalid Unicode, Forbidden (bot can't react), etc.
        print(f"Error adding reaction: {e}")
      except Exception as e:
        print(f"An unexpected error occurred: {e}")

    async def on_rescue_response(self, message, user):
      try:
        rescue_request = await RescueRequest.objects.select_related('character').aget(discord_message_id=message.id)
      except RescueRequest.DoesNotExists:
        print(f"Reaction to a rescue request without associated discord_message_id: {message.id}")
        return

      await announce(f"{user.display_name} responded to {rescue_request.character.name}'s rescue request!", self.bot.http_client_game)
      try:
        player = await Player.objects.aget(discord_user_id=user.id)
      except Player.DoesNotExists:
        print(f"User {user.id} has not verified yet responded to a rescue request")
        return

      await rescue_request.responders.aadd(player)

