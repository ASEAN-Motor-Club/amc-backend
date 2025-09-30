from io import BytesIO
from decimal import Decimal
from django.utils import timezone
from django.db import models
from django.db.models import Sum, OuterRef, Subquery, Value, Q, F, DecimalField, Case, When
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
  ServerTowRequestArrivedLog,
)
from amc.utils import get_timespan
from amc_finance.services import send_fund_to_player
from amc_finance.models import Account, LedgerEntry
from amc_finance.services import (
  get_player_bank_balance,
  get_player_loan_balance,
  get_character_max_loan,
  make_treasury_bank_deposit,
)
from amc.subsidies import DEFAULT_SAVING_RATE
from amc.save_file import decrypt


DONATION_EXPECTATION_BRACKETS = [
  {'threshold': 3_000_000, 'rate': Decimal('0.00')},
  {'threshold': 10_000_000, 'rate': Decimal('0.10')},
  {'threshold': 50_000_000, 'rate': Decimal('0.20')},
  {'threshold': None, 'rate': Decimal('0.40')},  # 'None' represents the highest bracket (100M+)
]

def get_progressive_donation_case(brackets_config):
  """
  Dynamically builds a Django ORM Case expression for progressive calculations
  based on a configuration list.
  """
  when_clauses = []
  cumulative_tax = Decimal(0)
  lower_bound = Decimal(0)
  default_expression = Value(Decimal(0)) # Default to 0 if config is empty

  for bracket in brackets_config:
    threshold = bracket.get('threshold')
    rate = bracket.get('rate')

    # The expression for calculating tax for earnings within this specific bracket
    then_expression = Value(cumulative_tax) + (F('total_earnings') - Value(lower_bound)) * Value(rate)

    if threshold is None:
      # This is the final, highest bracket, which becomes the default case
      default_expression = then_expression
      break

    when_clauses.append(
      When(total_earnings__lt=threshold, then=then_expression)
    )

    # Update cumulative values for the next iteration's calculation
    tax_in_this_bracket = (Decimal(str(threshold)) - lower_bound) * rate
    cumulative_tax += tax_in_this_bracket
    lower_bound = Decimal(str(threshold))
  
  return Case(*when_clauses, default=default_expression, output_field=DecimalField())


