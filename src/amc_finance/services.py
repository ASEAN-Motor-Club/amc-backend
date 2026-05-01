import math
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from django.db import transaction
from django.db.models import F, Sum
from asgiref.sync import sync_to_async
from amc_finance.models import Account, JournalEntry, LedgerEntry


from typing import Any, cast


TREASURY_FLOOR = Decimal(1_000_000)


async def get_treasury_fund_balance():
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    return treasury_fund.balance


async def get_sovereign_reserves_balance():
    reserves, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Sovereign Reserves",
    )
    return reserves.balance


async def check_treasury_floor(amount) -> bool:
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    return Decimal(str(treasury_fund.balance)) - Decimal(str(amount)) >= TREASURY_FLOOR


async def get_character_total_donations(character, start_time):
    aggregates = await (
        LedgerEntry.objects.filter_character_donations(character)
        .filter(journal_entry__created_at__gte=start_time)
        .aaggregate(total_donations=Sum("credit", default=0))
    )
    return aggregates["total_donations"]


async def get_character_total_interest(character, start_time):
    pass


async def register_player_deposit(
    amount, character, player, description="Player Deposit"
):
    account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.LIABILITY,
        book=Account.Book.BANK,
        character=character,
        defaults={
            "name": "Checking Account",
        },
    )

    bank_vault, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Vault",
        },
    )

    return await sync_to_async(create_journal_entry)(
        timezone.now(),
        description,
        character,
        [
            {
                "account": account,
                "debit": 0,
                "credit": amount,
            },
            {
                "account": bank_vault,
                "debit": amount,
                "credit": 0,
            },
        ],
    )


async def register_player_withdrawal(amount, character, player):
    account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.LIABILITY,
        book=Account.Book.BANK,
        character=character,
        defaults={
            "name": "Checking Account",
        },
    )

    bank_vault, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Vault",
        },
    )
    if amount > account.balance:
        raise ValueError("Unable to withdraw more than balance")

    return await sync_to_async(create_journal_entry)(
        timezone.now(),
        "Player Withdrawal",
        character,
        [
            {
                "account": account,
                "debit": amount,
                "credit": 0,
            },
            {
                "account": bank_vault,
                "debit": 0,
                "credit": amount,
            },
        ],
    )


async def player_donation(amount, character, description="Player Donation"):
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    treasury_revenue, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.REVENUE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Revenue",
    )

    await sync_to_async(create_journal_entry, thread_sensitive=True)(
        timezone.now(),
        description,
        character,
        [
            {
                "account": treasury_revenue,
                "debit": 0,
                "credit": amount,
            },
            {
                "account": treasury_fund,
                "debit": amount,
                "credit": 0,
            },
        ],
    )
    character.total_donations = F("total_donations") + amount
    await character.asave(update_fields=["total_donations"])


async def send_fund_to_player_wallet(amount, character, description):
    if not await check_treasury_floor(amount):
        return
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    treasury_expenses, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EXPENSE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Expenses",
    )

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        description,
        None,
        [
            {
                "account": treasury_expenses,
                "debit": amount,
                "credit": 0,
            },
            {
                "account": treasury_fund,
                "debit": 0,
                "credit": amount,
            },
        ],
    )


async def record_treasury_expense(amount, description="Treasury Expense"):
    """Burn money from the treasury (Dr. Expenses / Cr. Treasury Fund)."""
    if not await check_treasury_floor(amount):
        return
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    treasury_expenses, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EXPENSE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Expenses",
    )

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        description,
        None,
        [
            {
                "account": treasury_expenses,
                "debit": amount,
                "credit": 0,
            },
            {
                "account": treasury_fund,
                "debit": 0,
                "credit": amount,
            },
        ],
    )


async def record_treasury_confiscation_income(
    amount, description="Police Confiscation"
):
    """Credit the treasury via confiscation (Dr. Treasury Fund / Cr. Confiscation Revenue).

    Unlike player_donation, this does NOT increment total_donations.
    """
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    confiscation_revenue, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.REVENUE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Confiscation Revenue",
    )

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        description,
        None,
        [
            {"account": confiscation_revenue, "debit": 0, "credit": amount},
            {"account": treasury_fund, "debit": amount, "credit": 0},
        ],
    )


