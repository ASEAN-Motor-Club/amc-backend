from django.db import models
from datetime import timedelta
from django.utils import timezone
from django.db.models import Sum, OuterRef, Subquery, Value, Q
from django.db.models.functions import Coalesce
import discord
from discord import app_commands
from discord.ext import commands
from django.conf import settings
from amc.models import Character, Player, ServerCargoArrivedLog, ServerSignContractLog
from amc_finance.services import send_fund_to_player


class EconomyCog(commands.Cog):
  def __init__(self, bot, general_channel_id=settings.DISCORD_GENERAL_CHANNEL_ID):
    self.bot = bot
    self.general_channel_id = general_channel_id

  @app_commands.command(name='calculate_gdp', description='Calculate the GDP figure')
  async def calculate_gdp(self, interaction, days: int = 1):
    await interaction.response.defer()
    start_time = timezone.now() - timedelta(days=days)
    deliveries_qs = ServerCargoArrivedLog.objects.filter(timestamp__gte=start_time)
    deliveries_aggregates = await deliveries_qs.aaggregate(total_payments=Sum('payment'))

    contracts_qs = ServerSignContractLog.objects.filter(timestamp__gte=start_time)
    contracts_aggregates = await contracts_qs.aaggregate(total_payments=Sum('payment'))

    total_gdp = deliveries_aggregates['total_payments'] + contracts_aggregates['total_payments']

    delivery_sum_subquery = ServerCargoArrivedLog.objects.filter(
      player=OuterRef('pk'),
      timestamp__gte=start_time,
    ).values(
      'player'
    ).annotate(
      total=Sum('payment')
    ).values('total')
    contracts_sum_subquery = ServerSignContractLog.objects.filter(
      player=OuterRef('pk'),
      timestamp__gte=start_time,
    ).values(
      'player'
    ).annotate(
      total=Sum('payment')
    ).values('total')

    top_players_qs = Player.objects.annotate(
      gdp_contribution=Coalesce(
        Subquery(delivery_sum_subquery, output_field=models.IntegerField()),
        Value(0),
      ) + Coalesce(
        Subquery(contracts_sum_subquery, output_field=models.IntegerField()),
        Value(0),
      )
    ).filter(gdp_contribution__gt=0).order_by('-gdp_contribution')[:20]

    async def get_player_name(player):
      if player.discord_user_id:
        try:
          user = await interaction.guild.fetch_member(player.discord_user_id)
          return user.display_name
        except discord.NotFound:
          pass
      try:
        latest_character = await (Character.objects
          .with_last_login()
          .filter(player=player, last_login__isnull=False)
          .alatest('last_login')
        )
      except Character.DoesNotExist:
        return player.unique_id
      except Exception:
        return f"Character not found ({player.unique_id})"
      return latest_character.name or latest_character.id

    top_players_str = '\n'.join([
      f"**{await get_player_name(player)}:** {player.gdp_contribution:,}"
      async for player in top_players_qs
    ])
    await interaction.followup.send(f"""
# Total GDP: {total_gdp:,}

Deliveries: {deliveries_aggregates['total_payments']:,}
Contracts: {contracts_aggregates['total_payments']:,}

## Top GDP Contributors
{top_players_str}
    """)

  @app_commands.command(name='government_funding', description='Send government funding to player')
  @app_commands.checks.has_permissions(administrator=True)
  async def government_funding(self, interaction, discord_user_id: str, character_name: str, amount: int, reason: str):
    await interaction.response.defer()
    try:
      character = await Character.objects.aget(
        Q(player__discord_user_id=int(discord_user_id)) | Q(player__unique_id=int(discord_user_id)),
        name=character_name,
      )
      await send_fund_to_player(amount, character, reason)
      await interaction.followup.send(f"Government funding deposited into {character_name}'s bank account.\nAmount: {amount:,}\nReason: {reason}")
    except Exception as e:
      await interaction.followup.send(f"Failed to send government funding: {e}")

