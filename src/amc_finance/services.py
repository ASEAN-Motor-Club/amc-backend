from decimal import Decimal
from django.utils import timezone
from django.db import transaction
from django.db.models import F
from asgiref.sync import sync_to_async
from amc_finance.models import Account, JournalEntry, LedgerEntry

def get_character_max_loan(character):
  return 10_000 + ((character.driver_level * 5_000) if character.driver_level else 0)

async def get_player_bank_balance(character):
  account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.LIABILITY,
    book=Account.Book.BANK,
    character=character,
    defaults={
      'name': 'Checking Account',
    }
  )
  return account.balance

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
    defaults={
      'name': 'Treasury Fund',
    }
  )
  return treasury_fund.balance

async def register_player_deposit(amount, character, player):
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
    "Player Deposit",
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


async def register_player_take_loan(amount, character):
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

  return await sync_to_async(create_journal_entry)(
    timezone.now(),
    "Player Loan",
    character,
    [
      {
        'account': loan_account,
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
    defaults={
      'name': 'Treasury Fund',
    }
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
    defaults={
      'name': 'Treasury Fund',
    }
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
    defaults={
      'name': 'Treasury Fund',
    }
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
async def apply_interest_to_bank_accounts(ctx, interest_rate=INTEREST_RATE):
  bank_expense_account, _ = await Account.objects.aget_or_create(
    account_type=Account.AccountType.EXPENSE,
    book=Account.Book.BANK,
    character=None,
    defaults={
      'name': 'Bank Expense',
    }
  )

  accounts_qs = Account.objects.filter(
    account_type=Account.AccountType.LIABILITY,
    book=Account.Book.BANK,
    character__isnull=False,
    balance__gt=0,
  )

  async for account in accounts_qs:
    if account.balance == 0:
      continue

    amount = account.balance * Decimal(interest_rate) / Decimal(24)
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