async def record_treasury_rent_income(amount, description="House Rent"):
    """Record rent payment as treasury revenue (Dr. Treasury Fund / Cr. Rent Revenue)."""
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    rent_revenue, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.REVENUE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Rent Revenue",
    )

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        description,
        None,
        [
            {"account": rent_revenue, "debit": 0, "credit": amount},
            {"account": treasury_fund, "debit": amount, "credit": 0},
        ],
    )


async def send_fund_to_player(amount, character, reason):
    if not await check_treasury_floor(amount):
        return
    account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.LIABILITY,
        book=Account.Book.BANK,
        character=character,
        defaults={
            "name": "Checking Account",
        },
    )

    bank_vault, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Vault",
        },
    )

    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )

    treasury_expenses, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EXPENSE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Expenses",
    )

    await sync_to_async(create_journal_entry, thread_sensitive=True)(
        timezone.now(),
        f"Government Funding: {reason}",
        None,
        [
            {
                "account": treasury_expenses,
                "debit": amount,
                "credit": 0,
            },
            {
                "account": treasury_fund,
                "debit": 0,
                "credit": amount,
            },
        ],
    )

    await sync_to_async(create_journal_entry, thread_sensitive=True)(
        timezone.now(),
        f"Government Funding: {reason}",
        None,
        [
            {
                "account": account,
                "debit": 0,
                "credit": amount,
            },
            {
                "account": bank_vault,
                "debit": amount,
                "credit": 0,
            },
        ],
    )


def create_journal_entry(date, description, creator_character, entries_data):
    """
    Creates a JournalEntry and its LedgerEntries atomically,
    and updates account balances.

    `entries_data` should be a list of dicts:
    [{'account': account_obj, 'debit': amount, 'credit': 0}, ...]
    """
    # 1. Filter out zero-amount entries and validate that the transaction is balanced
    entries_data = [
        d for d in entries_data if d.get("debit", 0) > 0 or d.get("credit", 0) > 0
    ]

    total_debits = sum(d.get("debit", 0) for d in entries_data)
    total_credits = sum(d.get("credit", 0) for d in entries_data)

    if total_debits == 0 and total_credits == 0:
        return None

    if total_debits != total_credits:
        raise ValueError("The provided entries are not balanced.")

    for d in entries_data:
        if d.get("debit", 0) > 0 and d.get("credit", 0) > 0:
            raise ValueError("An entry cannot have both a debit and a credit.")

    with transaction.atomic():
        # 2. Create the main journal entry
        journal_entry = JournalEntry.objects.create(
            date=date,
            description=description,
            creator=creator_character,
        )

        # 3. Create ledger entries and update account balances
        for entry_data in entries_data:
            account = entry_data["account"]
            debit = entry_data.get("debit", 0)
            credit = entry_data.get("credit", 0)

            LedgerEntry.objects.create(
                journal_entry=journal_entry, account=account, debit=debit, credit=credit
            )

            # 4. Calculate the change in balance
            balance_change = 0
            if account.account_type in [
                Account.AccountType.ASSET,
                Account.AccountType.EXPENSE,
            ]:
                balance_change = debit - credit
            else:
                balance_change = credit - debit

            account.balance = cast(Any, F("balance") + balance_change)
            account.save(update_fields=["balance"])

    return journal_entry


INTEREST_RATE = 0.022
ONLINE_INTEREST_MULTIPLIER = 2.0
INTEREST_THRESHOLD = 10_000_000
INTEREST_SCALE = 40_000_000
INTEREST_DECAY_K = 2.0  # controls how fast interest decays with offline time

