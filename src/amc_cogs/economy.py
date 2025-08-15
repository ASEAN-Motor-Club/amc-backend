from decimal import Decimal
from django.utils import timezone
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
from amc_finance.services import (
  get_player_bank_balance,
  get_player_loan_balance,
  get_character_max_loan,
)
from amc.subsidies import DEFAULT_SAVING_RATE


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
    deliveries_aggregates = await deliveries_qs.aaggregate(total_payments=Sum('payment', default=0))

    contracts_qs = ServerSignContractLog.objects.filter(
      timestamp__gte=start_time,
      timestamp__lt=end_time
    )
    contracts_aggregates = await contracts_qs.aaggregate(total_payments=Sum('payment', default=0))

    passengers_qs = ServerPassengerArrivedLog.objects.filter(
      timestamp__gte=start_time,
      timestamp__lt=end_time
    )
    passengers_aggregates = await passengers_qs.aaggregate(total_payments=Sum('payment', default=0))

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
Passengers (Taxi/Ambulance): {passengers_aggregates['total_payments']:,}

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
    today = timezone.now().date()
    treasury_fund, _ = await Account.objects.aget_or_create(
      account_type=Account.AccountType.ASSET,
      book=Account.Book.GOVERNMENT,
      character=None,
      defaults={
        'name': 'Treasury Fund',
      }
    )
    bank_assets_aggregate = await Account.objects.filter(
      account_type=Account.AccountType.ASSET,
      book=Account.Book.BANK,
    ).aaggregate(total_assets=Sum('balance', default=0))

    subsidies_agg = await (LedgerEntry.objects.filter_subsidies()
      .filter(journal_entry__date=today)
      .aaggregate(total_subsidies=Sum('debit', default=0))
    )
    contributors = (LedgerEntry.objects.filter_donations()
      .select_related('journal_entry', 'journal_entry__creator')
      .values('journal_entry__creator')
      .annotate(total_contribution=Sum('credit'), name=F('journal_entry__creator__name'))
      .order_by('-total_contribution')
    )
    contributors_str = '\n'.join([
      f"**{contribution['name']}:** {contribution['total_contribution']:,}"
      async for contribution in contributors
    ])
    await interaction.response.send_message(f"""\
# Treasury Report ({today.strftime('%A, %-d %B %Y')})

**Balance:** {treasury_fund.balance:,}

## Subsidies
**Total Subsidies Disbursed:** {subsidies_agg['total_subsidies']:,}

## Bank of ASEAN
**Total Assets**: {bank_assets_aggregate['total_assets']}

## Top Donors
{contributors_str}
""")

  @app_commands.command(name='bank_account', description='Display your bank account')
  async def bank_account(self, interaction):
    try:
      player = await Player.objects.aget(discord_user_id=interaction.user.id)
    except Player.DoesNotExist:
      await interaction.response.send_message('You first need to be verified. Use /verify', ephemeral=True)
      return
    character = await player.characters.with_last_login().filter(last_login__isnull=False).alatest('last_login')
    balance = await get_player_bank_balance(character)
    loan_balance = await get_player_loan_balance(character)
    max_loan = get_character_max_loan(character)
    saving_rate = character.saving_rate if character.saving_rate is not None else Decimal(DEFAULT_SAVING_RATE)
    await interaction.response.send_message(f"""\
# Your Bank ASEAN Account

**Owner:** {character.name}
**Balance:** `{balance:,}`
-# Daily Interest Rate: `2.2%` (offline), `4.4%` (online) 
**Loans:** `{loan_balance:,}`
**Max Available Loan:** `{max_loan:,}`
-# Max available loan depends on your driver level (currently {character.driver_level})
**Earnings Saving Rate:** `{saving_rate * Decimal(100):.0f}%`

### How to Put Money in the Bank
You can only fill your bank account by saving your earnings on this server.
Use the /set_saving_rate in the gameto set how much you want to save. It's 0 by default.
Once you withdraw your balance, you will not be able to deposit them back in.

### How ASEAN Loan Works
Our loans are interest free, and you only have to repay them when you make a profit.
The repayment will range from 10% to 40% of your income, depending on the amount of loan you took.
""", ephemeral=True)