class EconomyCog(commands.Cog):
  def __init__(self, bot, general_channel_id=settings.DISCORD_GENERAL_CHANNEL_ID):
    self.bot = bot
    self.general_channel_id = general_channel_id
    self.decrypt_save_file_channel_id = settings.DISCORD_DECRYPT_SAVE_FILE_CHANNEL_ID

  @app_commands.command(name='calculate_gdp', description='Calculate the GDP figure')
  async def calculate_gdp(self, interaction, num_days: int = 1):
    await interaction.response.defer()
    start_time, end_time = get_timespan(num_days, num_days)

    subsidies_agg = await (LedgerEntry.objects.filter_subsidies()
      .filter(journal_entry__created_at__gte=start_time, journal_entry__created_at__lte=end_time)
      .aaggregate(total_subsidies=Sum('debit', default=0))
    )
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

    tow_requests_qs = ServerTowRequestArrivedLog.objects.filter(
      timestamp__gte=start_time,
      timestamp__lt=end_time
    )
    tow_requests_aggregates = await tow_requests_qs.aaggregate(total_payments=Sum('payment', default=0))

    total_gdp = subsidies_agg['total_subsidies'] + deliveries_aggregates['total_payments'] + contracts_aggregates['total_payments'] + passengers_aggregates['total_payments'] + tow_requests_aggregates['total_payments']

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
    tow_requests_sum_subquery = ServerTowRequestArrivedLog.objects.filter(
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
      ) + Coalesce(
        Subquery(tow_requests_sum_subquery, output_field=models.IntegerField()),
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
-# {start_time} - {end_time}

Subsidies: {subsidies_agg['total_subsidies']:,}
Deliveries: {deliveries_aggregates['total_payments']:,}
Contracts: {contracts_aggregates['total_payments']:,}
Passengers (Taxi/Ambulance): {passengers_aggregates['total_payments']:,}
Tow Requests: {tow_requests_aggregates['total_payments']:,}

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

  @app_commands.command(name='donors', description='List the top donors')
  async def donors(self, interaction):
    await interaction.response.defer()
    contributors = (LedgerEntry.objects.filter_donations()
      .select_related('journal_entry', 'journal_entry__creator')
      .values('journal_entry__creator')
      .annotate(total_contribution=Sum('credit'), name=F('journal_entry__creator__name'))
      .order_by('-total_contribution')
    )[:100]
    
    # Build the contributors string, with a fallback message if empty
    contributors_list = [
      f"**{contribution['name']}:** {contribution['total_contribution']:,}"
      async for contribution in contributors
    ]
    contributors_str = '\n'.join(contributors_list) if contributors_list else "No donations recorded yet."
    # Create the embed
    embed = discord.Embed(
      title="❤️ Top Donors",
      description=contributors_str,
      color=discord.Color.gold(),
      timestamp=timezone.now()
    )
    embed.set_footer(text=f"Requested by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed)


  @app_commands.command(name='treasury_stats', description='Display treasury and donations info')
  async def treasury_stats(self, interaction):
    await interaction.response.defer()
    today = timezone.now().date()
    treasury_fund, _ = await Account.objects.aget_or_create(
      account_type=Account.AccountType.ASSET,
      book=Account.Book.GOVERNMENT,
      character=None,
      name='Treasury Fund',
    )
    treasury_fund_in_bank, _ = await Account.objects.aget_or_create(
      account_type=Account.AccountType.ASSET,
      book=Account.Book.GOVERNMENT,
      character=None,
      name='Treasury Fund (in Bank)',
    )
    bank_assets_aggregate = await Account.objects.filter(
      account_type=Account.AccountType.ASSET,
      book=Account.Book.BANK,
    ).aaggregate(
      total_assets=Sum('balance', default=0),
      total_loans=Sum('balance', default=0, filter=Q(character__isnull=False)),
      total_vault=Sum('balance', default=0, filter=Q(character__isnull=True)),
    )

    subsidies_agg = await (LedgerEntry.objects.filter_subsidies()
      .filter(journal_entry__date=today)
      .aaggregate(total_subsidies=Sum('debit', default=0))
    )
    contributors = (LedgerEntry.objects.filter_donations()
      .select_related('journal_entry', 'journal_entry__creator')
      .values('journal_entry__creator')
      .annotate(total_contribution=Sum('credit'), name=F('journal_entry__creator__name'))
      .order_by('-total_contribution')
    )[:20]
    
    # Build the contributors string, with a fallback message if empty
    contributors_list = [
      f"**{contribution['name']}:** {contribution['total_contribution']:,}"
      async for contribution in contributors
    ]
    contributors_str = '\n'.join(contributors_list) if contributors_list else "No donations recorded yet."

    # Create the embed
    embed = discord.Embed(
      title="📈 Treasury Report",
      description=f"Status as of {today.strftime('%A, %-d %B %Y')}",
      color=discord.Color.gold(),
      timestamp=timezone.now()
    )

    # Add Treasury field
    treasury_value = (
      f"**Vault Balance:** `{treasury_fund.balance:,}`\n"
      f"**Bank Deposit:** `{treasury_fund_in_bank.balance:,}`"
    )
    embed.add_field(name="💰 Government Treasury", value=treasury_value, inline=False)
    
    # Add Bank of ASEAN field
    bank_value = (
      f"**Total Assets:** `{bank_assets_aggregate['total_assets']:,}`\n"
      f"**Outstanding Loans:** `{bank_assets_aggregate['total_loans']:,}`\n"
      f"**Vault Cash:** `{bank_assets_aggregate['total_vault']:,}`"
    )
    embed.add_field(name="🏦 Bank of ASEAN", value=bank_value, inline=False)
    
    # Add Subsidies field
    subsidies_value = f"**Today's Disbursements:** `{subsidies_agg['total_subsidies']:,}`"
    embed.add_field(name="💸 Subsidies", value=subsidies_value, inline=False)
    
    # Add Top Donors field
    embed.add_field(name="❤️ Top Donors", value=contributors_str, inline=False)

    embed.set_footer(text=f"Requested by {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)

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
Use `/set_saving_rate` in the game to set how much you want to save. It's 0 by default.
Once you withdraw your balance, you will not be able to deposit them back in.

### How ASEAN Loan Works
Our loans have a flat one-off 10% fee, and you only have to repay them when you make a profit.
The repayment will range from 10% to 40% of your income, depending on the amount of loan you took.
""", ephemeral=True)

  @app_commands.command(name='treasury_liquidity_injection', description='Injects liquidity into the bank from treasury')
  @app_commands.checks.has_permissions(administrator=True)
  async def treasury_bank_deposit(self, interaction, amount: int, description: str):
    now = timezone.now()
    await make_treasury_bank_deposit(amount, description)
    await interaction.response.send_message(f"""\
# GOVERNMENT TREASURY: OFFICIAL TRANSACTION RECORD

**Date & Time:** {now.strftime("%d %B %Y, %I:%M %p")}
**Action Type:** {description}

### Transaction Details
- Originating Entity: Office of the Treasury
- Receiving Entity: aseanbank
- Transaction Method: Treasury Direct Deposit
- Amount: {amount:,}

### Purpose & Authorization
This transaction was authorized under Treasury Mandate 2.1 (Financial Mechanism and Liquidity Maintanance).
The purpose of this transfer is to ensure sufficient liquidity within the server's regulated financial system, promoting stability and confidence.
""")

  @app_commands.command(name='taxpayers', description='List top/bottom players by donation-to-earnings ratio.')
  async def taxpayers(self, interaction, num_days: int = 30):
    await interaction.response.defer()
    start_time, end_time = get_timespan(num_days, num_days)

    # Subqueries for each earning type
    delivery_sum_subquery = ServerCargoArrivedLog.objects.filter(
        player=OuterRef('pk'), timestamp__gte=start_time, timestamp__lt=end_time
    ).values('player').annotate(total=Sum('payment')).values('total')

    contracts_sum_subquery = ServerSignContractLog.objects.filter(
        player=OuterRef('pk'), timestamp__gte=start_time, timestamp__lt=end_time
    ).values('player').annotate(total=Sum('payment')).values('total')

    passengers_sum_subquery = ServerPassengerArrivedLog.objects.filter(
        player=OuterRef('pk'), timestamp__gte=start_time, timestamp__lt=end_time
    ).values('player').annotate(total=Sum('payment')).values('total')

    tow_requests_sum_subquery = ServerTowRequestArrivedLog.objects.filter(
        player=OuterRef('pk'), timestamp__gte=start_time, timestamp__lt=end_time
    ).values('player').annotate(total=Sum('payment')).values('total')
    
    # Subquery for donations
    donations_sum_subquery = LedgerEntry.objects.filter(
      journal_entry__creator__player=OuterRef('pk'),
      journal_entry__created_at__gte=start_time,
      journal_entry__created_at__lte=end_time
    ).filter_donations().values(
      'journal_entry__creator__player'
    ).annotate(total=Sum('credit', default=0)).values('total')

    progressive_case = get_progressive_donation_case(DONATION_EXPECTATION_BRACKETS)

    # Main Query to get player stats
    player_stats_qs = Player.objects.annotate(
      total_earnings=Coalesce(
        Subquery(delivery_sum_subquery, output_field=DecimalField()), Value(Decimal(0))
      ) + Coalesce(
        Subquery(contracts_sum_subquery, output_field=DecimalField()), Value(Decimal(0))
      ) + Coalesce(
        Subquery(passengers_sum_subquery, output_field=DecimalField()), Value(Decimal(0))
      ) + Coalesce(
        Subquery(tow_requests_sum_subquery, output_field=DecimalField()), Value(Decimal(0))
      )
    ).annotate(
      total_donations=Coalesce(
        Subquery(donations_sum_subquery, output_field=DecimalField()), Value(Decimal(0))
      )
    ).filter(total_earnings__gt=0).annotate(
      expected_donation=progressive_case,
      contribution_delta=F('total_donations') - F('expected_donation')
    )

    top_10_qs = player_stats_qs.order_by('-contribution_delta')[:10]
    bottom_10_qs = player_stats_qs.order_by('contribution_delta')[:10]
    
    async def get_player_name(player):
      if player.discord_user_id:
        try:
          user = await interaction.guild.fetch_member(player.discord_user_id)
          return user.display_name
        except discord.NotFound:
          pass
      try:
        latest_character = await (Character.objects
          .with_last_login().filter(player=player, last_login__isnull=False).alatest('last_login'))
        return latest_character.name
      except Character.DoesNotExist:
        return player.unique_id
      except Exception:
        return f"Character not found ({player.unique_id})"
      
    async def format_player_list(qs):
      lines = []
      async for p in qs:
        name = await get_player_name(p)
        line = (f"**{name}**: {p.contribution_delta:+,} "
                f"(Donated: `{p.total_donations:,.0f}`, Expected: `{p.expected_donation:,.0f}`)")
        lines.append(line)
      return '\n'.join(lines) if lines else "Not enough data."

    top_10_str = await format_player_list(top_10_qs)
    bottom_10_str = await format_player_list(bottom_10_qs)

    embed = discord.Embed(
      title="📊 Taxpayer Leaderboard",
      description=f"Ranking based on donation amount vs. expectation over the last **{num_days}** days.",
      color=discord.Color.blue(),
      timestamp=timezone.now()
    )
    embed.add_field(name="✅ Top 10 Contributors (Above Expectation)", value=top_10_str, inline=False)
    embed.add_field(name="✴️ Bottom 10 Contributors (Below Expectation)", value=bottom_10_str, inline=False)
    embed.set_footer(text=f"Requested by {interaction.user.display_name}")
    
    await interaction.followup.send(embed=embed)

  @commands.Cog.listener()
  async def on_message(self, message):
    if message.channel.id == self.decrypt_save_file_channel_id:
      channel = message.channel
      attachments = message.attachments
      if not attachments:
        await channel.send_message('You need to attach a file')
      if len(attachments) > 1:
        await channel.send_message('1 save file at a time please')

      attachment = attachments[0]
      attachment_bytes = await attachment.read()
      decrypted_bytes = decrypt(attachment_bytes)
      
      await channel.send(
        file=discord.File(fp=BytesIO(decrypted_bytes), filename=f"{attachment.filename}.json"),
        reference=message,
      )