# Wealth Tax — Progressive brackets with log-plateau decay
# Hourly rate: r(t) = k · ln(1 + t/S) / (S + t)
# Cumulative loss grows as k/2 · ln(1 + t/S)² — monotonic, no recovery
WEALTH_TAX_EXEMPT = 1_000_000
WEALTH_TAX_S = 2163  # time scale in hours (~90 days)
WEALTH_TAX_BRACKETS = [
    # (floor, ceiling, k)
    (1_000_000, 20_000_000, 0.65),  # Low
    (20_000_000, 100_000_000, 1.05),  # Mid
    (100_000_000, float("inf"), 1.55),  # High
]

# Sovereign Reserves — NIRC (Net Investment Returns Contribution)
NIRC_DAILY_RATE = Decimal("0.05") / 30


def wealth_tax_hourly_rate(k: float, t_hours: float) -> float:
    """Hourly tax rate: k · ln(1 + t/S) / (S + t). Monotonically decreasing."""
    if t_hours <= 0:
        return 0.0
    x = 1 + t_hours / WEALTH_TAX_S
    return k * math.log(x) / (WEALTH_TAX_S * x)


def calculate_wealth_tax(balance: int, hours_offline: float) -> int:
    """Calculate one hourly tick of wealth tax across progressive marginal brackets.

    Returns the integer amount to deduct.
    """
    if balance <= WEALTH_TAX_EXEMPT or hours_offline < 1:
        return 0

    tax = 0.0
    prev = WEALTH_TAX_EXEMPT
    for floor, ceiling, k in WEALTH_TAX_BRACKETS:
        if balance <= prev:
            break
        taxable = min(balance, ceiling) - prev
        if taxable > 0:
            tax += taxable * wealth_tax_hourly_rate(k, hours_offline)
        prev = ceiling

    return max(int(tax), 0)


def calculate_hourly_interest(balance: int, hours_offline: float) -> int:
    """Calculate one hourly tick of interest for an offline player.

    Mirrors calculate_wealth_tax — a pure function that returns the integer
    interest amount for a given balance and offline duration.
    """
    if balance <= 0 or hours_offline <= 0:
        return 0

    rate = INTEREST_RATE

    if hours_offline <= 1:
        rate = ONLINE_INTEREST_MULTIPLIER * rate
    else:
        decay = 1.0 / (1.0 + INTEREST_DECAY_K * math.log10(hours_offline))
        rate *= decay

    excess = max(0, balance - INTEREST_THRESHOLD)
    balance_multiplier = math.exp(-excess / INTEREST_SCALE)

    amount = balance * rate * balance_multiplier / 24
    return max(int(amount), 0)


def get_crossover_accounts():
    """Return bank accounts where hourly wealth tax exceeds hourly interest.

    Each account in the result list is annotated with:
    - hours_offline
    - hourly_tax
    - hourly_interest
    - net_hourly_loss  (tax - interest)
    """
    now = timezone.now()
    accounts = list(
        Account.objects.filter(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character__isnull=False,
            character__guid__isnull=False,
            balance__gt=WEALTH_TAX_EXEMPT,
        ).select_related("character", "character__player")
    )

    results = []
    for account in accounts:
        last_online_ts = account.character.last_online
        if last_online_ts is None:
            hours_offline = 365 * 24.0
        else:
            hours_offline = (now - last_online_ts).total_seconds() / 3600

        tax = calculate_wealth_tax(int(account.balance), hours_offline)
        interest = calculate_hourly_interest(int(account.balance), hours_offline)

        if tax > interest:
            account.hours_offline = hours_offline
            account.hourly_tax = tax
            account.hourly_interest = interest
            account.net_hourly_loss = tax - interest
            results.append(account)

    return results


def _bulk_create_interest_entries(entries_to_create, bank_expense_account, now):
    """Create all interest journal entries in a single transaction."""
    if not entries_to_create:
        return

    with transaction.atomic():
        total_expense = Decimal(0)
        for account, amount in entries_to_create:
            je = JournalEntry.objects.create(
                date=now,
                description="Interest Payment",
                creator=None,
            )
            LedgerEntry.objects.create(
                journal_entry=je,
                account=account,
                debit=0,
                credit=amount,
            )
            LedgerEntry.objects.create(
                journal_entry=je,
                account=bank_expense_account,
                debit=amount,
                credit=0,
            )
            # Update account balance (LIABILITY: credit increases balance)
            account.balance = cast(Any, F("balance") + amount)
            account.save(update_fields=["balance"])
            total_expense += amount

        # Update bank expense balance once (EXPENSE: debit increases balance)
        bank_expense_account.balance = cast(Any, F("balance") + total_expense)
        bank_expense_account.save(update_fields=["balance"])


