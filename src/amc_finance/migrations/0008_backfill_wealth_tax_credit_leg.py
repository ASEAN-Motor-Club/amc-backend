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

Only journals that still lack ANY credit leg are backfilled, so the
migration is idempotent and safe regardless of whether new (4-leg) code
posted Wealth Tax journals before this migration ran.

No pre-existing account balance changes except Bank Equity, whose stored
balance is adjusted by the same total its ledger gains (it was previously
understated by all historical wealth tax collections).

Reverse operation deletes only the legs on the two backfill accounts and
rewinds both equity balances by the exact sums deleted.
"""

from decimal import Decimal

from django.db import migrations
from django.db.models import F, Sum


BATCH = 20_000

WEALTH_TAX_ACCOUNTS = [("EQUITY", "BANK", "Bank Equity"),
                       ("EQUITY", "GOVERNMENT", "Sovereign Reserves Funding")]


def _ensure_account(Account, account_type, book, name):
    account, _ = Account.objects.get_or_create(
        account_type=account_type,
        book=book,
        character=None,
        name=name,
        defaults={"balance": Decimal(0)},
    )
    return account


def _unbalanced_wealth_tax_journal_ids(LedgerEntry):
    """Journal ids for Wealth Tax journals that have no credit leg at all.

    Historical (buggy) journals have only debit legs; balanced journals —
    whether backfilled here or posted by the fixed forward code — carry at
    least one credit leg, so this predicate is idempotent and safe if new
    code already posted balanced journals before the migration ran.
    """
    balanced_ids = (
        LedgerEntry.objects.filter(
            journal_entry__description="Wealth Tax", credit__gt=0
        ).values("journal_entry_id")
    )
    return (
        LedgerEntry.objects.filter(
            journal_entry__description="Wealth Tax",
            debit__gt=0,
        )
        .exclude(journal_entry_id__in=balanced_ids)
        .values("journal_entry_id", "account__book", "account__account_type")
        .annotate(total=Sum("debit"))
        .order_by("journal_entry_id")
    )


def backfill(apps, schema_editor):
    Account = apps.get_model("amc_finance", "Account")
    LedgerEntry = apps.get_model("amc_finance", "LedgerEntry")

    bank_equity = _ensure_account(Account, "EQUITY", "BANK", "Bank Equity")
    reserves_funding = _ensure_account(
        Account, "EQUITY", "GOVERNMENT", "Sovereign Reserves Funding"
    )

    bank_total = Decimal(0)
    gov_total = Decimal(0)
    bank_batch = []
    gov_batch = []
    inserted = 0
    for row in _unbalanced_wealth_tax_journal_ids(LedgerEntry).iterator(
        chunk_size=BATCH
    ):
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

    rewound_bank = Decimal(0)
    rewound_gov = Decimal(0)
    if bank_equity is not None:
        # Only the Wealth Tax legs this migration family created — Bank Equity
        # carries legitimate legs from other features that must survive.
        rewound_bank = (
            LedgerEntry.objects.filter(
                account=bank_equity,
                journal_entry__description="Wealth Tax",
                credit__gt=0,
                debit=0,
            ).aggregate(t=Sum("credit"))["t"]
            or Decimal(0)
        )
        LedgerEntry.objects.filter(
            account=bank_equity,
            journal_entry__description="Wealth Tax",
            credit__gt=0,
            debit=0,
        ).delete()
        bank_equity.balance = F("balance") - rewound_bank
        bank_equity.save(update_fields=["balance"])

    if reserves_funding is not None:
        rewound_gov = (
            LedgerEntry.objects.filter(account=reserves_funding).aggregate(
                t=Sum("credit")
            )["t"]
            or Decimal(0)
        )
        LedgerEntry.objects.filter(account=reserves_funding).delete()
        reserves_funding.balance = F("balance") - rewound_gov
        reserves_funding.save(update_fields=["balance"])

    print(
        "\n  Reversed backfill: "
        f"Bank Equity -{rewound_bank}, Sovereign Reserves Funding -{rewound_gov}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("amc_finance", "0007_fix_reserves_wealth_tax_entries"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_code=reverse_backfill),
    ]
