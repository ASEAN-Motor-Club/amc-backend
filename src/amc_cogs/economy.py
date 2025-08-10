from django.db import models
from django.db.models import Sum, OuterRef, Subquery, Value, Q, F
from django.db.models.functions import Coalesce
import discord
from discord import app_commands
from discord.ext import commands
from django.conf import settings
from amc.models import (
  Character,
  Player,
  ServerCargoArrivedLog,
  ServerSignContractLog,
  ServerPassengerArrivedLog,
)
from amc.utils import get_timespan
from amc_finance.services import send_fund_to_player
from amc_finance.models import Account, LedgerEntry


class EconomyCog(commands.Cog):
  def __init__(self, bot, general_channel_id=settings.DISCORD_GENERAL_CHANNEL_ID):
    self.bot = bot
    self.general_channel_id = general_channel_id

  @app_commands.command(name='calculate_gdp', description='Calculate the GDP figure')
  async def calculate_gdp(self, interaction, num_days: int = 1, days_before:int=0):
    await interaction.response.defer()
    start_time, end_time = get_timespan(days_before, num_days)
    deliveries_qs = ServerCargoArrivedLog.objects.filter(
      timestamp__gte=start_time,
      timestamp__lt=end_time
    )
    deliveries_aggregates = await deliveries_qs.aaggregate(total_payments=Sum('payment'))

    contracts_qs = ServerSignContractLog.objects.filter(
      timestamp__gte=start_time,
      timestamp__lt=end_time
    )
    contracts_aggregates = await contracts_qs.aaggregate(total_payments=Sum('payment'))

    passengers_qs = ServerPassengerArrivedLog.objects.filter(
      timestamp__gte=start_time,
      timestamp__lt=end_time
    )
    passengers_aggregates = await passengers_qs.aaggregate(total_payments=Sum('payment'))

    total_gdp = deliveries_aggregates['total_payments'] + contracts_aggregates['total_payments'] + passengers_aggregates['total_payments']

    delivery_sum_subquery = ServerCargoArrivedLog.objects.filter(
      player=OuterRef('pk'),
      timestamp__gte=start_time,
      timestamp__lt=end_time
    ).values(
      'player'
    ).annotate(
      total=Sum('payment')
    ).values('total')
    contracts_sum_subquery = ServerSignContractLog.objects.filter(
      player=OuterRef('pk'),
      timestamp__gte=start_time,
      timestamp__lt=end_time
    ).values(
      'player'
    ).annotate(
      total=Sum('payment')
    ).values('total')
    passengers_sum_subquery = ServerPassengerArrivedLog.objects.filter(
      player=OuterRef('pk'),
      timestamp__gte=start_time,
      timestamp__lt=end_time
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
      ) + Coalesce(
        Subquery(passengers_sum_subquery, output_field=models.IntegerField()),
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
Passengers (Taxi/Bus/Ambulance): {passengers_aggregates['total_payments']:,}

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

  @app_commands.command(name='treasury_stats', description='Display treasury info')
  async def treasury_stats(self, interaction):
    treasury_fund, _ = await Account.objects.aget_or_create(
      account_type=Account.AccountType.ASSET,
      book=Account.Book.GOVERNMENT,
      character=None,
      defaults={
        'name': 'Treasury Fund',
      }
    )
    donations = LedgerEntry.objects.filter(
      account__account_type=Account.AccountType.REVENUE,
      account__book=Account.Book.GOVERNMENT,
      account__character=None,
      journal_entry__creator__isnull=False,
    ).select_related('journal_entry', 'journal_entry__creator')
    contributors = (donations.values('journal_entry__creator')
      .annotate(total_contribution=Sum('credit'), name=F('journal_entry__creator__name'))
      .order_by('-total_contribution')
    )
    contributors_str = '\n'.join([
      f"**{contribution['name']}:** {contribution['total_contribution']:,}"
      async for contribution in contributors
    ])
    await interaction.response.send_message(f"""# Treasury

**Balance:** {treasury_fund.balance:,}

## Top Contributors
{contributors_str}
""")