async def apply_interest_to_bank_accounts(
    ctx,
    interest_rate=INTEREST_RATE,
    online_interest_multiplier=ONLINE_INTEREST_MULTIPLIER,
    compounding_hours=1,
):
    bank_expense_account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EXPENSE,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Expense",
        },
    )

    # Read last_online directly from Character (cached by monitor_locations)
    accounts = await sync_to_async(
        lambda: list(  # pyrefly: ignore
            Account.objects.filter(
                account_type=Account.AccountType.LIABILITY,
                book=Account.Book.BANK,
                character__isnull=False,
                character__guid__isnull=False,
                balance__gt=0,
            ).select_related("character")
        )
    )()

    # Calculate interest in-memory
    now = timezone.now()
    entries_to_create = []
    for account in accounts:
        character_interest_rate = interest_rate

        last_online_ts = account.character.last_online  # pyrefly: ignore
        if last_online_ts is None:
            time_since_last_online = timedelta(days=365)
        else:
            time_since_last_online = now - last_online_ts

        hours_offline = time_since_last_online.total_seconds() / 3600
        if hours_offline <= 1:
            character_interest_rate = (
                online_interest_multiplier * character_interest_rate
            )
        else:
            # Smooth logarithmic decay: rate × 1/(1 + k·log₁₀(hours))
            decay = 1.0 / (1.0 + INTEREST_DECAY_K * math.log10(hours_offline))
            character_interest_rate *= decay

        # Apply balance-based fall-off: full interest up to threshold,
        # then exponentially decreasing
        excess = max(Decimal(0), account.balance - INTEREST_THRESHOLD)
        balance_multiplier = Decimal(math.exp(-float(excess) / INTEREST_SCALE))

        amount = (
            account.balance
            * Decimal(character_interest_rate)
            * balance_multiplier
            / Decimal(24 / compounding_hours)
        )
        if amount >= Decimal(0.01):
            entries_to_create.append((account, amount))

    # Bulk create in single transaction
    await sync_to_async(_bulk_create_interest_entries)(
        entries_to_create, bank_expense_account, now
    )


def _bulk_create_wealth_tax_entries(entries_to_create, reserves_account, now):
    """Create all wealth tax journal entries in a single transaction.

    Each entry debits the character's LIABILITY account (reducing balance)
    and debits the Sovereign Reserves ASSET account (increasing balance).
    Standard double-entry: ASSET balance += debit - credit.
    """
    if not entries_to_create:
        return

    with transaction.atomic():
        total_tax = Decimal(0)
        for account, amount in entries_to_create:
            je = JournalEntry.objects.create(
                date=now,
                description="Wealth Tax",
                creator=account.character,
            )
            # Debit the player's checking account (LIABILITY: debit decreases balance)
            LedgerEntry.objects.create(
                journal_entry=je,
                account=account,
                debit=amount,
                credit=0,
            )
            # Debit the reserves account (ASSET: debit increases balance)
            LedgerEntry.objects.create(
                journal_entry=je,
                account=reserves_account,
                debit=amount,
                credit=0,
            )
            account.balance = cast(Any, F("balance") - amount)
            account.save(update_fields=["balance"])
            total_tax += amount

        # Standard ASSET debit: balance += debit - credit = +total_tax
        reserves_account.balance = cast(Any, F("balance") + total_tax)
        reserves_account.save(update_fields=["balance"])


