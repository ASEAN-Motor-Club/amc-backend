from decimal import Decimal

from django.db import models
from django.db.models import Sum
from django.core.exceptions import ValidationError
from typing import ClassVar, TYPE_CHECKING


class Account(models.Model):
    """
    Represents an account in a ledger
    """

    class AccountType(models.TextChoices):
        ASSET = "ASSET", "Asset"
        LIABILITY = "LIABILITY", "Liability"
        EQUITY = "EQUITY", "Equity"
        REVENUE = "REVENUE", "Revenue"
        EXPENSE = "EXPENSE", "Expense"

    class Book(models.TextChoices):
        GOVERNMENT = "GOVERNMENT", "Government"
        BANK = "BANK", "Bank of ASEAN"

    book = models.CharField(max_length=10, choices=Book.choices)

    character = models.ForeignKey(
        "amc.Character",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="accounts",
    )

    name = models.CharField(max_length=100)
    account_type = models.CharField(max_length=10, choices=AccountType.choices)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    npl_warning_sent_at = models.DateTimeField(null=True, blank=True)
    min_repayment_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Min repayment per period as fraction of balance (e.g. 0.10 = 10%). NULL = use global default.",
    )
    min_repayment_period_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Repayment period in days (e.g. 7 = weekly). NULL = use global default.",
    )
    last_credit_score_evaluated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this loan was last evaluated for credit scoring.",
    )

    def __str__(self):
        if self.character:
            return f"{self.name} ({self.character.name})"
        return f"{self.name} (Internal)"


class JournalEntry(models.Model):
    """
    A single financial transaction, composed of multiple balanced ledger entries.
    """

    date = models.DateField()
    description = models.CharField(max_length=255)
    creator = models.ForeignKey(
        "amc.Character", on_delete=models.PROTECT, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        entries: models.Manager["LedgerEntry"]

    def clean(self):
        """
        Ensures that the journal entry is balanced.
        This is a critical data integrity check.
        """
        # This check runs *before* saving from Django Admin or ModelForms.
        # It requires the entries to be already associated with the JournalEntry instance.
        if self.pk:  # Only run on existing objects that can have entries
            debits = self.entries.aggregate(total=Sum("debit"))["total"] or 0
            credits = self.entries.aggregate(total=Sum("credit"))["total"] or 0
            if debits != credits:
                raise ValidationError(
                    f"Unbalanced transaction: Debits ({debits}) do not equal Credits ({credits})."
                )

    def __str__(self):
        return f"{self.date} - {self.description}"


class LedgerEntriesQuerySet(models.QuerySet):
    def filter_donations(self):
        return self.filter(
            account__account_type=Account.AccountType.REVENUE,
            account__book=Account.Book.GOVERNMENT,
            account__character=None,
            journal_entry__creator__isnull=False,
        )

    def filter_subsidies(self):
        return self.filter(
            account__account_type=Account.AccountType.EXPENSE,
            account__book=Account.Book.GOVERNMENT,
            account__character=None,
            journal_entry__creator__isnull=True,
        )

    def filter_interest_payments(self):
        return self.filter(
            account__account_type=Account.AccountType.LIABILITY,
            account__book=Account.Book.BANK,
            account__character__isnull=False,
            journal_entry__creator__isnull=False,
            journal_entry__description="Interest Payment",
        )

    def filter_character_donations(self, character):
        return self.filter_donations().filter(journal_entry__creator=character)


class LedgerEntryManager(models.Manager.from_queryset(LedgerEntriesQuerySet)):  # type: ignore[misc]
    pass


class LedgerEntry(models.Model):
    """
    A single entry (a debit or a credit) in the ledger.
    Part of a JournalEntry.
    """

    journal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.CASCADE, related_name="entries"
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="entries"
    )
    debit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    credit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    objects: ClassVar[LedgerEntryManager] = LedgerEntryManager()

    class Meta:
        # Ensures that an entry is either a debit or a credit, but not both.
        constraints = [
            # pyrefly: ignore [deprecated]
            models.CheckConstraint(
                check=(
                    models.Q(debit__gt=0, credit=0) | models.Q(debit=0, credit__gt=0)
                ),
                name="debit_or_credit",
            )
        ]

    def __str__(self):
        if self.debit > 0:
            return f"{self.account} Dr. {self.debit}"
        return f"{self.account} Cr. {self.credit}"


class DailyTreasurySnapshot(models.Model):
    """
    Persisted end-of-day treasury summary for historical browsing.
    One row per day, created by the daily cron task at 02:30 UTC.
    """

    date = models.DateField(unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Point-in-time balances (as of end of day)
    treasury_balance = models.DecimalField(max_digits=14, decimal_places=2)
    reserves_balance = models.DecimalField(max_digits=14, decimal_places=2)

    # Aggregated totals
    total_income = models.DecimalField(max_digits=14, decimal_places=2)
    total_expenses = models.DecimalField(max_digits=14, decimal_places=2)
    surplus = models.DecimalField(max_digits=14, decimal_places=2)
    wealth_tax_collected = models.DecimalField(
        max_digits=14, decimal_places=2, default=0
    )
    nirc_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    # Full breakdown stored as JSON for flexibility
    data = models.JSONField(help_text="Full treasury summary dict for this day")

    class Meta:
        ordering = ["-date"]
        verbose_name_plural = "Daily treasury snapshots"

    def __str__(self):
        return f"Treasury Snapshot {self.date} (surplus: {self.surplus:+,.0f})"


class BankPolicy(models.Model):
    """Server-wide bank policy configuration. Singleton (pk=1 always)."""

    daily_interest_rate = models.DecimalField(
        max_digits=8,
        decimal_places=6,
        default=Decimal("0.022"),
        help_text="Nominal daily interest rate applied to bank balances "
        "(e.g. 0.022 = 2.2%/day). Fraction, not percent.",
    )

    class Meta:
        verbose_name_plural = "Bank policy"

    def __str__(self):
        return f"Bank policy (daily interest {self.daily_interest_rate:.2%})"

    @classmethod
    def load(cls) -> "BankPolicy":
        policy, _ = cls.objects.get_or_create(pk=1)
        return policy

    @classmethod
    def get_daily_interest_rate(cls) -> Decimal:
        return cls.load().daily_interest_rate
