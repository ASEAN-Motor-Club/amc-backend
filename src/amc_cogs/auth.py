from discord import app_commands
from discord.ext import commands

from django.core.signing import Signer
from django.contrib.auth import get_user_model
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.urls import reverse
from amc.tokens import account_activation_token_generator
from amc.models import Player

User = get_user_model()
# Make sure to import your token generator

class AuthenticationCog(commands.Cog):
  def __init__(self, bot):
    self.bot = bot

  @app_commands.command(name='verify', description='Verify that you own your in-game character')
  async def verify(self, ctx):
    signer = Signer()
    user_id = ctx.user.id
    value = signer.sign(str(user_id))
    await ctx.response.send_message(f"Send the following in the game chat: `/verify {value}`", ephemeral=True)


  @app_commands.command(name='login', description='Log in to the AMC Website')
  async def login(self, ctx):
    """
    Generates and sends a one-time login link to the user.
    """

    try:
      player = await Player.objects.aget(
        discord_user_id=ctx.user.id
      )
    except Player.DoesNotExist:
      await ctx.response.send_message('You are not verified. Please first verify your account with /verify', ephemeral=True)
      return

    user, created = await User.objects.aget_or_create(
      player=player,
      defaults={
        'username': str(player.unique_id),
      }
    )

    # Generate the token and user ID
    token = account_activation_token_generator.make_token(user)
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    
    login_url = reverse('token_login')  # Assumes your URL is named 'token_login'
    full_login_url = f"https://www.aseanmotorclub.com{login_url}?uidb64={uidb64}&token={token}"

    # For demonstration, we'll just print it. In a real app, you'd email this.
    await ctx.response.send_message(f"Login link: <{full_login_url}>", ephemeral=True)