async def apply_wealth_tax(ctx):
    """Hourly cron: apply progressive wealth tax to offline characters.

    Tax starts from hour 1 of being offline. Uses log-plateau decay
    with marginal brackets (exempt < 500K, Low, Mid, High).
    Revenue goes to Sovereign Reserves (locked, not operating treasury).
    """
    reserves_account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Sovereign Reserves",
    )

    now = timezone.now()
    accounts = await sync_to_async(
        lambda: list(  # pyrefly: ignore
            Account.objects.filter(
                account_type=Account.AccountType.LIABILITY,
                book=Account.Book.BANK,
                character__isnull=False,
                character__guid__isnull=False,
                balance__gt=WEALTH_TAX_EXEMPT,
            ).select_related("character")
        )
    )()

    entries_to_create = []
    for account in accounts:
        last_online_ts = account.character.last_online  # pyrefly: ignore
        if last_online_ts is None:
            hours_offline = 365 * 24.0
        else:
            hours_offline = (now - last_online_ts).total_seconds() / 3600

        tax = calculate_wealth_tax(int(account.balance), hours_offline)
        if tax > 0 and tax <= account.balance - WEALTH_TAX_EXEMPT:
            entries_to_create.append((account, Decimal(tax)))

    await sync_to_async(_bulk_create_wealth_tax_entries)(
        entries_to_create, reserves_account, now
    )


async def transfer_nirc(ctx):
    """Daily cron: transfer NIRC (Net Investment Returns Contribution)
    from Sovereign Reserves to Operating Treasury.

    Transfers a treasury-health-responsive portion of reserves balance daily.
    Rate scales inversely with treasury_mult: low treasury → faster transfer,
    high treasury → slower transfer. Clamped at 10× base rate.
    """
    from amc.jobs import calculate_treasury_multiplier
    from amc.models import JobPostingConfig

    reserves, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Sovereign Reserves",
    )
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )

    if reserves.balance <= 0:
        return

    config = await JobPostingConfig.aget_config()
    treasury_mult = calculate_treasury_multiplier(
        float(treasury_fund.balance),
        equilibrium=float(config.treasury_equilibrium),
        sensitivity=float(config.treasury_sensitivity),
        cap_ratio=float(config.treasury_cap_ratio),
    )
    nirc_rate_multiplier = min(10.0, 1.0 / max(treasury_mult, 0.1))
    effective_rate = NIRC_DAILY_RATE * Decimal(str(nirc_rate_multiplier))

    amount = int(reserves.balance * effective_rate)
    if amount <= 0:
        return

    amount_decimal = Decimal(amount)
    now = timezone.now()

    await sync_to_async(create_journal_entry, thread_sensitive=True)(
        now,
        "NIRC Transfer",
        None,
        [
            {
                "account": reserves,
                "debit": 0,
                "credit": amount_decimal,  # Credit reduces ASSET
            },
            {
                "account": treasury_fund,
                "debit": amount_decimal,  # Debit increases ASSET
                "credit": 0,
            },
        ],
    )


async def make_treasury_bank_deposit(amount, description):
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    treasury_fund_in_bank, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund (in Bank)",
    )
    bank_vault, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Vault",
        },
    )
    bank_treasury_account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EQUITY,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Equity",
        },
    )

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        description,
        None,
        [
            {
                "account": treasury_fund_in_bank,
                "debit": amount,
                "credit": 0,
            },
            {
                "account": treasury_fund,
                "debit": 0,
                "credit": amount,
            },
        ],
    )
    await sync_to_async(create_journal_entry)(
        timezone.now(),
        description,
        None,
        [
            {
                "account": bank_vault,
                "debit": amount,
                "credit": 0,
            },
            {
                "account": bank_treasury_account,
                "debit": 0,
                "credit": amount,
            },
        ],
    )


