from discord import app_commands
from discord.ext import commands
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

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
    await ctx.response.send_message(f"Send the following in the game chat:\n```/verify {value}```", ephemeral=True)


  @app_commands.command(name='login', description='Log in to the AMC Website')
  async def login(self, ctx):
    """
    Generates and sends a one-time login link to the user.
    """
    await self._generate_login_link(ctx, domain='https://www.aseanmotorclub.com', redirect_to='/')

  @app_commands.command(name='admin_login', description='Log in to the AMC Admin Panel')
  async def admin_login(self, ctx):
    """
    Generates and sends a one-time login link to the admin panel.
    """
    await self._generate_login_link(ctx, domain='https://api.aseanmotorclub.com', redirect_to='/admin/')

  async def _generate_login_link(self, ctx, domain: str, redirect_to: str = '/'):
    """
    Helper method to generate a one-time login link with a redirect.
    """
    try:
      player, player_created = await Player.objects.select_related('user').aget_or_create(
        discord_user_id=ctx.user.id,
        defaults={
          'unique_id': ctx.user.id,
        }
      )
    except Player.DoesNotExist:
      await ctx.response.send_message('You are not verified. Please first verify your account with /verify', ephemeral=True)
      return

    user = player.user
    if user is None:
      user = await User.objects.acreate(
        username=str(player.unique_id),
      )
      player.user = user
      await player.asave(update_fields=['user'])

    # Generate the token and user ID
    token = account_activation_token_generator.make_token(user)
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    
    login_url = reverse('token_login')
    full_login_url = f"{domain}{login_url}?uidb64={uidb64}&token={token}&next={redirect_to}"

    await ctx.response.send_message(f"Login link: <{full_login_url}>", ephemeral=True)
