from django.contrib import admin
from .models import Account, JournalEntry, LedgerEntry

@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
  list_display = ['id', 'account_type', 'book', 'name', 'character', 'balance']
  list_select_related = ['character']
  search_fields = ['character__name', 'name']
  autocomplete_fields = ['character']
  list_filter = ['account_type', 'book']

@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
  list_display = ['id', 'date', 'description', 'creator']
  list_select_related = ['creator']
  search_fields = ['creator__name', 'description']
  autocomplete_fields = ['creator']

@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
  list_display = ['id', 'journal_entry', 'account', 'debit', 'credit']
  list_select_related = ['journal_entry', 'account']
  search_fields = ['journal_entry__description', 'account__name']
  autocomplete_fields = ['journal_entry', 'account']