async def make_treasury_bank_withdrawal(amount, description):
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    treasury_fund_in_bank, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund (in Bank)",
    )
    bank_vault, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Vault",
        },
    )
    bank_treasury_account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EQUITY,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Equity",
        },
    )

    if amount > treasury_fund_in_bank.balance:
        raise ValueError(
            f"Insufficient treasury balance in bank: {treasury_fund_in_bank.balance:,} available, {amount:,} requested"
        )

    # Government books: Treasury Fund (in Bank) → Treasury Fund
    await sync_to_async(create_journal_entry)(
        timezone.now(),
        description,
        None,
        [
            {
                "account": treasury_fund,
                "debit": amount,
                "credit": 0,
            },
            {
                "account": treasury_fund_in_bank,
                "debit": 0,
                "credit": amount,
            },
        ],
    )
    # Bank books: Bank Equity → Bank Vault
    await sync_to_async(create_journal_entry)(
        timezone.now(),
        description,
        None,
        [
            {
                "account": bank_treasury_account,
                "debit": amount,
                "credit": 0,
            },
            {
                "account": bank_vault,
                "debit": 0,
                "credit": amount,
            },
        ],
    )


async def allocate_ministry_budget(amount, term):
    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    ministry_budget, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,  # Ministry is an org, not a character account
        name="Ministry of Commerce Budget",
    )

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        f"Ministry Budget Allocation - Term {term.id}",
        None,
        [
            {
                "account": treasury_fund,
                "debit": 0,
                "credit": amount,
            },
            {
                "account": ministry_budget,
                "debit": amount,
                "credit": 0,
            },
        ],
    )

    # Sync model
    term.current_budget = (
        ministry_budget.balance
    )  # Should be updated by journal entry logic but we read it back or trust the flow
    # Since create_journal_entry updates balance in-memory on the account object, we can use that if returned, but here we re-fetch or assume correctness.
    # Actually, create_journal_entry updates the passed account objects.
    # Since create_journal_entry updates the passed account objects with F expressions, we must refresh.
    await ministry_budget.arefresh_from_db()
    term.current_budget = ministry_budget.balance
    await term.asave()


async def escrow_ministry_funds(amount, job):
    ministry_budget, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Ministry of Commerce Budget",
    )
    ministry_escrow, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Ministry of Commerce Escrow",
    )

    if ministry_budget.balance < amount:
        return False

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        f"Job Escrow - {job.id}",
        None,
        [
            {
                "account": ministry_budget,
                "debit": 0,
                "credit": amount,
            },
            {
                "account": ministry_escrow,
                "debit": amount,
                "credit": 0,
            },
        ],
    )

    # Update Funding Term Budget
    if job.funding_term_id:
        from amc.models import MinistryTerm

        term = await MinistryTerm.objects.aget(id=job.funding_term_id)
        await ministry_budget.arefresh_from_db()
        term.current_budget = ministry_budget.balance
        term.created_jobs_count = cast(Any, F("created_jobs_count") + 1)
        await term.asave()

    return True


async def process_ministry_completion(job, bonus_amount):
    """
    Handles the Ministry side of job completion:
    1. Clears Escrow (Funds moved to Expense)
    2. Receives Performance Grant (Rebate)
    """
    ministry_escrow, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Ministry of Commerce Escrow",
    )
    ministry_expense, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EXPENSE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Ministry of Commerce Expenses",
    )

    # 1. Clear Escrow -> Expense
    # We move the *escrowed* amount (which should equal bonus_amount usually, but let's use job.escrowed_amount)
    escrowed = job.escrowed_amount

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        f"Job Completion Escrow Clear - {job.id}",
        None,
        [
            {
                "account": ministry_escrow,
                "debit": 0,
                "credit": escrowed,
            },
            {
                "account": ministry_expense,
                "debit": escrowed,
                "credit": 0,
            },
        ],
    )

    # 2. Performance Grant (20% Rebate)
    rebate_amount = int(escrowed * 0.20)
    if rebate_amount > 0:
        ministry_budget, _ = await Account.objects.aget_or_create(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.GOVERNMENT,
            character=None,
            name="Ministry of Commerce Budget",
        )
        treasury_revenue, _ = await Account.objects.aget_or_create(
            account_type=Account.AccountType.REVENUE,
            book=Account.Book.GOVERNMENT,
            character=None,
            name="Treasury Revenue",
        )

        await sync_to_async(create_journal_entry)(
            timezone.now(),
            f"Performance Grant (Rebate) - {job.id}",
            None,
            [
                {
                    "account": treasury_revenue,
                    "debit": 0,
                    "credit": rebate_amount,
                },
                {
                    "account": ministry_budget,
                    "debit": rebate_amount,  # Asset increases with Debit
                    "credit": 0,
                },
            ],
        )

        if job.funding_term_id:
            from amc.models import MinistryTerm

            term = await MinistryTerm.objects.aget(id=job.funding_term_id)
            await ministry_budget.arefresh_from_db()
            term.current_budget = ministry_budget.balance
            term.total_spent = cast(Any, F("total_spent") + (escrowed - rebate_amount))
            await term.asave()


