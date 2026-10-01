"""Backfill the missing credit legs on historical Wealth Tax journals.

Migration 0007 swapped the Sovereign Reserves legs from credit to debit so the
ASSET balance direction was correct, but that left every Wealth Tax journal
with two debit legs and no credit leg — an unbalanced journal. Account
balances were never wrong for checking/reserves; only the double-entry
invariant was broken.

This migration balances each historical journal with two credit legs, one per
book:

- Cr "Bank Equity" (EQUITY/BANK) for the checking-account debit — the bank's
  net worth rises by exactly the tax taken from its depositors.
- Cr "Sovereign Reserves Funding" (EQUITY/GOVERNMENT, created here) for the
  reserves debit — the government balance sheet grows by the collected tax.

Equity (not revenue) accounts keep the tax out of the treasury-summary
income sweep, which aggregates credit legs on REVENUE/GOVERNMENT accounts.

No pre-existing account balance changes except Bank Equity, whose stored
balance is adjusted by the same total its ledger gains (it was previously
understated by all historical wealth tax collections).

Reverse operation deletes the inserted legs and rewinds both equity
balances.
"""

from decimal import Decimal

from django.db import migrations
from django.db.models import F, Sum


BATCH = 20_000


def _ensure_account(Account, account_type, book, name):
    account, _ = Account.objects.get_or_create(
        account_type=account_type,
        book=book,
        character=None,
        name=name,
        defaults={"balance": Decimal(0)},
    )
    return account


def backfill(apps, schema_editor):
    Account = apps.get_model("amc_finance", "Account")
    LedgerEntry = apps.get_model("amc_finance", "LedgerEntry")

    bank_equity = _ensure_account(Account, "EQUITY", "BANK", "Bank Equity")
    reserves_funding = _ensure_account(
        Account, "EQUITY", "GOVERNMENT", "Sovereign Reserves Funding"
    )

    journal_debits = (
        LedgerEntry.objects.filter(journal_entry__description="Wealth Tax")
        .values("journal_entry_id", "account__book", "account__account_type")
        .annotate(total=Sum("debit"))
        .order_by("journal_entry_id", "account__book")
    )

    bank_total = Decimal(0)
    gov_total = Decimal(0)
    bank_batch = []
    gov_batch = []
    inserted = 0
    for row in journal_debits.iterator(chunk_size=BATCH):
        if row["account__book"] != "BANK" or row["account__account_type"] != "LIABILITY":
            continue  # only the checking-account debit drives the bank credit
        amount = row["total"]
        bank_batch.append(
            LedgerEntry(
                journal_entry_id=row["journal_entry_id"],
                account=bank_equity,
                debit=Decimal(0),
                credit=amount,
            )
        )
        gov_batch.append(
            LedgerEntry(
                journal_entry_id=row["journal_entry_id"],
                account=reserves_funding,
                debit=Decimal(0),
                credit=amount,
            )
        )
        bank_total += amount
        gov_total += amount
        inserted += 1
        if len(bank_batch) >= BATCH:
            LedgerEntry.objects.bulk_create(bank_batch + gov_batch, batch_size=BATCH)
            bank_batch, gov_batch = [], []
    if bank_batch:
        LedgerEntry.objects.bulk_create(bank_batch + gov_batch, batch_size=BATCH)

    # EQUITY balance direction: balance += credit - debit. Both accounts only
    # gain credits here.
    bank_equity.balance = F("balance") + bank_total
    bank_equity.save(update_fields=["balance"])
    reserves_funding.balance = F("balance") + gov_total
    reserves_funding.save(update_fields=["balance"])
    print(
        f"\n  Backfilled {inserted} Wealth Tax journals: "
        f"Bank Equity +{bank_total}, Sovereign Reserves Funding +{gov_total}"
    )


def reverse_backfill(apps, schema_editor):
    Account = apps.get_model("amc_finance", "Account")
    LedgerEntry = apps.get_model("amc_finance", "LedgerEntry")

    bank_equity = Account.objects.filter(
        account_type="EQUITY", book="BANK", name="Bank Equity"
    ).first()
    reserves_funding = Account.objects.filter(
        account_type="EQUITY", book="GOVERNMENT", name="Sovereign Reserves Funding"
    ).first()
    if reserves_funding is None:
        return

    deleted = 0
    rewound_bank = Decimal(0)
    if bank_equity is not None:
        rewound_bank = (
            LedgerEntry.objects.filter(
                account=bank_equity,
                journal_entry__description="Wealth Tax",
                credit__gt=0,
                debit=0,
            ).aggregate(t=Sum("credit"))["t"]
            or Decimal(0)
        )
    deleted = LedgerEntry.objects.filter(
        journal_entry__description="Wealth Tax",
        credit__gt=0,
        debit=0,
        account__account_type="EQUITY",
    ).delete()[0]
    if bank_equity is not None:
        bank_equity.balance = F("balance") - rewound_bank
        bank_equity.save(update_fields=["balance"])
    reserves_funding.balance = 0
    reserves_funding.save(update_fields=["balance"])
    print(f"\n  Reversed: deleted {deleted} backfilled credit legs")


class Migration(migrations.Migration):
    dependencies = [
        ("amc_finance", "0007_fix_reserves_wealth_tax_entries"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_code=reverse_backfill),
    ]
