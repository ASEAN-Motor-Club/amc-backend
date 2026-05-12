"""Treasury summary data service.

Provides structured breakdowns of treasury income, expenses,
surplus/deficit, and trend data for the Discord cog, V1 API,
and gov-web dashboard.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum, F
from django.db.models.functions import TruncDay
from django.utils import timezone

from amc_finance.models import Account, DailyTreasurySnapshot, LedgerEntry


# --- Income categorisation by journal entry description ---
# Revenue accounts in GOVERNMENT book. Grouped by description prefix/match.
INCOME_CATEGORIES = {
    "gov_employee": {
        "label": "Government Employee Contributions",
        "emoji": "🏛️",
        "match": lambda d: d.startswith("Government Service"),
    },
    "donations": {
        "label": "Player Donations",
        "emoji": "❤️",
        "match": lambda d: d in ("Player Donation", "Public service bill"),
    },
    "rent": {
        "label": "House Rent",
        "emoji": "🏠",
        "match": lambda d: d.startswith("House Rent"),
    },
    "nirc": {
        "label": "NIRC (Sovereign Fund Dividend)",
        "emoji": "🏦",
        # NIRC shows up as a debit on Treasury Fund — we track it via
        # the ASSET account movements, not via Revenue. Handled separately.
        "match": lambda d: False,  # never matched in revenue scan
    },
    "performance_grants": {
        "label": "Performance Grants (Rebates)",
        "emoji": "🎯",
        "match": lambda d: d.startswith("Performance Grant"),
    },
}

# --- Expense categorisation by journal entry description ---
# Expense accounts in GOVERNMENT book.
EXPENSE_CATEGORIES = {
    "subsidies": {
        "label": "Job Subsidies",
        "emoji": "💸",
        "match": lambda d: "Subsidy" in d,
    },
    "job_payouts": {
        "label": "Job Completion Payouts",
        "emoji": "📦",
        "match": lambda d: d == "Government Funding: Job Completion",
    },
    "ubi": {
        "label": "Universal Basic Income & Gov Salary",
        "emoji": "🤝",
        "match": lambda d: d in ("Universal Basic Income", "Government Salary"),
    },
    "gov_funding": {
        "label": "Government Funding (Manual)",
        "emoji": "🏗️",
        "match": lambda d: (
            d.startswith("Government Funding")
            and d != "Government Funding: Job Completion"
        ),
    },
    "penalties": {
        "label": "Job Expiration Penalties",
        "emoji": "⚠️",
        "match": lambda d: d.startswith("Job Expiration Penalty"),
    },
}


def _categorise(description: str, categories: dict) -> str:
    """Return the category key for a journal entry description, or 'other'."""
    for key, cat in categories.items():
        if cat["match"](description):
            return key
    return "other"


def get_treasury_summary(target_date=None, days=1):
    """Return a structured treasury summary for a given date range.

    Args:
        target_date: The end date (inclusive). Defaults to yesterday.
        days: Number of days to aggregate (default 1 = single day).

    Returns:
        dict with keys: date_start, date_end, income, expenses,
        surplus, treasury_balance, reserves_balance, nirc_amount,
        wealth_tax_collected.
    """
    now = timezone.now()
    if target_date is None:
        target_date = (now - timedelta(days=1)).date()

    date_end = target_date
    date_start = target_date - timedelta(days=days - 1)

    # --- Income (Revenue accounts, Government book) ---
    revenue_entries = (
        LedgerEntry.objects.filter(
            account__account_type=Account.AccountType.REVENUE,
            account__book=Account.Book.GOVERNMENT,
            journal_entry__date__range=[date_start, date_end],
        )
        .values("journal_entry__description")
        .annotate(total=Sum("credit"))
    )

    income_breakdown = {key: Decimal(0) for key in INCOME_CATEGORIES}
    income_breakdown["other"] = Decimal(0)
    total_income = Decimal(0)

    for entry in revenue_entries:
        desc = entry["journal_entry__description"] or ""
        amount = entry["total"] or Decimal(0)
        category = _categorise(desc, INCOME_CATEGORIES)
        income_breakdown[category] += amount
        total_income += amount

    # --- NIRC (special: internal transfer to Treasury Fund) ---
    nirc_entries = LedgerEntry.objects.filter(
        account__name="Treasury Fund",
        account__account_type=Account.AccountType.ASSET,
        account__book=Account.Book.GOVERNMENT,
        journal_entry__description="NIRC Transfer",
        journal_entry__date__range=[date_start, date_end],
    ).aggregate(total=Sum("debit", default=Decimal(0)))
    nirc_amount = nirc_entries["total"]
    income_breakdown["nirc"] = nirc_amount
    total_income += nirc_amount

    # --- Expenses (Expense accounts, Government book) ---
    expense_entries = (
        LedgerEntry.objects.filter(
            account__account_type=Account.AccountType.EXPENSE,
            account__book=Account.Book.GOVERNMENT,
            journal_entry__date__range=[date_start, date_end],
        )
        .values("journal_entry__description")
        .annotate(total=Sum("debit"))
    )

    expense_breakdown = {key: Decimal(0) for key in EXPENSE_CATEGORIES}
    expense_breakdown["other"] = Decimal(0)
    total_expenses = Decimal(0)

    for entry in expense_entries:
        desc = entry["journal_entry__description"] or ""
        amount = entry["total"] or Decimal(0)
        category = _categorise(desc, EXPENSE_CATEGORIES)
        expense_breakdown[category] += amount
        total_expenses += amount

    # --- Wealth Tax (to Sovereign Reserves, not operating treasury) ---
    wealth_tax_agg = LedgerEntry.objects.filter(
        account__name="Sovereign Reserves",
        account__account_type=Account.AccountType.ASSET,
        account__book=Account.Book.GOVERNMENT,
        journal_entry__description="Wealth Tax",
        journal_entry__date__range=[date_start, date_end],
    ).aggregate(total=Sum("debit", default=Decimal(0)))
    wealth_tax_collected = wealth_tax_agg["total"]

    # --- Point-in-time balances ---
    # Read current Account.balance and subtract changes after the report
    # date to reconstruct the historical balance.
    gov_accounts = {
        acc.name: acc
        for acc in Account.objects.filter(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.GOVERNMENT,
            character=None,
            name__in=["Treasury Fund", "Sovereign Reserves"],
        )
    }

    treasury_balance = (
        gov_accounts["Treasury Fund"].balance
        if "Treasury Fund" in gov_accounts
        else Decimal(0)
    )
    reserves_balance = (
        gov_accounts["Sovereign Reserves"].balance
        if "Sovereign Reserves" in gov_accounts
        else Decimal(0)
    )

    # If report date is in the past, rewind balances.
    # Standard double-entry: ASSET balance += debit - credit.
    today = now.date()
    if date_end < today:
        post_treasury = LedgerEntry.objects.filter(
            account__name="Treasury Fund",
            account__account_type=Account.AccountType.ASSET,
            account__book=Account.Book.GOVERNMENT,
            journal_entry__date__gt=date_end,
        ).aggregate(net=Sum(F("debit") - F("credit"), default=Decimal(0)))
        treasury_balance -= post_treasury["net"]

        post_reserves = LedgerEntry.objects.filter(
            account__name="Sovereign Reserves",
            account__account_type=Account.AccountType.ASSET,
            account__book=Account.Book.GOVERNMENT,
            journal_entry__date__gt=date_end,
        ).aggregate(net=Sum(F("debit") - F("credit"), default=Decimal(0)))
        reserves_balance -= post_reserves["net"]

    surplus = total_income - total_expenses

    return {
        "date_start": date_start.isoformat(),
        "date_end": date_end.isoformat(),
        "income": {
            "total": total_income,
            "breakdown": {
                key: {
                    "amount": income_breakdown[key],
                    "label": (
                        INCOME_CATEGORIES[key]["label"]
                        if key in INCOME_CATEGORIES
                        else "Other"
                    ),
                    "emoji": (
                        INCOME_CATEGORIES[key]["emoji"]
                        if key in INCOME_CATEGORIES
                        else "📦"
                    ),
                }
                for key in income_breakdown
                if income_breakdown[key] > 0
            },
        },
        "expenses": {
            "total": total_expenses,
            "breakdown": {
                key: {
                    "amount": expense_breakdown[key],
                    "label": (
                        EXPENSE_CATEGORIES[key]["label"]
                        if key in EXPENSE_CATEGORIES
                        else "Other"
                    ),
                    "emoji": (
                        EXPENSE_CATEGORIES[key]["emoji"]
                        if key in EXPENSE_CATEGORIES
                        else "📦"
                    ),
                }
                for key in expense_breakdown
                if expense_breakdown[key] > 0
            },
        },
        "surplus": surplus,
        "treasury_balance": treasury_balance,
        "reserves_balance": reserves_balance,
        "nirc_amount": nirc_amount,
        "wealth_tax_collected": wealth_tax_collected,
    }


def get_treasury_trend(days=7):
    """Return day-by-day treasury data for charts.

    Returns:
        dict with keys: labels (dates), income (daily totals by category),
        expenses (daily totals by category), surplus (daily net),
        treasury_balance (running), reserves_balance (running).
    """
    now = timezone.now()
    end_date = (now - timedelta(days=1)).date()
    start_date = end_date - timedelta(days=days - 1)

    date_range = []
    current = start_date
    while current <= end_date:
        date_range.append(current)
        current += timedelta(days=1)

    labels = [d.isoformat() for d in date_range]

    # --- Daily income by category ---
    revenue_qs = (
        LedgerEntry.objects.filter(
            account__account_type=Account.AccountType.REVENUE,
            account__book=Account.Book.GOVERNMENT,
            journal_entry__date__range=[start_date, end_date],
        )
        .annotate(day=TruncDay("journal_entry__date"))
        .values("day", "journal_entry__description")
        .annotate(total=Sum("credit"))
        .order_by("day")
    )

    # Initialise income map
    income_map = {
        d: {key: 0.0 for key in list(INCOME_CATEGORIES) + ["other"]} for d in date_range
    }
    for entry in revenue_qs:
        day = entry["day"]
        if hasattr(day, "date"):
            day = day.date()
        if day not in income_map:
            continue
        desc = entry["journal_entry__description"] or ""
        cat = _categorise(desc, INCOME_CATEGORIES)
        income_map[day][cat] += float(entry["total"] or 0)

    # NIRC daily (tracked via Treasury Fund debits)
    nirc_qs = (
        LedgerEntry.objects.filter(
            account__name="Treasury Fund",
            account__account_type=Account.AccountType.ASSET,
            account__book=Account.Book.GOVERNMENT,
            journal_entry__description="NIRC Transfer",
            journal_entry__date__range=[start_date, end_date],
        )
        .annotate(day=TruncDay("journal_entry__date"))
        .values("day")
        .annotate(total=Sum("debit"))
        .order_by("day")
    )
    for entry in nirc_qs:
        day = entry["day"]
        if hasattr(day, "date"):
            day = day.date()
        if day in income_map:
            income_map[day]["nirc"] += float(entry["total"] or 0)

    # --- Daily expenses by category ---
    expense_qs = (
        LedgerEntry.objects.filter(
            account__account_type=Account.AccountType.EXPENSE,
            account__book=Account.Book.GOVERNMENT,
            journal_entry__date__range=[start_date, end_date],
        )
        .annotate(day=TruncDay("journal_entry__date"))
        .values("day", "journal_entry__description")
        .annotate(total=Sum("debit"))
        .order_by("day")
    )

    expense_map = {
        d: {key: 0.0 for key in list(EXPENSE_CATEGORIES) + ["other"]}
        for d in date_range
    }
    for entry in expense_qs:
        day = entry["day"]
        if hasattr(day, "date"):
            day = day.date()
        if day not in expense_map:
            continue
        desc = entry["journal_entry__description"] or ""
        cat = _categorise(desc, EXPENSE_CATEGORIES)
        expense_map[day][cat] += float(entry["total"] or 0)

    # --- Build series ---
    income_series = {}
    for key in list(INCOME_CATEGORIES) + ["other"]:
        series = [income_map[d][key] for d in date_range]
        if any(v > 0 for v in series):
            income_series[key] = series

    expense_series = {}
    for key in list(EXPENSE_CATEGORIES) + ["other"]:
        series = [expense_map[d][key] for d in date_range]
        if any(v > 0 for v in series):
            expense_series[key] = series

    # Daily totals
    daily_income = [sum(income_map[d].values()) for d in date_range]
    daily_expenses = [sum(expense_map[d].values()) for d in date_range]
    daily_surplus = [i - e for i, e in zip(daily_income, daily_expenses)]

    # --- Running balances (backwards from current Account.balance) ---
    # Instead of scanning all historical ledger entries, read the current
    # denormalized balance and subtract forward changes to reconstruct
    # the balance at each day in the window.
    gov_accounts = {
        acc.name: acc
        for acc in Account.objects.filter(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.GOVERNMENT,
            character=None,
            name__in=["Treasury Fund", "Sovereign Reserves"],
        )
    }
    current_treasury = (
        float(gov_accounts["Treasury Fund"].balance)
        if "Treasury Fund" in gov_accounts
        else 0.0
    )
    current_reserves = (
        float(gov_accounts["Sovereign Reserves"].balance)
        if "Sovereign Reserves" in gov_accounts
        else 0.0
    )

    # Get daily net changes ONLY within the window (+ days after for backwards calc)
    treasury_txs = (
        LedgerEntry.objects.filter(
            account__name="Treasury Fund",
            account__account_type=Account.AccountType.ASSET,
            account__book=Account.Book.GOVERNMENT,
            journal_entry__date__range=[start_date, timezone.now().date()],
        )
        .annotate(day=TruncDay("journal_entry__date"))
        .values("day")
        .annotate(net_change=Sum(F("debit") - F("credit")))
        .order_by("day")
    )

    daily_treasury_changes = {}
    post_window_treasury_change = 0.0
    for entry in treasury_txs:
        day = entry["day"]
        if hasattr(day, "date"):
            day = day.date()
        change = float(entry["net_change"] or 0)
        if start_date <= day <= end_date:
            daily_treasury_changes[day] = change
        elif day > end_date:
            post_window_treasury_change += change

    # Balance at end of window = current - post-window changes
    treasury_at_end = current_treasury - post_window_treasury_change
    # Work backwards from end to build the series
    treasury_balance_series = [0.0] * len(date_range)
    treasury_balance_series[-1] = treasury_at_end
    for i in range(len(date_range) - 2, -1, -1):
        next_day = date_range[i + 1]
        treasury_balance_series[i] = treasury_balance_series[
            i + 1
        ] - daily_treasury_changes.get(next_day, 0.0)

    # Same approach for Sovereign Reserves
    reserves_txs = (
        LedgerEntry.objects.filter(
            account__name="Sovereign Reserves",
            account__account_type=Account.AccountType.ASSET,
            account__book=Account.Book.GOVERNMENT,
            journal_entry__date__range=[start_date, timezone.now().date()],
        )
        .annotate(day=TruncDay("journal_entry__date"))
        .values("day")
        .annotate(net_change=Sum(F("debit") - F("credit")))
        .order_by("day")
    )

    daily_reserves_changes = {}
    post_window_reserves_change = 0.0
    for entry in reserves_txs:
        day = entry["day"]
        if hasattr(day, "date"):
            day = day.date()
        change = float(entry["net_change"] or 0)
        if start_date <= day <= end_date:
            daily_reserves_changes[day] = change
        elif day > end_date:
            post_window_reserves_change += change

    reserves_at_end = current_reserves - post_window_reserves_change
    reserves_balance_series = [0.0] * len(date_range)
    reserves_balance_series[-1] = reserves_at_end
    for i in range(len(date_range) - 2, -1, -1):
        next_day = date_range[i + 1]
        reserves_balance_series[i] = reserves_balance_series[
            i + 1
        ] - daily_reserves_changes.get(next_day, 0.0)

    # Category labels for frontend
    all_income_labels = {
        key: INCOME_CATEGORIES[key]["label"] for key in INCOME_CATEGORIES
    }
    all_income_labels["other"] = "Other"

    all_expense_labels = {
        key: EXPENSE_CATEGORIES[key]["label"] for key in EXPENSE_CATEGORIES
    }
    all_expense_labels["other"] = "Other"

    return {
        "labels": labels,
        "income": {
            "series": income_series,
            "totals": daily_income,
            "category_labels": {k: all_income_labels.get(k, k) for k in income_series},
        },
        "expenses": {
            "series": expense_series,
            "totals": daily_expenses,
            "category_labels": {
                k: all_expense_labels.get(k, k) for k in expense_series
            },
        },
        "surplus": daily_surplus,
        "treasury_balance": treasury_balance_series,
        "reserves_balance": reserves_balance_series,
    }


def save_treasury_snapshot(target_date=None):
    """Save a DailyTreasurySnapshot for the given date.

    Computes the full summary via get_treasury_summary() and persists it.
    Uses update_or_create so re-running is idempotent.

    Args:
        target_date: The date to snapshot. Defaults to yesterday.

    Returns:
        The DailyTreasurySnapshot instance.
    """
    now = timezone.now()
    if target_date is None:
        target_date = (now - timedelta(days=1)).date()

    summary = get_treasury_summary(target_date=target_date)

    # Convert Decimal values to float for JSON serialization
    def _to_json_safe(obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return obj

    import json

    json_safe_data = json.loads(json.dumps(summary, default=_to_json_safe))

    snapshot, _ = DailyTreasurySnapshot.objects.update_or_create(
        date=target_date,
        defaults={
            "treasury_balance": summary["treasury_balance"],
            "reserves_balance": summary["reserves_balance"],
            "total_income": summary["income"]["total"],
            "total_expenses": summary["expenses"]["total"],
            "surplus": summary["surplus"],
            "wealth_tax_collected": summary["wealth_tax_collected"],
            "nirc_amount": summary["nirc_amount"],
            "data": json_safe_data,
        },
    )
    return snapshot


def get_snapshot_or_live(target_date=None):
    """Return a snapshot if available, otherwise compute live.

    For historical dates, returns the persisted snapshot data.
    For today/yesterday (or if no snapshot exists), falls back
    to live calculation.

    Returns:
        (data_dict, is_snapshot) tuple.
    """
    now = timezone.now()
    if target_date is None:
        target_date = (now - timedelta(days=1)).date()

    try:
        snapshot = DailyTreasurySnapshot.objects.get(date=target_date)
        return snapshot.data, True
    except DailyTreasurySnapshot.DoesNotExist:
        return get_treasury_summary(target_date=target_date), False


def get_snapshot_archive(limit=30):
    """Return a list of available snapshot dates for archive browsing.

    Returns:
        List of dicts with date, surplus, total_income, total_expenses.
    """
    snapshots = DailyTreasurySnapshot.objects.order_by("-date")[:limit]
    return [
        {
            "date": s.date.isoformat(),
            "surplus": float(s.surplus),
            "total_income": float(s.total_income),
            "total_expenses": float(s.total_expenses),
            "treasury_balance": float(s.treasury_balance),
            "reserves_balance": float(s.reserves_balance),
        }
        for s in snapshots
    ]