async def process_ministry_expiration(job):
    """
    Handles Ministry Job Expiration:
    - 50% Refunded to Budget
    - 50% Burned (Expense)
    - 100% Cleared from Escrow
    """
    ministry_budget, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Ministry of Commerce Budget",
    )
    ministry_escrow, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Ministry of Commerce Escrow",
    )
    ministry_expense, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EXPENSE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Ministry of Commerce Expenses",
    )

    escrowed = job.escrowed_amount
    refund_amount = int(escrowed * 0.50)
    burn_amount = escrowed - refund_amount

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        f"Job Expiration Refund/Burn - {job.id}",
        None,
        [
            {
                "account": ministry_escrow,
                "debit": 0,
                "credit": escrowed,
            },
            {
                "account": ministry_budget,
                "debit": refund_amount,
                "credit": 0,
            },
            {
                "account": ministry_expense,
                "debit": burn_amount,
                "credit": 0,
            },
        ],
    )

    if job.funding_term_id:
        from amc.models import MinistryTerm

        term = await MinistryTerm.objects.aget(id=job.funding_term_id)
        await ministry_budget.arefresh_from_db()
        term.current_budget = ministry_budget.balance
        term.expired_jobs_count = cast(Any, F("expired_jobs_count") + 1)
        term.total_spent = cast(Any, F("total_spent") + burn_amount)
        await term.asave()

    # Clear escrow on job to prevent double refund
    job.escrowed_amount = 0
    await job.asave(update_fields=["escrowed_amount"])


async def record_ministry_subsidy_spend(amount, term_id):
    """
    Records Ministry subsidy spend:
    1. Dr. Ministry Expense / Cr. Ministry Budget
    2. Updates term.current_budget and term.total_spent
    """
    ministry_budget, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Ministry of Commerce Budget",
    )
    ministry_expense, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EXPENSE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Ministry of Commerce Expenses",
    )

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        "Ministry Subsidy Payment",
        None,
        [
            {
                "account": ministry_budget,
                "debit": 0,
                "credit": amount,
            },
            {
                "account": ministry_expense,
                "debit": amount,
                "credit": 0,
            },
        ],
    )

    if term_id:
        from amc.models import MinistryTerm

        term = await MinistryTerm.objects.aget(id=term_id)
        await ministry_budget.arefresh_from_db()
        term.current_budget = ministry_budget.balance
        term.total_spent = cast(Any, F("total_spent") + amount)
        await term.asave()


async def process_treasury_expiration_penalty(job):
    """
    Handles Treasury penalty for expired unfulfilled jobs during government shutdown.
    Costs the treasury 50% of the completion bonus.
    Only applies when no MinistryTerm funded the job.
    """
    from amc import config

    if not config.TREASURY_EXPIRATION_PENALTY_ENABLED:
        return

    if job.completion_bonus <= 0:
        return

    penalty_amount = int(job.completion_bonus * 0.50)

    treasury_fund, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Fund",
    )
    treasury_expenses, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.EXPENSE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Treasury Expenses",
    )

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        f"Job Expiration Penalty - {job.id}",
        None,
        [
            {
                "account": treasury_expenses,
                "debit": penalty_amount,
                "credit": 0,
            },
            {
                "account": treasury_fund,
                "debit": 0,
                "credit": penalty_amount,
            },
        ],
    )

    # Mark job as processed to prevent double penalty
    job.completion_bonus = 0
    await job.asave(update_fields=["completion_bonus"])
