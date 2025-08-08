from django.db import models
from django.db.models import Sum
from django.core.exceptions import ValidationError

class Account(models.Model):
  """
  Represents an account in a ledger
  """
  class AccountType(models.TextChoices):
    ASSET = 'ASSET', 'Asset'
    LIABILITY = 'LIABILITY', 'Liability'
    EQUITY = 'EQUITY', 'Equity'
    REVENUE = 'REVENUE', 'Revenue'
    EXPENSE = 'EXPENSE', 'Expense'

  class Book(models.TextChoices):
    GOVERNMENT = 'GOVERNMENT', 'Government'
    BANK = 'BANK', 'Bank of ASEAN'

  book = models.CharField(max_length=10, choices=Book.choices)

  character = models.ForeignKey(
    'amc.Character',
    on_delete=models.PROTECT,
    null=True,
    blank=True,
    related_name='accounts'
  )

  name = models.CharField(max_length=100)
  account_type = models.CharField(max_length=10, choices=AccountType.choices)
  balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

  def __str__(self):
    if self.player:
      return f"{self.name} ({self.player.unique_id})"
    return f"{self.name} (Internal)"


class JournalEntry(models.Model):
  """
  A single financial transaction, composed of multiple balanced ledger entries.
  """
  date = models.DateField()
  description = models.CharField(max_length=255)
  creator = models.ForeignKey('amc.Character', on_delete=models.PROTECT, null=True, blank=True)
  created_at = models.DateTimeField(auto_now_add=True)

  def clean(self):
    """
    Ensures that the journal entry is balanced.
    This is a critical data integrity check.
    """
    # This check runs *before* saving from Django Admin or ModelForms.
    # It requires the entries to be already associated with the JournalEntry instance.
    if self.pk: # Only run on existing objects that can have entries
      debits = self.entries.aggregate(total=Sum('debit'))['total'] or 0
      credits = self.entries.aggregate(total=Sum('credit'))['total'] or 0
      if debits != credits:
        raise ValidationError(f"Unbalanced transaction: Debits ({debits}) do not equal Credits ({credits}).")

  def __str__(self):
    return f"{self.date} - {self.description}"


class LedgerEntry(models.Model):
  """
  A single entry (a debit or a credit) in the ledger.
  Part of a JournalEntry.
  """
  journal_entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='entries')
  account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='entries')
  debit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
  credit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

  class Meta:
    # Ensures that an entry is either a debit or a credit, but not both.
    constraints = [
      models.CheckConstraint(
        check=(models.Q(debit__gt=0, credit=0) | models.Q(debit=0, credit__gt=0)),
        name='debit_or_credit'
      )
    ]

  def __str__(self):
    if self.debit > 0:
      return f"{self.account} Dr. {self.debit}"
    return f"{self.account} Cr. {self.credit}"

