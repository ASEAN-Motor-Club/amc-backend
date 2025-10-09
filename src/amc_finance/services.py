from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from django.db import transaction
from django.db.models import F, Sum
from asgiref.sync import sync_to_async
from amc.models import Player, Delivery, CharacterLocation
from amc_finance.models import Account, JournalEntry, LedgerEntry


from enum import Enum
from typing import Optional, Tuple

class LoanLimitReason(Enum):
  """Enumeration for reasons a character's loan may be limited."""
  INELIGIBLE = "You are currently ineligible for a loan due to your social score."
  BANK_POLICY = "Your loan has reached the maximum amount set by bank policy."
  EARNINGS_HISTORY = "Your loan amount is limited by your recent earnings history."
  SOCIAL_SCORE = "Your loan amount has been reduced due to a low social score."

  def __str__(self):
      """Returns the human-readable message for the reason."""
      return self.value

async def get_character_max_loan(character) -> Tuple[int, Optional[LoanLimitReason]]:
  """
  Calculates the maximum loan amount for a character and identifies the reason if it's limited.

  Returns:
    A tuple containing:
    - int: The maximum loan amount.
    - Optional[LoanLimitReason]: The reason for the loan limit, or None if not limited.
  """
  SOCIAL_SCORE_LOAN_MODIFIER = 0.05
  BANK_POLICY_CAP = 6_000_000

  player = await Player.objects.aget(characters=character)
  deliveries_agg = await Delivery.objects.filter(character__player=player).aaggregate(
    total_payment=Sum('payment', default=0) + Sum('subsidy', default=0),
  )

  # --- Loan Calculation ---

  # 1. Start with the base loan from character levels
  base_loan = 10_000
  if character.driver_level:
    base_loan += character.driver_level * 3_000
  if character.truck_level:
    base_loan += character.truck_level * 3_000

  max_loan = float(base_loan)
  reason: Optional[LoanLimitReason] = None

  # 2. Apply social score modifier
  social_score_reduced_loan = False
  if player and hasattr(player, 'social_score'):
    loan_modifier = player.social_score * SOCIAL_SCORE_LOAN_MODIFIER
    max_loan *= (1 + loan_modifier)
    if player.social_score < 0:
      social_score_reduced_loan = True

  # If social score makes them ineligible, it's the final reason
  if max_loan <= 0:
    return 0, LoanLimitReason.INELIGIBLE

  # 3. Apply the bank's hard policy cap
  if max_loan > BANK_POLICY_CAP:
    max_loan = BANK_POLICY_CAP
    reason = LoanLimitReason.BANK_POLICY

  # 4. Apply the cap based on earnings history
  # This check comes after the bank policy cap, so it will correctly
  # become the reason if it's an even more restrictive limit.
  earnings_cap = deliveries_agg['total_payment'] * 5
  if max_loan > earnings_cap:
    max_loan = earnings_cap
    reason = LoanLimitReason.EARNINGS_HISTORY

  # 5. If no cap was hit, check if a negative social score was the limiting factor
  if reason is None and social_score_reduced_loan:
    reason = LoanLimitReason.SOCIAL_SCORE

  return int(max_loan), reason


async def get_player_loan_balance(character):
  loan_account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.BANK,
    character=character,
    defaults={
      'name': f'Loan #{character.id} - {character.name}',
    }
  )
  return loan_account.balance

async def get_treasury_fund_balance(character):
  treasury_fund, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.GOVERNMENT,
    character=None,
    name='Treasury Fund',
  )
  return treasury_fund.balance

async def get_character_total_donations(character, start_time):
  aggregates = await (LedgerEntry.objects
    .filter_character_donations(character)
    .filter(journal_entry__created_at__gte=start_time)
    .aaggregate(total_donations=Sum('credit', default=0))
  )
  return aggregates['total_donations']

async def get_character_total_interest(character, start_time):
  pass

async def register_player_deposit(amount, character, player, description="Player Deposit"):
  account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.LIABILITY,
    book=Account.Book.BANK,
    character=character,
    defaults={
      'name': 'Checking Account',
    }
  )

  bank_vault, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Vault',
    }
  )

  return await sync_to_async(create_journal_entry)(
    timezone.now(),
    description,
    character,
    [
      {
        'account': account,
        'debit': 0,
        'credit': amount,
      },
      {
        'account': bank_vault,
        'debit': amount,
        'credit': 0,
      },
    ]
  )


