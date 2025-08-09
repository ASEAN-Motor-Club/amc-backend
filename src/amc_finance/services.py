from django.utils import timezone
from django.db import transaction
from django.db.models import F
from asgiref.sync import sync_to_async
from amc_finance.models import Account, JournalEntry, LedgerEntry

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

  if amount + account.balance > 100_000:
    raise ValueError('Unable to deposit more than 100,000')

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