async def register_player_withdrawal(amount, character, player):
  account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.LIABILITY,
    book=Account.Book.BANK,
    character=character,
    defaults={
      'name': 'Checking Account',
    }
  )

  bank_vault, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Vault',
    }
  )
  if amount > account.balance:
    raise ValueError('Unable to withdraw more than balance')

  return await sync_to_async(create_journal_entry)(
    timezone.now(),
    "Player Withdrawal",
    character,
    [
      {
        'account': account,
        'debit': amount,
        'credit': 0,
      },
      {
        'account': bank_vault,
        'debit': 0,
        'credit': amount,
      },
    ]
  )

LOAN_INTEREST_RATES = [0.1, 0.2, 0.3]

def calc_loan_fee(amount, character, max_loan):
  threshold = 0
  fee = 0
  for i, interest_rate in enumerate(LOAN_INTEREST_RATES, start=1):
    prev_threshold = threshold
    threshold += max_loan / 2 ** i
    amount_under_threshold = min(max(0, int(amount) - prev_threshold), threshold - prev_threshold)
    if amount_under_threshold > 0:
      fee += int(amount_under_threshold) * interest_rate

  if amount > threshold:
    fee += (amount - threshold) * (interest_rate)
  return int(fee)

async def register_player_take_loan(amount, character):
  max_loan, _ = await get_character_max_loan(character)
  fee = calc_loan_fee(amount, character, max_loan)
  principal = Decimal(amount) + Decimal(fee)

  loan_account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.BANK,
    character=character,
    defaults={
      'name': f'Loan #{character.id} - {character.name}',
    }
  )

  bank_vault, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Vault',
    }
  )

  bank_revenue, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.REVENUE,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Revenue',
    }
  )

  await sync_to_async(create_journal_entry)(
    timezone.now(),
    "Player Loan",
    character,
    [
      {
        'account': loan_account,
        'debit': principal,
        'credit': 0,
      },
      {
        'account': bank_revenue,
        'debit': 0,
        'credit': principal - amount,
      },
      {
        'account': bank_vault,
        'debit': 0,
        'credit': amount,
      },
    ]
  )
  return principal, fee


async def register_player_repay_loan(amount, character):
  loan_account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.BANK,
    character=character,
    defaults={
      'name': f'Loan #{character.id} - {character.name}',
    }
  )

  bank_vault, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Vault',
    }
  )
  if loan_account.balance < amount:
    raise ValueError('You are repaying more than you owe')

  return await sync_to_async(create_journal_entry)(
    timezone.now(),
    "Player Loan Repayment",
    character,
    [
      {
        'account': bank_vault,
        'debit': amount,
        'credit': 0,
      },
      {
        'account': loan_account,
        'debit': 0,
        'credit': amount,
      },
    ]
  )


async def player_donation(amount, character):
  treasury_fund, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.GOVERNMENT,
    character=None,
    name='Treasury Fund',
  )
  treasury_revenue, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.REVENUE,
    book=Account.Book.GOVERNMENT,
    character=None,
    defaults={
      'name': 'Treasury Revenue',
    }
  )

  await sync_to_async(create_journal_entry, thread_sensitive=True)(
    timezone.now(),
    "Player Donation",
    character,
    [
      {
        'account': treasury_revenue,
        'debit': 0,
        'credit': amount,
      },
      {
        'account': treasury_fund,
        'debit': amount,
        'credit': 0,
      },
    ]
  )

async def send_fund_to_player_wallet(amount, character, description):
  treasury_fund, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.GOVERNMENT,
    character=None,
    name='Treasury Fund',
  )
  treasury_expenses, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.EXPENSE,
    book=Account.Book.GOVERNMENT,
    character=None,
    defaults={
      'name': 'Treasury Expenses',
    }
  )

  await sync_to_async(create_journal_entry)(
    timezone.now(),
    description,
    None,
    [
      {
        'account': treasury_expenses,
        'debit': amount,
        'credit': 0,
      },
      {
        'account': treasury_fund,
        'debit': 0,
        'credit': amount,
      },
    ]
  )


async def send_fund_to_player(amount, character, reason):
  account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.LIABILITY,
    book=Account.Book.BANK,
    character=character,
    defaults={
      'name': 'Checking Account',
    }
  )

  bank_vault, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Vault',
    }
  )

  treasury_fund, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.GOVERNMENT,
    character=None,
    name='Treasury Fund',
  )

  treasury_expenses, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.EXPENSE,
    book=Account.Book.GOVERNMENT,
    character=None,
    defaults={
      'name': 'Treasury Expenses',
    }
  )

  await sync_to_async(create_journal_entry, thread_sensitive=True)(
    timezone.now(),
    f"Government Funding: {reason}",
    None,
    [
      {
        'account': treasury_expenses,
        'debit': amount,
        'credit': 0,
      },
      {
        'account': treasury_fund,
        'debit': 0,
        'credit': amount,
      },
    ]
  )

  await sync_to_async(create_journal_entry, thread_sensitive=True)(
    timezone.now(),
    f"Government Funding: {reason}",
    None,
    [
      {
        'account': account,
        'debit': 0,
        'credit': amount,
      },
      {
        'account': bank_vault,
        'debit': amount,
        'credit': 0,
      },
    ]
  )


def create_journal_entry(date, description, creator_character, entries_data):
  """
  Creates a JournalEntry and its LedgerEntries atomically,
  and updates account balances.

  `entries_data` should be a list of dicts:
  [{'account': account_obj, 'debit': amount, 'credit': 0}, ...]
  """
  # 1. Validate that the transaction is balanced before hitting the DB
  total_debits = sum(d.get('debit', 0) for d in entries_data)
  total_credits = sum(d.get('credit', 0) for d in entries_data)
  if total_debits != total_credits:
    raise ValueError("The provided entries are not balanced.")

  with transaction.atomic():
    # 2. Create the main journal entry
    journal_entry = JournalEntry.objects.create(
      date=date,
      description=description,
      creator=creator_character,
    )

    # 3. Create ledger entries and update account balances
    for entry_data in entries_data:
      account = entry_data['account']
      debit = entry_data.get('debit', 0)
      credit = entry_data.get('credit', 0)

      LedgerEntry.objects.create(
        journal_entry=journal_entry,
        account=account,
        debit=debit,
        credit=credit
      )

      # 4. Calculate the change in balance
      balance_change = 0
      if account.account_type in [Account.AccountType.ASSET, Account.AccountType.EXPENSE]:
        balance_change = debit - credit
      else:
        balance_change = credit - debit

      account.balance = F('balance') + balance_change
      account.save(update_fields=['balance'])

  return journal_entry


INTEREST_RATE = 0.022
ONLINE_INTEREST_MULTIPLIER = 2.0

async def apply_interest_to_bank_accounts(ctx, interest_rate=INTEREST_RATE, online_interest_multiplier=ONLINE_INTEREST_MULTIPLIER, compounding_hours=1):
  bank_expense_account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.EXPENSE,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Expense',
    }
  )

  accounts_qs = Account.objects.select_related('character', 'character__player').filter(
    account_type=Account.AccountType.LIABILITY,
    book=Account.Book.BANK,
    character__isnull=False,
    balance__gt=0,
  )

  async for account in accounts_qs:
    if account.balance == 0:
      continue

    character_interest_rate = interest_rate
    character = account.character

    try:
      last_online = await CharacterLocation.objects.filter(character=character).alatest('timestamp')
      time_since_last_online = timezone.now() - last_online.timestamp
    except CharacterLocation.DoesNotExist:
      time_since_last_online = timedelta(days=365)

    if time_since_last_online <= timedelta(hours=1):
      character_interest_rate = online_interest_multiplier * character_interest_rate
    elif time_since_last_online <= timedelta(days=7):
      character_interest_rate = character_interest_rate 
    elif time_since_last_online <= timedelta(days=14):
      character_interest_rate = character_interest_rate / 2
    elif time_since_last_online <= timedelta(days=30):
      character_interest_rate = character_interest_rate / 4
    else:
      character_interest_rate = character_interest_rate / 8

    amount = account.balance * Decimal(character_interest_rate) / Decimal(24 / compounding_hours)
    if amount >= Decimal(0.01):
      await sync_to_async(create_journal_entry)(
        timezone.now(),
        "Interest Payment",
        None,
        [
          {
            'account': account,
            'debit': 0,
            'credit': amount,
          },
          {
            'account': bank_expense_account,
            'debit': amount,
            'credit': 0,
          },
        ]
      )


async def make_treasury_bank_deposit(amount, description):
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
  bank_vault, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.ASSET,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Vault',
    }
  )
  bank_treasury_account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.EQUITY,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Equity',
    }
  )

  await sync_to_async(create_journal_entry)(
    timezone.now(),
    description,
    None,
    [
      {
        'account': treasury_fund_in_bank,
        'debit': amount,
        'credit': 0,
      },
      {
        'account': treasury_fund,
        'debit': 0,
        'credit': amount,
      },
    ]
  )
  await sync_to_async(create_journal_entry)(
    timezone.now(),
    description,
    None,
    [
      {
        'account': bank_vault,
        'debit': amount,
        'credit': 0,
      },
      {
        'account': bank_treasury_account,
        'debit': 0,
        'credit': amount,
      },
    ]
  )


